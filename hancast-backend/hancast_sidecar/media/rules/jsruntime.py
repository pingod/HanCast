"""JS 脚本插件宿主：每个插件包一个 QuickJS Context（沙箱）

插件是 zip 包（``manifest.json`` + 若干 ``.js``），脚本里 ``registerRule({...})``
声明规则，宿主在解析管线需要时调用 ``resolve(ctx)``。

安全模型：QuickJS 没有任何内置 IO，脚本能做的事全部经由宿主注入的 ``host.*``
函数；运行时内存上限 64MB。线程模型（quickjs 绑定的两条硬约束，实测确认）：
设了 time limit 后 JS 回调 Python 会被拒（host 函数全废），跨线程先后访问
同一 runtime 会崩——因此每个插件的所有 QuickJS 访问钉在一条专属 daemon
线程上串行执行，纯 JS 死循环用「看门狗超时」兜底：卡死只废掉那一次调用
（超时让路），不拖累解析管线，插件线程自身留待进程退出回收。

与 JS 的边界全部走 JSON 字符串：``ctx.set("__ctxStr", …)`` 传入，
``__callResolve`` 返回 JSON 文本——避免 quickjs 对象包装层的类型转换限制。
"""

import base64
import hashlib
import json
import logging
import os
import queue
import threading
import time
from html import unescape as html_unescape
from urllib.parse import urljoin

import quickjs
from lxml import html as lxml_html

from .base import DESKTOP_UA, MOBILE_UA, decode_body, normalize_text

logger = logging.getLogger("hancast.media.jsplugin")

_MEMORY_LIMIT = 64 * 1024 * 1024     # 每插件 Context 内存上限
_LOAD_TIMEOUT = 30.0                 # 脚本加载看门狗（秒）
_META_TIMEOUT = 10.0                 # 读取规则元数据看门狗
_MIN_WATCHDOG = 5.0                  # resolve 看门狗下限（纯 JS 卡死的最快止损）
_MAX_FETCH_BODY = 4 * 1024 * 1024    # host 网络函数正文读取上限
_FETCH_TIMEOUT_CAP = 15.0            # host 单请求超时上限
_MAX_SELECT_NODES = 200

