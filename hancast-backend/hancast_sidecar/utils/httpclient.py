"""对外 HTTP 客户端：让 TLS/JA3 指纹与 UA 说的是同一件事

站点风控做交叉比对时看的不是单个字段，而是「UA、HTTP 头、TLS ClientHello」这三层
是否自洽。以前我们只改了 UA（``utils/identity.py``），TLS 那一层仍是 CPython `ssl`
的指纹 —— 越新的 Chrome UA 配越假的 TLS 反而越显眼。这里把解析链路（含规则包沙箱
的 ``host.*`` 请求）整体换成 curl_cffi，按 UA 反推对应的浏览器指纹；装不上或映射不到
就退回 requests，宁可不提升，也不制造新的不一致。

对上层保持 requests 的语义，换客户端不外泄成 API 差异（curl_cffi 0.16.3 实测）：
- ``Response`` 没有 ``__enter__`` → ``CurlResponse`` 补上（``share_resolver`` 用 ``with r:``）；
- 非 ``stream=True`` 时 ``iter_content`` 直接 assert 报错 → 按 ``content`` 分片；
- ``session.cookies`` / ``response.cookies`` 迭代出来的是 cookie **名字**字符串，
  而沙箱 ``_harvest_cookies`` 要的是带 ``.domain/.value/is_expired()`` 的 Cookie 对象
  → 用 stdlib ``CookieJar.extract_cookies`` 从 ``Set-Cookie`` 头重建（与 requests 同源）；
- 异常是 ``curl_cffi.requests.exceptions.*``，不是 ``requests.RequestException`` 的子类
  → 全部翻译成 requests 那套，否则上层十几处 ``except requests.RequestException`` 全漏。
"""

import logging
import re
from http.cookiejar import CookieJar
from typing import Iterable, Optional, Union
from urllib.parse import urlparse
from urllib.request import getproxies, proxy_bypass

import requests

logger = logging.getLogger("hancast.httpclient")

try:                        # 冻结产物里少一个 .so 就可能装不上，必须探测而不是假设
    from curl_cffi import requests as _curl
    from curl_cffi.requests import exceptions as _curl_ex
    _CURL_OK = True
except Exception as _e:     # noqa: BLE001 - 导入期任何异常都按「不可用」处理
    _curl = None
    _curl_ex = None
    _CURL_OK = False
    _CURL_IMPORT_ERROR = str(_e)

_CURL_IMPORT_ERROR = globals().get("_CURL_IMPORT_ERROR", "")

if _CURL_OK:
    import warnings as _warnings
    from curl_cffi.utils import CurlCffiWarning as _CurlCffiWarning
    # curl 没有「按 N 字节切」这个概念，requests 那套 iter_content(chunk_size=)
    # 每调一次就告一句。我们的读取是按累计字节封顶的，与切片粒度无关，
    # 所以只压这一句 —— 日志查看器里刷屏的 warning 会淹没真信息。
    _warnings.filterwarnings("ignore", message="chunk_size is ignored",
                             category=_CurlCffiWarning)

# 值为空的 proxies 才是「这一趟不经过任何代理」；proxy="" 会被 curl 忽略，
# 它照样去读环境变量里的 http_proxy（实测：死代理下 proxy="" 仍连 127.0.0.1:1）
_NO_PROXY = {"http": "", "https": "", "all": ""}

# requests 侧同理，但它认的是 None 值（select_proxy 会跳过假值）
_REQUESTS_NO_PROXY = {"http": None, "https": None}


def proxy_broken(exc: BaseException) -> bool:
    """这次失败是不是本地代理造成的（macOS 上 requests 会读系统代理配置）"""
    return (isinstance(exc, requests.exceptions.ProxyError)
            or "proxy" in str(exc).lower())


# ── UA → TLS 指纹 ──

_RE_CHROME = re.compile(r"(?:Chrome|Chromium|CriOS)/(\d+)")
_RE_EDGE = re.compile(r"Edg(?:e|A|iOS)?/(\d+)")
_RE_FIREFOX = re.compile(r"(?:Firefox|FxiOS)/(\d+)")
_RE_VERSION = re.compile(r"Version/(\d+)")
_RE_NAME = re.compile(r"([a-z]+)(\d+)(.*)")

_family_cache: dict[str, list[tuple[int, str]]] = {}


def available() -> bool:
    return _CURL_OK


def enabled() -> bool:
    """指纹伪造是否生效：库里装了 curl_cffi，且后台参数没关掉（关掉不用重新编译）"""
    if not _CURL_OK:
        return False
    try:
        from ..params import get as _param
        return bool(_param("http.tls_impersonate"))
    except Exception:       # noqa: BLE001 - 参数表还没登记时按默认开
        return True


