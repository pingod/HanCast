"""外置声明式规则：`output` 列表要逐个试，第一个**能播的**才交付

2026-11-15 审计：`DeclarativeRule.resolve()` 在循环第一次迭代就无条件 return，
于是 `output: ["{a}", "{b}"]` 里第一条被验证判死（HTTP 404 / 网页 / 图片）之后
仍然把死地址交出去，第二条永远不试。文档写的「逐个试，第一个能播的交付」
从未成立；`last_err` 也是死变量。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.media.rules.declarative import DeclarativeRule
from hancast_sidecar.media.share_resolver import UnplayableSource


class _Ctx:
    """只实现 resolve 用到的四个口，不碰网络/会话"""

    source_url = "https://site.test/page"

    def __init__(self, follow):
        self._follow = follow

    def follow(self, url, referer):
        return self._follow(url, referer)

    def publish(self, uri, mime, base, via, extra_headers=None):
        return uri, mime or "video/mp4", via

    def info(self, uri, mime, title, via, source_url=""):
        return {"uri": uri, "mime_type": mime, "via": via, "title": title}


def _rule(output):
    return DeclarativeRule({
        "name": "multi-output",
        "hosts": ["site.test"],
        "stage": "page",
        "output": output,
    })


class TestMultiOutputFallback(unittest.TestCase):
    def test_unplayable_first_candidate_falls_through_to_second(self):
        tried = []
        dead = "https://cdn.test/one.m3u8"
        good = "https://cdn.test/two.m3u8"

        def follow(url, referer):
            tried.append(url)
            if url == dead:
                raise UnplayableSource("HTTP 404")
            return url, "application/vnd.apple.mpegurl"

        out = _rule([dead, good]).resolve("https://site.test/page", "", _Ctx(follow))
        self.assertEqual(tried, [dead, good], "第一条判死之后必须去试第二条")
        self.assertEqual(out["uri"], good)
        self.assertEqual(out["mime_type"], "application/vnd.apple.mpegurl")

    def test_all_candidates_unplayable_is_an_error(self):
        def follow(url, referer):
            raise UnplayableSource("返回的是网页而非视频流")

        with self.assertRaises(ValueError) as cm:
            _rule(["https://cdn.test/a.mp4", "https://cdn.test/b.mp4"]).resolve(
                "https://site.test/page", "", _Ctx(follow))
        self.assertIn("候选地址均不可播", str(cm.exception))

    def test_network_failure_still_delivers_the_candidate_as_is(self):
        """网络层失败无法判定（电视端可能反而拉得通），保持旧行为：原样交付"""
        def follow(url, referer):
            raise ValueError("候选地址不可达: timed out")

        out = _rule(["https://cdn.test/only.mp4"]).resolve(
            "https://site.test/page", "", _Ctx(follow))
        self.assertEqual(out["uri"], "https://cdn.test/only.mp4")
        self.assertEqual(out["mime_type"], "video/mp4")


if __name__ == "__main__":
    unittest.main()
