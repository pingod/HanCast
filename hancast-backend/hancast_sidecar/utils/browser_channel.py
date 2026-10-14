"""浏览器通道：规则包借用主进程的 WebView 渲染页面、嗅探流地址、同步 Cookie

反爬站点里越来越多的是「地址不在 HTML 里」——播放器先调一个签名接口再拿流，
纯 HTTP 抓页永远拿不到。ssts 三端的答案都是同一个：开一个真浏览器内核渲染，
一边渲染一边嗅（见 docs/反爬实现取证与浏览器通道.md）。这里走同样的路子，
但内核是宿主 Tauri 已有的 WebView（macOS=WKWebView，Windows=WebView2），
不额外带一个 Chromium。

链路（两端各一半，靠 stdio 协议串起来）：

    sidecar 解析线程            Rust 主进程
        │  emit_event("browser_request", {request_id, url, options})
        │ ────────────────────────────►│  建离屏 WebView 窗口加载地址
        │                              │  注入嗅探脚本 + 截网络请求
        │  ◄────────────────────────────│  加载完成后再等 settle_ms
        │  send_command("browser_result",│  eval 收 HTML/候选地址/Cookie
        │            {request_id, ...}) │
        ▼                              ▼
    threading.Event 唤醒，结果回到沙箱

三条约束是这个形状的理由，改动前先看：

- **必须由 sidecar 主动发起**：站点判定发生在规则执行中，前端不知道要渲染谁。
  Rust 的 ``reader_loop`` 现在是「凡是事件一律转给前端」，所以 ``browser_request``
  在 Rust 侧被单独拦下来，并且**必须另起任务处理**——在读 stdout 的线程里等结果
  会把 ``browser_result`` 的响应一起堵死。
- **只能阻塞工作线程**：解析跑在 ``RESOLVE_*`` daemon 线程上，stdin 循环照常应答
  其它命令；沙箱那边 QuickJS 是钉线程的，host.render 卡住只废这一次调用。
- **预算从解析总时长里扣**：一次渲染的超时 = min(剩余预算, browser.timeout_s)，
  超时后短路整个通道（``_open_until``），免得每条规则都再烧 12 秒等一个不会来的回答。
"""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .. import params
from .identity import desktop_ua, mobile_ua
from .protocol_io import emit_event

logger = logging.getLogger("hancast.browser")

# 熔断时长：一次超时说明宿主那头多半不认这个通道（老 Rust 端、窗口建不起来），
# 没必要让后面每条规则都再等一轮
_COOLDOWN_S = 90.0
# 应答比超时多一点宽裕：Rust 侧算完还要走一次命令回传
_SLACK_S = 2.0

_MAX_CANDIDATES = 120
_MAX_COOKIE_CHARS = 6000

_lock = threading.Lock()
_pending: Dict[str, dict] = {}
_open_until = 0.0
_stats = {"calls": 0, "timeouts": 0, "errors": 0, "last_ms": 0.0}


@dataclass
class RenderResult:
    """一次渲染的产物（沙箱拿到的就是这个）"""

    ok: bool
    error: str = ""
    html: str = ""
    final_url: str = ""
    title: str = ""
    ua: str = ""
    candidates: List[str] = field(default_factory=list)
    cookies: List[dict] = field(default_factory=list)
    elapsed_ms: int = 0


def enabled() -> bool:
    return bool(params.get("browser.enabled"))


def available() -> bool:
    """通道此刻能不能用（关掉或正在熔断就不行）"""
    if not enabled():
        return False
    return time.monotonic() >= _open_until


def _budget(deadline: Optional[float]) -> float:
    """本次渲染的等待上限：参数上限与解析剩余预算取小，且留出一个网络往返"""
    cap = max(1.0, min(float(params.get("browser.timeout_s")), 25.0))
    if deadline:
        left = deadline - time.monotonic()
        if left <= 1.0:
            raise TimeoutError("解析超时（浏览器通道没有剩余预算）")
        cap = min(cap, max(1.0, left - 1.0))
    return cap


def render(url: str, *, want_html: bool = True, mobile: bool = False,
           settle_ms: Optional[int] = None,
           deadline: Optional[float] = None) -> RenderResult:
    """渲染一个页面并嗅探候选地址；失败/超时返回 ok=False 并说明原因

    ``deadline`` 传解析总预算（``time.monotonic()` 基准）时，超时会自动收到
    预算里，不会让一次渲染把整条解析拖过线。
    """
    global _open_until

    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("浏览器通道只接受 http(s) 地址")
    if not available():
        raise ValueError("浏览器通道不可用（参数已关闭，或刚刚超时在冷却）")

    timeout_s = _budget(deadline)
    options = {
        "want_html": bool(want_html),
        "mobile": bool(mobile),
        # UA 在这里定，不在 Rust 侧另算一份：浏览器身份与 HTTP 身份必须同一个来源
        "ua": mobile_ua() if mobile else desktop_ua(),
        "settle_ms": int(settle_ms if settle_ms is not None
                         else params.get("browser.settle_ms")),
        "timeout_s": timeout_s,
    }
    request_id = uuid.uuid4().hex[:16]
    waiter = {"event": threading.Event(), "result": None}
    with _lock:
        _pending[request_id] = waiter
        _stats["calls"] += 1

    started = time.monotonic()
    emit_event("browser_request", {"request_id": request_id, "url": url,
                                   "options": options})
    got = waiter["event"].wait(timeout_s + _SLACK_S)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    with _lock:
        _pending.pop(request_id, None)
        payload = waiter["result"] if got else None

    if not got or not isinstance(payload, dict):
        with _lock:
            _stats["timeouts"] += 1
            _open_until = time.monotonic() + _COOLDOWN_S
        logger.warning(f"浏览器通道超时（{elapsed_ms}ms, {url[:80]}），"
                       f"冷却 {_COOLDOWN_S:.0f}s")
        return RenderResult(False, error="宿主未在时限内回应（WebView 渲染超时或不可用）",
                            elapsed_ms=elapsed_ms)

    result = _parse_payload(payload, elapsed_ms)
    if not result.ok:
        with _lock:
            _stats["errors"] += 1
        logger.info(f"浏览器通道失败 {url[:80]}: {result.error}")
    with _lock:
        _stats["last_ms"] = float(elapsed_ms)
    return result


