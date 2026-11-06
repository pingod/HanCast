"""
手机镜像子系统单元测试

覆盖：adb 输出解析、scrcpy 参数拼接（版本相关的音频开关）、版本解析、
工具检测的「跑不起来不算可用」口径、管理器生命周期契约（启动/失败/停止/幂等）。

不启动真实 adb/scrcpy 进程：Popen、adb 探测、事件推送全部打桩。
本机 adb/scrcpy 的真实路径只用于一条手工验收，不进 CI。
"""

import io
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.mirror import manager as manager_mod
from hancast_sidecar.mirror import tools as tools_mod


class FakeConfig:
    """只实现 manager 用到的三个方法。"""

    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def save(self):
        pass


class FakeProcess:
    """posix 语义的假进程：wait() 会一直阻塞到 terminate/kill（真 scrcpy 也不会自己退出）。"""

    def __init__(self, *args, **kwargs):
        self.args = list(args[0]) if args else []
        self.pid = 4242
        self.returncode = None
        self._done = threading.Event()
        self.stderr = io.BytesIO(b"")

    def poll(self):
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return self.returncode if self._done.is_set() else None

    def terminate(self):
        self.returncode = 0
        self._done.set()

    def kill(self):
        self.returncode = -9
        self._done.set()


def _tool_status(ok=True, scrcpy_version="5.0.1", audio_supported=True):
    adb = {
        "key": "adb",
        "found": ok,
        "path": "/usr/bin/adb" if ok else None,
        "version": "1.0.41" if ok else None,
        "source": "system" if ok else None,
        "error": None if ok else "未找到可执行文件",
    }
    scrcpy = {
        "key": "scrcpy",
        "found": ok,
        "path": "/usr/bin/scrcpy" if ok else None,
        "version": scrcpy_version if ok else None,
        "source": "system" if ok else None,
        "error": None if ok else "未找到可执行文件",
    }
    return {
        "ok": ok,
        "platform": "linux",
        "tools": [adb, scrcpy],
        "tools_found": {"adb": ok, "scrcpy": ok},
        "install_commands": ["sudo apt install adb scrcpy"],
        "scrcpy_audio_supported": audio_supported,
        "scrcpy_audio_min_version": "2.0.0",
    }


ADB_DEVICES = """List of devices attached
emulator-5554          device product:sdk_gphone64_arm64 model:sdk_gphone64_arm64 device:emu64a transport_id:1
R58M123ABC            unauthorized usb:337641472X transport_id:2
192.168.1.20:5555      offline transport_id:3
"""


class ParseAdbDevicesTests(unittest.TestCase):
    def test_parses_states_and_models(self):
        devices = tools_mod.parse_adb_devices(ADB_DEVICES)
        self.assertEqual([d["serial"] for d in devices],
                         ["emulator-5554", "R58M123ABC", "192.168.1.20:5555"])
        self.assertTrue(devices[0]["online"])
        # adb 输出里的下划线是占位（型号名不能带空格），解析后还原成空格
        self.assertEqual(devices[0]["model"], "sdk gphone64 arm64")
        self.assertEqual(devices[0]["product"], "sdk_gphone64_arm64")
        self.assertEqual(devices[0]["transport_id"], "1")
        self.assertFalse(devices[1]["online"])
        self.assertEqual(devices[1]["state"], "unauthorized")
        # tcp/ip 连接的序列号带端口，界面靠它区分 USB 与无线
        self.assertTrue(devices[2]["wireless"])
        self.assertFalse(devices[0]["wireless"])

    def test_skips_daemon_noise_and_headers(self):
        text = (
            "* daemon not running; starting now at tcp:5037\n"
            "* daemon started successfully\n"
            "List of devices attached\n"
            "\n"
        )
        self.assertEqual(tools_mod.parse_adb_devices(text), [])

    def test_underscore_model_becomes_spaces(self):
        devices = tools_mod.parse_adb_devices(
            "abc123 device product:foo model:Pixel_7_Pro transport_id:9\n"
        )
        self.assertEqual(devices[0]["model"], "Pixel 7 Pro")


class VersionTests(unittest.TestCase):
    def test_parse_scrcpy_and_adb_versions(self):
        self.assertEqual(
            tools_mod.parse_version("scrcpy", "scrcpy 5.0.1 <https://github.com/Genymobile/scrcpy>"),
            "5.0.1",
        )
        self.assertEqual(
            tools_mod.parse_version("scrcpy", "scrcpy 2.4\nDependencies:"),
            "2.4",
        )
        self.assertEqual(
            tools_mod.parse_version("adb", "Android Debug Bridge version 1.0.41\nVersion 37.0.1-15733141"),
            "1.0.41",
        )
        self.assertEqual(
            tools_mod.parse_version("adb", "adb: android debug bridge\nVersion 37.0.1-15733141"),
            "37.0.1-15733141",
        )

    def test_version_tuple_and_audio_threshold(self):
        self.assertEqual(tools_mod.version_tuple("5.0.1"), (5, 0, 1))
        self.assertEqual(tools_mod.version_tuple(None), ())
        self.assertGreaterEqual(
            tools_mod.version_tuple("5.0.1"), tools_mod.SCRCPY_AUDIO_MIN_VERSION
        )
        self.assertLess(tools_mod.version_tuple("1.24"), tools_mod.SCRCPY_AUDIO_MIN_VERSION)


