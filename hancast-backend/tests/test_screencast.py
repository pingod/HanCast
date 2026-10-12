"""
屏幕投屏子系统单元测试

覆盖：FFmpeg 查找/参数拼接、音频设备分类、HLS 编码参数（电视解码兼容）、
HTTP 路径安全与拉流证据统计、DLNA 起流校验、管理器命令契约。
不启动真实 FFmpeg 进程；HTTP 服务只在临时目录上跑真实 asyncio 线程。
"""

import io
import os
import sys
import time
import asyncio
import unittest
import tempfile
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.screencast import manager as manager_mod
from hancast_sidecar.screencast.capture import FfmpegCapture
from hancast_sidecar.screencast.ffmpeg_manager import FfmpegManager
from hancast_sidecar.screencast.httpd import ScreenCastHttpd
from hancast_sidecar.screencast.manager import ScreenCastManager


def _fake_proc():
    proc = MagicMock()
    proc.poll.return_value = None
    proc.returncode = None
    proc.stderr = io.BytesIO(b"")
    return proc


class TestFfmpegManager(unittest.TestCase):
    """FFmpeg 管理器"""

    def test_audio_device_classify(self):
        self.assertEqual(FfmpegManager._classify_audio_device("立体声混音"), "system")
        self.assertEqual(FfmpegManager._classify_audio_device("Stereo Mix"), "system")
        self.assertEqual(FfmpegManager._classify_audio_device("VB-Audio Virtual Cable"), "system")
        self.assertEqual(FfmpegManager._classify_audio_device("BlackHole 2ch"), "system")
        self.assertEqual(FfmpegManager._classify_audio_device("BlackHole 16ch"), "system")
        self.assertEqual(FfmpegManager._classify_audio_device("Realtek Microphone"), "mic")
        self.assertEqual(FfmpegManager._classify_audio_device("麦克风 (Realtek Audio)"), "mic")
        self.assertEqual(FfmpegManager._classify_audio_device("Built-in Microphone"), "mic")

    def test_build_video_input_screen(self):
        args = FfmpegManager.build_video_input(
            {"type": "screen", "id": "desktop"}, 15, "1280:720")
        self.assertIn("-f", args)
        if os.name == "nt":
            self.assertIn("gdigrab", args)
            self.assertIn("desktop", args)
            self.assertIn("scale=1280:720", args)

    def test_build_video_input_window(self):
        args = FfmpegManager.build_video_input(
            {"type": "window", "title": "记事本"}, 10, "")
        if os.name == "nt":
            self.assertIn("title=记事本", args)

    def test_build_audio_input_none(self):
        self.assertEqual(FfmpegManager.build_audio_input({}), [])
        self.assertEqual(FfmpegManager.build_audio_input({"mic": False, "system": False}), [])

    def test_build_audio_input_single(self):
        args = FfmpegManager.build_audio_input({"mic": True, "mic_device": "麦克风"})
        if os.name == "nt":
            self.assertIn("audio=麦克风", args)
            self.assertNotIn("-filter_complex", args)

    def test_build_audio_input_mix(self):
        args = FfmpegManager.build_audio_input({
            "mic": True, "mic_device": "麦克风",
            "system": True, "system_device": "立体声混音",
        })
        if os.name == "nt":
            self.assertIn("-filter_complex", args)
            self.assertIn("amix=inputs=2", " ".join(args))

    @patch("sys.platform", "darwin")
    @patch("os.name", "posix")
    def test_build_audio_input_macos_mic_only(self):
        args = FfmpegManager.build_audio_input(
            {"mic": True, "mic_device": "0:Built-in Microphone", "system": False})
        self.assertEqual(args, ["-f", "avfoundation", "-i", ":0"])
        self.assertNotIn("-filter_complex", args)

    @patch("sys.platform", "darwin")
    @patch("os.name", "posix")
    def test_build_audio_input_macos_blackhole_only(self):
        # 已安装 BlackHole → 只捕获系统声音
        args = FfmpegManager.build_audio_input(
            {"mic": False, "system": True, "system_device": "2:BlackHole 2ch"})
        self.assertEqual(args, ["-f", "avfoundation", "-i", ":2"])
        self.assertNotIn("-filter_complex", args)

    @patch("sys.platform", "darwin")
    @patch("os.name", "posix")
    def test_build_audio_input_macos_mic_plus_blackhole(self):
        # 麦克风 + BlackHole 双路混音；amix 标签按音频起始序号（视频占输入 0）
        args = FfmpegManager.build_audio_input({
            "mic": True, "mic_device": "0:Built-in Microphone",
            "system": True, "system_device": "2:BlackHole 2ch",
        }, input_offset=1)
        self.assertEqual(args, [
            "-f", "avfoundation", "-i", ":0",
            "-f", "avfoundation", "-i", ":2",
            "-filter_complex",
            "[1:a][2:a]amix=inputs=2:duration=longest:dropout_transition=0",
        ])

    @patch("sys.platform", "darwin")
    @patch("os.name", "posix")
    def test_build_audio_input_macos_no_devices(self):
        # 请求了系统声音但没选设备 → 返回 []（不产生无效命令）
        args = FfmpegManager.build_audio_input(
            {"mic": False, "system": True, "system_device": ""})
        self.assertEqual(args, [])

    @patch("hancast_sidecar.screencast.ffmpeg_manager.subprocess.run")
    def test_validate(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(FfmpegManager()._validate("ffmpeg.exe"))
        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(FfmpegManager()._validate("ffmpeg.exe"))

    def test_set_path_missing(self):
        mgr = FfmpegManager()
        result = mgr.set_path("Z:\\definitely_not_exist_12345\\ffmpeg.exe")
        self.assertFalse(result["ok"])


class TestHlsEncode(unittest.TestCase):
    """HLS 输出参数：电视/浏览器共用同一路，兼容项错了就整条通道不可用"""

    def _cmd(self, video_cfg=None, audio_cfg=None):
        cap = FfmpegCapture("ffmpeg")
        out_dir = tempfile.mkdtemp(prefix="hancast-hls-enc-")
        self.addCleanup(lambda: __import__("shutil").rmtree(out_dir, ignore_errors=True))
        captured = {}
        m3u8 = os.path.join(out_dir, "live.m3u8")

        def fake_spawn(cmd):
            captured["cmd"] = cmd
            # 播放列表由"本次进程"新建，就绪判定才算通过
            with open(m3u8, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
            return _fake_proc()

        with patch.object(FfmpegCapture, "_spawn", side_effect=fake_spawn), \
                patch.object(FfmpegCapture, "_watch"):
            result = cap.start_hls(
                {"type": "screen", "id": "desktop"},
                audio_cfg or {},
                video_cfg or {"width": 1280, "height": 720, "fps": 15,
                              "bitrate": "2M"},
                out_dir)
        cap.stop()
        return result, captured.get("cmd", [])

    def test_result_contract(self):
        result, cmd = self._cmd()
        self.assertTrue(result["ok"])
        self.assertEqual(result["m3u8"], cmd[-1])
        self.assertEqual(result["fps"], 15)

    def test_main_profile_for_tv_decoders(self):
        _, cmd = self._cmd()
        joined = " ".join(cmd)
        i = cmd.index("-profile:v")
        self.assertEqual(cmd[i + 1], "main")
        self.assertIn("-level", cmd)
        self.assertIn("yuv420p", joined)

    def test_closed_gop_and_repeated_headers(self):
        """屏幕内容到处是场景切换：scenecut 默认会在切窗时插 IDR，
        HLS 只能按关键帧切片 → 碎分片让电视放弃播放"""
        _, cmd = self._cmd()
        i = cmd.index("-x264-params")
        params = cmd[i + 1]
        self.assertIn("scenecut=0", params)
        self.assertIn("open_gop=0", params)
        self.assertIn("repeat_headers=1", params)
        self.assertIn("-hls_segment_filename", cmd)

    def test_playlist_written_via_temp_file(self):
        """电视可能在半截播放列表上判定流损坏，必须原子改名"""
        _, cmd = self._cmd()
        i = cmd.index("-hls_flags")
        self.assertIn("temp_file", cmd[i + 1])
        self.assertIn("independent_segments", cmd[i + 1])
        self.assertEqual(cmd[cmd.index("-hls_time") + 1], "2")

    def test_muxing_queue_enlarged(self):
        _, cmd = self._cmd()
        self.assertIn("-max_muxing_queue_size", cmd)

    def test_cfr_cap(self):
        """avfoundation 屏幕设备时间戳不可靠，按目标帧率 CFR 封顶"""
        _, cmd = self._cmd(video_cfg={"width": 854, "height": 480, "fps": 10,
                                      "bitrate": "1M"})
        self.assertEqual(cmd[cmd.index("-r") + 1], "10")
        self.assertEqual(cmd[cmd.index("-b:v") + 1], "1M")

    def test_no_audio_means_no_aac(self):
        _, cmd = self._cmd()
        self.assertIn("-an", cmd)
        self.assertNotIn("aac", cmd)

    def test_old_session_dir_is_wiped(self):
        """残留播放列表会让电视拉到上一轮的死分片"""
        cap = FfmpegCapture("ffmpeg")
        out_dir = tempfile.mkdtemp(prefix="hancast-hls-stale-")
        self.addCleanup(
            lambda: __import__("shutil").rmtree(out_dir, ignore_errors=True))
        stale = os.path.join(out_dir, "live.m3u8")
        with open(stale, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n#EXTINF:1.0,\nseg999.ts\n")
        with open(os.path.join(out_dir, "seg999.ts"), "wb") as f:
            f.write(b"\x47" * 188)
        seen = {}

        def fake_spawn(cmd):
            seen["gone"] = not os.path.exists(stale)
            with open(cmd[-1], "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
            return _fake_proc()

        with patch.object(FfmpegCapture, "_spawn", side_effect=fake_spawn), \
                patch.object(FfmpegCapture, "_watch"):
            result = cap.start_hls({"type": "screen"}, {}, {}, out_dir)
        cap.stop()
        self.assertTrue(result["ok"])
        self.assertTrue(seen["gone"])


class TestHttpdPullEvidence(unittest.TestCase):
    """拉流证据：电视黑屏时唯一能区分「没拉到」和「解不了」的信号"""

    def _httpd(self):
        return ScreenCastHttpd(hls_root=tempfile.mkdtemp(prefix="hancast-hls-test-"))

    def test_no_pull_yet_reports_zero(self):
        httpd = self._httpd()
        self.assertEqual(httpd.pull_stats("abc"),
                         {"pulls": 0, "misses": 0, "clients": [], "last": None})

    def test_note_pull_counts_clients_and_requests(self):
        httpd = self._httpd()
        httpd._note_pull("abc", "192.168.1.13")
        httpd._note_pull("abc", "192.168.1.13")
        httpd._note_pull("abc", "192.168.1.5")
        stats = httpd.pull_stats("abc")
        self.assertEqual(stats["pulls"], 3)
        self.assertEqual(stats["clients"], ["192.168.1.13", "192.168.1.5"])
        self.assertLessEqual(stats["last"], time.time())
        self.assertEqual(httpd.viewers("abc"), 2)

    def test_misses_recorded(self):
        httpd = self._httpd()
        httpd._note_miss("abc")
        httpd._note_miss("abc")
        self.assertEqual(httpd.pull_stats("abc")["misses"], 2)

    def test_unregister_clears_stats(self):
        httpd = self._httpd()
        httpd.sessions.add("abc")
        httpd._note_pull("abc", "1.2.3.4")
        httpd.unregister_session("abc")
        self.assertEqual(httpd.pull_stats("abc")["pulls"], 0)
        self.assertNotIn("abc", httpd.sessions)

    def test_viewer_callback_fires_on_change(self):
        httpd = self._httpd()
        seen = []
        httpd.register_session("abc", on_viewers=lambda sid, n: seen.append(n))
        httpd._note_pull("abc", "10.0.0.2")
        httpd._note_pull("abc", "10.0.0.2")   # 同一 IP 不重复计数
        httpd._note_pull("abc", "10.0.0.3")
        self.assertEqual(seen, [1, 2])

    def test_register_rejects_duplicate_session(self):
        httpd = self._httpd()
        self.assertTrue(httpd.register_session("abc"))
        self.assertFalse(httpd.register_session("abc"))

    def test_safe_hls_path_traversal(self):
        httpd = self._httpd()
        # 目录穿越被拒绝
        self.assertIsNone(httpd._safe_hls_path("abc", "../secret.txt"))
        self.assertIsNone(httpd._safe_hls_path("abc", "..\\secret.txt"))
        self.assertIsNone(httpd._safe_hls_path("abc", "C:\\windows\\system32\\x"))
        # 正常文件路径可解析（不要求文件存在）
        path = httpd._safe_hls_path("abc", "seg001.ts")
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith(os.path.join("abc", "seg001.ts")))


class TestWatchPageResponses(unittest.TestCase):
    """观看页响应：这两条错误分支曾经因为 content_type 带 charset 直接 500"""

    def _httpd(self):
        return ScreenCastHttpd(hls_root=tempfile.mkdtemp(prefix="hancast-hls-resp-"))

    def _request(self, path, **match_info):
        from aiohttp.test_utils import make_mocked_request
        return make_mocked_request("GET", path, match_info=match_info)

    def test_stopped_session_watch_page_is_404_not_500(self):
        httpd = self._httpd()
        resp = asyncio.run(httpd._handle_watch_page(
            self._request("/r/abc", session_id="abc")))
        self.assertEqual(resp.status, 404)

    def test_hls_for_unregistered_session_404(self):
        """会话注销后目录可能还在（异常退出），电视必须拿不到死流"""
        httpd = self._httpd()
        session_dir = os.path.join(httpd.hls_root, "abc")
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, "live.m3u8"), "w",
                  encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        resp = asyncio.run(httpd._handle_hls(self._request(
            "/hls/abc/live.m3u8", session_id="abc", filename="live.m3u8")))
        self.assertEqual(resp.status, 404)
        self.assertEqual(httpd.pull_stats("abc")["misses"], 1)

    def test_hls_serves_registered_session(self):
        httpd = self._httpd()
        httpd.register_session("abc")
        session_dir = os.path.join(httpd.hls_root, "abc")
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, "live.m3u8"), "w",
                  encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        resp = asyncio.run(httpd._handle_hls(self._request(
            "/hls/abc/live.m3u8", session_id="abc", filename="live.m3u8")))
        self.assertEqual(resp.status, 200)
        self.assertIn("mpegurl", resp.headers["Content-Type"])
        self.assertEqual(httpd.pull_stats("abc")["pulls"], 1)
        self.assertEqual(httpd.viewers("abc"), 1)


def _manager(pull_stats=None, running=True):
    """不启动 FFmpeg / HTTP 服务的 ScreenCastManager 替身"""
    handler = MagicMock()
    handler._get_local_ip.return_value = "192.168.1.5"
    mgr = ScreenCastManager(handler)
    mgr.httpd = MagicMock()
    mgr.httpd.pull_stats.return_value = pull_stats or {
        "pulls": 0, "misses": 0, "clients": [], "last": None}
    mgr._capture = None
    mgr._session_id = "abc"
    mgr._state.update({"running": running, "target": "dlna",
                       "device_id": "tv1", "url": "http://x/live.m3u8"})
    mgr._emit = MagicMock()
    return mgr


class TestDlnaVerify(unittest.TestCase):
    """DLNA 起流校验：黑屏要给出可执行的结论，而不是让用户猜"""

    def setUp(self):
        patcher = patch.object(manager_mod, "DLNA_VERIFY_POLL", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pull_seen_reports_no_error(self):
        mgr = _manager(pull_stats={"pulls": 4, "misses": 0,
                                   "clients": ["192.168.1.13"],
                                   "last": time.time()})
        mgr._verify_dlna_pull("abc", "TCL TV")
        self.assertIsNone(mgr._state["error"])

    def test_no_pull_within_window_is_reported(self):
        with patch.object(manager_mod, "DLNA_VERIFY_SECONDS", 0.05):
            mgr = _manager()
            mgr._verify_dlna_pull("abc", "TCL TV")
        self.assertIn("没有拉取播放列表", mgr._state["error"])
        self.assertIn("同一网段", mgr._state["error"])
        mgr._emit.assert_called()

    def test_falling_behind_reports_bitrate(self):
        mgr = _manager(pull_stats={"pulls": 20, "misses": 9,
                                   "clients": ["192.168.1.13"], "last": None})
        mgr._verify_dlna_pull("abc", "TCL TV")
        self.assertIn("码率", mgr._state["error"])

    def test_stops_when_session_replaced(self):
        """用户已停止/重开：旧校验线程不能再改状态"""
        mgr = _manager()
        mgr._session_id = "other"
        mgr._verify_dlna_pull("abc", "TCL TV")
        self.assertIsNone(mgr._state["error"])
        mgr._emit.assert_not_called()

    def test_stops_when_not_running(self):
        mgr = _manager(running=False)
        mgr._state["running"] = False
        mgr._verify_dlna_pull("abc", "TCL TV")
        self.assertIsNone(mgr._state["error"])


class TestManagerContract(unittest.TestCase):
    """命令契约：target 只有 dlna/browser，会话每次全新"""

    def test_unknown_target_rejected(self):
        mgr = _manager()
        mgr._state["running"] = False
        result = mgr.start({"target": "webrtc", "source": {"type": "screen"}})
        self.assertFalse(result["ok"])
        self.assertIn("未知目标类型", result["error"])

    def test_running_blocks_new_session(self):
        mgr = _manager()
        result = mgr.start({"target": "browser", "source": {"type": "screen"}})
        self.assertFalse(result["ok"])
        self.assertIn("请先停止", result["error"])

    def test_dlna_requires_device(self):
        mgr = _manager()
        mgr._state["running"] = False
        with patch.object(mgr.ffmpeg, "check", return_value={"ok": True}):
            result = mgr.start({"target": "dlna",
                                "source": {"type": "screen", "id": "desktop"}})
        self.assertFalse(result["ok"])
        self.assertIn("device_id", result["error"])

    def test_system_audio_without_device_rejected(self):
        mgr = _manager()
        mgr._state["running"] = False
        with patch.object(mgr.ffmpeg, "check", return_value={"ok": True}):
            result = mgr.start({"target": "browser",
                                "source": {"type": "screen"},
                                "audio": {"system": True}})
        self.assertFalse(result["ok"])
        self.assertIn("系统声音设备", result["error"])

    def test_ffmpeg_missing_blocks_start(self):
        mgr = _manager()
        mgr._state["running"] = False
        with patch.object(mgr.ffmpeg, "check",
                          return_value={"ok": False, "error": "FFmpeg 未找到"}):
            result = mgr.start({"target": "browser",
                                "source": {"type": "screen"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "FFmpeg 未找到")

    def test_stop_is_idempotent(self):
        mgr = _manager(running=False)
        mgr._state["running"] = False
        result = mgr.stop()
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_stopped"])

    def test_stop_survives_broken_teardown(self):
        """会话注销报错也不能让「停止投屏」失败：用户点了就必须停得下来"""
        mgr = _manager()
        mgr._capture = MagicMock()
        mgr.httpd.unregister_session.side_effect = TypeError(
            "set.pop() takes no arguments (2 given)")
        result = mgr.stop()
        self.assertTrue(result["ok"])
        self.assertFalse(mgr._state["running"])
        self.assertIsNone(mgr._session_id)
        mgr.httpd.stop.assert_called_once()

    def test_teardown_releases_everything(self):
        mgr = _manager()
        capture = MagicMock()
        mgr._capture = capture
        mgr.httpd.reset_mock()
        mgr._teardown()
        capture.stop.assert_called_once()
        mgr.httpd.unregister_session.assert_called_once_with("abc")
        # 会话没了就停服务，下次 start 才能拿回固定端口
        mgr.httpd.stop.assert_called_once()
        self.assertFalse(mgr._state["running"])
        self.assertIsNone(mgr._session_id)

    def test_capture_error_frees_dlna_channel(self):
        """捕获中途死掉必须连电视一起放开，否则电视一直拉死流"""
        mgr = _manager()
        mgr.handler._stop_cast = MagicMock()
        mgr._on_capture_error("屏幕捕获进程已退出（退出码 1）")
        mgr.handler._stop_cast.assert_called_once_with({"device_id": "tv1"})
        self.assertFalse(mgr._state["running"])
        self.assertEqual(mgr._state["error"], "屏幕捕获进程已退出（退出码 1）")


if __name__ == "__main__":
    unittest.main()