def _supported() -> dict[str, list[tuple[int, str]]]:
    """把 curl_cffi 支持的指纹列成 {家族: [(主版本, 目标名), ...]}

    版本表在运行时现读，这样以后升级 curl_cffi 不用回来改映射。
    """
    if _family_cache:
        return _family_cache
    if not _CURL_OK:
        return _family_cache
    import typing
    try:
        from curl_cffi.requests.impersonate import BrowserTypeLiteral
        names = sorted(typing.get_args(BrowserTypeLiteral))
    except Exception:       # noqa: BLE001
        names = []
    for name in names:
        m = _RE_NAME.match(name)
        if not m:
            continue        # chrome133a / safari_beta 这类不规则命名，不做版本推断
        family, digits, tail = m.group(1), m.group(2), m.group(3)
        if family == "safari":
            family = "safari_ios" if "_ios" in tail else "safari"
            # safari153=15.3、safari170=17.0、safari2601=26.0.1：位数决定小数点位置
            major = int(digits) // (10 if len(digits) == 3 else 100 if len(digits) == 4 else 1)
        elif family == "chrome":
            family = "chrome_android" if "_android" in tail else "chrome"
            major = int(digits)
        elif family == "edge":
            major = int(digits)
        elif family == "firefox":
            major = int(digits)
        else:
            continue
        _family_cache.setdefault(family, []).append((major, name))
    for items in _family_cache.values():
        items.sort()
    return _family_cache


def _closest(family: str, major: int) -> str:
    """挑「不超过 UA 声称的主版本」里最新的那个指纹，同主版本再取最旧的次版本

    宁可报旧一点（站点见过、数据多），也不要报一个比 TLS 指纹更新的 UA ——
    后者正是「声称 Chrome/150 却交出 Chrome/124 的握手」这种能被交叉比对的破口。
    """
    items = _supported().get(family) or []
    ok = [n for ver, n in items if ver <= major]
    if not ok:
        return ""
    want = max(ver for ver, _ in items if ver <= major)
    return next(n for ver, n in items if ver == want)


def fingerprint_for(user_agent: str) -> str:
    """按 UA 反推 curl_cffi 的 impersonate 目标；认不出来就返回空（不伪装）"""
    ua = (user_agent or "").strip()
    if not ua or not enabled():
        return ""
    mobile = bool(re.search(r"iPhone|iPad|Android|Mobile", ua))
    m = _RE_EDGE.search(ua)
    if m:
        return _closest("edge", int(m.group(1)))
    m = _RE_FIREFOX.search(ua)
    if m:
        return _closest("firefox", int(m.group(1)))
    m = _RE_CHROME.search(ua)
    if m:
        if mobile and "Android" in ua:
            return _closest("chrome_android", int(m.group(1)))
        if mobile:              # iOS 上的 Chrome 是 WKWebView 套皮，TLS 走的是 Safari
            return _closest("safari_ios", _safari_major(ua))
        return _closest("chrome", int(m.group(1)))
    if "Safari/" in ua or "Version/" in ua:
        family = "safari_ios" if mobile else "safari"
        ios_only = "iPhone" in ua or "iPad" in ua
        if ios_only and family == "safari":
            family = "safari_ios"
        return _closest(family, _safari_major(ua))
    return ""


def _safari_major(ua: str) -> int:
    m = _RE_VERSION.search(ua)
    if not m:
        return 0
    return int(m.group(1))


# ── 异常翻译 ──

_EX_MAP = {
    "ConnectTimeout": requests.exceptions.ConnectTimeout,
    "ReadTimeout": requests.exceptions.ReadTimeout,
    "Timeout": requests.exceptions.Timeout,
    "ProxyError": requests.exceptions.ProxyError,
    "SSLError": requests.exceptions.SSLError,
    "CertificateVerifyError": requests.exceptions.SSLError,
    "DNSError": requests.exceptions.ConnectionError,
    "ConnectionError": requests.exceptions.ConnectionError,
    "TooManyRedirects": requests.exceptions.TooManyRedirects,
    "InvalidURL": requests.exceptions.InvalidURL,
    "MissingSchema": requests.exceptions.MissingSchema,
    "URLRequired": requests.exceptions.URLRequired,
    "InvalidHeader": requests.exceptions.InvalidHeader,
    "InvalidSchema": requests.exceptions.MissingSchema,
    "ChunkedEncodingError": requests.exceptions.ChunkedEncodingError,
    "IncompleteRead": requests.exceptions.ChunkedEncodingError,
    "ContentDecodingError": requests.exceptions.ContentDecodingError,
    "StreamConsumedError": requests.exceptions.StreamConsumedError,
}