_PRELUDE = r"""
var __defs = [];
function registerRule(def) {
  if (!def || typeof def.resolve !== 'function')
    throw new Error('registerRule 缺少 resolve 函数');
  if (!def.name) throw new Error('registerRule 缺少 name');
  if (!def.hosts || !def.hosts.length) throw new Error('registerRule 缺少 hosts');
  __defs.push(def);
}
function __callResolve(i, ctxStr) {
  var r = __defs[i].resolve(JSON.parse(ctxStr));
  if (r === undefined || r === null) return 'null';
  if (typeof r === 'string') return JSON.stringify({ url: r });
  if (typeof r !== 'object')
    throw new Error('resolve 返回值必须是地址字符串或 {url, headers?, title?} 对象');
  return JSON.stringify(r);
}
var host = {
  getString: function (u, o) { return __getString(String(u), o ? JSON.stringify(o) : null); },
  getHtml:   function (u, o) { return __getHtml(String(u), o ? JSON.stringify(o) : null); },
  getJson:   function (u, o) { return parseJson(host.getString(u, o)); },
  fetch:     function (u, o) { return JSON.parse(__fetch(String(u), o ? JSON.stringify(o) : null)); },
  finalUrl:  function (u, o) { return __finalUrl(String(u), o ? JSON.stringify(o) : null); },
  status:    function (u, o) { return __status(String(u), o ? JSON.stringify(o) : null); },
  select:    function (h, c, b) { return JSON.parse(__select(String(h || ''), String(c || ''), String(b || ''))); },
  userAgent: function (d) { return d ? __uaDesktop() : __uaMobile(); },
  readCache: function (n) { return __readCache(String(n)); },
  writeCache: function (n, t) { return __writeCache(String(n), String(t === undefined ? '' : t)); },
  cacheAge:  function (n) { return __cacheAge(String(n)); },
  getPref:   function (k, d) { return __getPref(String(k), d === undefined ? '' : String(d)); },
  setPref:   function (k, v) { return __setPref(String(k), String(v === undefined ? '' : v)); },
  sleep:     function (ms) { __sleep(Number(ms) || 0); },
  log:       function (m) { __log(String(m)); },
  md5:       function (t) { return __md5(String(t)); },
  sha256:    function (t) { return __sha256(String(t)); },
  base64Encode: function (t) { return __base64Encode(String(t)); },
  base64Decode: function (t) { return __base64Decode(String(t)); },
  htmlDecode: function (t) { return __htmlDecode(String(t)); }
};
function parseJson(text) {
  var t = String(text === undefined || text === null ? '' : text).trim();
  try { return JSON.parse(t); } catch (e) {}
  var m = /^[^(]+\(([\s\S]*)\)\s*;?$/.exec(t);   // JSONP: cb({...});
  if (m) { try { return JSON.parse(m[1]); } catch (e) {} }
  throw new Error('返回内容不是合法 JSON');
}
function at(o, p) {
  var cur = o, parts = String(p || '').split('.').filter(function (x) { return x !== ''; });
  for (var i = 0; i < parts.length; i++) {
    if (cur === null || cur === undefined) return null;
    var k = parts[i];
    if (Array.isArray(cur)) {
      var idx = parseInt(k, 10);
      if (isNaN(idx)) return null;
      cur = cur[idx < 0 ? cur.length + idx : idx];
    } else if (typeof cur === 'object') {
      cur = cur[k];
    } else {
      return null;
    }
  }
  return cur === undefined ? null : cur;
}
function str(o, p) { var v = at(o, p); return v === null || v === undefined ? '' : String(v); }
function int(o, p, d) {
  var v = at(o, p);
  if (v === null || v === undefined) return d === undefined ? 0 : d;
  var n = parseInt(v, 10);
  return isNaN(n) ? (d === undefined ? 0 : d) : n;
}
function items(o, p) {
  var v = at(o, p);
  return Array.isArray(v) ? v : [];
}
function regexInt(text, re, d) {
  var m = re.exec(String(text === undefined || text === null ? '' : text));
  if (!m || !m[1]) return d === undefined ? 0 : d;
  var n = parseInt(m[1], 10);
  return isNaN(n) ? (d === undefined ? 0 : d) : n;
}
function absUrl(base, u) {
  u = String(u === undefined || u === null ? '' : u);
  if (!u) return '';
  if (/^https?:\/\//i.test(u)) return u;
  return __absUrl(String(base || ''), u);
}
"""


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class _PinnedRunner:
    """所有 QuickJS 访问钉在同一条 daemon 线程上串行执行

    quickjs 绑定不允许：设 time limit 后回调 Python、跨线程访问同一 runtime
    （哪怕不同时）。钉线程解掉后者，看门狗超时替代前者——超时只让当前这次
    调用让路，卡死的线程留在原地直到进程退出（daemon，不阻塞退出）。
    """

    def __init__(self):
        self._tasks: queue.Queue = queue.Queue()
        threading.Thread(target=self._loop, daemon=True,
                         name="hancast-jsplugin").start()

    def _loop(self):
        while True:
            fn, box, done = self._tasks.get()
            try:
                box["val"] = fn()
            except BaseException as e:      # 原样转交调用线程
                box["exc"] = e
            finally:
                done.set()

    def run(self, fn, timeout: float):
        box: dict = {}
        done = threading.Event()
        self._tasks.put((fn, box, done))
        if not done.wait(timeout):
            raise TimeoutError("插件脚本执行超时")
        if "exc" in box:
            raise box["exc"]
        return box.get("val")


