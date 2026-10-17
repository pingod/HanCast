"""
Linux / Wayland 屏幕抓取与音频链路单元测试（v2.3.19）

这一版把「Linux 投屏全是黑屏 + 麦克风/系统声音不可用」的修法钉住：
  1. helper 探测：存在 ≠ 能用。必须真的跑一次 `list` 并解析 JSON——
     v2.3.17 随包 mpv「四条存在性检查全绿、装端 exit 127」就是没做这件事。
  2. 视频输入：Wayland 走 rawvideo 管道（helper 的 stdout），拿不到 helper
     就失败并把原因说出来，绝不退回 x11grab 再投一路纯黑（YAVG=16.03）。
  3. 音频输入：Linux 用 `-f pulse`，系统声音=输出总线的 `.monitor` 源。
  4. pulse 枚举：PipeWire 在 pactl JSON 里把 description 写成 "(null)"，
     可读名字来自 pw-dump；pactl 的 stderr 噪音不参与解析。

不启动真实 FFmpeg / 真实 helper：子进程全部为临时目录里的假脚本或 mock。
"""

import io
import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.screencast import pulse as pulse_mod
from hancast_sidecar.screencast import wlgrab
from hancast_sidecar.screencast.capture import FfmpegCapture
from hancast_sidecar.screencast.ffmpeg_manager import FfmpegManager

# 2026-10-17 CachyOS / Hyprland 真机的 `hancast-wlgrab list` 输出形状
REAL_OUTPUTS = [
    {"name": "DP-2", "description":
        "AOC AG273QG3R3B ASMP59A005500 (DP-2)",
     "make": "AOC", "model": "AG273QG3R3B",
     "x": 0, "y": 1120, "width": 2560, "height": 1440, "scale": 1},
    {"name": "DP-3", "description":
        "DEL S3220DGF 6H43NL1 (DP-3)",
     "make": "DEL", "model": "S3220DGF",
     "x": 2560, "y": 0, "width": 2560, "height": 1440, "scale": 1},
]

USB_MONITOR = ("alsa_output.usb-bestechnic_HUAWEI_Sound_Joy_20160406.1-00"
               ".analog-stereo.monitor")
HDMI_MONITOR = "alsa_output.pci-0000_0d_00.6.hdmi-stereo.monitor"
MIC = "alsa_input.usb-ORBBEC_ASTRA_Pro-01.analog-stereo"
USB_SINK = USB_MONITOR[: -len(".monitor")]


def _fake_proc():
    proc = MagicMock()
    proc.poll.return_value = None
    proc.returncode = None
    proc.stderr = io.BytesIO(b"")
    return proc


class WlgrabStateCase(unittest.TestCase):
    """wlgrab 的模块级探测缓存必须在用例之间还原，否则一条过了后面全过"""

    def setUp(self):
        self._saved_probe = wlgrab._probe_result
        self._saved_outputs = list(wlgrab._outputs_cache)
        self.addCleanup(self._restore)

    def _restore(self):
        wlgrab._probe_result = self._saved_probe
        wlgrab._outputs_cache[:] = self._saved_outputs