class FindToolTests(unittest.TestCase):
    def test_manual_path_does_not_fall_back(self):
        # 手动指定错文件时不静默回退：界面必须看见「没找到」，而不是用别的 adb
        with patch.object(tools_mod, "_executable", return_value=False):
            self.assertEqual(tools_mod.find_tool("adb", "/nonexistent/adb"), (None, None))

    def test_missing_executable_is_not_found(self):
        with patch.object(tools_mod, "find_tool", return_value=(None, None)):
            tools_mod.clear_cache()
            info = tools_mod.tool_info("scrcpy")
        self.assertFalse(info["found"])
        self.assertIsNone(info["path"])
        self.assertEqual(info["error"], "未找到可执行文件")

    def test_broken_binary_reports_reason_with_path(self):
        # 文件在、跑不起来（架构不对 / 依赖缺）→ 按缺失处理，但要说清是哪份文件
        with patch.object(tools_mod, "find_tool", return_value=("/opt/scrcpy", "system")), \
                patch.object(tools_mod, "_run", return_value=(-1, "", "bad CPU type")):
            tools_mod.clear_cache()
            info = tools_mod.tool_info("scrcpy")
        self.assertFalse(info["found"])
        self.assertIn("bad CPU type", info["error"])
        self.assertIn("/opt/scrcpy", info["error"])


