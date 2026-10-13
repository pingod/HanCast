"""
应用内日志查看（utils/logger.read_logs / available_modules / set_level）单测

覆盖：行解析、多行堆栈归并、模块前缀过滤、级别与关键字过滤、limit 截断、
轮转文件拼接、尾部 seek 丢半行、未知模块补全。全部离线，只读临时文件。
"""

import os
import sys
import shutil
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.utils import logger as logger_mod

TS = "2026-10-12 10:00:0{idx},000"


def line(idx, module, level, msg):
    return f"[{TS.format(idx=idx % 10)}] {module} {level}: {msg}"


class TestReadLogs(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hancast-log-test-")
        self.log = os.path.join(self.dir, "hancast.log")
        starter = patch.object(logger_mod, "LOG_FILE", self.log)
        starter.start()
        self.addCleanup(starter.stop)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _write(self, *lines, path=None):
        with open(path or self.log, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def test_parse_fields(self):
        self._write(line(1, "hancast.ssdp", "INFO", "SSDP search sent"))
        page = logger_mod.read_logs()
        self.assertEqual(len(page["lines"]), 1)
        row = page["lines"][0]
        self.assertEqual(row["module"], "hancast.ssdp")
        self.assertEqual(row["level"], "INFO")
        self.assertEqual(row["msg"], "SSDP search sent")
        self.assertIn("SSDP search sent", row["raw"])
        self.assertEqual(page["path"], self.log)

    def test_module_prefix_filter(self):
        self._write(
            line(1, "hancast.screencast", "INFO", "start"),
            line(2, "hancast.screencast.httpd", "INFO", "httpd up"),
            line(3, "hancast.ssdp", "INFO", "search"),
        )
        page = logger_mod.read_logs(module="hancast.screencast")
        self.assertEqual([r["module"] for r in page["lines"]],
                         ["hancast.screencast", "hancast.screencast.httpd"])

    def test_level_filter_excludes_stack_of_other_levels(self):
        self._write(
            line(1, "hancast.ssdp", "INFO", "ok"),
            line(2, "hancast.media", "ERROR", "boom"),
            line(3, "hancast.media", "DEBUG", "detail"),
        )
        for lvl, expected in (("ERROR", 1), ("DEBUG", 1), ("WARNING", 0)):
            page = logger_mod.read_logs(level=lvl)
            self.assertEqual(len(page["lines"]), expected, lvl)

    def test_keyword_is_case_insensitive_over_whole_line(self):
        self._write(
            line(1, "hancast.ssdp", "INFO", "Search sent on en0"),
            line(2, "hancast.dlna", "WARNING", "Set AVTransport failed"),
        )
        self.assertEqual(len(logger_mod.read_logs(keyword="search")["lines"]), 1)
        self.assertEqual(len(logger_mod.read_logs(keyword="SSDP".lower())["lines"]), 1)

    def test_stack_trace_merges_into_previous_row(self):
        self._write(
            line(1, "hancast.commands", "ERROR", "command failed"),
            "Traceback (most recent call last):",
            '  File "main.py", line 10, in run',
            "ValueError: bad payload",
        )
        page = logger_mod.read_logs()
        self.assertEqual(len(page["lines"]), 1)
        self.assertIn("ValueError: bad payload", page["lines"][0]["msg"])
        self.assertIn("Traceback", page["lines"][0]["raw"])

    def test_leading_unmatched_line_is_dropped(self):
        self._write("leftover from a crash", line(1, "hancast.ssdp", "INFO", "ok"))
        page = logger_mod.read_logs()
        self.assertEqual(len(page["lines"]), 1)

    def test_limit_keeps_newest_and_marks_truncated(self):
        self._write(*[line(i, "hancast.ssdp", "INFO", f"evt {i}") for i in range(10)])
        page = logger_mod.read_logs(limit=4)
        self.assertEqual(page["total"], 10)
        self.assertTrue(page["truncated"])
        self.assertEqual([r["msg"] for r in page["lines"]],
                         ["evt 6", "evt 7", "evt 8", "evt 9"])

    def test_limit_is_clamped(self):
        self._write(line(1, "hancast.ssdp", "INFO", "ok"))
        self.assertGreaterEqual(len(logger_mod.read_logs(limit=0)["lines"]), 1)
        self.assertEqual(logger_mod.read_logs(limit=99999)["total"], 1)

    def test_rotated_files_are_optional_and_ascending(self):
        self._write(line(5, "hancast.ssdp", "INFO", "newer"))
        self._write(line(2, "hancast.ssdp", "INFO", "older"),
                    path=f"{self.log}.1")
        current_only = logger_mod.read_logs()
        self.assertEqual([r["msg"] for r in current_only["lines"]], ["newer"])

        both = logger_mod.read_logs(include_older=True)
        self.assertEqual([r["msg"] for r in both["lines"]], ["older", "newer"])

    def test_tail_seek_drops_partial_first_line(self):
        body = "\n".join(
            line(i, "hancast.ssdp", "INFO", "x" * 60) for i in range(20)
        )
        self._write(body)
        size = os.path.getsize(self.log)
        with patch.object(logger_mod, "_tail_bytes", lambda: size // 2):
            page = logger_mod.read_logs(limit=100)
        # 只读后半段：首行是残行，必须丢掉，其余完整
        self.assertTrue(all(not r["msg"].startswith("x" * 61) for r in page["lines"]))
        self.assertTrue(all("hancast.ssdp" in r["raw"] for r in page["lines"]))
        self.assertLess(page["total"], 20)

    def test_available_modules_adds_unknown_modules_once(self):
        self._write(
            line(1, "hancast.ssdp", "INFO", "ok"),
            line(2, "hancast.screencast.httpd", "INFO", "ok"),
            line(3, "hancast.screencast.httpd", "INFO", "ok"),
        )
        modules = logger_mod.available_modules()
        ids = [m["id"] for m in modules]
        self.assertIn("hancast.ssdp", ids)
        self.assertEqual(ids.count("hancast.screencast.httpd"), 1)
        entry = next(m for m in modules if m["id"] == "hancast.screencast.httpd")
        self.assertEqual(entry["label"], "hancast.screencast.httpd")
        known = next(m for m in modules if m["id"] == "hancast.ssdp")
        self.assertNotEqual(known["label"], "hancast.ssdp")

    def test_missing_log_file_returns_empty_page(self):
        page = logger_mod.read_logs()
        self.assertEqual(page["lines"], [])
        self.assertEqual(page["total"], 0)
        self.assertFalse(page["truncated"])


class TestLevelApi(unittest.TestCase):
    def setUp(self):
        self.original = logger_mod.get_level()

    def tearDown(self):
        logger_mod.set_level(self.original)

    def test_set_and_get_level(self):
        self.assertEqual(logger_mod.set_level("debug"), "DEBUG")
        self.assertEqual(logger_mod.get_level(), "DEBUG")
        self.assertEqual(logger_mod.set_level("INFO"), "INFO")

    def test_unknown_level_raises(self):
        with self.assertRaises(ValueError):
            logger_mod.set_level("VERBOSE")


if __name__ == "__main__":
    unittest.main()
