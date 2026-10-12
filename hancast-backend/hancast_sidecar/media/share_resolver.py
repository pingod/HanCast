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
    # 快手分享域（link.kuaishou.com / v.m.chenzhongtech.com 常以裸链出现在口令里）
    "c.kuaishou.com", "link.kuaishou.com", "v.m.chenzhongtech.com",
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

# 已知有专路/通用提取把握较高的平台（候选排序时优先）
_KNOWN_MEDIA_HOSTS = (
    "bilibili.com", "b23.tv", "bilibili.tv", "douyin.com", "iesdouyin.com",
    "ixigua.com", "kuaishou", "chenzhongtech.com", "xiaohongshu.com",
    "xhslink.com", "weibo.com", "weibo.cn",
)

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
# m3u8 允许写成 /api/m3u8?xx 这种无扩展名的接口路径；扩展名后不能再跟单词字符，
# 否则 "dytt.m3u8s" 这类标识符会被当成流文件
_STREAM_EXT = (r"(?:\.(?:m3u8|mp4|m4v|flv|mkv|webm|mov|mp3|m4a|aac|flac|wav|ogg)"
               r"|/m3u8)(?![A-Za-z0-9])")
_EMBEDDED_STREAM_RE = re.compile(
    r"https?://" + _URL_CHARS + r"*?" + _STREAM_EXT + r"(?:\?" + _URL_CHARS + r"*)?", re.I)

# 引号内的相对流接口：/api/m3u8?origin=xx&url=yy 这类「播放地址由接口下发」的
# 站点最多，纯绝对 URL 正则永远匹配不到。m3u8 必须带 . 或 / 前导，否则页面里
# 的 JS 标识符（"bfzym3u8"、"m3u8Tab"）会被当成候选地址浪费验证预算
_Q = "[\"" + "'" + "`]"
_NOQ = "[^\"" + "'" + "`\\s]"
_QUOTED_EXT = (r"(?:\.m3u8|/m3u8|\.mp4|\.m4v|\.flv|\.mkv|\.webm|\.mov)(?![A-Za-z0-9])")
_QUOTED_STREAM_RE = re.compile(
    _Q + "(" + _NOQ + "{4,400}?" + _QUOTED_EXT + _NOQ + "{0,200}?)" + _Q, re.I)

_TRAILING_PUNCT = '.,;:!?)]}>"\'`'

_VERIFY_READ = 128 * 1024   # 验证候选流时最多读取的字节数（列表/接口正文都很小）
_MAX_VERIFY = 4             # 单次解析最多验证几个候选，避免吃满总预算
_MAX_HOP = 3                # 接口 → 主列表 → 分片列表 的下钻跳数
_M3U8_MIME = "application/vnd.apple.mpegurl"

# 模板/占位符残留（JS 拼接前的 "${url}"、未展开的正则）不可能可播
_JUNK_CHARS = set('<>*{}|\\$')


class UnplayableSource(ValueError):
    """候选地址明确不是可播流（返回网页 / 4xx / 接口里没有地址）

    与「网络不可达」区分开：后者无法判定，电视端可能反而能播，
    此时保留原始候选直接交给设备，而不是把一次成功的解析判成失败。
    """


def _clean_url(url: str) -> str:
    """剥离口令文案粘连在 URL 尾部的标点"""
    return url.rstrip(_TRAILING_PUNCT)


def _normalize_text(text: str) -> str:
    """归一化 JS 转义与 HTML 实体：\\/ 、\\u002F → / ；\\u0026 、&amp; → &"""
    text = text.replace("\\/", "/")
    text = re.sub(r"\\u002F", "/", text, flags=re.I)
    text = re.sub(r"\\u0026", "&", text, flags=re.I)
    return html_unescape(text)


def _sane_url(url: str) -> bool:
    return bool(url) and len(url) <= 600 and not (set(url) & _JUNK_CHARS)


def _stream_candidates_from_text(text: str) -> list[str]:
    """从页面/接口正文中取出候选流地址（绝对流 URL + 引号内的相对流接口）"""
    norm = _normalize_text(text)
    found = [m.group(0) for m in _EMBEDDED_STREAM_RE.finditer(norm)]
    found += [m.group(1) for m in _QUOTED_STREAM_RE.finditer(norm)]
    out: list[str] = []
    for u in found:
        u = _clean_url(u.strip())
        if _sane_url(u) and u not in out:
            out.append(u)
    # 绝对 URL 正则只吃到扩展名、引号正则吃到带 query 的完整地址时留最长的那个
    return [u for u in out if not any(o != u and o.startswith(u) for o in out)]


