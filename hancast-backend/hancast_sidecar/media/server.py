"""
本地媒体文件 HTTP 服务

功能:
- 将本地文件通过 HTTP 提供给 DLNA 设备访问
- 支持 Range 请求 (视频拖拽)
- 支持远程 URL 代理 (附加 Referer 头)
- 自动端口选择
"""

import os
import re
import json
import hashlib
import logging
import threading
import requests as http_requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
from typing import Dict, Tuple

from ..params import get as param

logger = logging.getLogger("hancast.media.server")


def _upstream_timeout() -> float:
    """代理单次上游请求的超时（params: media.http_timeout_seconds）"""
    return float(param("media.http_timeout_seconds"))


# 代理允许的域名后缀白名单（匹配 *.suffix）
PROXY_ALLOWED_SUFFIXES = {
    "bilivideo.com", "hdslb.com", "akamaized.net",
    "bilibili.com", "biliapi.net", "mountaintoys.cn",
}

# 解析器注册的外部站点（不在白名单内）不允许指向内网/回环，
# 避免局域网 HTTP 服务被当成访问路由器、metadata 端点的跳板
_PRIVATE_HOST_PREFIXES = ("127.", "10.", "169.254.", "192.168.", "0.")

_PROXY_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# requests 会把值为 None 的键从合并结果里删掉，等于「这一次不走任何代理」
_NO_PROXY = {"http": None, "https": None}


def _proxy_broken(exc: Exception) -> bool:
    """这次失败是不是本地代理造成的（macOS 上 requests 会读系统代理配置）"""
    return (isinstance(exc, http_requests.exceptions.ProxyError)
            or "proxy" in str(exc).lower())


def upstream_get(url: str, **kwargs):
    """系统代理坏了就改直连，而不是让代理整段 502

    本机这类 127.0.0.1 代理端口常只服务于浏览器，它一失效，第一个请求就是
    ConnectionError —— 用户看到的现象是「同一个链接昨天能投今天不能」。
    仍然先按环境配置走代理：公司网络里直连不通的用户不受影响。
    """
    try:
        return http_requests.get(url, **kwargs)
    except http_requests.RequestException as e:
        if kwargs.get("proxies") is not None or not _proxy_broken(e):
            raise
        logger.info(f"本地代理不可用（{type(e).__name__}），改用直连重试")
        return http_requests.get(url, **{**kwargs, "proxies": _NO_PROXY})


