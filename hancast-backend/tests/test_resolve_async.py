"""
resolve_share 异步任务化测试 — 命令循环立即返回，后台线程出结果
"""

import time
import unittest

from hancast_sidecar.commands import CommandHandler


class SlowResolver:
    def __init__(self):
        self.calls = 0

    def resolve(self, text):
        self.calls += 1
        time.sleep(0.3)
        return {"media_type": "url", "uri": "https://cdn.example/v.mp4",
                "title": "ok", "mime_type": "video/mp4", "duration": None,
                "file_size": None, "thumbnail": None, "source_url": text,
                "via": "direct"}


class FailResolver:
    def resolve(self, text):
        time.sleep(0.1)
        raise ValueError("站点防抓取")


class TestResolveAsync(unittest.TestCase):

    def setUp(self):
        self.handler = CommandHandler()

    def tearDown(self):
        self.handler.cleanup()

    def _wait(self, task_id, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            entry = self.handler.execute("get_resolve_result", {"task_id": task_id})
            if entry["status"] != "pending":
                return entry
            time.sleep(0.05)
        self.fail("任务未在超时内完成")

    def test_command_loop_not_blocked(self):
        """resolve_share 运行中时，命令循环仍可执行其他命令"""
        self.handler.share_resolver = SlowResolver()
        start = self.handler.execute("resolve_share", {"text": "https://x/abc"})
        self.assertEqual(start["status"], "pending")
        self.assertTrue(start["task_id"])

        t0 = time.monotonic()
        devices = self.handler.execute("get_devices", {})  # 应立即返回而非等 0.3s
        self.assertIsInstance(devices, list)
        self.assertLess(time.monotonic() - t0, 0.2)

        entry = self._wait(start["task_id"])
        self.assertEqual(entry["status"], "done")
        self.assertEqual(entry["result"]["uri"], "https://cdn.example/v.mp4")

    def test_error_propagates(self):
        self.handler.share_resolver = FailResolver()
        start = self.handler.execute("resolve_share", {"text": "https://x/abc"})
        entry = self._wait(start["task_id"])
        self.assertEqual(entry["status"], "error")
        self.assertIn("站点防抓取", entry["error"])

    def test_empty_text_rejected_sync(self):
        with self.assertRaises(ValueError):
            self.handler.execute("resolve_share", {"text": "   "})

    def test_pending_cap(self):
        for i in range(3):
            self.handler._resolve_tasks[f"fake{i}"] = {"status": "pending",
                                                       "ts": time.time()}
        with self.assertRaises(ValueError):
            self.handler.execute("resolve_share", {"text": "https://x/abc"})

    def test_unknown_task_id(self):
        with self.assertRaises(ValueError):
            self.handler.execute("get_resolve_result", {"task_id": "nope"})


if __name__ == "__main__":
    unittest.main()
