"""解析失败台账（media/failures.py）单测

契约：按域名分组、同址合并计数、成功即摘除、超期与超量淘汰、
设置页读的快照能删（按域名 / 按地址 / 清空），关掉开关就不再记录。
全部离线，写到临时目录。
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.media import failures
from hancast_sidecar import params


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hancast-failures-")
        self._real_path = failures.path
        failures.path = lambda: os.path.join(self.dir, failures.FILE_NAME)
        params.load_from({})

    def tearDown(self):
        failures.path = self._real_path
        params.load_from({})
        shutil.rmtree(self.dir, ignore_errors=True)


class TestRecording(LedgerTest):
    def test_grouped_by_host_and_case_insensitive_url(self):
        failures.record("https://m.ixigua.com/x/app/", "无播放地址")
        failures.record("https://m.ixigua.com/x/APP/", "无播放地址")
        failures.record("https://v.douyin.com/abc/", "接口 403")
        snap = failures.snapshot()
        self.assertEqual({h["host"] for h in snap["hosts"]},
                         {"m.ixigua.com", "v.douyin.com"})
        igx = next(h for h in snap["hosts"] if h["host"] == "m.ixigua.com")
        self.assertEqual(len(igx["entries"]), 1)
        self.assertEqual(igx["entries"][0]["count"], 2)

    def test_error_keeps_latest_reason(self):
        failures.record("https://a.com/v", "第一次的原因")
        failures.record("https://a.com/v", "最后一次的原因")
        entry = failures.snapshot()["hosts"][0]["entries"][0]
        self.assertEqual(entry["error"], "最后一次的原因")
        self.assertEqual(entry["count"], 2)

    def test_hostless_url_is_not_recorded(self):
        failures.record("BV1xx411c7mD", "不是链接")
        self.assertEqual(failures.snapshot()["hosts"], [])

    def test_disabled_switch_records_nothing(self):
        params.apply({"failures.enabled": False})
        failures.record("https://a.com/v", "x")
        self.assertEqual(failures.snapshot()["hosts"], [])
        self.assertFalse(failures.snapshot()["enabled"])

    def test_oversized_fields_are_clamped(self):
        failures.record("https://a.com/" + "u" * 900, "e" * 900)
        entry = failures.snapshot()["hosts"][0]["entries"][0]
        self.assertLessEqual(len(entry["url"]), 500)
        self.assertLessEqual(len(entry["error"]), 300)

    def test_per_host_cap_drops_oldest(self):
        params.apply({"failures.max_per_host": 5})
        for i in range(8):
            failures.record(f"https://a.com/v{i}", "x")
        self.assertEqual(len(failures.snapshot()["hosts"][0]["entries"]), 5)

    def test_retention_evicts_old_entries(self):
        params.apply({"failures.retention_days": 1})
        failures.record("https://a.com/fresh", "x")
        stale = "https://a.com/stale"
        failures.record(stale, "x")
        doc = json.loads(open(failures.path(), encoding="utf-8").read())
        for e in doc["hosts"]["a.com"]["entries"]:
            if e["url"] == stale:
                e["last_at"] = time.time() - 3 * 86400
        with open(failures.path(), "w", encoding="utf-8") as f:
            json.dump(doc, f)
        urls = [e["url"] for e in failures.snapshot()["hosts"][0]["entries"]]
        self.assertEqual(urls, ["https://a.com/fresh"])


class TestMutation(LedgerTest):
    def test_success_forgets_the_url(self):
        failures.record("https://a.com/v", "x")
        self.assertEqual(failures.forget("https://a.com/V"), 1)
        self.assertEqual(failures.snapshot()["hosts"], [])

    def test_forget_unknown_is_harmless(self):
        self.assertEqual(failures.forget("https://a.com/never"), 0)

    def test_delete_by_host_removes_whole_bucket(self):
        failures.record("https://a.com/1", "x")
        failures.record("https://a.com/2", "x")
        failures.record("https://b.com/1", "x")
        self.assertEqual(failures.delete(host="A.com"), 2)
        self.assertEqual([h["host"] for h in failures.snapshot()["hosts"]],
                         ["b.com"])

    def test_delete_by_url(self):
        failures.record("https://a.com/1", "x")
        failures.record("https://a.com/2", "x")
        self.assertEqual(failures.delete(url="https://a.com/1"), 1)
        self.assertEqual(len(failures.snapshot()["hosts"][0]["entries"]), 1)

    def test_delete_nothing_needs_no_args(self):
        self.assertEqual(failures.delete(), 0)

    def test_clear_removes_file_and_returns_count(self):
        failures.record("https://a.com/1", "x")
        failures.record("https://b.com/1", "x")
        self.assertEqual(failures.clear(), 2)
        self.assertFalse(os.path.exists(failures.path()))
        self.assertEqual(failures.snapshot()["hosts"], [])

    def test_broken_file_does_not_break_recording(self):
        with open(failures.path(), "w", encoding="utf-8") as f:
            f.write("{ not json")
        failures.record("https://a.com/1", "x")
        self.assertEqual(len(failures.snapshot()["hosts"]), 1)


class TestResolverHook(LedgerTest):
    """解析入口的记账行为：全失败才入账，成功要把这条摘掉"""

    def _resolver(self):
        from hancast_sidecar.media.share_resolver import ShareResolver

        resolver = ShareResolver.__new__(ShareResolver)
        resolver.media_server = object()
        resolver.rules_dir = self.dir
        resolver.plugin_mgr = type("P", (), {"refresh": lambda self: None})()
        return resolver

    def test_all_candidates_failed_are_recorded(self):
        resolver = self._resolver()
        resolver._load_external_rules = lambda: None
        resolver._resolve_url = lambda url, deadline, session, quality="": (
            _ for _ in ()).throw(ValueError("站点要登录"))
        with self.assertRaises(ValueError):
            resolver.resolve("看 https://weibo.com/tv/show/1001 这个")
        snap = failures.snapshot()
        self.assertEqual([h["host"] for h in snap["hosts"]], ["weibo.com"])
        self.assertEqual(snap["hosts"][0]["entries"][0]["error"], "站点要登录")

    def test_success_clears_the_ledger_entry(self):
        resolver = self._resolver()
        resolver._load_external_rules = lambda: None
        failures.record("https://weibo.com/tv/show/1001", "上一次也没解出来")
        resolver._resolve_url = lambda url, deadline, session, quality="": {"url": url}
        self.assertEqual(
            resolver.resolve("看 https://weibo.com/tv/show/1001 这个"),
            {"url": "https://weibo.com/tv/show/1001"})
        self.assertEqual(failures.snapshot()["hosts"], [])

    def test_mixed_candidates_do_not_record_the_noise(self):
        """口令文案里混的推广链接：有一条成功就不该留台账"""
        resolver = self._resolver()
        resolver._load_external_rules = lambda: None

        def resolve(url, deadline, session, quality=""):
            if "ad.com" in url:
                raise ValueError("不是视频站")
            return {"url": url}

        resolver._resolve_url = resolve
        resolver.resolve("https://ad.com/p https://weibo.com/tv/show/1001")
        self.assertEqual(failures.snapshot()["hosts"], [])


class TestCommands(unittest.TestCase):
    """三个命令走通：设置页拿到什么、删完还剩什么

    这里验的是命令表里的登记与出参形状——名字打错一个字母，界面上就是一片空白。
    """

    @classmethod
    def setUpClass(cls):
        from hancast_sidecar.commands import CommandHandler
        cls.handler = CommandHandler()

    @classmethod
    def tearDownClass(cls):
        cls.handler.cleanup()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hancast-failures-cmd-")
        self._real = failures.SETTING_DIR
        failures.SETTING_DIR = self.dir
        params.load_from({})

    def tearDown(self):
        failures.SETTING_DIR = self._real
        params.load_from({})
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_round_trip(self):
        self.assertEqual(self.handler.execute("failure_list", {})["hosts"], [])

        failures.record("https://a.com/1", "页面里没有可播放地址")
        failures.record("https://a.com/2", "接口 403")
        snap = self.handler.execute("failure_list", {})
        self.assertEqual([h["host"] for h in snap["hosts"]], ["a.com"])
        self.assertEqual(snap["total"], 2)
        self.assertEqual(snap["path"], failures.path())

        gone = self.handler.execute("failure_delete", {"host": "a.com", "url": ""})
        self.assertEqual(gone["removed"], 2)
        self.assertEqual(gone["hosts"], [])

        failures.record("https://b.com/1", "x")
        self.assertEqual(
            self.handler.execute("failure_delete", {"host": "", "url": "https://b.com/1"})["removed"],
            1)

        failures.record("https://c.com/1", "x")
        cleared = self.handler.execute("failure_clear", {})
        self.assertEqual(cleared["removed"], 1)
        self.assertEqual(cleared["hosts"], [])


if __name__ == "__main__":
    unittest.main()
