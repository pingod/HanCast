"""
分享文本 / 任意页面 → 可播放直链

处理三类粘贴场景:
1. 官方分享文案（抖音/B站/小红书等"口令"式复制）: 从混排中文文本中提取链接或 BV 号
2. 短链 (b23.tv / v.douyin.com / xhslink.com 等): 跟随重定向展开后再路由
3. 普通网页: 解析 og:video / <video>/<source> / 内嵌 m3u8・mp4 链接，
   产出电视可直接拉流的播放地址

执行环境约束: 命令在 sidecar 单线程命令循环中执行，全部网络请求共享一个
总预算 (_TOTAL_BUDGET)，单请求超时按剩余预算裁剪，避免拖住投屏状态轮询。
"""

import os
import re
import json
import time
import logging
import requests
from urllib.parse import urlparse, urljoin, unquote
from html import unescape as html_unescape

from .bili_resolver import BiliResolver
from .server import MediaServer
from ..models.media import MediaInfo

logger = logging.getLogger("hancast.media.share")

_TOTAL_BUDGET = 25.0       # 单次 resolve 总时长上限（秒）
_MAX_BODY = 2 * 1024 * 1024  # 页面正文读取上限（字节）

_DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# 已知短链域名（分享文案中常不带 scheme）
_SHORT_HOSTS = (
    "b23.tv", "v.douyin.com", "www.iesdouyin.com", "v.ixigua.com", "m.ixigua.com",
    "xhslink.com", "t.cn", "url.cn", "s.url.cn", "dwz.cn", "bit.ly", "tinyurl.com",
    "t.me",
)

# URL 允许字符：排除空白、引号尖括号反斜杠、CJK 字符与全角标点（口令文案分界）
_URL_CHARS = r"[^\s<>\"'`\\\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]"
# 逐条正则
URL_RE = re.compile(r"https?://" + _URL_CHARS + r"+", re.I)
_SHORT_ALT = "|".join(re.escape(h) for h in _SHORT_HOSTS)
SHORT_RE = re.compile(
    r"(?<![\w./-])(" + _SHORT_ALT + r")(" + _URL_CHARS + r"*)?", re.I)
BV_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
AV_RE = re.compile(r"\bav\d{1,9}\b", re.I)

BILI_SUFFIXES = {"bilibili.com", "b23.tv", "bilibili.tv"}

_EXT_MIME = {
    "mp4": "video/mp4", "m4v": "video/mp4", "mkv": "video/x-matroska",
    "avi": "video/x-msvideo", "mov": "video/quicktime", "webm": "video/webm",
    "flv": "video/x-flv", "ts": "video/mp2t", "mpg": "video/mpeg",
    "mpeg": "video/mpeg", "wmv": "video/x-ms-wmv", "3gp": "video/3gpp",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac",
    "flac": "audio/flac", "wav": "audio/wav", "ogg": "audio/ogg",
    "m3u8": "application/vnd.apple.mpegurl",
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
}

# 页面内嵌流地址（先归一化 \/ 与 \u0026 转义再匹配）
_EMBEDDED_STREAM_RE = re.compile(
    r"https?://" + _URL_CHARS + r"*?\.(?:m3u8|mp4|m4v|flv|mkv|webm|mov|mp3|m4a|aac|flac|wav|ogg)"
    r"(?:\?" + _URL_CHARS + r"*)?", re.I)

_TRAILING_PUNCT = '.,;:!?)]}>"\'`'


def _clean_url(url: str) -> str:
    """剥离口令文案粘连在 URL 尾部的标点"""
    return url.rstrip(_TRAILING_PUNCT)


def extract_candidates(text: str) -> list[str]:
    """从分享文案中提取按优先级排序的候选地址（去重，保持出现顺序）"""
    seen: set[str] = set()
    candidates: list[str] = []

    def _add(url: str):
        url = _clean_url(url)
        if url and url.lower() not in seen:
            seen.add(url.lower())
            candidates.append(url)

    spans = []
    for m in URL_RE.finditer(text):
        spans.append(m.span())
        _add(m.group(0))

    # 短链裸域名（去掉已匹配的带 scheme 区间，避免把 URL 内部的主机名再匹配一遍）
    rest = text
    for s, e in reversed(spans):
        rest = rest[:s] + " " + rest[e:]
    for m in SHORT_RE.finditer(rest):
        if m.group(2):  # 必须带路径才有意义
            _add("https://" + m.group(1) + m.group(2))

    # B站号：没有可用链接时的口令兜底
    if not candidates:
        for m in BV_RE.finditer(text):
            _add(f"https://www.bilibili.com/video/{m.group(0)}/")
        for m in AV_RE.finditer(text):
            _add(f"https://www.bilibili.com/video/{m.group(0)}/")

    return candidates


def _ext_of(url: str) -> str:
    path = urlparse(url).path
    name = os.path.basename(path)
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _decode_body(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type, re.I)
    encodings = ([m.group(1)] if m else []) + ["utf-8", "gbk"]
    for enc in encodings:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


