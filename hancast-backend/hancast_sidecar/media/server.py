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

logger = logging.getLogger("hancast.media.server")


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


def _rewrite_playlist(text: str, base_url: str, headers: dict) -> str:
    """把 m3u8 里的每一处地址换成本机代理路径

    电视不会带 Cookie/Referer，所以列表与分片（含 EXT-X-KEY / EXT-X-MAP）
    都必须经过本机代理，否则拉到的就是防盗链占位图。
    """
    from urllib.parse import urljoin

    def _local(remote: str) -> str:
        return f"{MediaHandler.base_url}{MediaHandler.register(remote, headers)}"

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
    def register(cls, remote_url: str, headers: dict = None) -> str:
        """登记一个代理目标，返回本机相对路径（调用方拼上 base_url）"""
        url_hash = hashlib.md5(
            (remote_url + json.dumps(headers or {}, sort_keys=True)).encode()).hexdigest()[:12]
        path = f"/proxy/{url_hash}"
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
            resp = http_requests.get(remote_url, headers=headers, timeout=30, stream=True)
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
            if "Range" not in headers:
                # 电视不带 Range：先看首块是不是防盗链占位
                first = self._peek_upstream(resp)
                if _is_leech_placeholder(first):
                    resp.close()
                    # 必须是闭合区间：这些站点按 end 是否为空判断「像不像播放器」，
                    # bytes=0- 会被判定为非法并 302 到推广图
                    resp = http_requests.get(
                        remote_url, headers={**headers, "Range": f"bytes=0-{_RANGE_FALLBACK_END}"},
                        timeout=30, stream=True)
                    ctype = resp.headers.get("Content-Type", ctype)
                    full_len = _content_range_total(resp.headers.get("Content-Range"))
                    first = self._peek_upstream(resp)

            out_status = 200 if full_len else resp.status_code
            resp_headers = {
                "Content-Type": ctype or "video/mp4",
                "Access-Control-Allow-Origin": "*",
            }
            if full_len:
                # 上游被我们驱动成了分段请求，对电视仍按完整资源应答
                resp_headers["Content-Length"] = str(full_len)
                resp_headers["Accept-Ranges"] = "bytes"
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
    def _peek_upstream(resp) -> bytes:
        """取上游首块（只够判型），后续 iter_content 会从断点继续"""
        for chunk in resp.iter_content(chunk_size=1024):
            return chunk[:1024]
        return b""

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

    def register_proxy(self, remote_url: str, headers: dict = None) -> str:
        """注册远程 URL 代理，返回本地代理 URL

        headers 为空时走白名单站点默认（B站系 Referer）；外部站点必须显式带上
        Cookie/Referer，否则代理出去同样被防盗链挡掉。
        """
        path = MediaHandler.register(remote_url, headers)
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
