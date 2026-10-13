"""
分享文本解析（share_resolver）+ 媒体代理改写（server）单元测试 — 全部离线，不触网

覆盖:
- 口令/分享文案中的地址提取（含短链裸域名、BV/av 兜底、尾部中文标点）
- 页面流地址提取：绝对内嵌流、引号内相对流接口（/api/m3u8?…）、JS 转义
- 通用页面直链提取（og:video / <video> / 内嵌 m3u8・mp4，相对地址归一）
- 主列表下钻（BANDWIDTH 最优）、播放列表粘贴路由
- 电视视角可播性判定（TS/HTML/PNG 魔数）与 Cookie 快照
- 代理侧播放列表改写、防盗链占位图识别、Content-Range 总量
- 站点规则：内置抖音/B站规则、外置声明式 JSON（page 与 url 阶段、失效回退）
- B站路由 → 本地代理注册
"""

import os
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

import requests

from hancast_sidecar.media.server import (
    MediaHandler, _content_range_total, _is_leech_placeholder, _rewrite_playlist,
)
from hancast_sidecar.media.share_resolver import (
    ShareResolver, extract_candidates, _EXT_MIME, _ext_of,
    _master_variant_url, _stream_candidates_from_text, UnplayableSource,
)


class FakeMediaServer:
    """只记录注册行为的 MediaServer 替身"""

    def __init__(self):
        self.proxies: list[str] = []

    def register_proxy(self, remote_url: str, headers: dict = None) -> str:
        self.proxies.append(remote_url)
        return f"http://127.0.0.1:9/proxy/{len(self.proxies)}"


def _no_follow(url, session, referer, deadline, _hop=0):
    """离线替身：不验证取流，按扩展名给 MIME"""
    return url, _EXT_MIME.get(_ext_of(url)) or "video/mp4"


def _no_publish(uri, mime, session, referer, deadline, via):
    """离线替身：跳过「电视不带 Cookie 能否拉到」的探测"""
    return uri, mime, via


