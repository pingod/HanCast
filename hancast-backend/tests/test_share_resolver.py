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
import time
import unittest
from unittest.mock import patch

import requests

from hancast_sidecar.media.server import (
    MediaHandler, _DISGUISE_SCAN, _DISGUISE_SCAN_IMAGE, _content_range_total,
    _disguise_offset, _disguise_peek_len, _disguised_ts,
    _is_leech_placeholder, _needs_bare_retry, _rewrite_playlist,
    player_ext_ok, player_safe_suffix,
)
from hancast_sidecar.media.share_resolver import (
    ShareResolver, extract_candidates, _EXT_MIME, _ascii_url, _ext_of,
    _master_variant_url, _nested_media_urls, _stream_candidates_from_text,
    UnplayableSource,
)


class FakeMediaServer:
    """只记录注册行为的 MediaServer 替身"""

    def __init__(self):
        self.proxies: list[str] = []

    def register_proxy(self, remote_url: str, headers: dict = None,
                       suffix: str = "") -> str:
        self.proxies.append(remote_url)
        return f"http://127.0.0.1:9/proxy/{len(self.proxies)}{suffix}"


def _no_follow(url, session, referer, deadline, _hop=0):
    """离线替身：不验证取流，按扩展名给 MIME"""
    return url, _EXT_MIME.get(_ext_of(url)) or "video/mp4"


def _no_publish(uri, mime, session, referer, deadline, via, extra_headers=None):
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


CJK_M3U8 = ("https://m3.example.com:907/hls7/"
            "%E7%94%9F%E5%8C%96%E5%8D%B1%E6%9C%BA%EF%BC%9A%E7%88%86%E5%8F%91%E5%A4%9C.m3u8")
# 采集站的真实形态：iframe → 播放器壳，壳把真流地址塞在 url= 里（4kcz / 站群模板）
SHELL = ("https://plaa.example.com:8181/player/py.php?code=cs&if=1&url="
         "https://m3.example.com:907/hls7/生化危机：爆发夜.m3u8")