_PLAYLIST_MIMES = {"application/vnd.apple.mpegurl", "application/x-mpegurl",
                   "application/mpegurl", "application/vnd.mpegurl",
                   "application/audio/mpegurl"}


def _playlist_path(url: str) -> bool:
    """路径末段是否就是播放列表（/api/m3u8 这类无扩展名接口也算）"""
    seg = urlparse(url).path.rsplit("/", 1)[-1].lower()
    return seg == "m3u8" or seg.endswith(".m3u8")


def _playlist_mime(mime: str, url: str) -> bool:
    return (mime or "") in _PLAYLIST_MIMES or _playlist_path(url)


def _is_playlist(content_type: str, url: str, body: bytes | None) -> bool:
    """响应是否为 HLS 播放列表（m3u8 的 Content-Type 是 application/*，
    落不进 video/ 分支，粘贴裸 m3u8 链接时会在这里被接住）"""
    if _playlist_mime(content_type, url):
        return True
    return bool(body) and body.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"#EXTM3U")


def _master_variant_url(text: str, base: str) -> str | None:
    """主列表 → 最高码率版本的绝对地址；不是主列表（即分片列表）时返回 None"""
    if "#EXT-X-STREAM-INF" not in text.upper():
        return None
    best: tuple[int, str] | None = None
    pending: int | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line, re.I)
            pending = int(m.group(1)) if m else 0
        elif line.startswith("#"):
            continue
        elif pending is not None:
            if best is None or pending > best[0]:
                best = (pending, line)
            pending = None
    return urljoin(base, best[1]) if best else None


def _decode_js_string(raw: str) -> str:
    """JS/JSON 字符串字面量 → 真实文本（\\/ 、\\u002F 、\\u0026 等转义一次解干净）"""
    try:
        return json.loads(f'"{raw}"')
    except (ValueError, json.JSONDecodeError):
        return _normalize_text(raw)