def _is_private_host(host: str) -> bool:
    h = (host or "").lower()
    if h in ("localhost", "0.0.0.0"):
        return True
    if any(h.startswith(p) for p in _PRIVATE_HOST_PREFIXES):
        return True
    # 172.16.0.0/12
    if h.startswith("172."):
        try:
            return 16 <= int(h.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def _playlist_header(base_headers: dict) -> dict:
    """播放列表专用请求头：Accept 明确一点，某些站点会因此走 CDN 缓存"""
    h = dict(base_headers or {})
    h.setdefault("Accept", "application/vnd.apple.mpegurl, */*")
    return h


def _looks_like_playlist(ctype: str, url: str) -> bool:
    c = (ctype or "").lower()
    if "mpegurl" in c or "m3u8" in c:
        return True
    seg = urlparse(url).path.lower().rsplit("/", 1)[-1]
    return seg.endswith(".m3u8") or seg == "m3u8"


# mpv/ffmpeg 的 hls.c 与多数电视的 HLS 客户端会按「分片 URL 的后缀」白名单决定
# 拉不拉这个分片，无后缀或 .png 这类一律 "blocked for security reasons"。
# 采集站爱把 TS 分片伪装成 .png 传到图片 CDN 上（file.icve.com.cn 等），
# 所以代理改写列表时必须给路径换一个播放器认账的后缀。
_PLAYER_SEGMENT_EXTS = frozenset("""
ts m2ts mp2t mp4 m4s m4v mov aac ac3 ec3 eac3 mp3 mp2 mkv mka webm ogg ogx
opus flac wav wma wmv flv f4f m3u8""".split())
_SEG_EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,5})(?:[?#]|$)")


def player_ext_ok(url: str) -> bool:
    """分片子列表的后缀是否落在播放器的白名单里（无后缀也算不合格）"""
    m = _SEG_EXT_RE.search(urlparse(url).path.rsplit("/", 1)[-1])
    return bool(m) and m.group(1).lower() in _PLAYER_SEGMENT_EXTS


def player_safe_suffix(url: str) -> str:
    """代理路径该带的后缀：远端后缀本身合规就用它，否则按类型兜底

    只改「看起来的后缀」，字节仍是上游原样 —— 播放器靠魔数判型，实测带
    61 字节假 PNG 头的 TS 照样能解出 10.5 秒。
    """
    if _looks_like_playlist("", url):
        return ".m3u8"
    seg = urlparse(url).path.rsplit("/", 1)[-1]
    m = _SEG_EXT_RE.search(seg)
    ext = m.group(1).lower() if m else ""
    return f".{ext}" if ext in _PLAYER_SEGMENT_EXTS else ".ts"


_LEECH_PLACEHOLDER_PREFIXES = (b"\x89png", b"\xff\xd8", b"gif8", b"<html", b"<!doc", b"bm")
_RANGE_FALLBACK_END = 4294967295          # 4G-1，足够覆盖任何分片，服务端会钳到文件尾


def _is_leech_placeholder(head: bytes) -> bool:
    """首块是否为防盗链占位（海报图 / 提示网页）

    有的站点给「不带 Range 的整段 GET」回占位图，只有 Range 请求才吐真分片，
    而电视拉 TS 普遍不带 Range —— 代理需要替它补上。
    """
    return head.lower().lstrip(b"\xef\xbb\xbf \r\n\t")[:6].startswith(
        _LEECH_PLACEHOLDER_PREFIXES)


def _content_range_total(value: str) -> int | None:
    """Content-Range: bytes 0-123/124 → 124"""
    m = re.search(r"/(\d+)\s*$", value or "")
    return int(m.group(1)) if m else None


_SYNC = 0x47
_TS_PACKET = 188
# 假图片头的长度按站点差得很远：icve 实测 61 字节，dyrs/低端影视是 583 字节
# （整段 1.7MB 的分片只有开头这截是 PNG）。窗口一窄就等于没修 —— 播放器会按
# PNG 魔数选 png_pipe 解码器，然后 mpv 报 "no audio or video data played"。
# v2.2.20 就是栽在窗口只有 400：列表照转、分片全哑，日志里却看不出破了。
_DISGUISE_SCAN = 2048        # 上游不说自己是图片时的判型窗口
_DISGUISE_SCAN_IMAGE = 16384  # 上游自称 image/*：这就是「皮」的确认信号，多等 16KB
_DISGUISE_PACKETS = 6        # 要连续 6 个包都对齐才认，防真海报图里蒙对同步字节
_FAKE_IMAGE_MAGICS = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def _disguise_peek_len(ctype: str) -> int:
    """判型前要向上游取多少字节：图片皮有多长只能靠上游自己报的类型来赌窗口

    窗口按 Content-Type 分档，是为了不给普通分片凭空加几 KB 的首字节延迟。
    """
    scan = _DISGUISE_SCAN_IMAGE if ctype.lower().startswith("image") else _DISGUISE_SCAN
    return scan + 12 + _DISGUISE_PACKETS * _TS_PACKET


def _disguise_offset(head: bytes) -> int:
    """「披着图片皮的 TS」里图片头有多少字节；不是这种分片就返回 0

    采集站把分片当图片传到免费 CDN（file.icve.com.cn、box.dyrs.com.de 等）：
    开头是一段合法的 PNG 头（IHDR+IDAT），之后才是 188 字节对齐的 TS。后缀换成
    .ts 只够骗过后缀白名单 —— mpv 内的 ffmpeg 会用 analyzeduration 有限的首块
    探测，看到 PNG 魔数就判成 png 解码器然后报 "error reading packet"，
    所以代理转发时要把这段伪装头剥掉，让播放器从同步字节开始读。

    扫描范围只看传进来的头有多长（窗口由 _disguise_peek_len 决定）：6 个包在
    188 步长上全中同步字节的误判概率约 (1/256)^5，头部多取几 KB 也不会把真海报
    图误认成皮。
    """
    if not head.startswith(_FAKE_IMAGE_MAGICS):
        return 0
    need = 12 + _DISGUISE_PACKETS * _TS_PACKET
    if len(head) < need:
        return 0
    for off in range(1, len(head) - need + 1):
        if all(head[off + _TS_PACKET * i] == _SYNC
               for i in range(_DISGUISE_PACKETS)):
            return off
    return 0


def _disguised_ts(head: bytes) -> bool:
    return _disguise_offset(head) > 0


def _needs_bare_retry(headers: dict) -> bool:
    """页面域请求头是否可能正是被拒绝的原因（Referer/Origin/Cookie 全空再试一次）"""
    lowered = {k.lower() for k in headers}
    return bool(lowered & {"referer", "origin", "cookie"})


def _rewrite_playlist(text: str, base_url: str, headers: dict) -> str:
    """把 m3u8 里的每一处地址换成本机代理路径

    电视不会带 Cookie/Referer，所以列表与分片（含 EXT-X-KEY / EXT-X-MAP）
    都必须经过本机代理，否则拉到的就是防盗链占位图。
    """
    from urllib.parse import urljoin

    def _local(remote: str) -> str:
        return f"{MediaHandler.base_url}" \
               f"{MediaHandler.register(remote, headers, player_safe_suffix(remote))}"

    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
            # URI="..." 出现在 EXT-X-KEY / EXT-X-MAP / EXT-X-MEDIA 中
            out.append(re.sub(
                r'(URI=)"([^"]+)"',
                lambda m: f'{m.group(1)}"{_local(urljoin(base_url, m.group(2)))}"',
                line))
            continue
        out.append(_local(urljoin(base_url, stripped)))
    return "\n".join(out) + "\n"


class MediaHandler(BaseHTTPRequestHandler):
    """媒体文件 HTTP 处理器"""

    served_files: Dict[str, str] = {}        # url_path → file_path
    proxy_targets: Dict[str, str] = {}       # url_path → remote_url
    proxy_headers: Dict[str, dict] = {}      # url_path → 转发请求头（Cookie/Referer…）
    base_url: str = ""                        # http://<局域网IP>:<端口>，改写列表时拼绝对地址

    _BILI_HEADERS = {
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }

    @classmethod
    def register(cls, remote_url: str, headers: dict = None,
                 suffix: str = "") -> str:
        """登记一个代理目标，返回本机相对路径（调用方拼上 base_url）

        suffix 只影响路径看起来的扩展名（HLS 客户端按后缀白名单决定拉不拉分片），
        转发的字节仍是上游原样。
        """
        url_hash = hashlib.md5(
            (remote_url + json.dumps(headers or {}, sort_keys=True)).encode()).hexdigest()[:12]
        path = f"/proxy/{url_hash}{suffix}"
        cls.proxy_targets[path] = remote_url
        if headers:
            cls.proxy_headers[path] = headers
        else:
            cls.proxy_headers.pop(path, None)
        _prune_registry(cls.proxy_targets, MAX_PROXY_ENTRIES)
        # 分片被挤掉时同步清掉其请求头，避免头字典只增不减
        for stale in [k for k in cls.proxy_headers if k not in cls.proxy_targets]:
            del cls.proxy_headers[stale]
        return path

    def do_HEAD(self):
        """处理 HEAD 请求 (DLNA 设备用来探测文件)"""
        file_path = self.served_files.get(self.path.split("?")[0])
        if not file_path or not os.path.exists(file_path):
            self.send_error(404)
            return

        file_size = os.path.getsize(file_path)
        mime_type = self._guess_mime(file_path)

        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        # 代理路由: /proxy/{hash}?url=xxx
        if self.path.startswith("/proxy/"):
            self._handle_proxy()
            return

        # 忽略 query string（某些 DLNA 设备会在 URL 后附加参数）
        file_path = self.served_files.get(self.path.split("?")[0])
        if not file_path or not os.path.exists(file_path):
            self.send_error(404)
            return

        file_size = os.path.getsize(file_path)
        mime_type = self._guess_mime(file_path)

        # 处理 Range 请求；解析失败/无效时回退全量 200（兼容性优先）
        range_header = self.headers.get("Range")
        range_result = self._parse_range(range_header, file_size) if range_header else None
        if range_result:
            start, end = range_result
            self.send_response(206)
            self.send_header("Content-Type", mime_type)
            self.send_header(
                "Content-Range",
                f"bytes {start}-{end}/{file_size}"
            )
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(8192, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (ConnectionResetError, BrokenPipeError):
                        return  # 客户端断开，静默退出
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            with open(file_path, "rb") as f:
                while chunk := f.read(8192):
                    try:
                        self.wfile.write(chunk)
                    except (ConnectionResetError, BrokenPipeError):
                        return  # 客户端断开，静默退出

    def _parse_range(self, header: str, file_size: int):
        """解析 Range 头，返回 (start, end) 或 None（无效时回退全量）

        只支持单个 bytes 范围；多范围、非法格式、越界值统一返回 None。
        """
        try:
            if not header or not header.lower().startswith("bytes="):
                return None
            range_spec = header[6:].strip()
            # 多范围请求（逗号分隔）不支持，回退全量
            if "," in range_spec:
                return None
            if range_spec.startswith("-"):
                # 后缀范围: bytes=-N → 最后 N 个字节
                n = int(range_spec[1:])
                if n <= 0:
                    return None
                start = max(file_size - n, 0)
                end = file_size - 1
            elif range_spec.endswith("-"):
                # 前缀范围: bytes=N- → 从 N 到结尾
                start = int(range_spec[:-1])
                end = file_size - 1
            else:
                start, end = range_spec.split("-", 1)
                start, end = int(start), int(end)

            # 钳制到合法范围；start >= file_size 或 start > end 视为无效
            if start < 0 or end < start or start >= file_size:
                return None
            if end >= file_size:
                end = file_size - 1
            return start, end
        except (ValueError, TypeError):
            return None

    def _guess_mime(self, path: str) -> str:
        import mimetypes
        mime, _ = mimetypes.guess_type(path)
        return mime or "application/octet-stream"

    def _handle_proxy(self):
        """处理代理请求: 从 proxy_targets 中查找远程 URL 并转发

        两种目标:
        - 白名单站点（B站系）：附加固定的 Referer
        - 解析器带请求头注册的外部站点：原样携带 Cookie/Referer（站点用它做防盗链）
        播放列表会被改写成本机代理地址，电视拉分片时同样经由这里补上请求头。
        """
        path = self.path.split("?")[0]
        remote_url = self.proxy_targets.get(path)
        if not remote_url:
            logger.warning(f"Proxy target not found: {self.path}")
            self.send_error(404, "Proxy target not found")
            return

        host = urlparse(remote_url).hostname or ""
        # 命中即置尾：直播列表反复拉取时，正在用的分片不该被上限挤掉
        self.proxy_targets[path] = remote_url
        whitelisted = any(host == h or host.endswith("." + h)
                          for h in PROXY_ALLOWED_SUFFIXES)
        reg_headers = self.proxy_headers.get(path)
        if not whitelisted:
            if _is_private_host(host):
                logger.warning(f"Blocked internal proxy target: {host}")
                self.send_error(403, "Forbidden domain")
                return
            if reg_headers is None:
                logger.warning(f"Proxy target without headers: {host}")
                self.send_error(403, "Forbidden domain")
                return
        headers = dict(self._BILI_HEADERS if whitelisted and reg_headers is None
                       else reg_headers or {})
        headers.setdefault("User-Agent", _PROXY_UA)

        logger.info(f"Proxy request: {self.path} → {remote_url[:100]}...")

        # 转发 Range 请求头（播放列表不需要，改写后的子请求自带上层头）
        range_val = self.headers.get("Range")
        if range_val and not _looks_like_playlist("", remote_url):
            headers["Range"] = range_val

        try:
            resp = upstream_get(remote_url, headers=headers, timeout=_upstream_timeout(), stream=True)
            if resp.status_code >= 400 and _needs_bare_retry(headers):
                # 另一类站点正好相反：把「页面域的 Referer」当盗链特征拒掉
                # （file.icve.com.cn 带 4kcz 的 Referer 回 403，裸 UA 才给量）
                denied = resp.status_code
                resp.close()
                logger.info(f"代理带页面头被拒 {denied}，改用裸 UA 重试: {host}")
                bare = {"User-Agent": _PROXY_UA}
                if range_val:
                    # 裸头只甩掉防盗链字段，Range 必须原样保留，否则电视的
                    # 分段请求会拿到整条资源却仍是 206 语义之外的响应
                    bare["Range"] = range_val
                resp = upstream_get(remote_url, headers=bare,
                                    timeout=_upstream_timeout(), stream=True)
        except Exception as e:
            logger.error(f"代理请求失败: {e}")
            self.send_error(502, f"Proxy Error: {e}")
            return

        logger.info(f"Proxy response: status={resp.status_code}, "
                     f"content-type={resp.headers.get('Content-Type')}, "
                     f"content-length={resp.headers.get('Content-Length')}")

        try:
            ctype = resp.headers.get("Content-Type", "")
            if _looks_like_playlist(ctype, resp.url):
                self._send_rewritten_playlist(resp, headers)
                return

            first = b""
            full_len: int | None = None
            rng = headers.get("Range", "")
            if not rng or rng.startswith("bytes=0-"):
                # 只有从文件头开始的请求能判伪装头/占位图：播放器（mpv、浏览器）
                # 几乎必然带 bytes=0-… 的 Range，不覆盖这条分支就等于永远不剥
                first = self._peek_upstream(resp, _disguise_peek_len(ctype))
                if (_is_leech_placeholder(first) and not _disguised_ts(first)
                        and (not rng or rng.rstrip().endswith("-"))):
                    resp.close()
                    # 必须是闭合区间：这些站点按 end 是否为空判断「像不像播放器」，
                    # bytes=0- 会被判定为非法并 302 到推广图
                    resp = upstream_get(
                        remote_url, headers={**headers, "Range": f"bytes=0-{_RANGE_FALLBACK_END}"},
                        timeout=_upstream_timeout(), stream=True)
                    ctype = resp.headers.get("Content-Type", ctype)
                    full_len = _content_range_total(resp.headers.get("Content-Range"))
                    first = self._peek_upstream(resp, _disguise_peek_len(ctype))

            skip = _disguise_offset(first)
            if skip:
                logger.info(f"分片带 {skip} 字节图片伪装头，转发前剥掉: {host}")
                first = first[skip:]
                # 类型也一并纠正：播放器会按 Content-Type 选解封装器，
                # 留着 image/png 它就去找 PNG 解码器了
                ctype = "video/mp2t"
            elif _is_leech_placeholder(first):
                # 开头是图片/HTML 魔数，可判型窗口内又找不到 TS 对齐：要么是真推广
                # 占位图，要么皮长过了窗口。这两种播放器都会哑，而日志里必须留下
                # 区别，否则下轮又只能人肉猜「有列表、画面全黑」到底是哪一种
                logger.warning(
                    f"分片以图片/HTML 魔数开头且 {_disguise_peek_len(ctype)} 字节窗口内"
                    f"无 TS 对齐，原样透传（播放器多半报 no audio or video data）: {host}")

            out_status = 200 if full_len else resp.status_code
            resp_headers = {
                "Content-Type": ctype or "video/mp4",
                "Access-Control-Allow-Origin": "*",
            }
            if full_len:
                # 上游被我们驱动成了分段请求，对电视仍按完整资源应答
                resp_headers["Content-Length"] = str(full_len - skip)
                resp_headers["Accept-Ranges"] = "bytes"
            elif skip:
                # 伪装头一剥，上游报的区间起点就不再是播放器请求的那个偏移，
                # ffmpeg 会直接判 "Unexpected offset" 拒收 —— 改成整条资源应答，
                # 只保留修正后的长度，不带 Content-Range
                out_status = 200
                length = resp.headers.get("Content-Length", "")
                if length.isdigit():
                    resp_headers["Content-Length"] = str(int(length) - skip)
                if "Accept-Ranges" in resp.headers:
                    resp_headers["Accept-Ranges"] = resp.headers["Accept-Ranges"]
            else:
                for h in ("Content-Range", "Accept-Ranges", "Content-Length"):
                    if h in resp.headers:
                        resp_headers[h] = resp.headers[h]

            self.send_response(out_status)
            for k, v in resp_headers.items():
                self.send_header(k, v)
            self.end_headers()

            # 流式转发响应体（判型时读过的前 1KB 先补回去）
            total_bytes = 0
            if first:
                self.wfile.write(first)
                total_bytes += len(first)
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    self.wfile.write(chunk)
                    total_bytes += len(chunk)
            logger.info(f"Proxy done: {total_bytes} bytes forwarded")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass  # 客户端断开连接，正常
        except Exception as e:
            logger.error(f"代理转发失败: {e}")
            try:
                self.send_error(502, f"Proxy Error: {e}")
            except Exception:
                pass
        finally:
            resp.close()

    @staticmethod
    def _peek_upstream(resp, want: int = _disguise_peek_len("")) -> bytes:
        """取上游开头若干字节（至少够判型），返回的就是「已经从流里读走的」全部字节

        分块编码下首个 chunk 可能只有几十字节，而「图片伪装头 + 连续对齐的 TS 包」
        的判定至少要扫到判型窗口再留出几个包，所以按目标长度累计而不是取一块就返回。
        窗口多深由 _disguise_peek_len 按上游 Content-Type 决定。

        **不许截断成 want 字节**：调用方把返回值当作响应体的开头写回客户端，
        多读的那点（chunk 对齐产生的 ≤1KB）一裁就从流中间挖掉一段。B 站 mp4 的
        moov 在文件尾，中间少 908 字节 mpv 直接 "Errors when loading file"，
        表现为「解析成功、投屏黑屏」。
        """
        buf = b""
        for chunk in resp.iter_content(chunk_size=1024):
            buf += chunk
            if len(buf) >= want:
                break
        return buf

    def _send_rewritten_playlist(self, resp, headers: dict):
        text = resp.content.decode("utf-8", errors="replace")
        if not text.lstrip().startswith("#EXTM3U"):
            # 站点用列表接口返回了 HTML（防盗链页），原样透传让上层判死
            self.send_response(resp.status_code)
            self.send_header("Content-Type",
                             resp.headers.get("Content-Type", "text/plain"))
            self.end_headers()
            self.wfile.write(text.encode("utf-8", errors="replace"))
            return
        child_headers = {k: v for k, v in headers.items() if k.lower() != "range"}
        data = _rewrite_playlist(text, resp.url, child_headers).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        # 直播列表每秒重生成，缓存会让电视停在旧分片上
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        logger.info(f"Proxy playlist rewritten: {resp.url[:80]}")

    def log_message(self, format, *args):
        logger.debug(format % args)


# 代理/文件注册表上限：防止反复投屏导致字典无限增长（内存泄漏）
MAX_REGISTRY_ENTRIES = 100
# 改写后的播放列表会把每个分片登记成一条代理，两小时 13s 分片 ≈ 550 条；
# 上限太小会在播放中途被挤掉，电视直接 404
MAX_PROXY_ENTRIES = 4000


def _prune_registry(registry: dict, limit: int = MAX_REGISTRY_ENTRIES):
    """超出上限时删除最旧的条目（dict 保持插入顺序）"""
    while len(registry) > limit:
        oldest = next(iter(registry))
        del registry[oldest]


class MediaServer:
    """媒体文件 HTTP 服务器"""

    def __init__(self, host: str = "0.0.0.0", port: int = 0, lan_ip: str = None):
        self.host = host
        self.port = port
        self._server: HTTPServer = None
        self._thread: threading.Thread = None
        self._served: Dict[str, str] = {}
        self._lan_ip = lan_ip or self._get_local_ip()

    @staticmethod
    def _get_local_ip() -> str:
        """获取本机局域网 IP"""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def start(self, port: int = 0):
        """启动服务器"""
        self.port = port or self._find_free_port()
        # 改写播放列表时给电视拼绝对地址用
        MediaHandler.base_url = f"http://{self._lan_ip}:{self.port}"

        class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        self._server = ThreadedHTTPServer(
            (self.host, self.port), MediaHandler
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        logger.info(f"Media server started on {self.host}:{self.port}")

    def stop(self):
        """停止服务器"""
        if self._server:
            self._server.shutdown()

    def serve_file(self, file_path: str) -> str:
        """将本地文件注册为可访问的 URL，返回 HTTP URL"""
        url_hash = hashlib.md5(file_path.encode()).hexdigest()[:12]
        url_path = f"/media/{url_hash}"

        MediaHandler.served_files[url_path] = file_path
        self._served[url_path] = file_path
        _prune_registry(MediaHandler.served_files)
        _prune_registry(self._served)

        return f"http://{self._lan_ip}:{self.port}{url_path}"

    def register_proxy(self, remote_url: str, headers: dict = None,
                       suffix: str = "") -> str:
        """注册远程 URL 代理，返回本地代理 URL

        headers 为空时走白名单站点默认（B站系 Referer）；外部站点必须显式带上
        Cookie/Referer，否则代理出去同样被防盗链挡掉。
        suffix 用于给播放器一个合规的后缀（见 player_safe_suffix）。
        """
        path = MediaHandler.register(remote_url, headers, suffix)
        return f"http://{self._lan_ip}:{self.port}{path}"

    def get_file(self, file_id: str) -> Tuple[str, int, str]:
        """获取文件信息"""
        url_path = f"/media/{file_id}"
        file_path = self._served.get(url_path)
        if not file_path or not os.path.exists(file_path):
            return None, 0, ""

        file_size = os.path.getsize(file_path)
        import mimetypes
        mime_type, _ = mimetypes.guess_type(file_path)
        return file_path, file_size, mime_type or "application/octet-stream"

    def _find_free_port(self) -> int:
        import socket
        with socket.socket() as s:
            s.bind(("", 0))
            return s.getsockname()[1]
