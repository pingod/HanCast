"""抖音规则：分享页内嵌 JSON 比通用 HTML 提取可靠（page 阶段）"""

import re
import json
import logging
from html import unescape
from urllib.parse import urlparse

from .base import (SiteRule, RuleContext, register, MOBILE_UA,
                   decode_js_string, decode_body)

logger = logging.getLogger("hancast.media.rules")

_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.I)


class DouyinRule(SiteRule):
    name = "douyin"
    hosts = ("douyin.com", "iesdouyin.com")
    stage = "page"
    priority = 20

    def matches(self, url: str) -> bool:
        # 与既有管线一致：主机名包含 douyin.com 即算（iesdouyin 这类分享域不是子域）
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        return "douyin.com" in host

    def resolve(self, url: str, html: str, ctx: RuleContext) -> dict:
        m = re.search(r"/(?:video|note)/(\d+)", url) or \
            re.search(r"[?&](?:vid|aweme_id)=(\d+)", url)
        if not m:
            raise ValueError("未能提取抖音视频 ID")
        share_html = decode_body(
            ctx.open(f"https://www.iesdouyin.com/share/video/{m.group(1)}/",
                     ua=MOBILE_UA, referer=url)[3] or b"", "text/html")

        mp = re.search(r'"play_addr"\s*:\s*\{[^{}]*?"url_list"\s*:\s*\[\s*"([^"]+)"',
                       share_html)
        if not mp:
            raise ValueError("抖音分享页未找到播放地址")
        # 分享页 JSON 里的斜杠是 \u002F 形式，必须按 JSON 字面量解码，
        # 否则交给电视的是一个带 \\u002F 的废地址
        video_url = decode_js_string(mp.group(1))
        # 去水印变体（playwm → play），两者内容一致、play 更通用
        video_url = video_url.replace("playwm", "play")

        title = ""
        mt = re.search(r'"desc"\s*:\s*"((?:[^"\\]|\\.)*)"', share_html)
        if mt:
            try:
                title = json.loads(f'"{mt.group(1)}"')
            except (ValueError, json.JSONDecodeError):
                title = mt.group(1)
        if not title:
            t = _TITLE_RE.search(html or "")
            title = unescape(t.group(1)).strip() if t else "抖音视频"

        uri, mime, via = ctx.publish(video_url, "video/mp4", url, "douyin")
        return ctx.info(uri, mime, title[:80], via)


register(DouyinRule())
