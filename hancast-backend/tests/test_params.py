"""后台可调参数登记表（params.py）单测

覆盖：默认值即契约、只存差异项、越界/类型错误只拒绝该项、
枚举与布尔的宽松入站、等于默认值即清除覆盖、specs() 给设置页的字段、
httpd 的播放器配置下限（hls.js 要求最大滞后严格大于同步分片数）。
全部离线，不碰真实配置文件。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar import params
from hancast_sidecar.screencast import httpd as httpd_mod

KEY = "screencast.browser_segment_seconds"


class TestDefaults(unittest.TestCase):
    def tearDown(self):
        params.load_from({})

    def test_browser_channel_defaults_to_1s_tv_2s(self):
        """本轮的契约：浏览器通道降到 1 秒分片，电视通道保持 2 秒"""
        self.assertEqual(params.get("screencast.browser_segment_seconds"), 1)
        self.assertEqual(params.get("screencast.dlna_segment_seconds"), 2)

    def test_every_spec_is_self_describing(self):
        """登记表即 UI 契约：设置页只靠这张表渲染，字段缺一个就是一块空白

        分组名不写死清单：v2.3.3 加 rules 段时这里的白名单没跟着改，
        契约测试当场变红（而设置页其实已经渲染了）。改成直接查前端两份语言文件
        里有没有 `settings.paramsGroup.<section>`——缺文案才是真会在界面上出问题的东西。
        """
        import json as _json
        locales = []
        for name in ("zh-CN", "en-US"):
            p = os.path.join(os.path.dirname(__file__), "..", "..",
                             "src", "locales", f"{name}.json")
            with open(p, encoding="utf-8") as f:
                locales.append(_json.load(f))
        # 与 SettingsView.vue 的分支保持一致：text / number / enum / bool
        rendered = ("text", "number", "enum", "bool")
        for spec in params.PARAM_SPECS:
            self.assertIn(spec["type"], rendered)
            self.assertTrue(spec["label_zh"] and spec["label_en"], spec["key"])
            key = f"settings.paramsGroup.{spec['section']}"
            for doc in locales:
                self.assertIn(key, doc, f"{spec['key']} 的分组 {key} 缺文案")
            if spec["type"] == "number":
                self.assertLessEqual(spec["min"], spec["default"])
                self.assertGreaterEqual(spec["max"], spec["default"])
            if spec["type"] == "enum":
                self.assertIn(spec["default"], spec["options"])

    def test_get_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            params.get("screencast.nope")


class TestApply(unittest.TestCase):
    def tearDown(self):
        params.load_from({})

    def test_only_diff_from_default_is_stored(self):
        params.apply({KEY: 1})
        self.assertEqual(params.snapshot(), {})
        params.apply({KEY: 3})
        self.assertEqual(params.snapshot(), {KEY: 3})
        self.assertTrue(params.specs_by_key(KEY)["overridden"])

    def test_out_of_range_rejects_only_that_item(self):
        errors = params.apply({KEY: 99, "screencast.playlist_size": 8})
        self.assertEqual(len(errors), 1)
        self.assertIn("不能大于 10", errors[0])
        self.assertEqual(params.get("screencast.playlist_size"), 8)
        self.assertEqual(params.get(KEY), 1)

    def test_bool_and_enum_tokens(self):
        params.apply({"player.low_latency_mode": "false",
                      "screencast.x264_preset": "ultrafast"})
        self.assertIs(params.get("player.low_latency_mode"), False)
        self.assertEqual(params.get("screencast.x264_preset"), "ultrafast")
        self.assertIn("只能取", params.apply(
            {"screencast.x264_preset": "placebo"})[0])
        self.assertIn("需要布尔值", params.apply(
            {"player.low_latency_mode": "maybe"})[0])

    def test_int_like_float_stays_int(self):
        """下游把这些值拼进 ffmpeg 命令行，出现 '1024.0' 就是坏参数"""
        params.apply({"screencast.muxing_queue_size": "1024.0"})
        value = params.get("screencast.muxing_queue_size")
        self.assertEqual(value, 1024)
        self.assertIsInstance(value, int)

    def test_unknown_key_rejected(self):
        self.assertIn("未知参数", params.apply({"x.y": 1})[0])

    def test_reset_to_default_drops_override(self):
        params.apply({KEY: 5})
        params.apply({KEY: 1})
        self.assertEqual(params.snapshot(), {})


class TestLoadFrom(unittest.TestCase):
    def tearDown(self):
        params.load_from({})

    def test_saved_overrides_reload_and_values_equal_default_are_dropped(self):
        params.load_from({params.ADVANCED_KEY: {KEY: 4, "discovery.ssdp_mx": 3}})
        self.assertEqual(params.snapshot(), {KEY: 4})

    def test_garbage_is_ignored_not_fatal(self):
        params.load_from({params.ADVANCED_KEY: {KEY: "abc", "nope": 1}})
        self.assertEqual(params.snapshot(), {})
        params.load_from({params.ADVANCED_KEY: "not-a-dict"})
        self.assertEqual(params.snapshot(), {})
        params.load_from({})


class TestSpecsPayload(unittest.TestCase):
    def tearDown(self):
        params.load_from({})

    def test_specs_expose_value_and_overridden(self):
        params.apply({"discovery.multicast_ttl": 4})
        row = params.specs_by_key("discovery.multicast_ttl")
        self.assertEqual(row["value"], 4)
        self.assertTrue(row["overridden"])
        untouched = params.specs_by_key("logs.tail_mb")
        self.assertEqual(untouched["value"], 2)
        self.assertFalse(untouched["overridden"])


class TestPlayerConfig(unittest.TestCase):
    def tearDown(self):
        params.load_from({})

    def test_max_latency_must_exceed_sync_count(self):
        """hls.js 在 liveMaxLatencyDurationCount <= liveSyncDurationCount 时直接抛错"""
        params.apply({"player.live_sync_duration_count": 6,
                      "player.live_max_latency_duration_count": 2})
        cfg = httpd_mod.player_config()
        self.assertGreater(cfg["liveMaxLatencyDurationCount"],
                           cfg["liveSyncDurationCount"])

    def test_defaults_enable_low_latency(self):
        cfg = httpd_mod.player_config()
        self.assertIs(cfg["lowLatencyMode"], True)
        self.assertEqual(cfg["liveSyncDurationCount"], 1)


if __name__ == "__main__":
    unittest.main()