class ShareResolver:
    """分享文本 → 可播放 MediaInfo"""

    def __init__(self, media_server: MediaServer,
                 bili_resolver: BiliResolver | None = None):
        self.media_server = media_server
        # 共享 BiliResolver 实例：反爬 Cookie 缓存跨调用复用
        self.bili = bili_resolver or BiliResolver()

    # ── 对外入口 ──

    def resolve(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("粘贴内容为空")

        candidates = extract_candidates(text)
        if not candidates:
            raise ValueError(
                "未从粘贴内容中找到链接（支持视频网址、抖音/B站/小红书分享口令、BV/av 号）")

        deadline = time.monotonic() + _TOTAL_BUDGET
        errors: list[str] = []
        for url in candidates[:3]:
            try:
                return self._resolve_url(url, deadline)
            except Exception as e:
                logger.warning(f"候选地址解析失败 {url}: {e}")
                errors.append(f"{urlparse(url).netloc}: {e}")
        raise ValueError("无法解析出可播放地址 — " + "；".join(errors))

    def resolve_bilibili_media_info(self, url: str) -> dict:
        """B站链接 → 本地代理可播信息（原 resolve_bilibili 命令的实现）"""
        if not self.bili.belongs_to(url):
            raise ValueError(f"不是B站链接: {url}")

        result = self.bili.resolve(url)

        # 将视频直链注册为本地代理 (附加 Referer 头)
        proxy_url = self.media_server.register_proxy(result["video_url"])

        # 将封面图也注册为本地代理 (B站 CDN 有防盗链)
        cover_url = result.get("cover_url", "")
        cover_proxy = self.media_server.register_proxy(cover_url) if cover_url else None

        media_info = MediaInfo(
            media_type="url",
            uri=proxy_url,
            title=result["title"],
            mime_type="video/mp4",
            file_size=None,
            thumbnail=cover_proxy or cover_url,
        )
        return {
            **media_info.to_dict(),
            "author": result.get("author", ""),
            "bvid": result.get("bvid", ""),
            "cover_url": result.get("cover_url", ""),
            "source_url": url,
            "via": "bilibili",
        }

    # ── 路由 ──

    def _resolve_url(self, url: str, deadline: float) -> dict:
        if self._is_bili(url):
            return self.resolve_bilibili_media_info(url)

        # 短链/重定向展开：一次 GET 同时拿到最终地址与正文
        final_url, content_type, content_length, body = self._open(url, deadline)

        if self._is_bili(final_url):
            return self.resolve_bilibili_media_info(final_url)

        base_mime = content_type.split(";")[0].strip().lower()
        if base_mime.startswith(("video/", "audio/", "image/")) or \
                (base_mime == "application/octet-stream" and _ext_of(final_url) in _EXT_MIME):
            return self._direct_info(final_url, base_mime, content_length, url)

        # 抖音专路（share 页内嵌 JSON 比通用 HTML 提取可靠）
        if "douyin.com" in (urlparse(final_url).hostname or ""):
            try:
                return self._resolve_douyin(final_url, deadline, url)
            except Exception as e:
                logger.info(f"抖音专路解析失败，回退通用提取: {e}")

        html = _decode_body(body or b"", content_type)
        return self._resolve_page(final_url, html, url)

    @staticmethod
    def _is_bili(url: str) -> bool:
        host = urlparse(url).hostname or ""
        return any(host == s or host.endswith("." + s) for s in BILI_SUFFIXES)

    # ── HTTP ──

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            raise TimeoutError("解析超时（超出总时长预算）")
        return min(remaining, 10.0)

    def _open(self, url: str, deadline: float,
              ua: str = _DESKTOP_UA) -> tuple[str, str, str | None, bytes | None]:
        """GET（流式、正文封顶）→ (最终URL, Content-Type, Content-Length, 正文或None)

        正文为媒体类型时不下载，返回 None。
        """
        timeout = self._remaining(deadline)
        try:
            r = requests.get(url, timeout=timeout, allow_redirects=True,
                             headers={"User-Agent": ua, "Accept": "*/*"},
                             stream=True)
        except requests.RequestException as e:
            raise ValueError(f"地址不可达: {e}") from e
        try:
            content_type = r.headers.get("Content-Type", "")
            content_length = r.headers.get("Content-Length")
            base = content_type.split(";")[0].strip().lower()
            if base.startswith(("video/", "audio/", "image/")):
                body = None
            else:
                buf, size = [], 0
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    buf.append(chunk)
                    size += len(chunk)
                    if size >= _MAX_BODY:
                        break
                body = b"".join(buf)
            return r.url, content_type, content_length, body
        finally:
            r.close()

    # ── 直链 ──

    def _direct_info(self, final_url: str, mime_hint: str,
                     content_length: str | None, source_url: str) -> dict:
        ext = _ext_of(final_url)
        mime = _EXT_MIME.get(ext) or mime_hint or "video/mp4"
        name = unquote(os.path.basename(urlparse(final_url).path)) or \
            urlparse(final_url).netloc
        media_info = MediaInfo(
            media_type="url",
            uri=final_url,
            title=name or final_url,
            mime_type=mime,
            file_size=int(content_length) if content_length else None,
        )
        return {**media_info.to_dict(), "source_url": source_url, "via": "direct"}

    # ── 抖音 ──

    def _resolve_douyin(self, page_url: str, deadline: float,
                        source_url: str) -> dict:
        m = re.search(r"/(?:video|note)/(\d+)", page_url) or \
            re.search(r"[?&](?:vid|aweme_id)=(\d+)", page_url)
        if not m:
            raise ValueError("未能提取抖音视频 ID")
        html = _decode_body(
            self._open(f"https://www.iesdouyin.com/share/video/{m.group(1)}/",
                       deadline, ua=_MOBILE_UA)[3] or b"", "text/html")

        mp = re.search(r'"play_addr"\s*:\s*\{[^{}]*?"url_list"\s*:\s*\[\s*"([^"]+)"', html)
        if not mp:
            raise ValueError("抖音分享页未找到播放地址")
        video_url = html_unescape(
            mp.group(1).replace("\\/", "/").replace("\\u0026", "&"))
        # 去水印变体（playwm → play），两者内容一致、play 更通用
        video_url = video_url.replace("playwm", "play")

        title = ""
        mt = re.search(r'"desc"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
        if mt:
            try:
                title = json.loads(f'"{mt.group(1)}"')
            except (ValueError, json.JSONDecodeError):
                title = mt.group(1)
        if not title:
            t = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
            title = html_unescape(t.group(1)).strip() if t else "抖音视频"

        media_info = MediaInfo(
            media_type="url",
            uri=video_url,
            title=title[:80],
            mime_type="video/mp4",
        )
        return {**media_info.to_dict(), "source_url": source_url, "via": "douyin"}

    # ── 通用页面提取 ──

    def _resolve_page(self, page_url: str, html: str, source_url: str) -> dict:
        declared: list[str] = []   # og:video / twitter:player:stream
        title = ""
        thumb = ""

        for tag in re.finditer(r"<meta[^>]+>", html, re.I):
            attrs = tag.group(0)
            p = re.search(r"(?:property|name)\s*=\s*[\"']([^\"']+)[\"']", attrs, re.I)
            c = re.search(r"content\s*=\s*[\"']([^\"']*)[\"']", attrs, re.I)
            if not (p and c and c.group(1)):
                continue
            key, val = p.group(1).strip().lower(), c.group(1).strip()
            if key in ("og:video", "og:video:url", "og:video:secure_url",
                       "og:video:src", "twitter:player:stream"):
                declared.append(val)
            elif key in ("og:title", "twitter:title") and not title:
                title = html_unescape(val)
            elif key == "og:image" and not thumb:
                thumb = html_unescape(val)

        # <video>/<source> 标签直链
        for m in re.finditer(
                r"<(?:video|source)\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"']",
                html, re.I):
            declared.append(m.group(1))

        # 内嵌 JSON/脚本中的流地址（先归一化 \/ 与 &u0026 转义）
        norm = html.replace("\\/", "/").replace("\\u0026", "&")
        embedded = [m.group(0) for m in _EMBEDDED_STREAM_RE.finditer(norm)]

        if not title:
            t = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
            title = html_unescape(t.group(1)).strip() if t else ""

        streams: list[str] = []
        for cand in declared + embedded:
            absu = urljoin(page_url, cand.strip())
            if absu.lower().startswith(("http://", "https://")) and absu not in streams:
                streams.append(absu)
        if not streams:
            netloc = urlparse(page_url).netloc
            raise ValueError(
                f"页面中未找到可播放的视频源（{netloc}），该站点可能需要登录或由脚本动态加载")

        streams.sort(key=self._stream_rank)
        chosen = streams[0]
        mime = _EXT_MIME.get(_ext_of(chosen)) or "video/mp4"
        media_info = MediaInfo(
            media_type="url",
            uri=chosen,
            title=title or urlparse(page_url).netloc,
            mime_type=mime,
            thumbnail=(urljoin(page_url, thumb) if thumb else None),
        )
        return {**media_info.to_dict(), "source_url": source_url, "via": "page"}

    @staticmethod
    def _stream_rank(url: str) -> tuple[int, int]:
        """播放兼容性排序：HLS 优先（DLNA 电视普遍支持），其次 mp4 系，再其余"""
        ext = _ext_of(url)
        if ext == "m3u8":
            tier = 0
        elif ext in ("mp4", "m4v", "mov", "webm"):
            tier = 1
        elif ext in ("mkv", "flv", "avi", "wmv", "ts", "mpg", "mpeg", "3gp"):
            tier = 2
        elif ext in ("mp3", "m4a", "aac", "flac", "wav", "ogg"):
            tier = 4
        else:
            # og:video 等声明源常无扩展名，电视对未知 URL 容忍度较高
            tier = 3
        return (tier, len(url))