class JsRuntime:
    """一个插件包的 QuickJS Context：加载脚本、收集规则、调用 resolve

    生命周期由上层（``jsplugins.PluginManager``）管理：指纹变化时重建。
    """

    def __init__(self, plugin_name: str, script_texts: list[str],
                 prefs: dict, on_prefs_change, cache_dir: str):
        self.plugin_name = plugin_name
        self.runner = _PinnedRunner()
        self._prefs = prefs
        self._on_prefs_change = on_prefs_change
        self._cache_dir = cache_dir
        self._deadline = 0.0
        self._session = None          # 当次解析的会话（call_rule 传入）
        self._ctx = quickjs.Context()
        self.rule_count = 0
        self.runner.run(lambda: self._load(script_texts), _LOAD_TIMEOUT)

    # ── 加载 ──

    def _load(self, script_texts: list[str]) -> None:
        ctx = self._ctx
        ctx.set_memory_limit(_MEMORY_LIMIT)
        ctx.set_max_stack_size(1024 * 1024)
        for name, fn in self._host_functions():
            ctx.add_callable(name, fn)
        try:
            ctx.eval(_PRELUDE)
            for text in script_texts:
                ctx.eval(text)
            self.rule_count = int(ctx.eval("__defs.length"))
        except quickjs.JSException as e:
            raise ValueError(str(e).strip() or "脚本报错") from e

    def rule_meta(self, index: int) -> dict:
        """规则的元数据（JSON 序列化侧的视图，不含 resolve 函数本身）"""
        raw = self.runner.run(
            lambda: self._ctx.eval(f"JSON.stringify(__defs[{index}])"),
            _META_TIMEOUT)
        meta = json.loads(raw)
        meta.pop("resolve", None)
        return meta

    def call_rule(self, index: int, payload: dict, deadline: float,
                  session) -> dict | None:
        """调用第 index 条规则的 resolve；返回 None（让路）或 {url, headers?, title?}

        JSException / ValueError / TimeoutError 原样抛出——调用方按
        「这条规则不适用」记录让路。看门狗 = 剩余解析预算 + 网络收尾余量。
        """
        self._deadline = deadline
        self._session = session

        def _invoke():
            self._ctx.set("__ctxStr", json.dumps(payload))
            return self._ctx.eval(f"__callResolve({index}, __ctxStr)")

        watchdog = max((deadline - time.monotonic()) + 2.0, _MIN_WATCHDOG)
        raw = self.runner.run(_invoke, watchdog)
        return None if raw == "null" else json.loads(raw)

    # ── host 函数共享的请求底座 ──

    def _request(self, session, url: str, options_json: str | None,
                 read_body: bool) -> tuple[int, str, str, bytes | None]:
        """统一出口：共享解析会话（Cookie 跨调用延续），超时挂解析总预算"""
        opts = {}
        if options_json:
            try:
                opts = json.loads(options_json)
            except ValueError:
                raise ValueError("options 不是合法 JSON 对象")
        if not isinstance(opts, dict):
            raise ValueError("options 必须是对象")
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("只允许 http(s) 请求")
        timeout = _clamp(self._remaining(self._deadline), 1.0, _FETCH_TIMEOUT_CAP)
        # UA 开关按字面意思走：默认桌面 UA，要手机 UA 显式传 {mobile: true}
        # （以前这个键叫 desktop，语义正好是反的——写第一个官方包时就踩中了）
        headers = {"User-Agent": MOBILE_UA if opts.get("mobile") else DESKTOP_UA,
                   "Accept": "*/*"}
        if opts.get("referer"):
            headers["Referer"] = str(opts["referer"])
        for k, v in (opts.get("headers") or {}).items():
            headers[str(k)] = str(v)
        method = str(opts.get("method") or "get").upper()
        if method not in ("GET", "POST", "HEAD", "PUT"):
            raise ValueError(f"不支持的请求方法: {method}")
        r = session.request(method, url, timeout=timeout, allow_redirects=True,
                            headers=headers,
                            data=(str(opts["body"]).encode("utf-8")
                                  if opts.get("body") is not None else None))
        try:
            ctype = r.headers.get("Content-Type", "")
            body = None
            if read_body:
                buf, size = [], 0
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    buf.append(chunk)
                    size += len(chunk)
                    if size >= _MAX_FETCH_BODY:
                        break
                body = b"".join(buf)
            return r.status_code, r.url, ctype, body
        finally:
            r.close()

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            raise TimeoutError("解析超时（超出总时长预算）")
        return remaining

    # ── host 函数表 ──

    def _host_functions(self) -> list[tuple[str, object]]:
        def _opts_json(options_json: str | None) -> str:
            return options_json or ""

        def get_string(url, options_json=None):
            status, final, ctype, body = self._request(
                self._session, url, _opts_json(options_json), True)
            if status >= 400:
                raise ValueError(f"请求失败（HTTP {status}）: {url[:120]}")
            return decode_body(body or b"", ctype)

        def get_html(url, options_json=None):
            # 与声明式规则同一视图：正文先归一化 JS 转义与 HTML 实体
            return normalize_text(get_string(url, options_json))

        def fetch(url, options_json=None):
            status, final, ctype, body = self._request(
                self._session, url, _opts_json(options_json), True)
            return json.dumps({
                "status": status,
                "finalUrl": final,
                "body": normalize_text(decode_body(body or b"", ctype)) if body else "",
            })

        def final_url(url, options_json=None):
            _, final, _, _ = self._request(
                self._session, url, _opts_json(options_json), False)
            return final

        def status(url, options_json=None):
            code, _, _, _ = self._request(
                self._session, url, _opts_json(options_json), False)
            return code

        def select(html, css, base):
            try:
                doc = lxml_html.fromstring(html)
            except Exception as e:
                raise ValueError(f"HTML 解析失败: {e}")
            try:
                els = doc.cssselect(css)
            except Exception as e:
                raise ValueError(f"CSS 选择器无效: {e}")
            nodes = []
            for el in els[:_MAX_SELECT_NODES]:
                attrs = dict(el.attrib)
                absd = {}
                for k in ("href", "src"):
                    if attrs.get(k):
                        absd[k] = urljoin(base or "", attrs[k].strip())
                inner = "".join(
                    lxml_html.tostring(c, encoding="unicode") for c in el)
                nodes.append({"tag": el.tag, "text": (el.text_content() or "").strip(),
                              "attrs": attrs, "abs": absd, "html": inner})
            return json.dumps(nodes)

        def read_cache(name):
            path = self._cache_path(name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except OSError:
                return ""

        def write_cache(name, text):
            path = self._cache_path(name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text[:_MAX_FETCH_BODY])
            return True

        def cache_age(name):
            path = self._cache_path(name)
            try:
                return int((time.time() - os.path.getmtime(path)) * 1000)
            except OSError:
                return -1

        def get_pref(key, default=""):
            return str(self._prefs.get(key, default if default is not None else ""))

        def set_pref(key, value):
            self._prefs[key] = value
            self._on_prefs_change()
            return True

        def sleep(ms):
            time.sleep(_clamp(float(ms) / 1000.0, 0.0,
                              _clamp(self._remaining(self._deadline), 0.0, 10.0)))

        def log(msg):
            logger.info("[%s] %s", self.plugin_name, msg[:500])

        # 名称带 __ 前缀，JS 侧只经 host 门面访问
        return [
            ("__getString", get_string), ("__getHtml", get_html),
            ("__fetch", fetch), ("__finalUrl", final_url), ("__status", status),
            ("__select", select),
            ("__uaDesktop", lambda: DESKTOP_UA), ("__uaMobile", lambda: MOBILE_UA),
            ("__absUrl", lambda base, u: urljoin(base or "", u)),
            ("__readCache", read_cache), ("__writeCache", write_cache),
            ("__cacheAge", cache_age),
            ("__getPref", get_pref), ("__setPref", set_pref),
            ("__sleep", sleep), ("__log", log),
            ("__md5", lambda t: hashlib.md5(t.encode("utf-8")).hexdigest()),
            ("__sha256", lambda t: hashlib.sha256(t.encode("utf-8")).hexdigest()),
            ("__base64Encode",
             lambda t: base64.b64encode(t.encode("utf-8")).decode("ascii")),
            ("__base64Decode",
             lambda t: base64.b64decode(t + "=" * (-len(t) % 4)).decode("utf-8", "replace")),
            ("__htmlDecode", lambda t: html_unescape(t)),
        ]

    def _cache_path(self, name: str) -> str:
        safe = os.path.basename(name)
        if not safe or safe.startswith("."):
            raise ValueError(f"缓存名不合法: {name!r}")
        return os.path.join(self._cache_dir, safe)