class TestCollectSiteExtraction(unittest.TestCase):
    """采集站三坑：真流藏在壳地址的 query 里、文件名是中文、分片伪装成 .png"""

    def test_non_ascii_url_is_percent_encoded(self):
        self.assertEqual(_ascii_url("https://m3.example.com:907/hls7/生化危机：爆发夜.m3u8"),
                         CJK_M3U8)
        # 幂等：已经编码过的地址不再动
        self.assertEqual(_ascii_url(CJK_M3U8), CJK_M3U8)
        # 纯 ASCII 与相对路径原样保留
        self.assertEqual(_ascii_url("/api/m3u8?id=1"), "/api/m3u8?id=1")
        self.assertEqual(_ascii_url("https://a.example/x.mp4?t=1"),
                         "https://a.example/x.mp4?t=1")

    def test_nested_stream_url_is_unwrapped(self):
        self.assertEqual(_nested_media_urls(SHELL), [CJK_M3U8])
        # 二次编码的壳地址同样能解出来
        from urllib.parse import quote
        self.assertEqual(_nested_media_urls(
            "https://p.example/x.php?url=" + quote(CJK_M3U8, safe="")), [CJK_M3U8])
        # 普通地址不该被拆
        self.assertEqual(_nested_media_urls("https://a.example/x.mp4?t=1"), [])

    def test_page_scan_yields_unwrapped_normalized_stream(self):
        html = f'<html><body><iframe src="{SHELL}"></iframe></body></html>'
        cands = _stream_candidates_from_text(html)
        self.assertIn(CJK_M3U8, cands)
        # 壳排在前面（保持文档顺序），交给 _stream_rank 决定谁先验证
        self.assertLess(cands.index(CJK_M3U8), len(cands))

    def test_disguised_segment_extension_forces_local_proxy(self):
        """分片是 .png：字节能解，但播放器按后缀白名单拒绝拉 → 只能走代理"""
        resolver = ShareResolver(FakeMediaServer(), rules_dir=_isolated_rules_dir(self))
        playlist = b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttps://cdn.example/1.png\n"
        peeked: list[str] = []

        def fake_peek(url, deadline, use_range=True):
            peeked.append(url)
            return (playlist if url.endswith(".m3u8")
                    else b"\x89PNG\r\n\x1a\n", "text/html", 200)

        with patch.object(ShareResolver, "_peek", side_effect=fake_peek):
            self.assertFalse(resolver._tv_can_fetch(
                "https://m3.example.com:907/hls7/x.m3u8",
                "application/vnd.apple.mpegurl", time.monotonic() + 20))
        # 后缀不合规时分片根本不该被请求
        self.assertEqual(peeked, ["https://m3.example.com:907/hls7/x.m3u8"])

    def test_friendly_segment_extension_still_delivered_direct(self):
        resolver = ShareResolver(FakeMediaServer(), rules_dir=_isolated_rules_dir(self))
        playlist = b"#EXTM3U\n#EXTINF:10,\nhttps://cdn.example/1.ts\n"

        def fake_peek(url, deadline, use_range=True):
            return (playlist if url.endswith(".m3u8") else b"G\x47\x00\x10",
                    "video/mp2t", 200)

        with patch.object(ShareResolver, "_peek", side_effect=fake_peek):
            self.assertTrue(resolver._tv_can_fetch(
                "https://cdn.example/a.m3u8", "application/vnd.apple.mpegurl",
                time.monotonic() + 20))


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

        def fake_register(cls, remote_url, headers=None, suffix=""):
            self.registered.append((remote_url, headers, suffix))
            return f"/proxy/{len(self.registered):012x}{suffix}"

        self._reg = patch.object(MediaHandler, "register", classmethod(fake_register))
        self._reg.start()
        self.addCleanup(self._reg.stop)

    def test_playlist_children_registered(self):
        text = ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://k.example.com/k.bin"\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=1000000\n/api/m3u8?id=2&x=1\n'
                '#EXTINF:10,\n/api/ts?i=0\n')
        out = _rewrite_playlist(text, "https://box.example.com/api/m3u8?id=1",
                               {"Cookie": "sion_id=6ac4"})
        # 密钥/分片/子列表都要带一个播放器认账的后缀，否则 mpv 与电视按
        # 扩展名白名单直接把分片判死（"blocked for security reasons"）
        self.assertIn('URI="http://192.168.1.9:8080/proxy/000000000001.ts"', out)
        self.assertIn("http://192.168.1.9:8080/proxy/000000000002.m3u8", out)
        self.assertIn("http://192.168.1.9:8080/proxy/000000000003.ts", out)
        self.assertNotIn("/api/ts", out)
        self.assertEqual(
            [(u, s) for u, _, s in self.registered],
            [("https://k.example.com/k.bin", ".ts"),
             ("https://box.example.com/api/m3u8?id=2&x=1", ".m3u8"),
             ("https://box.example.com/api/ts?i=0", ".ts")])
        self.assertTrue(all(h == {"Cookie": "sion_id=6ac4"} for _, h, _ in self.registered))

    def test_player_suffix(self):
        """伪装成图片的 TS 分片：给代理路径换成 .ts，字节原样透传"""
        self.assertEqual(player_safe_suffix("https://f.cdn/x/y_1.png"), ".ts")
        self.assertEqual(player_safe_suffix("https://f.cdn/x/y_1.ts?k=a"), ".ts")
        self.assertEqual(player_safe_suffix("https://f.cdn/x/init.mp4"), ".mp4")
        self.assertEqual(player_safe_suffix("https://f.cdn/api/m3u8?id=1"), ".m3u8")
        self.assertEqual(player_safe_suffix("https://f.cdn/proxy/abcdef"), ".ts")
        self.assertTrue(player_ext_ok("https://f.cdn/a.mp4?x=1"))
        self.assertFalse(player_ext_ok("https://f.cdn/a.png"))
        self.assertFalse(player_ext_ok("https://f.cdn/m3u8"))

    def test_leech_placeholder_detection(self):
        self.assertTrue(_is_leech_placeholder(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(_is_leech_placeholder(b"\xff\xd8\xff\xe0"))
        self.assertTrue(_is_leech_placeholder(b"<!DOCTYPE html>"))
        self.assertFalse(_is_leech_placeholder(b"G\x00\x00\x00\x00"))
        self.assertFalse(_is_leech_placeholder(b"\x00\x00\x00 ftypisom"))

    def test_disguised_ts_is_not_a_placeholder(self):
        """假 PNG 头 + 188 对齐的 TS：是真分片，代理要剥头再转发"""
        stub = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"IHDR" + b"\x00" * 41
        head = stub + b"".join(b"\x47\x40\x11\x00" + b"\x00" * 184 for _ in range(8))
        self.assertEqual(len(stub), 61)
        self.assertTrue(_is_leech_placeholder(head[:6]))
        self.assertEqual(_disguise_offset(head), 61)
        self.assertTrue(_disguised_ts(head))
        # 真海报图：没有 TS 对齐，不许剥字节
        self.assertEqual(_disguise_offset(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096), 0)
        # 裸 TS（无伪装头）不动
        self.assertEqual(_disguise_offset(head[61:]), 0)
        # 首块太短判不出来就不动，宁可少剥也不要剥错
        self.assertEqual(_disguise_offset(head[:400]), 0)

    def test_disguise_offset_handles_long_skin(self):
        """低端影视（box.dyrs.com.de）的皮有 583 字节：窄窗口等于没修

        实测 1.7MB 的分片前 583 字节是合法 PNG（IHDR+IDAT），ffprobe 原样读它
        判成 png_pipe，mpv 直接 "no audio or video data played"。
        """
        ts = b"".join(b"\x47\x40\x11\x00" + b"\x00" * 184 for _ in range(12))
        for skin_len in (61, 112, 583, 653, 1900):
            stub = b"\x89PNG\r\n\x1a\n" + b"\x00" * (skin_len - 8)
            self.assertEqual(len(stub), skin_len)
            self.assertEqual(_disguise_offset(stub + ts), skin_len,
                             f"{skin_len} 字节的图片头要能扫出来")

    def test_disguise_peek_window_follows_upstream_image_type(self):
        """上游自称 image/* 就是「皮」的确认信号：判型窗口要跟着加深

        v2.2.20 的翻车方式是窗口只有 400，583 字节的皮扫不到 → 列表照转、分片全哑。
        窗口按 Content-Type 分档，既给皮留足空间，也不给普通分片凭空加首字节延迟。
        """
        pad = 12 + 6 * 188
        self.assertEqual(_disguise_peek_len("image/png"), _DISGUISE_SCAN_IMAGE + pad)
        self.assertEqual(_disguise_peek_len("Image/JPEG"), _DISGUISE_SCAN_IMAGE + pad)
        self.assertEqual(_disguise_peek_len("video/mp2t"), _DISGUISE_SCAN + pad)
        self.assertEqual(_disguise_peek_len(""), _DISGUISE_SCAN + pad)
        # 加深的窗口不是无限：皮长到 9KB 也要能扫出来，且不許把真海报图剥穿
        ts = b"".join(b"\x47\x40\x11\x00" + b"\x00" * 184 for _ in range(12))
        for skin_len in (2049, 4000, 9000):
            stub = b"\x89PNG\r\n\x1a\n" + b"\x00" * (skin_len - 8)
            head = (stub + ts)[:_disguise_peek_len("image/png")]
            self.assertEqual(_disguise_offset(head), skin_len,
                             f"{skin_len} 字节的皮要在 image 窗口内扫出来")
        poster = b"\x89PNG\r\n\x1a\n" + b"\x00" * _disguise_peek_len("image/png")
        self.assertEqual(_disguise_offset(poster), 0)

    def test_peek_upstream_accumulates_small_chunks(self):
        """分块编码下首块可能只有几十字节：peek 要累计到够判型的长度"""
        class Resp:
            @staticmethod
            def iter_content(chunk_size=1024):
                data = b"".join(b"\x47\x40\x11\x00" + b"\x00" * 184 for _ in range(8))
                for i in range(0, len(data), 40):
                    yield data[i:i + 40]

        head = MediaHandler._peek_upstream(Resp())
        self.assertGreaterEqual(len(head), 1024)

    def test_proxy_bare_retry_predicate(self):
        """页面域头可能就是被拒的原因，4xx 时该用裸 UA 再试一次"""
        self.assertTrue(_needs_bare_retry({"Referer": "https://a.example/p"}))
        self.assertTrue(_needs_bare_retry({"Cookie": "t=1", "User-Agent": "x"}))
        self.assertFalse(_needs_bare_retry({"User-Agent": "x"}))

    def test_proxy_strips_disguise_for_range_requests(self):
        """播放器一定带 Range：bytes=0-… 也要剥伪装头

        剥过头就不能再宣称「这是从 0 开始的区间」，ffmpeg 的 http.c 会报
        Unexpected offset 直接拒收，所以按整条资源（200）应答。
        """
        status, hdrs, body = self._drive_proxy("bytes=0-")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["Content-Type"], "video/mp2t")
        self.assertEqual(hdrs["Content-Length"], str(len(body)))
        self.assertNotIn("Content-Range", hdrs)
        self.assertEqual(body, self._DISGUISED_TS[61:])
        self.assertEqual(len(body), len(self._DISGUISED_TS) - 61)

    def test_proxy_strips_disguise_without_range(self):
        """电视不带 Range：照旧剥头，状态按上游 200 透传"""
        status, hdrs, body = self._drive_proxy(None)
        self.assertEqual(status, 200)
        self.assertEqual(body, self._DISGUISED_TS[61:])
        self.assertNotIn("Content-Range", hdrs)

    def test_proxy_leaves_mid_range_untouched(self):
        """拖拽后的分段请求从文件中间开始，那里没有伪装头，不许动字节"""
        status, hdrs, body = self._drive_proxy("bytes=4000-8000")
        self.assertEqual(status, 206)
        self.assertEqual(body, self._DISGUISED_TS)
        self.assertEqual(hdrs["Content-Type"], "image/png")

    def _drive_proxy(self, range_header):
        from io import BytesIO
        from hancast_sidecar.media import server as srv

        class Resp:
            url = "https://file.icve.com.cn/rc/a.png"

            def __init__(self, data):
                self.status_code = 206 if range_header else 200
                self.headers = {"Content-Type": "image/png",
                                "Content-Length": str(len(data))}
                if range_header:
                    self.headers["Content-Range"] = f"bytes 0-{len(data) - 1}/{len(data)}"
                    self.headers["Accept-Ranges"] = "bytes"
                self._parts = [data]

            def iter_content(self, chunk_size=1024):
                # 与真实流一致：peek 读过的字节不会在后续转发里重复出现
                while self._parts:
                    yield self._parts.pop(0)

            def close(self):
                pass

        sent: dict = {}
        h = MediaHandler.__new__(MediaHandler)
        h.path = "/proxy/abc.ts"
        h.headers = {} if range_header is None else {"Range": range_header}
        h.proxy_targets = {h.path: "https://file.icve.com.cn/rc/a.png"}
        h.proxy_headers = {h.path: {"Referer": "https://www.4kcz.com/v/1.html"}}
        h.wfile = BytesIO()
        h.send_response = lambda code: sent.__setitem__("status", code)
        h.send_header = lambda k, v: sent.setdefault("headers", {}).__setitem__(k, v)
        h.end_headers = lambda: None
        h.send_error = lambda code, msg=None: sent.__setitem__("status", code)

        with patch.object(srv, "upstream_get",
                          return_value=Resp(self._DISGUISED_TS)):
            h._handle_proxy()
        return sent["status"], sent.get("headers", {}), h.wfile.getvalue()

    _DISGUISED_TS = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"IHDR" + b"\x00" * 41
                     + b"".join(b"\x47\x40\x11\x00" + b"\x00" * 184 for _ in range(12)))

    def test_upstream_get_falls_back_to_direct(self):
        """系统代理端口一坏（127.0.0.1:9565 类），代理转发不该整段 502"""
        from hancast_sidecar.media import server as srv
        seen: list = []

        def fake_get(url, **kw):
            seen.append(kw.get("proxies"))
            if kw.get("proxies") is None:
                raise requests.exceptions.ProxyError("Cannot connect to proxy")
            return "ok"

        with patch.object(srv.http_requests, "get", side_effect=fake_get):
            self.assertEqual(srv.upstream_get("https://cdn.example/a.ts"), "ok")
        self.assertIsNone(seen[0])
        self.assertEqual(seen[1], {"http": None, "https": None})

    def test_upstream_get_does_not_retry_real_errors(self):
        """非代理故障只试一次：DNS/超时不该被伪装成「直连就能通」"""
        from hancast_sidecar.media import server as srv
        hits: list[int] = []

        def fake_get(url, **kw):
            hits.append(1)
            raise requests.exceptions.ConnectionError("name resolution failed")

        with patch.object(srv.http_requests, "get", side_effect=fake_get):
            with self.assertRaises(requests.exceptions.ConnectionError):
                srv.upstream_get("https://nope.example/a.ts")
        self.assertEqual(len(hits), 1)

    def test_content_range_total(self):
        self.assertEqual(_content_range_total("bytes 0-1023/409600"), 409600)
        self.assertIsNone(_content_range_total(None))


if __name__ == "__main__":
    unittest.main()