def _sniff_magic(body: bytes) -> str | None:
    """靠魔数识别正文是否为媒体（不少站点给流文件回 application/octet-stream）"""
    if len(body) < 12:
        return None
    if body[4:8] == b"ftyp":
        return "video/mp4"
    if body[:3] == b"FLV":
        return "video/x-flv"
    if body[:4] == b"\x1aE\xdf\xa3":
        return "video/x-matroska"
    if body[:4] == b"RIFF" and body[8:12] == b"WAVE":
        return "audio/wav"
    if body[:3] == b"ID3" or body[:2] in (b"\xff\xf8", b"\xff\xf9", b"\xff\xfa"):
        return "audio/mpeg"
    if body[0] == 0x47 and (len(body) <= 189 or body[188] == 0x47):
        return "video/mp2t"
    return None


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
        # 媒体似然度优先：已知平台/带媒体扩展名的链接先试（口令文案常混入推广链接）
        candidates = sorted(candidates, key=self._candidate_priority)

        deadline = time.monotonic() + _TOTAL_BUDGET
        errors: list[str] = []
        # 一次解析共用一个 Session：页面下发的 Cookie 要能带进
        # /api/m3u8?origin=xx&url=yy 这类「播放地址由接口再给一次」的二次请求
        with requests.Session() as session:
            for url in candidates[:3]:
                try:
                    return self._resolve_url(url, deadline, session)
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

    def _resolve_url(self, url: str, deadline: float,
                     session: requests.Session) -> dict:
        if self._is_bili(url):
            return self.resolve_bilibili_media_info(url)

        # 短链/重定向展开：一次 GET 同时拿到最终地址与正文
        final_url, content_type, content_length, body = self._open(
            url, deadline, session=session)

        if self._is_bili(final_url):
            return self.resolve_bilibili_media_info(final_url)

        base_mime = content_type.split(";")[0].strip().lower()
        if base_mime.startswith(("video/", "audio/", "image/")) or \
                (base_mime == "application/octet-stream" and _ext_of(final_url) in _EXT_MIME):
            return self._direct_info(final_url, base_mime, content_length, url)

        # 粘贴的就是播放列表：主列表先挑最高码率的分片列表，电视不用自己选流
        if _is_playlist(base_mime, final_url, body):
            text = _decode_body(body or b"", content_type)
            uri = _master_variant_url(text, final_url) or final_url
            media_info = MediaInfo(
                media_type="url",
                uri=uri,
                title=unquote(os.path.basename(urlparse(uri).path)) or urlparse(uri).netloc,
                mime_type=_M3U8_MIME,
                file_size=int(content_length) if content_length else None,
            )
            return {**media_info.to_dict(), "source_url": url, "via": "playlist"}

        # 抖音专路（share 页内嵌 JSON 比通用 HTML 提取可靠）
        if "douyin.com" in (urlparse(final_url).hostname or ""):
            try:
                return self._resolve_douyin(final_url, deadline, url, session)
            except Exception as e:
                logger.info(f"抖音专路解析失败，回退通用提取: {e}")

        html = _decode_body(body or b"", content_type)
        return self._resolve_page(final_url, html, url, deadline, session)

    @staticmethod
    def _is_bili(url: str) -> bool:
        host = urlparse(url).hostname or ""
        return any(host == s or host.endswith("." + s) for s in BILI_SUFFIXES)

    @staticmethod
    def _candidate_priority(url: str) -> int:
        host = (urlparse(url).hostname or "").lower()
        if any(h in host for h in _KNOWN_MEDIA_HOSTS):
            return 0
        if _ext_of(url) in _EXT_MIME:
            return 0
        return 1

    # ── 交付前检查：电视不带 Cookie/Referer 能否拉到流 ──

    def _publish(self, uri: str, mime: str, session: requests.Session,
                 referer: str, deadline: float, via: str) -> tuple[str, str, str]:
        """返回 (uri, mime, via)；需要防盗链头时改走本机代理"""
        if deadline - time.monotonic() < 3.0:
            return uri, mime, via          # 预算见底，按直链交付（原行为）
        try:
            if self._tv_can_fetch(uri, mime, deadline):
                return uri, mime, via
        except Exception as e:
            logger.info(f"直链可播性检查跳过 {uri[:80]}: {e}")
            return uri, mime, via
        headers = self._snapshot_headers(session, referer, uri)
        proxied = self.media_server.register_proxy(uri, headers)
        logger.info(f"直链需要页面请求头，改走本机代理: {uri[:80]}")
        return proxied, mime, via + "+proxy"

    def _tv_can_fetch(self, url: str, mime: str, deadline: float) -> bool:
        """模拟电视：只有 UA，不带页面 Cookie/Referer"""
        head, ctype, status = self._peek(url, deadline)
        if not self._media_ok(status, ctype, head):
            return False
        if _playlist_mime(mime, url) and b"#EXTM3U" in head[:512]:
            text = head.decode("utf-8", errors="replace")
            if "EXT-X-STREAM-INF" in text:
                return True                # 主列表：子列表仍走同一检查由代理负责
            seg = next((ln.strip() for ln in text.splitlines()
                        if ln.strip() and not ln.startswith("#")), None)
            if seg:
                h2, c2, s2 = self._peek(urljoin(url, seg), deadline, use_range=False)
                return self._media_ok(s2, c2, h2)
        return True

    def _peek(self, url: str, deadline: float,
              use_range: bool = True) -> tuple[bytes, str, int]:
        """取响应前 1KB（够判魔数），避免为检查下一个大文件

        循环累计：分块编码下首块可能只有几十字节，只取一块会把魔数判成不可播。
        检查 TS 分片时不能带 Range —— 有些站点只对 Range 请求吐真分片，
        而电视恰恰不带 Range。
        """
        timeout = self._remaining(deadline)
        headers = {"User-Agent": _DESKTOP_UA}
        if use_range:
            headers["Range"] = "bytes=0-1023"
        r = requests.get(url, timeout=min(timeout, 6.0), stream=True, headers=headers)
        try:
            head = b""
            for chunk in r.iter_content(chunk_size=1024):
                head += chunk
                if len(head) >= 1024:
                    break
            return head[:2048], r.headers.get("Content-Type", ""), r.status_code
        finally:
            r.close()

    @staticmethod
    def _media_ok(status: int, ctype: str, head: bytes) -> bool:
        if status >= 400:
            return False
        stripped = head.lstrip(b"\xef\xbb\xbf \r\n\t")
        if stripped.startswith(b"#EXTM3U") or _sniff_magic(head):
            return True                    # 魔数最可信：不少站点给 TS 回 text/html
        c = (ctype or "").lower()
        if c.startswith(("video/", "audio/")) or "mpegurl" in c:
            return True
        return not c.startswith(("text/", "image/", "application/json"))

    @staticmethod
    def _snapshot_headers(session: requests.Session, referer: str,
                          target: str) -> dict:
        """把页面会话里的 Cookie 固化成请求头交给代理

        这类站点常用「页面域下发 Cookie、另一台流媒体主机校验」的做法
        （global.dyrsxm.cc 发 sion_id，box.dyrs.com.de 认它），按浏览器同源规则
        过滤会把关键 Cookie 滤掉，所以页面域与目标域都取。
        """
        headers = {"User-Agent": _DESKTOP_UA, "Referer": referer}
        origin = urlparse(referer)
        if origin.scheme and origin.netloc:
            headers["Origin"] = f"{origin.scheme}://{origin.netloc}"
        hosts = {(urlparse(target).hostname or "").lower()}
        ref_host = (urlparse(referer).hostname or "").lower()
        if ref_host:
            hosts.add(ref_host)
        pairs: list[str] = []
        for c in session.cookies:
            domain = (c.domain or "").lstrip(".").lower()
            if domain and any(h == domain or h.endswith("." + domain) for h in hosts):
                item = f"{c.name}={c.value}"
                if item not in pairs:
                    pairs.append(item)
        if pairs:
            headers["Cookie"] = "; ".join(pairs)
        return headers

    # ── HTTP ──

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            raise TimeoutError("解析超时（超出总时长预算）")
        return min(remaining, 10.0)

    def _open(self, url: str, deadline: float,
              ua: str = _DESKTOP_UA, session: requests.Session | None = None,
              referer: str | None = None) -> tuple[str, str, str | None, bytes | None]:
        """GET（流式、正文封顶）→ (最终URL, Content-Type, Content-Length, 正文或None)

        正文为媒体类型时不下载，返回 None。
        """
        timeout = self._remaining(deadline)
        headers = {"User-Agent": ua, "Accept": "*/*"}
        if referer:
            headers["Referer"] = referer
        get = session.get if session is not None else requests.get
        try:
            r = get(url, timeout=timeout, allow_redirects=True,
                    headers=headers, stream=True)
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
                        source_url: str, session: requests.Session) -> dict:
        m = re.search(r"/(?:video|note)/(\d+)", page_url) or \
            re.search(r"[?&](?:vid|aweme_id)=(\d+)", page_url)
        if not m:
            raise ValueError("未能提取抖音视频 ID")
        html = _decode_body(
            self._open(f"https://www.iesdouyin.com/share/video/{m.group(1)}/",
                       deadline, ua=_MOBILE_UA, session=session,
                       referer=page_url)[3] or b"", "text/html")

        mp = re.search(r'"play_addr"\s*:\s*\{[^{}]*?"url_list"\s*:\s*\[\s*"([^"]+)"', html)
        if not mp:
            raise ValueError("抖音分享页未找到播放地址")
        # 分享页 JSON 里的斜杠是 \u002F 形式，必须按 JSON 字面量解码，
        # 否则交给电视的是一个带 \\u002F 的废地址
        video_url = _decode_js_string(mp.group(1))
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

        uri, mime, via = self._publish(video_url, "video/mp4", session,
                                       page_url, deadline, "douyin")
        media_info = MediaInfo(
            media_type="url",
            uri=uri,
            title=title[:80],
            mime_type=mime,
        )
        return {**media_info.to_dict(), "source_url": source_url, "via": via}

    # ── 通用页面提取 ──

    def _resolve_page(self, page_url: str, html: str, source_url: str,
                      deadline: float, session: requests.Session) -> dict:
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

        # 内嵌 JSON/脚本中的流地址（含 /api/m3u8?origin=xx 这类相对流接口）
        embedded = _stream_candidates_from_text(html)

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
        resolved: tuple[str, str] | None = None
        dead: list[str] = []
        for cand in streams[:_MAX_VERIFY]:
            try:
                resolved = self._follow_stream(cand, session, page_url, deadline)
                break
            except UnplayableSource as e:
                logger.info(f"候选地址不可播，换下一个 {cand}: {e}")
                dead.append(str(e))
            except Exception as e:
                # 网络层失败无法判定（电视端反而可能拉得通），按原样交付而不是判死
                logger.info(f"候选地址无法验证，按原样交付 {cand}: {e}")
                resolved = (cand, _EXT_MIME.get(_ext_of(cand)) or "video/mp4")
                break
        if resolved is None:
            netloc = urlparse(page_url).netloc
            raise ValueError(
                f"页面里的候选地址均不可播（{netloc}）：" + "；".join(dead[:2]))

        chosen, mime = resolved
        chosen, mime, via = self._publish(chosen, mime, session, page_url,
                                          deadline, "page")
        media_info = MediaInfo(
            media_type="url",
            uri=chosen,
            title=title or urlparse(page_url).netloc,
            mime_type=mime,
            thumbnail=(urljoin(page_url, thumb) if thumb else None),
        )
        return {**media_info.to_dict(), "source_url": source_url, "via": via}

    # ── 候选地址验证 / 「接口下发地址」下钻 ──

    def _follow_stream(self, url: str, session: requests.Session, referer: str,
                       deadline: float, _hop: int = 0) -> tuple[str, str]:
        """确认候选真的能播，并顺着主列表 / JSON 接口下钻到最终流地址

        返回 (uri, mime)。明确非媒体 → UnplayableSource；网络故障 → 原样抛出，
        由调用方决定是否按原样交付。
        """
        timeout = self._remaining(deadline)
        try:
            r = session.get(url, timeout=min(timeout, 8.0), allow_redirects=True,
                            headers={"User-Agent": _DESKTOP_UA, "Referer": referer,
                                     "Accept": "*/*"}, stream=True)
        except requests.RequestException as e:
            raise ValueError(f"候选地址不可达: {e}") from e
        with r:
            if r.status_code >= 400:
                raise UnplayableSource(f"HTTP {r.status_code}")
            ct = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ct.startswith(("video/", "audio/")):
                return r.url, ct          # 已经是媒体流，不必再下载正文判型
            body = self._read_bounded(r, _VERIFY_READ)
            if ct.startswith("image/"):
                raise UnplayableSource("指向的是图片而非视频")
            stripped = body.lstrip(b"\xef\xbb\xbf \r\n\t")
            if "mpegurl" in ct or ct.endswith(".m3u8") or stripped.startswith(b"#EXTM3U"):
                return self._from_playlist(r.url, body, session, referer, deadline, _hop)
            magic = _sniff_magic(body)
            if magic:
                return r.url, magic
            if ct.startswith("text/html") or stripped[:5].lower().startswith(b"<html"):
                raise UnplayableSource("返回的是网页而非视频流")

            # 「播放地址由接口下发」：JSON/纯文本里再抽一次候选
            nested = _stream_candidates_from_text(
                _decode_body(body, r.headers.get("Content-Type", "")))
            if nested and _hop < _MAX_HOP:
                nested.sort(key=self._stream_rank)
                last: UnplayableSource | None = None
                for cand in nested[:2]:
                    try:
                        return self._follow_stream(urljoin(r.url, cand), session,
                                                   referer, deadline, _hop + 1)
                    except UnplayableSource as e:
                        last = e
                raise last or UnplayableSource("接口下发的地址不可播")
            raise UnplayableSource(f"未识别的内容类型（{ct or '未声明'}）")

    def _from_playlist(self, url: str, body: bytes, session: requests.Session,
                       referer: str, deadline: float, hop: int) -> tuple[str, str]:
        text = body.decode("utf-8", errors="replace")
        variant = _master_variant_url(text, url)
        if variant is None:
            return url, _M3U8_MIME          # 分片列表，电视可直接拉
        if hop >= _MAX_HOP:
            return variant, _M3U8_MIME
        return self._follow_stream(variant, session, referer, deadline, hop + 1)

    @staticmethod
    def _read_bounded(r: requests.Response, limit: int) -> bytes:
        buf, size = [], 0
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            buf.append(chunk)
            size += len(chunk)
            if size >= limit:
                break
        return b"".join(buf)

    @staticmethod
    def _stream_rank(url: str) -> tuple[int, int]:
        """播放兼容性排序：HLS 优先（DLNA 电视普遍支持），其次 mp4 系，再其余"""
        ext = _ext_of(url)
        if ext == "m3u8" or _playlist_path(url):
            tier = 0
        elif ext in ("mp4", "m4v", "mov", "webm"):
            tier = 1
        elif ext in ("mkv", "flv", "avi", "wmv", "ts", "mpg", "mpeg", "3gp"):
            tier = 2
        elif ext in ("mp3", "m4a", "aac", "flac", "wav", "ogg"):
            tier = 4
        elif ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp"):
            tier = 6                       # 海报图绝不能当视频用
        else:
            # og:video 等声明源常无扩展名，电视对未知 URL 容忍度较高
            tier = 3
        return (tier, len(url))