class BuildArgsTests(unittest.TestCase):
    def test_defaults_only_pass_serial(self):
        args = manager_mod.build_scrcpy_args("scrcpy", {"serial": "abc"})
        self.assertEqual(args, ["scrcpy", "--serial", "abc"])

    def test_full_options(self):
        args = manager_mod.build_scrcpy_args("scrcpy", {
            "serial": "abc",
            "max_size": 720,
            "max_fps": 30,
            "video_bitrate": 4,
            "audio": False,
            "control": False,
            "turn_screen_off": True,
            "stay_awake": True,
            "always_on_top": True,
            "window_title": "HanCast 镜像",
        })
        self.assertEqual(args[:3], ["scrcpy", "--serial", "abc"])
        for flag in ("--no-audio", "--no-control", "--turn-screen-off", "--stay-awake",
                     "--always-on-top"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--max-size") + 1], "720")
        self.assertEqual(args[args.index("--max-fps") + 1], "30")
        self.assertEqual(args[args.index("--video-bit-rate") + 1], "4M")
        self.assertEqual(args[args.index("--window-title") + 1], "HanCast 镜像")

    def test_clamps_absurd_values(self):
        args = manager_mod.build_scrcpy_args("scrcpy", {
            "serial": "abc", "max_size": 99999, "max_fps": 999, "video_bitrate": 9999,
        })
        self.assertEqual(args[args.index("--max-size") + 1], "4096")
        self.assertEqual(args[args.index("--max-fps") + 1], "240")
        self.assertEqual(args[args.index("--video-bit-rate") + 1], "200M")

    def test_old_scrcpy_gets_no_audio_flag(self):
        # 2.0 之前没有音频转发：传 --no-audio 会直接 unknown option 打死会话
        args = manager_mod.build_scrcpy_args("scrcpy", {"serial": "abc", "audio": False},
                                             audio_supported=False)
        self.assertNotIn("--no-audio", args)

    def test_requires_serial(self):
        with self.assertRaises(ValueError):
            manager_mod.build_scrcpy_args("scrcpy", {})


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.config = FakeConfig()
        self.manager = manager_mod.MirrorManager(self.config)
        self.events = []
        self.emit_patch = patch.object(
            manager_mod.protocol_io, "emit_event",
            side_effect=lambda name, data: self.events.append((name, data)),
        )
        self.emit_patch.start()
        self.addCleanup(self.emit_patch.stop)
        grace_patch = patch.object(manager_mod, "_START_GRACE_SECONDS", 0.01)
        grace_patch.start()
        self.addCleanup(grace_patch.stop)

    def _patch_tools(self, status=None, adb_output=ADB_DEVICES, adb_code=0):
        status = status or _tool_status()
        patches = [
            patch.object(manager_mod.tools, "tool_status", return_value=status),
            patch.object(manager_mod.tools, "run_tool",
                         return_value=(adb_code, adb_output, "")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_status_reports_tools(self):
        self._patch_tools()
        state = self.manager.status()
        self.assertTrue(state["ok"])
        self.assertFalse(state["running"])
        self.assertEqual([t["key"] for t in state["tools"]], ["adb", "scrcpy"])
        self.assertEqual(state["window"], "external")

    def test_start_spawns_scrcpy_and_emits_event(self):
        self._patch_tools()
        with patch.object(manager_mod.subprocess, "Popen", side_effect=FakeProcess) as popen:
            result = self.manager.start({"serial": "emulator-5554", "max_size": 720})
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["running"])
        self.assertEqual(result["pid"], 4242)
        self.assertEqual(result["model"], "sdk gphone64 arm64")
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], "/usr/bin/scrcpy")
        self.assertIn("--serial", argv)
        self.assertIn("emulator-5554", argv)
        self.assertIn("--max-size", argv)
        # 进程是独立会话启动的：sidecar 被 SIGTERM 时不连坐带走
        self.assertTrue(popen.call_args[1].get("start_new_session"))
        self.assertEqual(self.events[-1][0], "mirror_state_changed")
        self.assertTrue(self.events[-1][1]["running"])

    def test_start_rejects_second_session(self):
        self._patch_tools()
        with patch.object(manager_mod.subprocess, "Popen", side_effect=FakeProcess):
            self.assertTrue(self.manager.start({"serial": "emulator-5554"})["ok"])
            second = self.manager.start({"serial": "emulator-5554"})
        self.assertFalse(second["ok"])
        self.assertIn("已有镜像会话", second["error"])

    def test_start_reports_unauthorized_hint(self):
        self._patch_tools()
        result = self.manager.start({"serial": "R58M123ABC"})
        self.assertFalse(result["ok"])
        self.assertIn("授权", result["error"])

    def test_start_rejects_unknown_serial(self):
        self._patch_tools()
        result = self.manager.start({"serial": "nope"})
        self.assertFalse(result["ok"])
        self.assertIn("找不到设备", result["error"])

    def test_start_reports_missing_tools(self):
        self._patch_tools(status=_tool_status(ok=False))
        result = self.manager.start({"serial": "emulator-5554"})
        self.assertFalse(result["ok"])
        self.assertIn("adb", result["error"])
        self.assertIn("scrcpy", result["error"])

    def test_immediate_exit_returns_stderr_tail(self):
        self._patch_tools()

        class DyingProcess(FakeProcess):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.returncode = 1
                self.stderr = io.BytesIO(b"ERROR: Could not find any ADB device\n")
                self._done.set()

        with patch.object(manager_mod.subprocess, "Popen", side_effect=DyingProcess):
            result = self.manager.start({"serial": "emulator-5554"})
        self.assertFalse(result["ok"])
        self.assertIn("ADB device", result["error"])
        self.assertFalse(self.manager.status()["running"])

    def test_stop_is_idempotent_and_emits(self):
        self._patch_tools()
        with patch.object(manager_mod.subprocess, "Popen", side_effect=FakeProcess):
            self.manager.start({"serial": "emulator-5554"})
            stopped = self.manager.stop()
        self.assertTrue(stopped["ok"])
        self.assertTrue(stopped["stopped"])
        self.assertFalse(stopped["running"])
        self.assertFalse(self.manager.stop()["stopped"])
        self.assertFalse(self.manager.status()["running"])

    def test_spawn_failure_is_reported(self):
        self._patch_tools()
        with patch.object(manager_mod.subprocess, "Popen", side_effect=OSError("permission denied")):
            result = self.manager.start({"serial": "emulator-5554"})
        self.assertFalse(result["ok"])
        self.assertIn("permission denied", result["error"])

    def test_set_tool_path_roundtrip(self):
        with tempfile.NamedTemporaryFile(prefix="fake-scrcpy-", delete=False) as fh:
            fh.write(b"#!/bin/sh\necho 'scrcpy 5.0.1'\n")
            path = fh.name
        os.chmod(path, 0o755)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        self._patch_tools()
        result = self.manager.set_tool_path({"tool": "scrcpy", "path": path})
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.config.get(manager_mod.KEY_SCRCPY), path)

        # 空路径恢复自动检测
        self.assertTrue(self.manager.set_tool_path({"tool": "scrcpy", "path": ""})["ok"])
        self.assertEqual(self.config.get(manager_mod.KEY_SCRCPY), "")

        bad = self.manager.set_tool_path({"tool": "adb", "path": "/nope/adb"})
        self.assertFalse(bad["ok"])
        self.assertIn("不存在", bad["error"])
        self.assertFalse(self.manager.set_tool_path({"tool": "ffmpeg", "path": "/bin/sh"})["ok"])


class ConnectTests(unittest.TestCase):
    def test_connect_validates_address(self):
        manager = manager_mod.MirrorManager(FakeConfig())
        with patch.object(manager_mod.tools, "tool_status", return_value=_tool_status()):
            self.assertFalse(manager.connect({"address": "not-an-address"})["ok"])
            self.assertFalse(manager.connect({"address": "192.168.1.20"})["ok"])

    def test_connect_success_resolves_device(self):
        manager = manager_mod.MirrorManager(FakeConfig())
        with patch.object(manager_mod.tools, "tool_status", return_value=_tool_status()), \
                patch.object(manager_mod.tools, "run_tool",
                             return_value=(0, "already connected to 192.168.1.20:5555", "")):
            result = manager.connect({"address": "192.168.1.20:5555"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["address"], "192.168.1.20:5555")


if __name__ == "__main__":
    unittest.main()
