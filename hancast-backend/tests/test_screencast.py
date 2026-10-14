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
import threading
import unittest
import tempfile
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lxml import etree

from hancast_sidecar import params as app_params
from hancast_sidecar.screencast import manager as manager_mod
from hancast_sidecar.screencast import capture as capture_mod
from hancast_sidecar.screencast.manager import HLS_MIME, ScreenCastManager
from hancast_sidecar.screencast.capture import FfmpegCapture
from hancast_sidecar.screencast.ffmpeg_manager import (
    FfmpegManager, _parse_screen_region)
from hancast_sidecar.screencast.httpd import ScreenCastHttpd
from hancast_sidecar.protocol.dlna import build_didl_item


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
        # macOS 自建的 Audio Tap 环回（macos_tap.py）
        self.assertEqual(FfmpegManager._classify_audio_device("HanCast 系统声音"), "system")
        self.assertEqual(FfmpegManager._classify_audio_device("Realtek Microphone"), "mic")
        self.assertEqual(FfmpegManager._classify_audio_device("麦克风 (Realtek Audio)"), "mic")
        self.assertEqual(FfmpegManager._classify_audio_device("Built-in Microphone"), "mic")

    @patch("sys.platform", "darwin")
    @patch("os.name", "posix")
    @patch("hancast_sidecar.screencast.ffmpeg_manager._run_utf8")
    def test_macosenumerates_tap_device_is_marked(self, mock_run):
        """自建环回要能被标出来：它的 avfoundation 索引随别的设备增减而变"""
        mock_run.return_value = MagicMock(stderr=(
            "AVFoundation video devices:\n"
            "[0] FaceTime高清相机\n"
            "[1] Capture screen 0\n"
            "AVFoundation audio devices:\n"
            "[0] MacBook Pro麦克风\n"
            "[3] HanCast 系统声音\n"), stdout="")
        fm = FfmpegManager()
        fm._path = "/usr/bin/ffmpeg"
        with patch("hancast_sidecar.screencast.macos_tap.ensure_device",
                   return_value=True), \
             patch("hancast_sidecar.screencast.macos_tap.is_tap_device",
                   lambda n: n == "HanCast 系统声音"):
            devices = fm._list_audio_devices()
        by_name = {d["name"]: d for d in devices}
        self.assertEqual(by_name["3:HanCast 系统声音"]["kind"], "system")
        self.assertTrue(by_name["3:HanCast 系统声音"]["tap"])
        self.assertNotIn("tap", by_name["0:MacBook Pro麦克风"])

    # .68 实测：打包的 ffmpeg 6.1.1 与系统 8.1.2 都不打印
    # "DirectShow audio devices" 分段头（那条字符串根本不在 exe 里），
    # 设备行只靠尾缀标类型。只按分段头切段的话 Windows 永远列不出麦克风。
    DSHOW_NO_HEADER = (
        "[dshow @ 1] \"Smart Connect Camera\" (video)\n"
        "[dshow @ 1]   Alternative name \"@device_pnp_x\"\n"
        "[dshow @ 1] \"OBS Virtual Camera\" (none)\n"
        "[dshow @ 1] \"麦克风 (2- ORBBEC Audio Device)\" (audio)\n"
        "[dshow @ 1]   Alternative name \"@device_cm_y\"\n"
        "[dshow @ 1] \"耳机式麦克风 (HUAWEI Sound Joy-06342)\" (audio)\n"
        "[dshow @ 1] \"麦克风 (Realtek(R) Audio)\" (audio)\n"
        "[dshow @ 1] \"立体声混音 (Realtek(R) Audio)\" (audio)\n"
        "[in#0 @ 2] Error opening input: Immediate exit requested\n"
        "Error opening input file dummy.\n")

    @patch("sys.platform", "win32")
    @patch("os.name", "nt")
    @patch("hancast_sidecar.screencast.ffmpeg_manager._run_utf8")
    def test_windows_lists_mics_from_dshow_suffix(self, mock_run):
        """按 (audio) 后缀认设备：Windows 麦克风选择器不再空列表"""
        mock_run.return_value = MagicMock(stderr=self.DSHOW_NO_HEADER, stdout="")
        fm = FfmpegManager()
        fm._path = "ffmpeg"
        with patch("hancast_sidecar.screencast.loopback.loopback_devices",
                   return_value=[]):
            devices = fm._list_audio_devices()
        mics = [d["name"] for d in devices if d["kind"] == "mic"]
        self.assertEqual(mics, ["麦克风 (2- ORBBEC Audio Device)",
                                "耳机式麦克风 (HUAWEI Sound Joy-06342)",
                                "麦克风 (Realtek(R) Audio)"])
        # 相机、虚拟摄像头、Alternative name 行、错误行都不能混进来
        self.assertNotIn("Smart Connect Camera", [d["name"] for d in devices])
        self.assertNotIn("OBS Virtual Camera", [d["name"] for d in devices])

    @patch("sys.platform", "win32")
    @patch("os.name", "nt")
    @patch("hancast_sidecar.screencast.ffmpeg_manager._run_utf8")
    def test_windows_falls_back_to_dshow_section_header(self, mock_run):
        """带分段头的 build 仍然认得出（别把老格式判成没设备）"""
        mock_run.return_value = MagicMock(stderr=(
            "DirectShow video devices (some may be both video and audio devices)\n"
            "\"相机\" (video)\n"
            "DirectShow audio devices\n"
            "\"麦克风 (Realtek(R) Audio)\"\n"
            "  Alternative name \"@device_cm_abc\"\n"), stdout="")
        fm = FfmpegManager()
        fm._path = "ffmpeg"
        with patch("hancast_sidecar.screencast.loopback.loopback_devices",
                   return_value=[]):
            devices = fm._list_audio_devices()
        self.assertEqual([d["name"] for d in devices],
                         ["麦克风 (Realtek(R) Audio)"])

    @patch("sys.platform", "darwin")
    @patch("os.name", "posix")
    @patch("hancast_sidecar.screencast.ffmpeg_manager._run_utf8")
    def test_macos_tap_absent_when_device_not_built(self, mock_run):
        """环回没建起来（macOS 14.1 及以下）时列表里就没有它，别给界面假选项"""
        mock_run.return_value = MagicMock(stderr=(
            "AVFoundation audio devices:\n"
            "[0] MacBook Pro麦克风\n"), stdout="")
        fm = FfmpegManager()
        fm._path = "/usr/bin/ffmpeg"
        with patch("hancast_sidecar.screencast.macos_tap.ensure_device",
                   return_value=False):
            devices = fm._list_audio_devices()
        self.assertEqual([d["name"] for d in devices], ["0:MacBook Pro麦克风"])

    def test_audio_input_silent_reads_wav_pcm(self):
        """静音判据只能认「全零」：真实麦克风的底噪也必须算有信号"""
        import io as _io
        import wave as _wave

        def wav(samples):
            buf = _io.BytesIO()
            with _wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(48000)
                w.writeframes("".join(chr(s & 0xFF) + chr((s >> 8) & 0xFF)
                                      for s in samples).encode("latin-1"))
            return buf.getvalue()

        fm = FfmpegManager()
        fm._path = "/usr/bin/ffmpeg"
        for payload, want in ((wav([0] * 5000), True), (wav([0] * 4999 + [7]), False)):
            with patch("subprocess.run",
                       return_value=MagicMock(returncode=0, stdout=payload,
                                              stderr=b"")):
                self.assertIs(fm.audio_input_silent("0:Mic"), want)
        with patch("subprocess.run",
                   side_effect=OSError("ffmpeg 起不来")):
            self.assertIsNone(fm.audio_input_silent("0:Mic"))

    def test_build_video_input_screen(self):
        args, vf = FfmpegManager.build_video_input(
            {"type": "screen", "id": "desktop"}, 15,
            FfmpegManager.build_scale_filter(1280, 720))
        self.assertIn("-f", args)
        # 滤镜不再混在输入参数里：-i 之前的选项会被判给「下一个输入」
        self.assertNotIn("-vf", args)
        self.assertIn("scale=1280:720", vf)
        if os.name == "nt":
            self.assertIn("gdigrab", args)
            self.assertIn("desktop", args)

    def test_build_video_input_window(self):
        args, _vf = FfmpegManager.build_video_input(
            {"type": "window", "title": "记事本"}, 10, "")
        if os.name == "nt":
            self.assertIn("title=记事本", args)

    def test_scale_filter_keeps_aspect_and_even_dims(self):
        # 桌面尺寸常为奇数（多屏并集实测 4000x2571），x264 会直接拒绝初始化
        self.assertEqual(
            FfmpegManager.build_scale_filter(1921, 1081),
            "scale=1921:1081:force_original_aspect_ratio=decrease"
            ":force_divisible_by=2")

    def test_scale_filter_defaults_to_tv_safe_cap(self):
        self.assertEqual(
            FfmpegManager.build_scale_filter(0, 0),
            "scale=1920:1080:force_original_aspect_ratio=decrease"
            ":force_divisible_by=2")
        # 只给一个维度时不能退化成 "1280:0"
        self.assertIn("scale=1280:1080:", FfmpegManager.build_scale_filter(1280, 0))

    def test_parse_screen_region(self):
        self.assertEqual(_parse_screen_region("screen:0,0,2560,1440"),
                         (0, 0, 2560, 1440))
        self.assertEqual(_parse_screen_region("screen:-1920,0,1920,1080"),
                         (-1920, 0, 1920, 1080))
        for bad in ("desktop", "window:记事本", "screen:1,2,3", "screen:a,b,c,d",
                    "screen:0,0,0,1080"):
            self.assertIsNone(_parse_screen_region(bad))

    def test_monitor_region_reaches_gdigrab(self):
        old = os.name
        try:
            os.name = "nt"
            args, _vf = FfmpegManager.build_video_input(
                {"type": "screen", "id": "screen:1920,0,1920,1080"}, 15, "")
        finally:
            os.name = old
        self.assertEqual(
            args[args.index("-offset_x") + 1], "1920")
        self.assertEqual(
            args[args.index("-offset_y") + 1], "0")
        self.assertEqual(
            args[args.index("-video_size") + 1], "1920x1080")
        # 抓取参数必须排在 -i 之前，否则 gdigrab 用默认整屏
        self.assertLess(args.index("-video_size"), args.index("desktop"))

    def test_monitor_region_falls_back_to_desktop(self):
        old = os.name
        try:
            os.name = "nt"
            args, _vf = FfmpegManager.build_video_input(
                {"type": "screen", "id": "desktop"}, 15, "")
        finally:
            os.name = old
        self.assertNotIn("-offset_x", args)
        self.assertIn("desktop", args)

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
        # 麦克风 + 系统声音双路混音；amix 标签按音频起始序号（视频占输入 0），
        # 且每路先 aformat 规整成 48k/立体声/s16，否则 amix 图建不起来
        args = FfmpegManager.build_audio_input({
            "mic": True, "mic_device": "0:Built-in Microphone",
            "system": True, "system_device": "2:BlackHole 2ch",
        }, input_offset=1)
        self.assertEqual(args, [
            "-f", "avfoundation", "-i", ":0",
            "-f", "avfoundation", "-i", ":2",
            "-filter_complex",
            "[1:a]aformat=sample_fmts=s16:sample_rates=48000"
            ":channel_layouts=stereo[m0];"
            "[2:a]aformat=sample_fmts=s16:sample_rates=48000"
            ":channel_layouts=stereo[m1];"
            "[m0][m1]amix=inputs=2:duration=longest"
            ":dropout_transition=0:normalize=0",
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

    def _cmd(self, video_cfg=None, audio_cfg=None, target="dlna"):
        cap = FfmpegCapture("ffmpeg")
        out_dir = tempfile.mkdtemp(prefix="hancast-hls-enc-")
        self.addCleanup(lambda: __import__("shutil").rmtree(out_dir, ignore_errors=True))
        captured = {}
        m3u8 = os.path.join(out_dir, "live.m3u8")

        def fake_spawn(cmd, use_stdin=False):
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
                out_dir, target=target)
        cap.stop()
        return result, captured.get("cmd", [])

    def test_result_contract(self):
        result, cmd = self._cmd()
        self.assertTrue(result["ok"])
        self.assertEqual(result["m3u8"], cmd[-1])
        self.assertEqual(result["fps"], 15)

    def test_video_filter_sits_after_all_inputs(self):
        """-vf 夹在两个 -i 之间时，ffmpeg 把它判给「下一个输入」（麦克风），报
        Option vf cannot be applied to input url audio=… 后 EINVAL 退出。
        .68 实测（v2.2.22）：选了麦克风或系统声音，屏幕投屏必然起不来。"""
        cases = [
            {"mic": True, "mic_device": "麦克风 (Realtek(R) Audio)"},
            {"mic": True, "mic_device": "麦克风 (Realtek(R) Audio)",
             "system": True, "system_device": "立体声混音 (Realtek(R) Audio)"},
        ]
        for audio in cases:
            with patch.object(os, "name", "nt"):
                result, cmd = self._cmd(audio_cfg=audio)
            self.assertTrue(result["ok"], audio)
            self.assertIn("-vf", cmd, audio)
            last_input = max(i for i, a in enumerate(cmd) if a == "-i")
            self.assertGreater(cmd.index("-vf"), last_input, audio)
            if "system" in audio:
                # 双路必须真的混音，否则只有一路有声
                self.assertIn("-filter_complex", cmd, audio)
                self.assertIn("amix", cmd[cmd.index("-filter_complex") + 1])

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

    def test_browser_channel_uses_shorter_segments(self):
        """浏览器通道 1 秒分片，电视通道保持 2 秒（电视按整分片起解）"""
        result, cmd = self._cmd(target="browser")
        self.assertEqual(cmd[cmd.index("-hls_time") + 1], "1")
        self.assertEqual(result["segment_seconds"], 1)
        _, dlna_cmd = self._cmd(target="dlna")
        self.assertEqual(dlna_cmd[dlna_cmd.index("-hls_time") + 1], "2")

    def test_gop_follows_segment_length(self):
        """HLS 只能在关键帧处切片：-g 必须等于 帧率 × 分片秒数，否则分片降不到 1 秒"""
        _, cmd = self._cmd(target="browser")
        self.assertEqual(cmd[cmd.index("-g") + 1], str(15 * 1))
        _, dlna_cmd = self._cmd(target="dlna")
        self.assertEqual(dlna_cmd[dlna_cmd.index("-g") + 1], str(15 * 2))

    def test_segment_length_is_tunable(self):
        """分片时长来自后台参数登记表，改参数不用重新编译"""
        with patch.object(capture_mod, "param",
                          lambda key: 4 if key == "screencast.browser_segment_seconds"
                          else app_params.get(key)):
            _, cmd = self._cmd(target="browser")
        self.assertEqual(cmd[cmd.index("-hls_time") + 1], "4")
        self.assertEqual(cmd[cmd.index("-g") + 1], str(15 * 4))

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

        def fake_spawn(cmd, use_stdin=False):
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


class TestLoopbackOwnership(unittest.TestCase):
    """WASAPI 环回的两条命门：不许饿死管线，也不许跨线程关流

    .68 实测（v2.2.22/.2.2.23）：
      1) 「扬声器 (Realtek(R) Audio)」的环回在没人出声时 read() 一个字节都不给，
         amix 要凑齐每路输入才出帧 → 整条管线停在 0.42 秒、8 秒内不落播放列表，
         表现是「一选系统声音屏幕投屏就起不来」；
      2) 停止链路在采集线程还阻塞于 read() 时 close 流 → 事件日志
         「异常代码 0xc0000005 / ntdll.dll」，sidecar 整个进程没了。
    """

    class _FakePa:
        """只够 LoopbackStream 用的假 pyaudiowpatch：记录每个 Pa 调用发生在哪个线程"""

        paInt16 = 1

        def __init__(self):
            self.calls = []
            self.thread = {}

        def PyAudio(self):
            # 生产代码是 pyaudio.PyAudio() 再 open/terminate：这里一个对象兼任
            return self

        def open(self, **_kw):
            outer = self

            class _Stream:
                def __init__(self):
                    self.block = threading.Event()
                    self.released = threading.Event()

                def read(self, _frames, exception_on_overflow=False):
                    outer.calls.append("read")
                    outer.thread.setdefault("read", threading.current_thread().name)
                    # 模拟静音设备上一直不返回的阻塞读：交付节奏只能自己撑
                    self.block.set()
                    self.released.wait(10)
                    return b"\x11" * 4096

                def stop_stream(self):
                    outer.calls.append("stop_stream")

                def close(self):
                    outer.calls.append("close")
                    outer.thread["close"] = threading.current_thread().name

            self.stream = _Stream()
            return self.stream

        def terminate(self):
            self.calls.append("terminate")

    def _stream(self):
        from hancast_sidecar.screencast import loopback as lb_mod
        pa = self._FakePa()
        with patch.object(lb_mod, "_import_pyaudio", return_value=pa):
            stream = lb_mod.LoopbackStream({"name": "x", "index": 0,
                                            "rate": 48000, "channels": 2})
        return pa, stream

    def _wait_pump_reading(self, pa, stream):
        """等采集线程真正进到 read()，否则用例前提不成立"""
        deadline = time.time() + 2
        while "read" not in pa.calls and time.time() < deadline:
            time.sleep(0.01)
        self.assertIn("read", pa.calls)

    def test_starved_device_still_feeds_realtime_silence(self):
        """设备零交付时 read() 必须按实时节奏给静音，且不许冲到实时前面"""
        pa, stream = self._stream()
        self._wait_pump_reading(pa, stream)

        start = time.monotonic()
        delivered = 0
        while time.monotonic() - start < 0.5:
            data = stream.read()
            self.assertIsNotNone(data, "设备只是没数据，不是流结束了")
            if data:
                self.assertEqual(data, b"\x00" * len(data))
                delivered += len(data)
        elapsed = time.monotonic() - start
        stream.close()

        # 欠账补齐 → 至少到实时的一半；补太多 → 音频会冲到视频前面憋死交织器
        self.assertGreater(delivered, elapsed * 192000 * 0.5)
        self.assertLessEqual(delivered, elapsed * 192000 * 1.2)

    def test_close_is_executed_by_the_pump_thread_only(self):
        """主线程 close() 只发通知；碰 C 层的必须是采集线程，且只碰一次"""
        pa, stream = self._stream()
        self._wait_pump_reading(pa, stream)

        stream.close()
        stream.close()                     # 重复关不许再走一遍
        self.assertNotIn("close", pa.calls, "主线程踩了已阻塞的 PaStream")

        pa.stream.released.set()           # read 返回，采集线程自己收尾
        deadline = time.time() + 3
        while "close" not in pa.calls and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(pa.thread.get("close"), "loopback-pump")
        self.assertEqual(pa.calls.count("close"), 1)
        self.assertEqual(pa.calls.count("terminate"), 1)
        self.assertIsNone(stream.read())   # feeder 据此退出，不空转

    def test_stop_leaves_the_wedged_stream_to_the_pump(self):
        """capture.stop()：feeder 不被设备拖住，关流仍只发生在采集线程"""
        pa, stream = self._stream()
        cap = FfmpegCapture("ffmpeg")
        cap._loopback = stream
        cap._running = True
        proc = _fake_proc()
        proc.stdin = io.BytesIO()

        feeder = threading.Thread(target=cap._feed_loopback, args=(proc,),
                                  name="ffmpeg-hls-loopback")
        cap._feeder = feeder
        feeder.start()
        self._wait_pump_reading(pa, stream)

        cap.stop()
        feeder.join(3)                     # 零交付只是静音，不该把 feeder 拖死
        self.assertFalse(feeder.is_alive())
        self.assertIsNone(cap._loopback)
        self.assertNotEqual(pa.thread.get("close"), "MainThread",
                            "停止链路在主线程上关了 PaStream")

        pa.stream.released.set()
        deadline = time.time() + 3
        while "close" not in pa.calls and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(pa.thread.get("close"), "loopback-pump")


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
        # 确认窗口现在是后台参数（screencast.dlna_verify_seconds，下限 3 秒）：
        # 用例只替换读取入口，不动登记表
        def short_param(key):
            return 0.05 if key == "screencast.dlna_verify_seconds" else app_params.get(key)

        with patch.object(manager_mod, "param", short_param):
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

    @patch("sys.platform", "darwin")
    def test_macos_tap_index_is_refreshed_at_start(self):
        """环回设备是枚举时现建的，avfoundation 索引随别的设备增减而变：
        界面里存的那份必须按名称换成本次枚举到的真实索引，否则采的是别的设备"""
        mgr = _manager()
        mgr._state["running"] = False
        started = {}

        def fake_start(target, params, source, audio, video):
            started.update(audio)
            return {"ok": True, "target": target, "session_id": "abc123"}

        with patch.object(mgr.ffmpeg, "check", return_value={"ok": True}), \
             patch.object(mgr.ffmpeg, "_list_audio_devices", return_value=[
                 {"name": "2:MacBook Pro麦克风", "kind": "mic"},
                 {"name": "5:HanCast 系统声音", "kind": "system", "tap": True}]), \
             patch.object(mgr.ffmpeg, "audio_input_silent", return_value=False), \
             patch.object(mgr, "_start", side_effect=fake_start):
            result = mgr.start({"target": "browser",
                                "source": {"type": "screen"},
                                "audio": {"system": True,
                                          "system_device": "3:HanCast 系统声音"}})
        self.assertTrue(result["ok"])
        self.assertEqual(started["system_device"], "5:HanCast 系统声音")

    def test_mic_without_signal_blocks_start(self):
        """没有录音权限时系统不报错、也不弹窗，采回来的全是 0：
        宁可不投，也不能投出一条用户以为在收声的哑流"""
        mgr = _manager()
        mgr._state["running"] = False
        with patch.object(mgr.ffmpeg, "check", return_value={"ok": True}), \
             patch.object(mgr.ffmpeg, "_list_audio_devices", return_value=[
                 {"name": "麦克风", "kind": "mic"}]), \
             patch.object(mgr.ffmpeg, "audio_input_silent", return_value=True):
            result = mgr.start({"target": "browser",
                                "source": {"type": "screen"},
                                "audio": {"mic": True, "mic_device": "麦克风"}})
        self.assertFalse(result["ok"])
        self.assertIn("静音", result["error"])
        self.assertIn("权限", result["error"])

    @patch("sys.platform", "darwin")
    def test_teardown_releases_tap_device(self):
        """停投就把自建的系统声音设备摘掉，别在用户的声音列表里留尾巴"""
        mgr = _manager(running=False)
        with patch("hancast_sidecar.screencast.macos_tap.destroy_device") as dd:
            mgr._teardown()
        dd.assert_called_once()

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


class TestHlsDidl(unittest.TestCase):
    """DLNA 推送必须自带 DIDL：渲染器靠 res 的 protocolInfo 选解码栈"""

    def test_advertises_hls_mime_and_title(self):
        uri = "http://192.168.1.68:8765/hls/a50d42/live.m3u8"
        didl = build_didl_item(uri, HLS_MIME, '屏幕投屏 <"HanCast">')
        self.assertIn(f"http-get:*:{HLS_MIME}:*", didl)
        self.assertIn("&lt;\"HanCast\"&gt;", didl)
        root = etree.fromstring(didl.encode())
        ns = {"d": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
              "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/"}
        self.assertEqual(root.find("d:item/d:res", ns).text, uri)
        self.assertEqual(
            root.find("d:item/upnp:class", ns).text, "object.item.videoItem")

    def test_empty_title_falls_back(self):
        self.assertIn("<dc:title>HanCast</dc:title>",
                      build_didl_item("http://x/y.m3u8", HLS_MIME, ""))


if __name__ == "__main__":
    unittest.main()