class TestHelperProbe(WlgrabStateCase):
    """find_helper 认的是「跑起来的 helper」，不是「躺在磁盘上的文件」"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="hancast-wlgrab-probe-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _helper(self, body: str, mode: int = 0o755) -> str:
        path = os.path.join(self.tmp, "hancast-wlgrab")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, mode)
        return path

    def _find(self, path: str):
        with patch.object(wlgrab, "_candidate_paths", return_value=[path]):
            return wlgrab.find_helper(force=True)

    @unittest.skipIf(os.name == "nt", "chmod 执行位只对 POSIX 有意义")
    def test_non_executable_helper_rejected(self):
        """mode 644 的 helper：文件在、`isfile` 也真，exec 直接 PermissionError。
        Preflight（scripts/check-resources.cjs）已经查执行位，运行侧再兜一层。"""
        path = self._helper("echo '[]'\n", mode=0o644)
        found, reason = self._find(path)
        self.assertIsNone(found)
        self.assertIn("已试", reason)
        self.assertIn(path, reason)

    def test_non_json_helper_rejected(self):
        """能执行但吐的不是 JSON 数组 → 不算就绪（同名异物、旧版本都要挡掉）"""
        found, reason = self._find(self._helper("echo 'hancast-wlgrab 0.1'\n"))
        self.assertIsNone(found)
        self.assertIn("JSON", reason)

    def test_helper_without_screencopy_rejected(self):
        """合成器不给 wlroots screencopy 全局时 helper 退出码 3 + ERR 行：
        原因要原样带给用户，那是「换个会话/换台机器」还是「补包」的判据。"""
        path = self._helper("echo 'ERR 没有 zwlr_screencopy_manager_v1' >&2\n"
                           "exit 3\n")
        found, reason = self._find(path)
        self.assertIsNone(found)
        self.assertIn("退出码 3", reason)
        self.assertIn("zwlr_screencopy_manager_v1", reason)

    def test_good_helper_accepted(self):
        path = self._helper(f"echo '{json.dumps(REAL_OUTPUTS)}'\n")
        found, reason = self._find(path)
        self.assertEqual(found, path)
        self.assertEqual(reason, "")

    def test_env_override_is_first_candidate(self):
        """`HANCAST_WLGRAB` 排在候选首位：真机排障时不用重装包就能试另一个 helper"""
        mine = os.path.join(self.tmp, "mine")
        with patch.dict(os.environ, {"HANCAST_WLGRAB": mine}):
            self.assertEqual(wlgrab._candidate_paths()[0], mine)


class TestStatusAndSources(WlgrabStateCase):
    def test_status_off_wayland(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(sys, "platform", "linux"):
            self.assertEqual(wlgrab.status(),
                             {"wayland": False, "helper": None, "reason": ""})

    def test_status_reports_missing_helper(self):
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-1"}), \
                patch.object(sys, "platform", "linux"), \
                patch.object(wlgrab, "find_helper",
                             return_value=(None, "未随包找到 hancast-wlgrab")):
            st = wlgrab.status()
        self.assertTrue(st["wayland"])
        self.assertIsNone(st["helper"])
        self.assertIn("hancast-wlgrab", st["reason"])

    def test_list_sources_carries_capture_diagnostics(self):
        """前端要靠 capture 那块决定「错误文案说什么」：Wayland 无 helper 与
        FFmpeg 缺失是两件完全不同的事，混成一句就没法修。"""
        mgr = FfmpegManager()
        with patch.object(wlgrab, "status", return_value={
                "wayland": True, "helper": None,
                "reason": "未随包找到 hancast-wlgrab"}), \
                patch.object(wlgrab, "desktop_title", return_value=None), \
                patch.object(FfmpegManager, "_list_monitors", return_value=[]), \
                patch.object(FfmpegManager, "_list_windows", return_value=[]), \
                patch.object(FfmpegManager, "_list_audio_devices",
                             return_value=[]), \
                patch.object(FfmpegManager, "check",
                             return_value={"ok": True}):
            out = mgr.list_sources()
        self.assertEqual(out["capture"]["helper"], None)
        self.assertIn("hancast-wlgrab", out["capture"]["reason"])
        self.assertEqual(out["platform"], sys.platform)


class TestMonitorEntries(WlgrabStateCase):
    def test_entries_ids_labels_and_primary(self):
        with patch.object(wlgrab, "list_outputs", return_value=REAL_OUTPUTS):
            entries = wlgrab.monitor_entries()
        self.assertEqual([e["id"] for e in entries], ["wl:DP-2", "wl:DP-3"])
        self.assertIn("2560x1440", entries[0]["title"])
        self.assertIn("AOC AG273QG3R3B", entries[0]["title"])
        self.assertNotIn("ASMP59A005500", entries[0]["title"])  # 序列号是噪音
        self.assertTrue(entries[0]["primary"])
        self.assertIn("（主屏）", entries[0]["title"])
        self.assertNotIn("（主屏）", entries[1]["title"])

    def test_primary_falls_back_to_first_output(self):
        """没有一块屏摆在坐标原点（罕见但 Hyprland 允许）时不能整列表没有主屏，
        否则「整个屏幕」这一项和单屏列表会指向不同的屏"""
        shifted = [dict(o, x=o["x"] + 100) for o in REAL_OUTPUTS]
        with patch.object(wlgrab, "list_outputs", return_value=shifted):
            entries = wlgrab.monitor_entries()
        self.assertEqual(wlgrab._primary_index(shifted), 0)
        self.assertTrue(entries[0]["primary"])

    def test_desktop_title_names_the_output(self):
        with patch.object(wlgrab, "list_outputs", return_value=REAL_OUTPUTS):
            title = wlgrab.desktop_title()
        self.assertIn("Wayland", title)
        self.assertIn("DP-2", title)

    def test_desktop_title_none_without_outputs(self):
        with patch.object(wlgrab, "list_outputs", return_value=[]):
            self.assertIsNone(wlgrab.desktop_title())

    def test_resolve_output(self):
        cases = [({"id": "wl:DP-3"}, "DP-3"),
                 ({"id": "wl:HDMI-A-99"}, "DP-2"),   # 列表里没有 → 主屏
                 ({"id": "desktop"}, "DP-2"),
                 ({}, "DP-2")]
        with patch.object(wlgrab, "list_outputs", return_value=REAL_OUTPUTS):
            for source, want in cases:
                self.assertEqual(wlgrab.resolve_output(source), want, source)
        with patch.object(wlgrab, "list_outputs", return_value=[]):
            self.assertIsNone(wlgrab.resolve_output({"id": "wl:DP-2"}))


class TestWaylandVideoInput(unittest.TestCase):
    """rawvideo 管道的命令形状：错一个选项名就是一屏斜纹雪花，而且不报错"""

    def _build(self, wl_size):
        with patch.object(os, "name", "posix"), \
                patch.object(sys, "platform", "linux"):
            return FfmpegManager.build_video_input(
                {"type": "screen", "id": "wl:DP-2"}, 15, "", "",
                wl_size=wl_size)[0]

    def test_helper_frames_are_input_zero(self):
        args = self._build((2560, 1440))
        self.assertEqual(args[:2], ["-f", "rawvideo"])
        self.assertIn("-pixel_format", args)
        self.assertEqual(args[args.index("-pixel_format") + 1], "bgra")
        self.assertEqual(args[args.index("-video_size") + 1], "2560x1440")
        self.assertEqual(args[-2:], ["-i", "pipe:0"])
        self.assertNotIn("x11grab", args)

    def test_uses_pixel_format_not_pix_fmt(self):
        """rawvideo 只认 -pixel_format。写成 -pix_fmt 会被当未知选项丢掉，
        然后按默认 yuv420p 解释 BGRA 字节——画面歪成斜纹，日志一个字都不报。"""
        args = self._build((2560, 1440))
        self.assertNotIn("-pix_fmt", args)

    def test_x11grab_only_when_no_helper_size(self):
        args = self._build(None)
        self.assertEqual(args[:2], ["-f", "x11grab"])
        self.assertNotIn("rawvideo", args)

    def test_scale_filter_kept_separate(self):
        """缩放滤镜要留在返回值里由调用方排到所有 -i 之后（夹在中间会落到麦克风）"""
        with patch.object(os, "name", "posix"), \
                patch.object(sys, "platform", "linux"):
            args, vf = FfmpegManager.build_video_input(
                {"type": "screen", "id": "desktop"}, 15, "scale=1280:720", "",
                wl_size=(2560, 1440))
        self.assertEqual(vf, "scale=1280:720")
        self.assertNotIn("-vf", args)


class TestLinuxAudioInput(unittest.TestCase):
    def _build(self, cfg, offset=1):
        with patch.object(os, "name", "posix"), \
                patch.object(sys, "platform", "linux"):
            return FfmpegManager.build_audio_input(cfg, offset)

    def test_mic_plus_monitor_is_two_pulse_inputs_amixed(self):
        args = self._build({"mic": True, "mic_device": MIC,
                            "system": True, "system_device": USB_MONITOR})
        self.assertEqual(args.count("-f"), 2)
        self.assertEqual(args.count("pulse"), 2)
        self.assertIn(MIC, args)
        self.assertIn(USB_MONITOR, args)
        graph = args[args.index("-filter_complex") + 1]
        # 视频占输入 0，所以标签从 1:a 起；指错输入 FFmpeg 直接启动失败
        self.assertIn("[1:a]", graph)
        self.assertIn("[2:a]", graph)
        self.assertIn("amix=inputs=2", graph)
        self.assertIn("normalize=0", graph)

    def test_monitor_only(self):
        args = self._build({"system": True, "system_device": USB_MONITOR})
        self.assertEqual(args, ["-f", "pulse", "-i", USB_MONITOR])

    def test_mic_without_device_falls_back_to_default(self):
        """老客户端/列表没刷新时退回默认录音源，别把「勾了麦克风」变成起不来"""
        args = self._build({"mic": True})
        self.assertEqual(args, ["-f", "pulse", "-i", "default"])

    def test_nothing_checked_means_no_audio_inputs(self):
        self.assertEqual(self._build({}), [])


class TestPulseEnumeration(unittest.TestCase):
    """pactl/pw-dump 的解析：PipeWire 不给可读名字，pactl 还往 stderr 喷噪音"""

    SOURCES = [
        {"name": MIC, "description": "(null)"},
        {"name": USB_MONITOR, "description": "(null)"},
        {"name": HDMI_MONITOR, "description": "(null)"},
    ]
    PW_DUMP = [
        {"info": {"props": {"media.class": "Audio/Sink",
                           "node.name": USB_SINK,
                           "node.description": "HUAWEI Sound Joy 模拟立体声"}}},
        {"info": {"props": {"media.class": "Audio/Source",
                           "node.name": MIC,
                           "node.description": "Astra Pro 麦克风"}}},
    ]
    PACTL_INFO = ("Server Name: pulseaudio\n"
                  f"Default Source: {MIC}\n"
                  f"Default Sink: {USB_SINK}\n")

    def _run(self, argv, **_kw):
        r = MagicMock()
        r.returncode = 0
        r.stderr = ("Invalid ASCII character: 0xe2  (pactl 自己的转义噪音)\n"
                    if argv[:2] == ["pactl", "--format=json"] else "")
        if argv[:2] == ["pactl", "--format=json"]:
            r.stdout = json.dumps(self.SOURCES)
        elif argv[0] == "pw-dump":
            r.stdout = json.dumps(self.PW_DUMP)
        else:
            r.stdout = self.PACTL_INFO
        return r

    def _list(self):
        with patch.object(sys, "platform", "linux"), \
                patch.object(pulse_mod.shutil, "which",
                             return_value="/usr/bin/pactl"), \
                patch.object(pulse_mod.subprocess, "run", side_effect=self._run):
            return pulse_mod.list_input_devices()

    def test_monitor_is_system_sound_and_names_come_from_pw_dump(self):
        devices = {d["name"]: d for d in self._list()}
        self.assertEqual(devices[USB_MONITOR]["kind"], "system")
        self.assertTrue(devices[USB_MONITOR]["monitor"])
        self.assertEqual(devices[USB_MONITOR]["title"], "HUAWEI Sound Joy 模拟立体声")
        self.assertEqual(devices[MIC]["kind"], "mic")
        self.assertEqual(devices[MIC]["title"], "Astra Pro 麦克风")

    def test_default_marks_the_live_bus(self):
        """闲置输出的 monitor 采得到数据但永远是静音：不标「（默认）」，
        用户就只能一块屏一块屏地试出「投屏正常但没声音」的原因。"""
        devices = {d["name"]: d for d in self._list()}
        self.assertTrue(devices[MIC]["default"])
        self.assertTrue(devices[USB_MONITOR]["default"])
        self.assertFalse(devices[HDMI_MONITOR]["default"])

    def test_defaults_sort_first(self):
        order = [d["name"] for d in self._list()]
        self.assertEqual(order[:2], [USB_MONITOR, MIC])
        self.assertEqual(order[-1], HDMI_MONITOR)

    def test_fallback_label_without_pw_dump(self):
        """pw-dump 缺失（纯 PulseAudio）时 description 是 "(null)"，
        至少把 alsa 设备名摊成人能看的串，而不是整列表空白"""
        def run(argv, **_kw):
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            r.stdout = (json.dumps(self.SOURCES)
                        if argv[:2] == ["pactl", "--format=json"]
                        else ("{}" if argv[0] == "pw-dump" else self.PACTL_INFO))
            return r
        with patch.object(sys, "platform", "linux"), \
                patch.object(pulse_mod.shutil, "which",
                             return_value="/usr/bin/pactl"), \
                patch.object(pulse_mod.subprocess, "run", side_effect=run):
            devices = {d["name"]: d for d in pulse_mod.list_input_devices()}
        self.assertIn("hdmi-stereo", devices[HDMI_MONITOR]["title"])

    def test_no_pactl_returns_empty(self):
        with patch.object(sys, "platform", "linux"), \
                patch.object(pulse_mod.shutil, "which", return_value=None):
            self.assertEqual(pulse_mod.list_input_devices(), [])

    def test_not_linux_returns_empty(self):
        with patch.object(sys, "platform", "darwin"):
            self.assertEqual(pulse_mod.list_input_devices(), [])


class _FakeWlSession:
    """够 capture.FfmpegCapture 用的假 WlGrabSession"""

    def __init__(self, path):
        self.path = path
        self.hdr = {"width": 2560, "height": 1440, "stride": 10240,
                   "pix_fmt": "ARGB8888", "output": "DP-2", "scale": 1,
                   "y_invert": False}
        self.error = None
        self.tail = []
        self.closed = 0
        self.stopped = 0
        self.started = None
        self.ok = True
        self._stdout = io.BytesIO()

    def start(self, output, fps=15, cursor=False, region=None):
        self.started = (output, fps)
        return self.ok

    @property
    def stdout(self):
        return self._stdout

    def close_stdout(self):
        self.closed += 1

    def stop(self):
        self.stopped += 1


class TestCaptureWaylandGate(WlgrabStateCase):
    """Wayland 会话里「起不来」必须是失败，不能是黑屏"""

    def _start(self, **patches):
        cap = FfmpegCapture("ffmpeg")
        out_dir = tempfile.mkdtemp(prefix="hancast-hls-wl-")
        self.addCleanup(lambda: shutil.rmtree(out_dir, ignore_errors=True))
        with patch.object(sys, "platform", "linux"), \
                patch.object(os, "name", "posix"), \
                patch.object(wlgrab, "wayland_session", return_value=True), \
                patch.object(FfmpegCapture, "_watch"):
            for key, value in patches.items():
                self.enterPatch(key, value)
            result = cap.start_hls({"type": "screen", "id": "wl:DP-2"}, {},
                                   {"width": 1280, "height": 720, "fps": 15,
                                    "bitrate": "2M"},
                                   out_dir, target="browser")
            return cap, result

    def enterPatch(self, name, value):
        cm = value if hasattr(value, "__enter__") else patch(**{name: value})
        cm.start()
        self.addCleanup(cm.stop)

    def test_missing_helper_fails_without_black_fallback(self):
        """拿不到 helper 就报错退出：_spawn 一次都不能被调用，
        否则退回 x11grab 投的是空的 root window——流畅、无声异常、纯黑。"""
        spawn = MagicMock()
        cap, result = self._start(
            find_helper=patch.object(wlgrab, "find_helper",
                                     return_value=(None, "未随包找到 hancast-wlgrab")),
            _spawn=patch.object(FfmpegCapture, "_spawn", spawn))
        self.assertFalse(result["ok"])
        self.assertIn("Wayland 屏幕抓取不可用", result["error"])
        self.assertIn("hancast-wlgrab", result["error"])
        self.assertIn("x11grab", result["error"])
        spawn.assert_not_called()
        self.assertFalse(cap.running)
        cap.stop()

    def test_helper_that_cannot_stream_fails(self):
        session = _FakeWlSession("/fake/hancast-wlgrab")
        session.ok = False
        session.error = ("抓取进程没报出首帧尺寸（退出码 3）："
                         "ERR 没有 zwlr_screencopy_manager_v1")
        spawn = MagicMock()
        cap, result = self._start(
            find_helper=patch.object(wlgrab, "find_helper",
                                     return_value=("/fake/hancast-wlgrab", "")),
            resolve_output=patch.object(wlgrab, "resolve_output",
                                        return_value="DP-2"),
            WlGrabSession=patch.object(wlgrab, "WlGrabSession",
                                       return_value=session),
            _spawn=patch.object(FfmpegCapture, "_spawn", spawn))
        self.assertFalse(result["ok"])
        self.assertIn("Wayland 屏幕抓取失败", result["error"])
        self.assertIn("zwlr_screencopy_manager_v1", result["error"])
        spawn.assert_not_called()
        self.assertEqual(session.stopped, 1)
        cap.stop()

    def test_helper_stdout_becomes_ffmpeg_stdin(self):
        """喂帧的三件事缺一不可：rawvideo pipe:0 的输入、helper stdout 挂到
        FFmpeg 的 stdin、父进程那份读端立刻关掉（不关，helper 退出时 FFmpeg
        收不到 EOF，停止投屏变成等满超时）"""
        session = _FakeWlSession("/fake/hancast-wlgrab")
        seen = {}
        m3u8_written = {}

        def fake_spawn(cmd, use_stdin=False, stdin_stream=None):
            seen["cmd"] = cmd
            seen["stdin_stream"] = stdin_stream
            seen["use_stdin"] = use_stdin
            with open(cmd[-1], "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
            m3u8_written["ok"] = True
            return _fake_proc()

        cap, result = self._start(
            find_helper=patch.object(wlgrab, "find_helper",
                                     return_value=("/fake/hancast-wlgrab", "")),
            resolve_output=patch.object(wlgrab, "resolve_output",
                                        return_value="DP-2"),
            WlGrabSession=patch.object(wlgrab, "WlGrabSession",
                                       return_value=session),
            _spawn=patch.object(FfmpegCapture, "_spawn",
                                side_effect=fake_spawn))
        self.assertTrue(result["ok"], result.get("error"))
        self.assertTrue(m3u8_written.get("ok"))
        self.assertEqual(session.started, ("DP-2", 15))
        cmd = seen["cmd"]
        self.assertIn("rawvideo", cmd)
        self.assertEqual(cmd[cmd.index("-video_size") + 1], "2560x1440")
        self.assertIs(seen["stdin_stream"], session._stdout)
        self.assertFalse(seen["use_stdin"])   # 环回是 Windows 的事
        self.assertEqual(session.closed, 1)   # 父进程那份读端已释放
        self.assertIs(cap._wl, session)
        cap.stop()
        self.assertGreaterEqual(session.stopped, 1)
        self.assertFalse(cap.running)


if __name__ == "__main__":
    unittest.main()
