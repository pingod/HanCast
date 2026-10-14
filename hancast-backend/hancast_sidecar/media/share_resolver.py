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
import time
import logging
import requests
from urllib.parse import (parse_qs, quote, urlparse, urljoin, unquote, urlunparse)
from html import unescape as html_unescape

from .bili_resolver import BiliResolver
from .server import (MediaServer, player_ext_ok, player_safe_suffix,
                     _proxy_broken)
from .rules import (RuleContext, matching as rules_matching,
                    refresh_external as refresh_external_rules)
from .rules.base import DESKTOP_UA, decode_body, normalize_text
from .rules.jsplugins import PluginManager
from ..models.media import MediaInfo
from ..utils.config import SETTING_DIR

logger = logging.getLogger("hancast.media.share")

_TOTAL_BUDGET = 25.0       # 单次 resolve 总时长上限（秒）
_MAX_BODY = 2 * 1024 * 1024  # 页面正文读取上限（字节）

# 外置声明式规则目录：改这里的 JSON 不用重启应用、不用重新打包
DEFAULT_RULES_DIR = os.path.join(SETTING_DIR, "share_rules")

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


class ResilientSession(requests.Session):
    """系统代理坏了就改直连，而不是让整条解析链报错

    本机这类 127.0.0.1 代理端口常只服务于浏览器，它一失效，第一个请求就是
    ConnectionError —— 用户看到的现象是「同一个链接昨天能投今天不能」。
    仍然先按环境配置走代理：公司网络里直连不通的用户不受影响。
    """

    def __init__(self):
        super().__init__()
        self._bypassed = False

    def send(self, request, **kwargs):
        try:
            return super().send(request, **kwargs)
        except requests.RequestException as e:
            if self._bypassed or not _proxy_broken(e):
                raise
            logger.info(f"本地代理不可用（{type(e).__name__}），改用直连重试")
            self._bypassed = True
            self.trust_env = False
            kwargs.pop("proxies", None)
            return super().send(self.prepare_request(request), **kwargs)


def _clean_url(url: str) -> str:
    """剥离口令文案粘连在 URL 尾部的标点"""
    return url.rstrip(_TRAILING_PUNCT)


_URL_SAFE_CHARS = "/%:@!$&'()*+,;=~-._"


def _ascii_url(url: str) -> str:
    """把 URL 里的非 ASCII 字符转成百分号编码

    中文文件名在采集站很常见（…/生化危机：爆发夜.m3u8），留着原样会炸三处：
    requests 按 latin-1 编码请求头，把它当 Referer 直接 UnicodeEncodeError；
    电视与 mpv 的 URI 解析只吃 ASCII；代理改写分片地址时同样会带上裸中文。
    """
    if not any(ord(c) > 127 for c in url):
        return url
    try:
        p = urlparse(url)
    except ValueError:
        return url
    netloc = p.netloc
    try:
        if any(ord(c) > 127 for c in netloc):
            netloc = netloc.encode("idna").decode()
    except UnicodeError:
        pass
    return urlunparse((p.scheme, netloc,
                       quote(p.path, safe=_URL_SAFE_CHARS), p.params,
                       quote(p.query, safe=_URL_SAFE_CHARS + "?"),
                       quote(p.fragment, safe=_URL_SAFE_CHARS)))


# 「壳地址」：真流挂在包装 URL 的 query 里。采集站的播放 iframe 十有八九是这一型
# （iframe → py.php?code=cs&url=<m3u8>），而壳本身常带防盗链、甚至直接 403
_NESTED_QUERY_KEYS = ("url", "u", "src", "source", "playurl", "play",
                      "video", "v", "m3u8", "vid")


def _is_media_url(url: str) -> bool:
    """绝对地址本身是否已经指向流文件 / 流接口"""
    return bool(_EMBEDDED_STREAM_RE.fullmatch(url)) or _playlist_path(url)


def _nested_media_urls(url: str) -> list[str]:
    """从包装地址的 query 里解出真正的流地址（值可能被二次编码）"""
    out: list[str] = []
    try:
        pairs = parse_qs(urlparse(url).query, keep_blank_values=True)
    except ValueError:
        return out
    for key, vals in pairs.items():
        if key.lower() not in _NESTED_QUERY_KEYS:
            continue
        for raw in vals:
            for cand in (raw, unquote(raw)):
                cand = _ascii_url(cand.strip())
                if cand.startswith(("http://", "https://")) and _is_media_url(cand):
                    if cand not in out:
                        out.append(cand)
                    break
    return out


def _with_nested(streams: list[str]) -> list[str]:
    """给每个包装地址补一条「query 里那条真流地址」，紧跟在壳后面"""
    out: list[str] = []
    for u in streams:
        if u not in out:
            out.append(u)
        for nested in _nested_media_urls(u):
            if nested not in out:
                out.append(nested)
    return out