def _isolated_rules_dir(test) -> str:
    """测试专用规则目录 —— 绝不读用户真实的 share_rules"""
    d = tempfile.mkdtemp(prefix="hancast-rules-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


class TestExtractCandidates(unittest.TestCase):
    def test_douyin_share_text(self):
        text = ('8.88 复制打开抖音，看看【某人的作品】 https://v.douyin.com/aB3xKp/ '
                '复制此链接，打开Dou音搜索，直接观看视频！')
        self.assertEqual(extract_candidates(text), ["https://v.douyin.com/aB3xKp/"])

    def test_bilibili_b23_short(self):
        text = '【官方MV】某视频 https://b23.tv/Rq1AbCd 我在B站看过了，你也一起来！'
        self.assertEqual(extract_candidates(text), ["https://b23.tv/Rq1AbCd"])

    def test_bare_short_host_gets_scheme(self):
        text = '看看这个 xhslink.com/m/AbCdEf 里面的内容'
        self.assertEqual(extract_candidates(text), ["https://xhslink.com/m/AbCdEf"])

    def test_bv_only_fallback(self):
        text = '这视频不错 BV1xx411c7mD 看看'
        cands = extract_candidates(text)
        self.assertEqual(cands, ["https://www.bilibili.com/video/BV1xx411c7mD/"])

    def test_av_only_fallback(self):
        cands = extract_candidates('av113 老视频')
        self.assertEqual(cands, ["https://www.bilibili.com/video/av113/"])

    def test_trailing_cjk_punct_stripped(self):
        text = '看看这个（https://example.org/a.m3u8）。'
        self.assertEqual(extract_candidates(text), ["https://example.org/a.m3u8"])

    def test_multiple_urls_kept_in_order(self):
        text = 'a https://example.com/1.png 和 https://cdn.example.net/main.m3u8?token=xyz'
        self.assertEqual(
            extract_candidates(text),
            ["https://example.com/1.png", "https://cdn.example.net/main.m3u8?token=xyz"])

    def test_no_url(self):
        self.assertEqual(extract_candidates('一段没有链接的中文文本'), [])

    def test_kuaishou_bare_short_hosts(self):
        self.assertEqual(
            extract_candidates('看快手这个视频 link.kuaishou.com/Ab1Cd 超精彩'),
            ["https://link.kuaishou.com/Ab1Cd"])
        self.assertEqual(
            extract_candidates('https://v.m.chenzhongtech.com/fw/photo/3xYz123 快手直链'),
            ["https://v.m.chenzhongtech.com/fw/photo/3xYz123"])

    def test_known_platform_candidate_preferred(self):
        """推广链接在前时，已知平台链接优先解析"""
        resolver = ShareResolver(FakeMediaServer(),
                                 rules_dir=_isolated_rules_dir(self))
        tried: list[str] = []

        def fake(url, deadline, session):
            tried.append(url)
            raise ValueError("stub fail to continue")

        with patch.object(ShareResolver, "_resolve_url", side_effect=fake):
            with self.assertRaises(ValueError):
                resolver.resolve('推广 https://ads.example.com/lp 快看 https://b23.tv/AbCdEf')
        self.assertEqual(tried[0], "https://b23.tv/AbCdEf")


PAGE_HTML = """<html><head>
<meta property="og:title" content="测试视频标题">
<meta property="og:image" content="/img/poster.jpg">
<meta property="og:video" content="/stream/main.m3u8?token=abc">
<title>忽略</title>
</head><body>
<video src="/legacy.mp4"></video>
<script>{"backup": "https:\\/\\/cdn.example.net\\/backup.mp4\\u0026t=1"}</script>
</body></html>"""


class TestResolveRouting(unittest.TestCase):
    def setUp(self):
        self.server = FakeMediaServer()
        self.resolver = ShareResolver(self.server, rules_dir=_isolated_rules_dir(self))
        self._offline = [
            patch.object(ShareResolver, "_follow_stream", side_effect=_no_follow),
            patch.object(ShareResolver, "_publish", side_effect=_no_publish),
        ]
        for p in self._offline:
            p.start()
            self.addCleanup(p.stop)

    def test_bilibili_routes_to_proxy(self):
        fake = {"title": "B站视频", "video_url": "https://xy.bilivideo.com/v.m4s",
                "audio_url": None, "cover_url": "https://i.hdslb.com/cover.jpg",
                "bvid": "BV1xx411c7mD", "author": "up", "duration": 100}
        with patch.object(self.resolver.bili, "belongs_to", return_value=True), \
             patch.object(self.resolver.bili, "resolve", return_value=fake):
            info = self.resolver.resolve("看看 https://b23.tv/AbCdEf 这个视频")
        self.assertEqual(info["via"], "bilibili")
        self.assertTrue(info["uri"].startswith("http://127.0.0.1:9/proxy/"))
        self.assertEqual(len(self.server.proxies), 2)  # 视频 + 封面
        self.assertEqual(info["source_url"], "https://b23.tv/AbCdEf")

    def test_page_extraction_priority_and_absolutize(self):
        with patch.object(ShareResolver, "_open",
                          return_value=("https://example.org/watch/1",
                                        "text/html; charset=utf-8", None,
                                        PAGE_HTML.encode())):
            info = self.resolver.resolve("https://example.org/watch/1")
        self.assertEqual(info["via"], "page")
        self.assertEqual(info["uri"], "https://example.org/stream/main.m3u8?token=abc")
        self.assertEqual(info["mime_type"], "application/vnd.apple.mpegurl")
        self.assertEqual(info["title"], "测试视频标题")
        self.assertEqual(info["thumbnail"], "https://example.org/img/poster.jpg")

    def test_page_without_stream_raises(self):
        with patch.object(ShareResolver, "_open",
                          return_value=("https://example.org/x", "text/html",
                                        None, b"<html><body>no video</body></html>")):
            with self.assertRaises(ValueError) as ctx:
                self.resolver.resolve("https://example.org/x")
        self.assertIn("未找到可播放的视频源", str(ctx.exception))

    def test_all_candidates_unplayable_raises(self):
        """验证明确失败的站点给出诚实错误，而不是丢一个黑屏地址"""
        def dead(url, session, referer, deadline, _hop=0):
            raise UnplayableSource("返回的是网页而非视频流")

        with patch.object(ShareResolver, "_follow_stream", side_effect=dead), \
             patch.object(ShareResolver, "_open",
                          return_value=("https://example.org/watch/1", "text/html",
                                        None, PAGE_HTML.encode())):
            with self.assertRaises(ValueError) as ctx:
                self.resolver.resolve("https://example.org/watch/1")
        self.assertIn("均不可播", str(ctx.exception))

    def test_direct_media_content_type(self):
        with patch.object(ShareResolver, "_open",
                          return_value=("https://cdn.example.net/big.mp4",
                                        "video/mp4", "12345", None)):
            info = self.resolver.resolve("https://short.example/nxY 看下")
        self.assertEqual(info["via"], "direct")
        self.assertEqual(info["uri"], "https://cdn.example.net/big.mp4")
        self.assertEqual(info["mime_type"], "video/mp4")
        self.assertEqual(info["file_size"], 12345)

    def test_pasted_master_playlist_descends_to_variant(self):
        """粘贴裸 m3u8：主列表要挑出最高码率的分片列表再交给电视"""
        body = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000\n/low.m3u8\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=2000000\n/api/m3u8?id=9\n").encode()
        with patch.object(ShareResolver, "_open",
                          return_value=("https://cdn.example.net/index.m3u8?k=1",
                                        "application/vnd.apple.mpegurl", None, body)):
            info = self.resolver.resolve("https://cdn.example.net/index.m3u8?k=1")
        self.assertEqual(info["via"], "playlist")
        self.assertEqual(info["uri"], "https://cdn.example.net/api/m3u8?id=9")
        self.assertEqual(info["mime_type"], "application/vnd.apple.mpegurl")

    def test_audio_mpegurl_playlist_is_not_treated_as_direct(self):
        """CDN 给 m3u8 回 audio/mpegurl 时也要走播放列表分支，把主列表选完流再交给电视"""
        body = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000\nlo.m3u8\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=3000000\nhi.m3u8\n").encode()
        with patch.object(ShareResolver, "_open",
                          return_value=("https://streams.example/x/index.m3u8",
                                        "audio/mpegurl", None, body)):
            info = self.resolver.resolve("https://streams.example/x/index.m3u8")
        self.assertEqual(info["via"], "playlist")
        self.assertEqual(info["uri"], "https://streams.example/x/hi.m3u8")

    def test_douyin_share_json(self):
        dy_html = (
            '{"video":{"play_addr":{"url_list":['
            '"https:\\u002F\\u002Fwww.douyin.com\\u002Faweme\\u002Fv1\\u002Fplaywm\\u002F?video_id=v0200f'
            '\\u0026ratio=1080p\\u0026line=0"}]}},"desc":"抖音标题"'
        ).encode()
        with patch.object(ShareResolver, "_open",
                          return_value=("https://www.douyin.com/video/7412345678901234567",
                                        "text/html", None, dy_html)):
            info = self.resolver.resolve("8.88 打开抖音 https://v.douyin.com/abc123/ 看视频")
        self.assertEqual(info["via"], "douyin")
        self.assertIn("https://www.douyin.com/aweme/v1/play/", info["uri"])
        self.assertNotIn("playwm", info["uri"])
        self.assertNotIn("u002F", info["uri"])
        self.assertEqual(info["title"], "抖音标题")

    def test_empty_text_raises(self):
        with self.assertRaises(ValueError):
            self.resolver.resolve("   ")

    def test_no_candidate_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.resolver.resolve("纯文本没有任何链接")
        self.assertIn("未从粘贴内容中找到链接", str(ctx.exception))


COLLECT_HTML = ('<html><head><title>采集站·第1集</title></head><body>'
                'var player_aaaa={"url":"/api/h.m3u8?id=7&t=abc"};'
                '</body></html>')


class TestSiteRules(unittest.TestCase):
    """站点规则：内置模块 + 外置声明式 JSON（share_rules/*.json），全程离线"""

    def setUp(self):
        from hancast_sidecar.media.rules.base import refresh_external
        self.rules_dir = _isolated_rules_dir(self)
        self.server = FakeMediaServer()
        self.resolver = ShareResolver(self.server, rules_dir=self.rules_dir)
        # 规则注册表是进程级全局状态，进出都要清空外置规则
        refresh_external(self.rules_dir)
        self.addCleanup(refresh_external, self.rules_dir)
        self._offline = [
            patch.object(ShareResolver, "_follow_stream", side_effect=_no_follow),
            patch.object(ShareResolver, "_publish", side_effect=_no_publish),
        ]
        for p in self._offline:
            p.start()
            self.addCleanup(p.stop)

    def _write(self, spec, name="rule"):
        with open(os.path.join(self.rules_dir, f"{name}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False)

    def test_builtin_rules_are_registered(self):
        from hancast_sidecar.media.rules.base import registered
        names = {r.name for r in registered()}
        self.assertTrue({"bilibili", "douyin"} <= names)

    def test_external_page_rule_resolves_collect_site(self):
        """采集站「正文里那坨 player_aaaa」由一条 JSON 规则描述，不用改代码"""
        self._write({
            "name": "collect", "hosts": ["box.example.net"], "path_regex": "^/play/",
            "steps": [
                {"regex": r"player_aaaa\s*=\s*(\{.*?\})", "as": "cfg"},
                {"json": "url", "from": "cfg", "as": "play"},
            ],
            "output": "{play}",
        })
        with patch.object(ShareResolver, "_open",
                          return_value=("https://box.example.net/play/1.html",
                                        "text/html", None, COLLECT_HTML.encode())):
            info = self.resolver.resolve("https://box.example.net/play/1.html")
        self.assertEqual(info["via"], "collect")
        self.assertEqual(info["uri"], "https://box.example.net/api/h.m3u8?id=7&t=abc")
        self.assertEqual(info["mime_type"], "application/vnd.apple.mpegurl")
        self.assertEqual(info["title"], "采集站·第1集")

    def test_external_rule_without_output_scans_body(self):
        self._write({"name": "scan", "hosts": ["scan.example"]})
        with patch.object(ShareResolver, "_open",
                          return_value=("https://scan.example/watch", "text/html",
                                        None,
                                        b'<script>{"u":"https:\\/\\/cdn.scan.example'
                                        b'\\/a.m3u8\\u0026t=1"}</script>')):
            info = self.resolver.resolve("https://scan.example/watch")
        self.assertEqual(info["via"], "scan")
        self.assertEqual(info["uri"], "https://cdn.scan.example/a.m3u8&t=1")

    def test_external_page_rule_overrides_builtin(self):
        """外置规则 priority 更低：同一站点先按用户的取法，失败才回退内置"""
        self._write({"name": "dy-custom", "hosts": ["douyin.com"],
                     "output": "https://custom.example/v.mp4"})
        with patch.object(ShareResolver, "_open",
                          return_value=("https://www.douyin.com/video/74123456789",
                                        "text/html", None, b"<html></html>")):
            info = self.resolver.resolve("https://v.douyin.com/abc/")
        self.assertEqual(info["via"], "dy-custom")
        self.assertEqual(info["uri"], "https://custom.example/v.mp4")

    def test_url_stage_rule_runs_without_fetching_page(self):
        self._write({"name": "api-first", "hosts": ["vod.example.net"],
                     "stage": "url", "output": "https://cdn.example.net/{host}/x.m3u8"})

        def no_fetch(*_a, **_k):
            self.fail("url 阶段规则命中后不应再抓页面")

        with patch.object(ShareResolver, "_open", side_effect=no_fetch):
            info = self.resolver.resolve("https://vod.example.net/play/1")
        self.assertEqual(info["via"], "api-first")
        self.assertEqual(info["uri"],
                         "https://cdn.example.net/vod.example.net/x.m3u8")

    def test_rule_failure_falls_back_to_generic_extraction(self):
        """一条不适用/写坏的规则只能让路，绝不能把本来成功的粘贴判成失败"""
        self._write({"name": "wrong", "hosts": ["example.org"],
                     "steps": [{"regex": "player_aaaa", "as": "x"}]})
        with patch.object(ShareResolver, "_open",
                          return_value=("https://example.org/watch/1",
                                        "text/html; charset=utf-8", None,
                                        PAGE_HTML.encode())):
            info = self.resolver.resolve("https://example.org/watch/1")
        self.assertEqual(info["via"], "page")
        self.assertEqual(info["uri"], "https://example.org/stream/main.m3u8?token=abc")

    def test_broken_rule_file_only_skips_itself(self):
        with open(os.path.join(self.rules_dir, "broken.json"), "w",
                  encoding="utf-8") as f:
            f.write("{ this is not json")
        self._write({"name": "good", "hosts": ["good.example"],
                     "output": "https://good.example/a.m3u8"}, name="good")
        with patch.object(ShareResolver, "_open",
                          return_value=("https://good.example/p", "text/html",
                                        None, b"<html></html>")):
            info = self.resolver.resolve("https://good.example/p")
        self.assertEqual(info["via"], "good")

    def test_disabled_rule_is_not_loaded(self):
        self._write({"name": "off", "hosts": ["off.example"], "enabled": False,
                     "output": "https://off.example/a.m3u8"})
        with patch.object(ShareResolver, "_open",
                          return_value=("https://off.example/p", "text/html",
                                        None, PAGE_HTML.encode())):
            info = self.resolver.resolve("https://off.example/p")
        self.assertEqual(info["via"], "page")   # 规则让路，通用提取接手


class TestStreamExtraction(unittest.TestCase):
    def test_quoted_relative_stream_api(self):
        """引号内的相对流接口才是「播放地址由接口下发」站点的常态"""
        html = 'var u="/api/m3u8?origin=%E8%B6%85&url=6aa5&sion_id=6ac4";'
        self.assertEqual(
            _stream_candidates_from_text(html),
            ["/api/m3u8?origin=%E8%B6%85&url=6aa5&sion_id=6ac4"])

    def test_js_identifiers_are_not_candidates(self):
        html = 'var f="bfzym3u8", g="m3u8Tab", h="dytt.m3u8s";'
        self.assertEqual(_stream_candidates_from_text(html), [])

    def test_template_placeholder_rejected(self):
        html = 'var t="${player.url}.m3u8";'
        self.assertEqual(_stream_candidates_from_text(html), [])

    def test_json_escape_normalized(self):
        html = r'{"a": "https:\/\/cdn.x.com\/v.m3u8\u0026t=1"}'
        self.assertEqual(_stream_candidates_from_text(html),
                         ["https://cdn.x.com/v.m3u8&t=1"])

    def test_master_variant_picks_highest_bandwidth(self):
        text = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000\n/api/direct?id=1\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=1000000\n/api/m3u8?id=1\n")
        self.assertEqual(
            _master_variant_url(text, "https://h/api/m3u8?a=1"),
            "https://h/api/m3u8?id=1")

    def test_media_playlist_is_not_master(self):
        self.assertIsNone(_master_variant_url(
            "#EXTM3U\n#EXTINF:10,\n/api/ts?i=0\n", "https://h/a.m3u8"))

    def test_stream_rank_prefers_hls_then_avoids_poster(self):
        rank = ShareResolver._stream_rank
        urls = ["https://h/img.jpg", "https://h/a.mp4", "/api/m3u8?id=1", "https://h/a.mkv"]
        self.assertEqual(sorted(urls, key=rank)[0], "/api/m3u8?id=1")
        self.assertEqual(sorted(urls, key=rank)[-1], "https://h/img.jpg")


class TestPlayableSniff(unittest.TestCase):
    TS = b"G" + b"\x00" * 187 + b"G" + b"\x10" * 300

    def test_media_ok(self):
        ok = ShareResolver._media_ok
        self.assertTrue(ok(200, "video/mp4", b""))
        self.assertTrue(ok(200, "application/octet-stream", self.TS))
        self.assertTrue(ok(200, "text/html", self.TS))     # 有站点给 TS 回 text/html
        self.assertTrue(ok(200, "text/plain", b"#EXTM3U\n"))
        self.assertFalse(ok(200, "image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40))
        self.assertFalse(ok(200, "text/html", b"<html><body>x</body></html>" + b"\x00" * 40))
        self.assertFalse(ok(403, "video/mp4", b""))

    def test_snapshot_headers_keeps_page_cookie_for_media_host(self):
        """Cookie 由页面域下发、流媒体主机校验，跨主机也必须带上"""
        jar = requests.cookies.RequestsCookieJar()
        jar.set("sion_id", "6ac4", domain=".dyrsxm.cc")
        jar.set("other", "x", domain=".adsystem.com")
        h = ShareResolver._snapshot_headers(
            type("S", (), {"cookies": jar})(),
            "https://global.dyrsxm.cc/movie/1.html",
            "https://box.dyrs.com.de/api/m3u8?id=1")
        self.assertEqual(h["Cookie"], "sion_id=6ac4")
        self.assertEqual(h["Referer"], "https://global.dyrsxm.cc/movie/1.html")
        self.assertEqual(h["Origin"], "https://global.dyrsxm.cc")


class TestMediaProxyRewrite(unittest.TestCase):
    def setUp(self):
        self._base = patch.object(MediaHandler, "base_url", "http://192.168.1.9:8080")
        self._base.start()
        self.addCleanup(self._base.stop)
        self.registered: list[tuple[str, dict]] = []

        def fake_register(cls, remote_url, headers=None):
            self.registered.append((remote_url, headers))
            return f"/proxy/{len(self.registered):012x}"

        self._reg = patch.object(MediaHandler, "register", classmethod(fake_register))
        self._reg.start()
        self.addCleanup(self._reg.stop)

    def test_playlist_children_registered(self):
        text = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://k.example.com/k.bin"\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=1000000\n/api/m3u8?id=2&x=1\n'
                '#EXTINF:10,\n/api/ts?i=0\n')
        out = _rewrite_playlist(text, "https://box.example.com/api/m3u8?id=1",
                               {"Cookie": "sion_id=6ac4"})
        self.assertIn('URI="http://192.168.1.9:8080/proxy/000000000001"', out)
        self.assertIn("http://192.168.1.9:8080/proxy/000000000003", out)
        self.assertNotIn("/api/ts", out)
        self.assertEqual(
            [u for u, _ in self.registered],
            ["https://k.example.com/k.bin", "https://box.example.com/api/m3u8?id=2&x=1",
             "https://box.example.com/api/ts?i=0"])
        self.assertTrue(all(h == {"Cookie": "sion_id=6ac4"} for _, h in self.registered))

    def test_leech_placeholder_detection(self):
        self.assertTrue(_is_leech_placeholder(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(_is_leech_placeholder(b"\xff\xd8\xff\xe0"))
        self.assertTrue(_is_leech_placeholder(b"<!DOCTYPE html>"))
        self.assertFalse(_is_leech_placeholder(b"G\x00\x00\x00\x00"))
        self.assertFalse(_is_leech_placeholder(b"\x00\x00\x00 ftypisom"))

    def test_content_range_total(self):
        self.assertEqual(_content_range_total("bytes 0-1023/409600"), 409600)
        self.assertIsNone(_content_range_total(None))


if __name__ == "__main__":
    unittest.main()
