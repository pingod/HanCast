"""
分享文本解析（share_resolver）单元测试 — 全部离线，不触网

覆盖:
- 口令/分享文案中的地址提取（含短链裸域名、BV/av 兜底、尾部中文标点）
- 通用页面直链提取（og:video / <video> / 内嵌 m3u8・mp4，相对地址归一）
- 抖音分享页 JSON 提取
- B站路由 → 本地代理注册
"""

import unittest
from unittest.mock import patch

from hancast_sidecar.media.share_resolver import (
    ShareResolver, extract_candidates,
)


class FakeMediaServer:
    """只记录注册行为的 MediaServer 替身"""

    def __init__(self):
        self.proxies: list[str] = []

    def register_proxy(self, remote_url: str) -> str:
        self.proxies.append(remote_url)
        return f"http://127.0.0.1:9/proxy/{len(self.proxies)}"


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
        resolver = ShareResolver(FakeMediaServer())
        tried: list[str] = []

        def fake(url, deadline):
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
        self.resolver = ShareResolver(self.server)

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

    def test_direct_media_content_type(self):
        with patch.object(ShareResolver, "_open",
                          return_value=("https://cdn.example.net/big.mp4",
                                        "video/mp4", "12345", None)):
            info = self.resolver.resolve("https://short.example/nxY 看下")
        self.assertEqual(info["via"], "direct")
        self.assertEqual(info["uri"], "https://cdn.example.net/big.mp4")
        self.assertEqual(info["mime_type"], "video/mp4")
        self.assertEqual(info["file_size"], 12345)

    def test_douyin_share_json(self):
        dy_html = (
            '{"video":{"play_addr":{"url_list":['
            '"https://www.douyin.com/aweme/v1/playwm/?video_id=v0200f&ratio=1080p'
            '\\u0026line=0"}]}},"desc":"抖音标题"'
        ).encode()
        with patch.object(ShareResolver, "_open",
                          return_value=("https://www.douyin.com/video/7412345678901234567",
                                        "text/html", None, dy_html)):
            info = self.resolver.resolve("8.88 打开抖音 https://v.douyin.com/abc123/ 看视频")
        self.assertEqual(info["via"], "douyin")
        self.assertIn("https://www.douyin.com/aweme/v1/play/", info["uri"])
        self.assertNotIn("playwm", info["uri"])
        self.assertEqual(info["title"], "抖音标题")

    def test_empty_text_raises(self):
        with self.assertRaises(ValueError):
            self.resolver.resolve("   ")

    def test_no_candidate_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.resolver.resolve("纯文本没有任何链接")
        self.assertIn("未从粘贴内容中找到链接", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