def _parse_payload(payload: dict, elapsed_ms: int) -> RenderResult:
    error = str(payload.get("error") or "")
    cands = [str(u)[:600] for u in (payload.get("candidates") or [])
             if isinstance(u, str) and u][:_MAX_CANDIDATES]
    cookies = _clean_cookies(payload.get("cookies") or [])
    return RenderResult(
        ok=bool(payload.get("ok", True)) and not error,
        error=error or ("宿主未给出原因" if not payload.get("ok", True) else ""),
        html=str(payload.get("html") or ""),
        final_url=str(payload.get("finalUrl") or ""),
        title=str(payload.get("title") or ""),
        ua=str(payload.get("ua") or ""),
        candidates=cands,
        cookies=cookies,
        elapsed_ms=elapsed_ms,
    )


def _clean_cookies(raw: Any) -> List[dict]:
    """把 WebView 给的 cookie 收成 [{name, value, domain, path}]

    域名不能丢：没有域名的 cookie 注进会话会挂在错误的主机上，
    站点直接判「cookie 对不上」——比不带 cookie 更糟。
    """
    out: List[dict] = []
    budget = _MAX_COOKIE_CHARS
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            # "name=value" 或整条 Set-Cookie
            head, _, rest = item.partition(";")
            name, sep, value = head.partition("=")
            if not sep:
                continue
            domain = ""
            for part in rest.split(";"):
                key, _, val = part.strip().partition("=")
                if key.strip().lower() == "domain":
                    domain = val.strip().lstrip(".").lower()
            item = {"name": name.strip(), "value": value.strip(), "domain": domain}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        domain = str(item.get("domain") or "").strip().lstrip(".").lower()
        if not name or not value or not domain:
            continue
        cost = len(name) + len(value)
        if cost > budget:
            break
        budget -= cost
        out.append({"name": name, "value": value, "domain": domain,
                    "path": str(item.get("path") or "/") or "/"})
    return out


def deliver(params_: dict) -> dict:
    """``browser_result`` 命令的落点：按 request_id 唤醒等待者"""
    request_id = str((params_ or {}).get("request_id") or "")
    with _lock:
        waiter = _pending.get(request_id)
        if waiter is None:
            # 等待者已经超时离开；这时候的应答没有归宿，如实记下别硬塞
            logger.debug(f"browser_result 迟到，丢弃: {request_id[:16]}")
            return {"acknowledged": False, "reason": "no pending request"}
        waiter["result"] = params_
    waiter["event"].set()
    return {"acknowledged": True}


def stats() -> dict:
    with _lock:
        cooldown = max(0.0, _open_until - time.monotonic())
        return {**_stats, "cooldown_s": round(cooldown, 1),
                "enabled": enabled(), "available": cooldown <= 0.0 and enabled()}


def reset() -> None:
    """测试用：清掉在途请求与熔断"""
    global _open_until
    with _lock:
        _pending.clear()
        _stats.update({"calls": 0, "timeouts": 0, "errors": 0, "last_ms": 0.0})
        _open_until = 0.0


def inject_cookies(session, cookies: List[dict]) -> int:
    """把 WebView 的 cookie 灌进解析会话（「双罐同步」的浏览器→HTTP 方向）

    requests 与 curl_cffi 的会话在这里是同一个口（``session.cookies.set``）：
    前者是真 ``RequestsCookieJar``，后者被 utils.httpclient.CurlSession 的
    property 遮成 stdlib CookieJar。灌不进去（域名不合）就跳过那条，
    不值得为一枚 cookie 让整个解析炸掉。
    """
    jar = getattr(session, "cookies", None)
    if jar is None or not hasattr(jar, "set"):
        return 0
    done = 0
    for item in cookies:
        try:
            jar.set(item["name"], item["value"], domain=item["domain"],
                    path=item.get("path") or "/")
            done += 1
        except Exception as e:                      # noqa: BLE001 - 单条 cookie 不值得炸
            logger.debug(f"cookie 注入跳过 {item.get('name')}: {e}")
    return done


def media_candidates(urls: List[str]) -> List[str]:
    """嗅到的地址里挑出像媒体的（扩展名 / 已知媒体域名）

    判据直接复用解析管线的表（``_EXT_MIME``、``_KNOWN_MEDIA_HOSTS``），
    免得沙箱和管线两套标准各说各话。
    """
    from ..media.share_resolver import _EXT_MIME, _KNOWN_MEDIA_HOSTS, _ext_of

    out = []
    for u in urls or []:
        host = (urlparse(str(u)).hostname or "").lower()
        if _ext_of(str(u)) in _EXT_MIME or any(k in host for k in _KNOWN_MEDIA_HOSTS):
            out.append(str(u))
    return out
