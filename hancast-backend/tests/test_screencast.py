"""
屏幕投屏子系统单元测试

覆盖：FFmpeg 查找/参数拼接、音频设备分类、捕获管线、HTTP 路径安全、
WebRTC 房间共享 track。不启动真实 FFmpeg 进程或网络服务。
"""

import io
import os
import sys
import asyncio
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.screencast.ffmpeg_manager import FfmpegManager
from hancast_sidecar.screencast.capture import FfmpegCapture, _read_exact
from hancast_sidecar.screencast.httpd import ScreenCastHttpd
from hancast_sidecar.screencast.webrtc_room import SharedVideoTrack, SharedAudioTrack


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
        # 麦克风 + BlackHole 双路混音
        args = FfmpegManager.build_audio_input({
            "mic": True, "mic_device": "0:Built-in Microphone",
            "system": True, "system_device": "2:BlackHole 2ch",
        })
        self.assertEqual(args, [
            "-f", "avfoundation", "-i", ":0",
            "-f", "avfoundation", "-i", ":2",
            "-filter_complex",
            "[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0",
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


class TestCapture(unittest.TestCase):
    """捕获管线"""

    def test_read_exact(self):
        stream = io.BytesIO(b"abcdef")
        self.assertEqual(_read_exact(stream, 3), b"abc")
        self.assertEqual(_read_exact(stream, 2), b"de")
        # EOF 返回 None
        self.assertIsNone(_read_exact(stream, 10))

    def test_stop_idempotent(self):
        cap = FfmpegCapture("ffmpeg")
        cap.stop()  # 空状态，不抛异常
        cap.stop()  # 幂等
        self.assertFalse(cap.running)


class TestHttpd(unittest.TestCase):
    """HTTP 服务"""

    def test_safe_hls_path_traversal(self):
        import tempfile
        httpd = ScreenCastHttpd(hls_root=tempfile.mkdtemp(prefix="hancast-hls-test-"))
        # 目录穿越被拒绝
        self.assertIsNone(httpd._safe_hls_path("dlna", "../secret.txt"))
        self.assertIsNone(httpd._safe_hls_path("dlna", "..\\secret.txt"))
        self.assertIsNone(httpd._safe_hls_path("dlna", "C:\\windows\\system32\\x"))
        # 正常文件路径可解析（不要求文件存在）
        path = httpd._safe_hls_path("dlna", "seg001.ts")
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith(os.path.join("dlna", "seg001.ts")))


class TestWebRtcRoom(unittest.TestCase):
    """WebRTC 房间（不启动网络）"""

    def _loop(self):
        """每个测试独立事件循环，避免污染默认 loop"""
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        return loop

    def test_video_track_no_frame(self):
        from hancast_sidecar.screencast.webrtc_room import MediaStreamError
        track = SharedVideoTrack(fps=15)
        loop = self._loop()
        with self.assertRaises(MediaStreamError):
            loop.run_until_complete(track.recv())

    def test_video_track_with_frame(self):
        from hancast_sidecar.screencast.webrtc_room import av, MediaStreamError
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not available")
        if av is None:
            self.skipTest("av not available")
        track = SharedVideoTrack(fps=15)
        arr = np.zeros((360, 640, 3), dtype=np.uint8)
        track.set_frame(arr)
        loop = self._loop()
        frame = loop.run_until_complete(track.recv())
        self.assertEqual(frame.width, 640)
        self.assertEqual(frame.height, 360)

    def test_audio_track_push(self):
        from hancast_sidecar.screencast.webrtc_room import av, MediaStreamError
        if av is None:
            self.skipTest("av not available")
        loop = self._loop()
        track = SharedAudioTrack(loop)
        # 20ms 帧：1920 samples * 2ch * 2bytes
        data = bytes(1920 * 2 * 2)
        track.push(data)
        frame = loop.run_until_complete(track.recv())
        self.assertEqual(frame.sample_rate, 48000)


if __name__ == "__main__":
    unittest.main()
