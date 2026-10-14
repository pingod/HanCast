"""多画质契约（默认最高 + 投屏中按档位重解）单测

契约：
- 规则包能回报档位表 [{id,label}] 与「本次实际交付」的档位；
- 客户端点名的 quality 原样送进沙箱 ctx.quality，包按它取流；
- 档位表只做形状清洗：脏数据不影响交付，也不落任何会过期的直链；
- 命令层 resolve_share 带 quality，透传给 share_resolver.resolve(text, quality)。
全部离线，写到临时目录。
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hancast_sidecar.media.rules.jsruntime as jsruntime_mod
from hancast_sidecar.media import failures
from hancast_sidecar.media.rules.jsplugins import _clean_qualities
from hancast_sidecar.media.share_resolver import ShareResolver, _EXT_MIME, _ext_of
from hancast_sidecar.models.media import MediaInfo


def _tmp_dir(test) -> str:
    d = tempfile.mkdtemp(prefix="hancast-quality-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


class TestCleanQualities(unittest.TestCase):
    def test_strings_dicts_and_junk(self):
        out = _clean_qualities({"qualities": [
            "1080P", {"id": "64", "label": "720P"}, 12, {"label": "没 id"},
            {"id": "  "}, None]})
        self.assertEqual(out, [{"id": "1080P", "label": "1080P"},
                               {"id": "64", "label": "720P"}])

    def test_missing_label_falls_back_to_id(self):
        self.assertEqual(_clean_qualities({"qualities": [{"id": "32"}]}),
                         [{"id": "32", "label": "32"}])

    def test_not_a_list_is_no_qualities(self):
        for bad in (None, "80", {"id": "80"}, 5):
            self.assertEqual(_clean_qualities({"qualities": bad}), [])

    def test_capped_and_clamped_length(self):
        items = [{"id": f"q{i}" + "x" * 40, "label": "L" * 40} for i in range(30)]
        out = _clean_qualities({"qualities": items})
        self.assertEqual(len(out), 12)          # 最多 12 档
        self.assertTrue(all(len(q["id"]) <= 24 for q in out))
        self.assertTrue(all(len(q["label"]) <= 24 for q in out))


class TestMediaInfoFields(unittest.TestCase):
    def test_defaults_are_empty_not_none(self):
        d = MediaInfo(media_type="url", uri="https://a.b/c.mp4", title="t").to_dict()
        self.assertEqual(d["qualities"], [])
        self.assertEqual(d["quality"], "")

    def test_carries_table_and_delivered(self):
        d = MediaInfo(media_type="url", uri="https://a.b/c.mp4", title="t",
                      qualities=[{"id": "80", "label": "1080P"}],
                      quality="80").to_dict()
        self.assertEqual(d["qualities"][0]["label"], "1080P")
        self.assertEqual(d["quality"], "80")


# ── 走完整解析管线：包回报的档位表能否原样到达命令出参 ──

class FakeMediaServer:
    def __init__(self):
        self.proxies: list[str] = []

    def register_proxy(self, remote_url, headers=None, suffix=""):
        self.proxies.append(remote_url)
        return f"http://127.0.0.1:9/proxy/{len(self.proxies)}{suffix}"


def make_zip(name, js, manifest=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest or {
            "name": name, "version": "1.0.0", "author": "t",
            "description": "d", "scripts": ["site.js"],
        }))
        zf.writestr("site.js", js)
    return buf.getvalue()


QUAL_JS = """
registerRule({
  id: 'q-rule', name: 'q', hosts: ['demo.example'], stage: 'url',
  resolve: function (c) {
    var q = c.quality || '80';
    return { url: 'https://cdn.example/v/' + q + '.mp4', title: 'Q' + q,
             qualities: [{id: '80', label: '1080P'}, '64', 12],
             quality: q };
  }
});
"""


class TestQualityThroughResolver(unittest.TestCase):
    def setUp(self):
        self.resolver = ShareResolver(FakeMediaServer(),
                                      rules_dir=_tmp_dir(self))
        self.resolver.plugin_mgr.import_zip_bytes(make_zip("qdemo", QUAL_JS))
        # 解析成功要摘台账：把它挪到临时目录，绝不碰用户真实的台账文件
        self._real_ledger = failures.path
        failures.path = lambda: os.path.join(_tmp_dir(self), failures.FILE_NAME)

    def tearDown(self):
        failures.path = self._real_ledger

    def _resolve(self, quality=""):
        with patch.object(ShareResolver, "_follow_stream",
                          side_effect=lambda u, s, r, d:
                              (u, _EXT_MIME.get(_ext_of(u)) or "video/mp4")), \
             patch.object(ShareResolver, "_publish",
                          side_effect=lambda u, m, s, r, d, v, **kw: (u, m, v)):
            return self.resolver.resolve(
                "https://demo.example/play/1", quality)

    def test_default_when_client_does_not_pick(self):
        out = self._resolve()
        self.assertEqual(out["uri"], "https://cdn.example/v/80.mp4")
        self.assertEqual(out["quality"], "80")
        self.assertEqual(out["qualities"],
                         [{"id": "80", "label": "1080P"},
                          {"id": "64", "label": "64"}])

    def test_named_quality_reaches_the_pack(self):
        out = self._resolve("64")
        self.assertEqual(out["uri"], "https://cdn.example/v/64.mp4")
        self.assertEqual(out["quality"], "64")

    def test_table_is_a_key_not_a_cached_url(self):
        """档位表里不许出现任何地址：切换靠重新解析，直链会过期"""
        for q in self._resolve()["qualities"]:
            self.assertNotIn("http", q["id"] + q["label"])


# ── 官方 B 站包对着「假 B 站」跑：qn 选档 + accept_quality 回报 ──

class FakeJsonResponse:
    def __init__(self, doc, url="https://api.bilibili.com/x/mock"):
        self._doc = doc
        self.status_code = 200
        self.url = url
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._doc

    def iter_content(self, chunk_size=65536):
        yield json.dumps(self._doc).encode()

    def close(self):
        pass


class FakeBilibili:
    """playurl 按 qn 给档，档位不在 accept_quality 里就降级到能给的最高档"""

    def __init__(self, accept=(16, 32, 64, 80), app_lines_broken=False):
        self.accept = list(accept)
        self.app_lines_broken = app_lines_broken
        self.calls: list[str] = []

    def new_session(self):
        return self

    def request(self, method, url, **kw):
        self.calls.append(url)
        if "/x/frontend/finger/spi" in url:
            return FakeJsonResponse(
                {"code": 0, "data": {"b_3": "BUVID3", "b_4": "BUVID4"}}, url)
        if "/x/web-interface/view" in url:
            return FakeJsonResponse({"code": 0, "data": {
                "cid": "9527", "title": "B 站标题",
                "pic": "http://i0.hdslb.com/bfs/cover.jpg"}}, url)
        if "/x/player/playurl" in url:
            html5 = "platform=html5" in url
            if self.app_lines_broken and not html5:
                return FakeJsonResponse({"code": -400, "message": "请求错误"}, url)
            qn = int(re.search(r"[?&]qn=(\d+)", url).group(1))
            served = max((q for q in self.accept if q <= qn),
                         default=min(self.accept))
            return FakeJsonResponse({"code": 0, "data": {
                "quality": served, "accept_quality": self.accept,
                "durl": [{"url": f"https://cdn.bilivideo.com/v/{served}.mp4",
                          "size": 1}]}}, url)
        raise AssertionError(f"用例未桩掉该地址（保持离线）: {url}")


class TestShippedBilibiliPack(unittest.TestCase):
    PACK_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "..", "rule-packs", "hancast-bilibili", "main.js")

    def _resolve(self, net, quality=None):
        with open(self.PACK_JS, encoding="utf-8") as f:
            js = f.read()
        rt = jsruntime_mod.JsRuntime(
            "hancast-bilibili", [js], prefs={},
            on_prefs_change=lambda: None, cache_dir=_tmp_dir(self))
        payload = {"url": "https://www.bilibili.com/video/BV1xx411c7mD/",
                   "body": "", "base": "https://www.bilibili.com/"}
        if quality is not None:
            payload["quality"] = quality
        return rt.call_rule(0, payload, time.monotonic() + 30, net)

    def test_defaults_to_highest_and_reports_table(self):
        net = FakeBilibili()
        out = self._resolve(net)
        self.assertIn("qn=80", net.calls[-1])
        self.assertEqual(out["url"], "https://cdn.bilivideo.com/v/80.mp4")
        self.assertEqual(out["quality"], "80")
        self.assertEqual([q["id"] for q in out["qualities"]],
                         ["80", "64", "32", "16"])          # 从高到低
        self.assertEqual(out["qualities"][0]["label"], "1080P 高清")
        self.assertEqual(out["title"], "B 站标题")

    def test_requested_tier_is_what_gets_asked(self):
        net = FakeBilibili()
        out = self._resolve(net, quality="32")
        self.assertIn("qn=32", net.calls[-1])
        self.assertEqual((out["quality"], out["url"]),
                         ("32", "https://cdn.bilivideo.com/v/32.mp4"))

    def test_downgrade_is_reported_honestly(self):
        """点了 127（4K）但线路只给到 80：回报实际交付档，界面不会显示一个假的 4K"""
        net = FakeBilibili(accept=(16, 32, 64, 80))
        out = self._resolve(net, quality="127")
        self.assertEqual(out["quality"], "80")
        self.assertNotIn("127", [q["id"] for q in out["qualities"]])

    def test_unknown_tier_cannot_reach_the_query(self):
        """档位 id 只认白名单：乱七八糟的输入不会拼进签名 query"""
        net = FakeBilibili()
        out = self._resolve(net, quality="80&cid=9&bvid=BV0evil")
        self.assertIn("qn=80", net.calls[-1])
        self.assertNotIn("evil", net.calls[-1])
        self.assertEqual(out["quality"], "80")

    def test_html5_fallback_reports_table(self):
        net = FakeBilibili(app_lines_broken=True)
        out = self._resolve(net, quality="64")
        self.assertTrue(any("platform=html5" in c for c in net.calls))
        self.assertEqual(out["url"], "https://cdn.bilivideo.com/v/64.mp4")
        self.assertEqual(len(out["qualities"]), 4)


# ── 命令层：quality 参数能否穿过任务表到达解析器 ──

class TestCommandThreading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from hancast_sidecar.commands import CommandHandler
        cls.handler = CommandHandler()

    @classmethod
    def tearDownClass(cls):
        cls.handler.cleanup()

    def test_resolve_share_passes_quality_to_task(self):
        seen = {}

        def spawn(text, quality=""):
            seen["text"], seen["quality"] = text, quality
            return {"task_id": "t1", "status": "pending"}

        with patch.object(type(self.handler), "_spawn_resolve_task",
                          side_effect=spawn):
            out = self.handler.execute("resolve_share",
                                        {"text": "口令 https://a.b/c", "quality": "64"})
        self.assertEqual(out["task_id"], "t1")
        self.assertEqual(seen, {"text": "口令 https://a.b/c", "quality": "64"})

    def test_omitted_quality_is_empty_string(self):
        with patch.object(type(self.handler), "_spawn_resolve_task",
                          side_effect=lambda text, quality="": {"task_id": "t", "q": quality}):
            out = self.handler.execute("resolve_share", {"text": "https://a.b/c"})
        self.assertEqual(out["q"], "")

    def test_task_runs_resolver_with_quality(self):
        recorded = {}

        class Resolver:
            def resolve(self, text, quality=""):
                recorded["args"] = (text, quality)
                return {"uri": "https://cdn/x.mp4", "quality": quality}

        real = self.handler.share_resolver
        self.handler.share_resolver = Resolver()
        try:
            task_id = self.handler._spawn_resolve_task("https://a.b/c", "32")["task_id"]
            for _ in range(50):
                entry = self.handler.execute("get_resolve_result", {"task_id": task_id})
                if entry["status"] != "pending":
                    break
                time.sleep(0.05)
        finally:
            self.handler.share_resolver = real
        self.assertEqual(entry["status"], "done")
        self.assertEqual(entry["result"]["quality"], "32")
        self.assertEqual(recorded["args"], ("https://a.b/c", "32"))


if __name__ == "__main__":
    unittest.main()
