"""get_params / save_params 命令往返（真实 CommandHandler + 临时配置目录）

覆盖：设置页拿到的登记表结构、保存后落盘内容与重启后重载一致、
校验失败时 ok=False 且合法项照常生效、保存不会冲掉其他设置项。
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar import params
from hancast_sidecar.commands import CommandHandler
from hancast_sidecar.utils import config as config_mod

KEY = "screencast.browser_segment_seconds"


class TestParamsCommands(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hancast-params-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.addCleanup(params.load_from, {})
        patcher = patch.object(config_mod, "SETTING_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.handler = CommandHandler()

    def _setting_file(self):
        return os.path.join(self.dir, "hancast_setting.json")

    def test_get_params_payload(self):
        res = self.handler.execute("get_params", {})
        specs = res["specs"]
        self.assertGreaterEqual(len(specs), 18)
        row = next(s for s in specs if s["key"] == KEY)
        self.assertEqual(row["value"], 1)
        self.assertFalse(row["overridden"])
        for spec in specs:
            self.assertTrue(spec["label_zh"] and spec["label_en"])

    def test_save_writes_only_overrides_and_survives_restart(self):
        res = self.handler.execute("save_params", {"values": {KEY: 3}})
        self.assertTrue(res["ok"])
        self.assertEqual(res["errors"], [])
        saved = json.load(open(self._setting_file(), encoding="utf-8"))
        self.assertEqual(saved[params.ADVANCED_KEY], {KEY: 3})
        self.assertEqual(params.get(KEY), 3)

        # 模拟重启：新 handler 从同一份配置重载
        params.load_from({})
        second = CommandHandler()
        try:
            self.assertEqual(params.get(KEY), 3)
            row = next(s for s in second.execute("get_params", {})["specs"]
                       if s["key"] == KEY)
            self.assertEqual(row["value"], 3)
            self.assertTrue(row["overridden"])
        finally:
            second.cleanup()

    def test_invalid_value_reports_error_and_keeps_valid_ones(self):
        res = self.handler.execute("save_params", {"values": {
            KEY: 99, "discovery.multicast_ttl": 4}})
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["errors"]), 1)
        self.assertEqual(params.get("discovery.multicast_ttl"), 4)
        self.assertEqual(params.get(KEY), 1)
        row = next(s for s in res["specs"] if s["key"] == "discovery.multicast_ttl")
        self.assertEqual(row["value"], 4)

    def test_reset_path_clears_override(self):
        self.handler.execute("save_params", {"values": {KEY: 5}})
        self.handler.execute("save_params", {"values": {KEY: 1}})
        saved = json.load(open(self._setting_file(), encoding="utf-8"))
        self.assertEqual(saved[params.ADVANCED_KEY], {})

    def test_saving_other_settings_keeps_advanced(self):
        self.handler.execute("save_params", {"values": {KEY: 4}})
        self.handler.execute("save_settings", {"settings": {"media_port": 8081}})
        saved = json.load(open(self._setting_file(), encoding="utf-8"))
        self.assertEqual(saved[params.ADVANCED_KEY], {KEY: 4})
        self.assertEqual(params.get(KEY), 4)

    def test_change_applies_to_next_capture_without_recompile(self):
        """读取时机是调用点：改完参数，下一次 start_hls 就用新分片时长"""
        from hancast_sidecar.screencast import capture as capture_mod
        self.handler.execute("save_params", {"values": {KEY: 6}})
        cap = capture_mod.FfmpegCapture("ffmpeg")
        captured = {}

        def fake_spawn(cmd, use_stdin=False):
            captured["cmd"] = cmd
            # 播放列表必须落在本次会话目录里，就绪判定才成立
            with open(os.path.join(out_dir, "live.m3u8"), "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
            proc = MagicMock()
            proc.poll.return_value = None
            proc.returncode = None
            proc.stderr = None
            return proc

        out_dir = tempfile.mkdtemp(prefix="hancast-hls-cmd-")
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        with patch.object(capture_mod.FfmpegCapture, "_spawn", side_effect=fake_spawn), \
                patch.object(capture_mod.FfmpegCapture, "_watch"):
            result = cap.start_hls({"type": "screen"}, {}, {
                "width": 1280, "height": 720, "fps": 15, "bitrate": "2M"},
                out_dir, target="browser")
        cap.stop()
        cmd = captured["cmd"]
        self.assertEqual(cmd[cmd.index("-hls_time") + 1], "6")
        self.assertEqual(cmd[cmd.index("-g") + 1], str(15 * 6))
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