def _translate(exc: BaseException) -> requests.RequestException:
    """curl_cffi 的异常体系与 requests 无继承关系，按同名对齐后再抛"""
    target = _EX_MAP.get(type(exc).__name__)
    if target is None:
        target = requests.exceptions.RequestException
    err = target(str(exc))
    err.__cause__ = exc
    return err


# ── Response 适配 ──

class _RequestShim:
    """stdlib CookieJar 要的「请求」，只为填 host-only cookie 的默认域"""

    type = "GET"

    def __init__(self, url: str) -> None:
        self._url = url

    def get_full_url(self) -> str:
        return self._url

    @property
    def origin_req_host(self) -> str:
        return urlparse(self._url).hostname or ""

    def unverifiable(self) -> bool:
        return False


class _ResponseShim:
    def __init__(self, set_cookies: list[str]) -> None:
        self._set_cookies = set_cookies

    def info(self):
        outer = self._set_cookies

        class _Msg:
            def get(self, key, default=None):
                return default

            def get_all(self, key, default=None):
                return outer if key.lower() == "set-cookie" else (default or [])

        return _Msg()


def _cookies_of(raw, url: str) -> CookieJar:
    """从这条响应自己的 Set-Cookie 建罐子（域名语义与 requests 一致）"""
    jar = CookieJar()
    try:
        getter = getattr(raw.headers, "get_list", None)
        set_cookies = list(getter("Set-Cookie")) if getter else []
        if set_cookies:
            jar.extract_cookies(_ResponseShim(set_cookies), _RequestShim(url))
    except Exception:       # noqa: BLE001 - cookie 解析不该连累解析主链路
        logger.debug(f"Set-Cookie 解析失败 {url}")
    return jar


class CurlResponse:
    """包一层 curl_cffi 的 Response，补齐 requests 的用法"""

    def __init__(self, raw, streamed: bool) -> None:
        self._raw = raw
        self._streamed = streamed
        self.cookies = _cookies_of(raw, getattr(raw, "url", "") or "")

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    @property
    def history(self) -> list["CurlResponse"]:
        # 重定向链同样要有「真 Cookie 对象」，沙箱按链收割 cookie
        return [CurlResponse(r, True) for r in (getattr(self._raw, "history", None) or [])]

    def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False) -> Iterable[bytes]:
        if not self._streamed:
            # curl_cffi 在非流式下 iter_content 会 assert；requests 则是从 content 切片
            body = self._raw.content or b""
            chunks = (body[i:i + chunk_size] for i in range(0, len(body), max(chunk_size, 1)))
            return list(chunks)
        return self._raw.iter_content(chunk_size=chunk_size, decode_unicode=decode_unicode)

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception as e:    # noqa: BLE001 - 已经拿到正文，收尾失败不影响结果
            logger.debug(f"关闭响应失败: {type(e).__name__}: {e}")


# ── Session 适配 ──