def _sane_url(url: str) -> bool:
    return bool(url) and len(url) <= 600 and not (set(url) & _JUNK_CHARS)


def _stream_candidates_from_text(text: str) -> list[str]:
    """从页面/接口正文中取出候选流地址（绝对流 URL + 引号内的相对流接口）"""
    norm = normalize_text(text)
    found = [m.group(0) for m in _EMBEDDED_STREAM_RE.finditer(norm)]
    found += [m.group(1) for m in _QUOTED_STREAM_RE.finditer(norm)]
    out: list[str] = []
    for u in found:
        u = _ascii_url(_clean_url(u.strip()))
        if _sane_url(u) and u not in out:
            out.append(u)
    # 绝对 URL 正则只吃到扩展名、引号正则吃到带 query 的完整地址时留最长的那个
    out = [u for u in out if not any(o != u and o.startswith(u) for o in out)]
    return _with_nested(out)


def _playlist_path(url: str) -> bool:
    """路径末段是否就是播放列表（/api/m3u8 这类无扩展名接口也算）"""
    seg = urlparse(url).path.rsplit("/", 1)[-1].lower()
    return seg == "m3u8" or seg.endswith(".m3u8")


def _playlist_mime(mime: str, url: str) -> bool:
    """m3u8 的 Content-Type 五花八门（application/vnd.apple.mpegurl、
    audio/mpegurl、x-mpegURL…），认子串比枚举整串可靠；无扩展名的 /m3u8 接口也算"""
    return "mpegurl" in (mime or "").lower() or _playlist_path(url)


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
        url = _ascii_url(_clean_url(url))
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


class ShareResolver:
    """分享文本 → 可播放 MediaInfo"""

    def __init__(self, media_server: MediaServer,
                 bili_resolver: BiliResolver | None = None,
                 rules_dir: str | None = None, config=None):
        self.media_server = media_server
        # 共享 BiliResolver 实例：反爬 Cookie 缓存跨调用复用
        self.bili = bili_resolver or BiliResolver()
        self.rules_dir = rules_dir or getattr(config, "share_rules_dir",
                                              None) or DEFAULT_RULES_DIR
        # JS 脚本插件根：<share_rules>/plugins/；导入/启停/测试命令经它操作
        self.plugin_mgr = PluginManager(os.path.join(self.rules_dir, "plugins"))

    # ── 对外入口 ──

    def resolve(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("粘贴内容为空")

        self._load_external_rules()
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
        with ResilientSession() as session:
            for url in candidates[:3]:
                try:
                    return self._resolve_url(url, deadline, session)
                except Exception as e:
                    logger.warning(f"候选地址解析失败 {url}: {e}")
                    errors.append(f"{urlparse(url).netloc}: {e}")
        raise ValueError("无法解析出可播放地址 — " + "；".join(errors) +
                         f"（可为该站点在 {self.rules_dir} 添加解析规则，无需重装）")

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

    def rule_inventory(self) -> dict:
        """三种来源的规则一处列全：脚本包 / 声明式 JSON / 随包编译的 Python 规则

        设置页靠它把「这台机器现在到底认哪些站」摊开给用户看，
        顺带告诉他规则目录在哪（改 JSON 不用重启、不用重新打包）。
        """
        self._load_external_rules()
        from .rules.base import builtin_rules_info
        from .rules.declarative import declarative_inventory
        return {
            "rules_dir": self.rules_dir,
            "script_packs": self.plugin_mgr.list_info(),
            "declarative": declarative_inventory(self.rules_dir),
            "builtin": builtin_rules_info(),
        }

    def _load_external_rules(self) -> None:
        """按目录指纹重载外置规则与 JS 插件；规则目录有问题不能连累解析本身"""
        try:
            os.makedirs(self.rules_dir, exist_ok=True)
            refresh_external_rules(self.rules_dir)
        except Exception as e:
            logger.warning(f"外置解析规则加载失败 {self.rules_dir}: {e}")
        try:
            self.plugin_mgr.refresh()
        except Exception as e:
            logger.warning(f"解析插件加载失败 {self.plugin_mgr.root}: {e}")

    def _run_rules(self, url: str, html: str, stage: str,
                   ctx: RuleContext) -> dict | None:
        """逐条试命中该地址的规则，全都不适用时返回 None 交给通用管线

        「这条规则拿不下」不是错误：只记日志，绝不冒泡成用户看到的解析失败，
        否则加一条写错的规则就会毁掉本来能成功的粘贴。
        """
        for rule in rules_matching(url, stage):
            try:
                return rule.resolve(url, html, ctx)
            except Exception as e:
                logger.info(f"规则 {rule.name}（{stage} 阶段）未取到地址，换下一条: {e}")
        return None

    def _resolve_url(self, url: str, deadline: float,
                     session: requests.Session) -> dict:
        ctx = RuleContext(resolver=self, session=session, deadline=deadline,
                          source_url=url)

        # 拿到链接就能解析的站点（B 站官方接口、声明式 url 阶段规则）
        hit = self._run_rules(url, "", "url", ctx)
        if hit is not None:
            return hit

        # 短链/重定向展开：一次 GET 同时拿到最终地址与正文
        final_url, content_type, content_length, body = self._open(
            url, deadline, session=session)

        if final_url != url:
            # 展开后可能落到某个规则负责的域（b23.tv → bilibili.com）
            hit = self._run_rules(final_url, "", "url", ctx)
            if hit is not None:
                return hit

        base_mime = content_type.split(";")[0].strip().lower()
        # 播放列表判定在「音视频类型」之前：不少 CDN 给 m3u8 回 audio/mpegurl，
        # 按直链处理就把主列表原样丢给电视，省掉了选最高码率那一步。
        # 反过来，服务器明确说这是视频/音频文件时（哪怕路径以 .m3u8 结尾）按文件交付。
        concrete_media = (base_mime.startswith(("video/", "audio/"))
                          and "mpegurl" not in base_mime)
        if not concrete_media and _is_playlist(base_mime, final_url, body):
            text = decode_body(body or b"", content_type)
            uri = _master_variant_url(text, final_url) or final_url
            media_info = MediaInfo(
                media_type="url",
                uri=uri,
                title=unquote(os.path.basename(urlparse(uri).path)) or urlparse(uri).netloc,
                mime_type=_M3U8_MIME,
                file_size=int(content_length) if content_length else None,
            )
            return {**media_info.to_dict(), "source_url": url, "via": "playlist"}

        if concrete_media or base_mime.startswith("image/") or \
                (base_mime == "application/octet-stream" and _ext_of(final_url) in _EXT_MIME):
            return self._direct_info(final_url, base_mime, content_length, url)

        html = decode_body(body or b"", content_type)

        # 必须先见到正文才能取地址的站点（抖音分享页、声明式 page 阶段规则）
        hit = self._run_rules(final_url, html, "page", ctx)
        if hit is not None:
            return hit

        return self._resolve_page(final_url, html, url, deadline, session)

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
                 referer: str, deadline: float, via: str,
                 extra_headers: dict | None = None) -> tuple[str, str, str]:
        """返回 (uri, mime, via)；需要防盗链头时改走本机代理

        ``extra_headers``：规则自带的关键请求头（JS 插件的签名头这类），
        改走代理时优先于会话快照里的同名头。
        """
        if deadline - time.monotonic() < 3.0:
            return uri, mime, via          # 预算见底，按直链交付（原行为）
        try:
            if self._tv_can_fetch(uri, mime, deadline):
                return uri, mime, via
        except Exception as e:
            logger.info(f"直链可播性检查跳过 {uri[:80]}: {e}")
            return uri, mime, via
        headers = self._snapshot_headers(session, referer, uri)
        if extra_headers:
            headers = {**headers, **{str(k): str(v)
                                     for k, v in extra_headers.items()}}
        suffix = ".m3u8" if _playlist_mime(mime, uri) else player_safe_suffix(uri)
        proxied = self.media_server.register_proxy(uri, headers, suffix)
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
                seg_url = urljoin(url, seg)
                if not player_ext_ok(seg_url):
                    # 采集站把 TS 分片伪装成 .png 传到图片 CDN：字节没问题，
                    # 但 mpv/电视的 HLS 客户端按后缀白名单直接拒绝拉流，
                    # 只有本机代理能给路径换回合规后缀
                    logger.info(f"分片后缀播放器不认（{seg_url[-60:]}），改走本机代理")
                    return False
                h2, c2, s2 = self._peek(seg_url, deadline, use_range=False)
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
        headers = {"User-Agent": DESKTOP_UA}
        if use_range:
            headers["Range"] = "bytes=0-1023"
        with ResilientSession() as probe:
            r = probe.get(url, timeout=min(timeout, 6.0), stream=True, headers=headers)
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
        headers = {"User-Agent": DESKTOP_UA, "Referer": referer}
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
              ua: str = DESKTOP_UA, session: requests.Session | None = None,
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
            # 播放列表要读正文（挑最高码率的分片列表）；mpegurl 常挂在 audio/ 前缀下，
            # 别被它骗住。真正的音视频/图片文件正文没有判断价值，不下载。
            if base.startswith(("video/", "audio/", "image/")) and "mpegurl" not in base:
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
            absu = _ascii_url(urljoin(page_url, cand.strip()))
            if absu.lower().startswith(("http://", "https://")):
                streams.append(absu)
        streams = _with_nested(streams)
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
                            headers={"User-Agent": DESKTOP_UA, "Referer": referer,
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
                decode_body(body, r.headers.get("Content-Type", "")))
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