class CurlSession(_curl.Session if _CURL_OK else object):   # type: ignore[misc,valid-type]
    """requests 风格会话 + 按 UA 选 TLS 指纹 + 代理坏了自动直连"""

    def __init__(self, **kwargs) -> None:
        # 不在构造期固定 impersonate：指纹要跟「这一次请求的 UA」走，由 request() 逐条决定
        self._jar = CookieJar()
        self._bypassed = False
        self._logged_fallback = False
        super().__init__(**kwargs)

    # session.cookies 会被基类的 property 接管，这里显式遮蔽成 requests 那套 jar 语义
    @property
    def cookies(self) -> CookieJar:
        return self._jar

    @cookies.setter
    def cookies(self, value) -> None:
        for cookie in _as_cookies(value):
            self._jar.set_cookie(cookie)

    def _proxy_for(self, url: str) -> Optional[str]:
        """macOS 的系统代理存在 SystemConfiguration 里，curl 只读环境变量

        不补这一刀，「换客户端」就顺带换了网络出口 —— 同一台机器上解析通不通
        取决于它有没有走浏览器那套代理，用户看到的是「昨天还能投今天不行」。
        """
        if self._bypassed:
            return None
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        try:
            if proxy_bypass(parsed.hostname):
                return None
            return getproxies().get(parsed.scheme) or None
        except Exception:       # noqa: BLE001
            return None

    def request(self, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        ua = _header_value(headers, "User-Agent") or _header_value(self.headers, "User-Agent")
        fp = fingerprint_for(ua)
        if fp:
            kwargs["impersonate"] = fp
        else:
            # 认不出来的 UA（自定义导入 UA、老爬虫 UA）就原样发：
            # 伪装成别的浏览器只会让「声称的身份」和「握手的身份」又对不上
            kwargs["impersonate"] = None
        if "proxy" not in kwargs and "proxies" not in kwargs:
            sysp = self._proxy_for(str(url))
            if sysp:
                kwargs["proxy"] = sysp
        elif self._bypassed:
            kwargs["proxies"] = dict(_NO_PROXY)
        stream = bool(kwargs.get("stream"))
        try:
            raw = super().request(method, url, **kwargs)
        except Exception as e:    # noqa: BLE001 - curl 的异常不止 RequestException 一路
            if not self._bypassed and proxy_broken(_translate(e)):
                logger.info(f"本地代理不可用（{type(e).__name__}），改用直连重试")
                self._bypassed = True
                kwargs.pop("proxy", None)
                kwargs["proxies"] = dict(_NO_PROXY)
                try:
                    raw = super().request(method, url, **kwargs)
                except Exception as e2:   # noqa: BLE001
                    raise _translate(e2) from e2
            else:
                raise _translate(e) from e
        resp = CurlResponse(raw, stream)
        for item in [resp] + resp.history:
            for cookie in item.cookies:
                try:
                    self._jar.set_cookie(cookie)
                except Exception:   # noqa: BLE001 - 同名同域重复下发，留一条即可
                    pass
        return resp


def _header_value(headers, name: str) -> str:
    """从 dict 或 [(k, v)] 两种 headers 写法里取值，大小写不敏感"""
    if not headers:
        return ""
    items = headers.items() if hasattr(headers, "items") else headers
    want = name.lower()
    for key, value in items:
        if str(key).lower() == want:
            return str(value or "")
    return ""


def _as_cookies(value) -> list:
    """把 cookies= 支持的几种写法收敛成 Cookie 对象（够读域/名/值即可）"""
    from http.cookiejar import Cookie

    if value is None:
        return []
    if isinstance(value, CookieJar):
        return list(value)
    if isinstance(value, dict):
        items = list(value.items())
    else:
        items = [tuple(x) for x in value]
    out = []
    for name, val in items:
        out.append(Cookie(version=0, name=str(name), value=str(val), port=None,
                          port_specified=False, domain="", domain_specified=False,
                          domain_initial_dot=False, path="/", path_specified=True,
                          secure=False, expires=None, discard=True,
                          comment=None, comment_url=None, rest={}, rfc2109=False))
    return out


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
            if self._bypassed or not proxy_broken(e):
                raise
            logger.info(f"本地代理不可用（{type(e).__name__}），改用直连重试")
            self._bypassed = True
            self.trust_env = False
            # 这里拿到的是已经准备好的 PreparedRequest，不能再 prepare 一次
            # （以前写成 self.prepare_request(request)，requests 会在
            #  `request.cookies` 上抛 AttributeError，等于「直连重试」从没生效过，
            #  用户看到的是解析失败）。值为 None 的键会被 requests 当成不代理。
            kwargs["proxies"] = _REQUESTS_NO_PROXY
            return super().send(request, **kwargs)


# 两种会话对外是同一套 requests 语义（get/request/cookies/with），上层只按这个联合类型标注
HttpSession = Union[requests.Session, "CurlSession", ResilientSession]


_logged_reason = False


def make_session():
    """解析链路的会话工厂：能用 curl_cffi 就用（TLS 指纹跟 UA 一致），否则退回 requests

    沙箱（``media/rules/jsruntime.py``）用的就是这里返回的对象，
    所以换客户端只需要改这一处，规则包与声明式规则都不用动。
    """
    global _logged_reason
    if _CURL_OK and enabled():
        try:
            session = CurlSession()
        except Exception as e:    # noqa: BLE001 - 引擎初始化失败也不能拖垮解析
            if not _logged_reason:
                _logged_reason = True
                logger.warning(f"curl_cffi 会话创建失败（{type(e).__name__}: {e}），本次解析用 requests")
            return ResilientSession()
        if not _logged_reason:
            _logged_reason = True
            logger.info("对外请求走 curl_cffi：TLS/JA3 指纹按 UA 对应浏览器发出")
        return session
    if not _logged_reason and not _CURL_OK:
        _logged_reason = True
        logger.info(f"未安装 curl_cffi（{_CURL_IMPORT_ERROR or 'import 失败'}），"
                    "TLS 指纹保持 Python 默认")
    return ResilientSession()
