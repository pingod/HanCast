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

签名类站点要的不是「更多算法」而是两样东西：算得出签名（``host.md5``/``hmac*``/
AES/RSA，见 ``crypto.py``）和端得住身份（``host.deviceId()`` 固定设备号 +
``host.cookies()``/``setCookie()`` 持久 cookie 罐）。cookie 罐落在包自己的
缓存目录，只对本包发出的请求生效，不会串到别的包。
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import secrets
import threading
import time
import uuid as uuid_module
from html import unescape as html_unescape
from urllib.parse import urlparse, urljoin

import quickjs
from lxml import html as lxml_html

from .base import decode_body, normalize_text
from ...utils.identity import desktop_ua, mobile_ua, device_id
from .crypto import (aes_cbc_decrypt, aes_cbc_encrypt, aes_ecb_decrypt,
                     aes_ecb_encrypt, rsa_encrypt)

logger = logging.getLogger("hancast.media.jsplugin")

_MEMORY_LIMIT = 64 * 1024 * 1024     # 每插件 Context 内存上限
_LOAD_TIMEOUT = 30.0                 # 脚本加载看门狗（秒）
_META_TIMEOUT = 10.0                 # 读取规则元数据看门狗
_MIN_WATCHDOG = 5.0                  # resolve 看门狗下限（纯 JS 卡死的最快止损）
_MAX_FETCH_BODY = 4 * 1024 * 1024    # host 网络函数正文读取上限
_FETCH_TIMEOUT_CAP = 15.0            # host 单请求超时上限
_MAX_SELECT_NODES = 200
_COOKIE_FILE = "cookies.json"        # 本包持久 cookie 罐（随包目录走）
_MAX_COOKIE_DOMAINS = 40
_MAX_COOKIE_PER_DOMAIN = 100
# Set-Cookie 里不是 cookie 的那些属性；domain 单独挑出来当归属域用
_COOKIE_ATTRS = {"path", "expires", "max-age", "secure", "httponly",
                 "samesite", "extension"}
_HASHES = {"md5": hashlib.md5, "sha1": hashlib.sha1,
          "sha256": hashlib.sha256, "sha512": hashlib.sha512}

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
var __hostRaw = {
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
  htmlDecode: function (t) { return __htmlDecode(String(t)); },

  // ── 签名原语 ──
  // 约定：文本参数按 UTF-8 看成字节；已经是字节的用 *Hex / *B64 变体，
  // 返回值除注明外是 hex。加解密的密文用 base64（默认）或 hex，由最后的 as 参数决定。
  sha1:        function (t) { return __hash(String(t), "utf8", "sha1", "hex"); },
  sha512:      function (t) { return __hash(String(t), "utf8", "sha512", "hex"); },
  md5Hex:      function (h) { return __hash(String(h), "hex", "md5", "hex"); },
  sha1Hex:     function (h) { return __hash(String(h), "hex", "sha1", "hex"); },
  sha256Hex:   function (h) { return __hash(String(h), "hex", "sha256", "hex"); },
  md5B64:      function (h) { return __hash(String(h), "hex", "md5", "base64"); },
  sha1B64:     function (h) { return __hash(String(h), "hex", "sha1", "base64"); },
  sha256B64:   function (h) { return __hash(String(h), "hex", "sha256", "base64"); },
  hmacMd5:     function (k, m) { return __hmac(String(k), "utf8", String(m), "utf8", "md5", "hex"); },
  hmacSha1:    function (k, m) { return __hmac(String(k), "utf8", String(m), "utf8", "sha1", "hex"); },
  hmacSha256:  function (k, m) { return __hmac(String(k), "utf8", String(m), "utf8", "sha256", "hex"); },
  hmacSha512:  function (k, m) { return __hmac(String(k), "utf8", String(m), "utf8", "sha512", "hex"); },
  hmacSha1B64:  function (k, m) { return __hmac(String(k), "utf8", String(m), "utf8", "sha1", "base64"); },
  hmacSha256B64: function (k, m) { return __hmac(String(k), "utf8", String(m), "utf8", "sha256", "base64"); },
  hmacHex:     function (algo, keyHex, msgHex) { return __hmac(String(keyHex), "hex", String(msgHex), "hex", String(algo), "hex"); },
  aesEcbEncrypt:  function (t, keyHex, as) { return __aes("ecb_encrypt", String(keyHex), "", String(t), "utf8", as || "base64"); },
  aesEcbEncryptBytes: function (hex, keyHex, as) { return __aes("ecb_encrypt", String(keyHex), "", String(hex), "hex", as || "base64"); },
  aesEcbDecrypt:  function (c, keyHex, as) { return __hexStr(__aes("ecb_decrypt", String(keyHex), "", String(c), as || "base64", "hex")); },
  aesEcbDecryptHex: function (c, keyHex, as) { return __aes("ecb_decrypt", String(keyHex), "", String(c), as || "base64", "hex"); },
  aesCbcEncrypt:  function (t, keyHex, ivHex, as) { return __aes("cbc_encrypt", String(keyHex), String(ivHex), String(t), "utf8", as || "base64"); },
  aesCbcDecrypt:  function (c, keyHex, ivHex, as) { return __hexStr(__aes("cbc_decrypt", String(keyHex), String(ivHex), String(c), as || "base64", "hex")); },
  aesCbcDecryptHex: function (c, keyHex, ivHex, as) { return __aes("cbc_decrypt", String(keyHex), String(ivHex), String(c), as || "base64", "hex"); },
  rsaEncrypt:      function (t, modulusHex, exponentHex, as) { return __rsa(String(t), "utf8", String(modulusHex), String(exponentHex), as || "base64"); },
  rsaEncryptHex:   function (hex, modulusHex, exponentHex, as) { return __rsa(String(hex), "hex", String(modulusHex), String(exponentHex), as || "base64"); },
  hexEncode: function (t) { return __hexOf(String(t)); },
  hexDecode: function (h) { return __hexStr(String(h)); },
  hexToBase64: function (h) { return __hexToB64(String(h)); },
  base64ToHex: function (b) { return __b64ToHex(String(b)); },

  // ── 身份与 cookie ──
  uuid:      function () { return __uuid(); },
  randomHex: function (n) { return __randomHex(Number(n) || 16); },
  // 同一台机器恒定：站点把设备号当账号用，每次随机就等于每次换设备
  deviceId:  function () { return __deviceId(); },
  // 本包持久 cookie 罐 + 本次解析会话里该域名已有的 cookie，拼成 "a=1; b=2"
  cookies:      function (u) { return __cookies(String(u)); },
  setCookie:    function (u, s) { return __setCookie(String(u), String(s)); },
  clearCookies: function (u) { return __clearCookies(u === undefined ? '' : String(u)); }
};
var __HOST_ERR = "\u0001hancast-host-error:";
function __unwrap(v) {
  if (typeof v === 'string' && v.indexOf(__HOST_ERR) === 0)
    throw new Error(v.slice(__HOST_ERR.length));
  return v;
}
// Python 异常绝不直接越过 C 边界：quickjs 只会留一句 "Python call failed"，
// 真实原因全丢，而且上下文被永久弄坏（实测此后每次 eval 都 SystemError，
// 整个包的规则一起废）。宿主侧 _guard 把异常编码成带标记的返回值，
// 这里统一还原成 JS 异常——包脚本 try/catch 得到的是人话。
var host = (function (raw) {
  var out = {};
  for (var k in raw) {
    out[k] = (function (f) {
      return function () { return __unwrap(f.apply(null, arguments)); };
    })(raw[k]);
  }
  return out;
})(__hostRaw);
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


_HOST_ERROR_PREFIX = "\x01hancast-host-error:"


def _guard(fn):
    """host 回调的统一外壳：Python 异常换成带标记的返回值，绝不让它越过 C 边界

    越过边界的话 quickjs 只得到一句 "Python call failed"（原因全丢），
    并且这个 Context 从此每次 eval 都 SystemError——一条规则失手就废掉整包。
    JS 侧 ``__unwrap`` 负责把它还原成普通 JS 异常（见 _PRELUDE）。
    """
    def wrapper(*args):
        try:
            return fn(*args)
        except Exception as e:
            text = str(e).strip() or type(e).__name__
            if not isinstance(e, ValueError):
                text = f"{type(e).__name__}: {text}"
            return f"{_HOST_ERROR_PREFIX}{text[:500]}"
    return wrapper


def _to_bytes(value: str, enc: str) -> bytes:
    """字符串 → 字节：文本走 utf8，已经是字节的走 hex / base64"""
    enc = (enc or "utf8").strip().lower()
    if enc in ("utf8", "utf-8", "text", "str"):
        return value.encode("utf-8")
    if enc == "hex":
        clean = value.strip().replace(" ", "")
        try:
            return bytes.fromhex(clean)
        except ValueError as e:
            raise ValueError(f"不是合法的 hex: {e}") from e
    if enc in ("base64", "b64"):
        pad = value.strip()
        try:
            return base64.b64decode(pad + "=" * (-len(pad) % 4))
        except Exception as e:
            raise ValueError(f"不是合法的 base64: {e}") from e
    raise ValueError(f"未知的编码: {enc}")


def _from_bytes(data: bytes, enc: str) -> str:
    enc = (enc or "hex").strip().lower()
    if enc == "hex":
        return data.hex()
    if enc in ("base64", "b64"):
        return base64.b64encode(data).decode("ascii")
    if enc in ("utf8", "utf-8", "text", "str"):
        return data.decode("utf-8", errors="replace")
    raise ValueError(f"未知的编码: {enc}")


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
        self._jar: dict | None = None   # 本包持久 cookie 罐（懒加载）
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
            ctx.add_callable(name, _guard(fn))
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
        headers = {"User-Agent": mobile_ua() if opts.get("mobile") else desktop_ua(),
                   "Accept": "*/*"}
        if opts.get("referer"):
            headers["Referer"] = str(opts["referer"])
        for k, v in (opts.get("headers") or {}).items():
            headers[str(k)] = str(v)
        method = str(opts.get("method") or "get").upper()
        if method not in ("GET", "POST", "HEAD", "PUT"):
            raise ValueError(f"不支持的请求方法: {method}")
        # 本包持久 cookie 罐按域名附加；请求自带 Cookie 头时以它为准
        # （requests 见到显式 Cookie 头就不再合并 cookie 罐）
        jar = self._cookies_for(url)
        r = session.request(method, url, timeout=timeout, allow_redirects=True,
                            headers=headers, cookies=jar or None,
                            data=(str(opts["body"]).encode("utf-8")
                                  if opts.get("body") is not None else None))
        try:
            self._harvest_cookies(r)
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

        # ── 签名/加密原语 ──

        def hash_fn(value, in_enc, algo, out_enc):
            factory = _HASHES.get(str(algo))
            if factory is None:
                raise ValueError(f"不支持的摘要算法: {algo}")
            return _from_bytes(factory(_to_bytes(str(value), str(in_enc))).digest(),
                               str(out_enc))

        def hmac_fn(key, key_enc, msg, msg_enc, algo, out_enc):
            factory = _HASHES.get(str(algo))
            if factory is None:
                raise ValueError(f"不支持的摘要算法: {algo}")
            return _from_bytes(
                hmac.new(_to_bytes(str(key), str(key_enc)),
                         _to_bytes(str(msg), str(msg_enc)), factory).digest(),
                str(out_enc))

        def aes_fn(op, key_hex, iv_hex, data, in_enc, out_enc):
            key = _to_bytes(str(key_hex), "hex")
            payload = _to_bytes(str(data), str(in_enc))
            op = str(op)
            if op == "ecb_encrypt":
                blob = aes_ecb_encrypt(key, payload)
            elif op == "ecb_decrypt":
                blob = aes_ecb_decrypt(key, payload)
            elif op in ("cbc_encrypt", "cbc_decrypt"):
                iv = _to_bytes(str(iv_hex), "hex")
                blob = (aes_cbc_encrypt if op == "cbc_encrypt"
                        else aes_cbc_decrypt)(key, iv, payload)
            else:
                raise ValueError(f"不支持的 AES 操作: {op}")
            return _from_bytes(blob, str(out_enc))

        def rsa_fn(text, in_enc, modulus_hex, exponent_hex, out_enc):
            blob = rsa_encrypt(_to_bytes(str(text), str(in_enc)),
                               str(modulus_hex), str(exponent_hex))
            return _from_bytes(blob, str(out_enc))

        # ── cookie / 身份 ──

        def cookies_fn(url):
            merged = self._jar_cookies(url)
            merged.update(self._session_cookies(url))   # 会话里的新鲜值优先
            return "; ".join(f"{k}={v}" for k, v in merged.items())

        def set_cookie_fn(url, text):
            # 接受 "a=1; b=2" 与整条 Set-Cookie（属性挑出来当域名用）
            host = (urlparse(url).hostname or "").lower()
            if not host:
                raise ValueError("setCookie 需要一个 http(s) 地址")
            jar = self._jar_data()
            domain, pairs, dropped = host, [], set()
            for part in str(text).split(";"):
                bit = part.strip()
                if not bit or "=" not in bit:
                    continue
                name, _, value = bit.partition("=")
                name = name.strip()
                low = name.lower()
                if low == "domain":
                    domain = value.strip().lstrip(".").lower() or domain
                    continue
                if low in _COOKIE_ATTRS:
                    continue
                if value.strip():
                    pairs.append((name, value.strip()))
                else:
                    dropped.add(name)
            if not pairs and not dropped:
                raise ValueError("setCookie 里没有读到 name=value")
            bucket = jar.setdefault(domain, {})
            for name in dropped:
                bucket.pop(name, None)
            for name, value in pairs:
                if name not in bucket and len(bucket) >= _MAX_COOKIE_PER_DOMAIN:
                    continue
                bucket[name] = value
            self._prune_jar(jar)
            self._save_jar()
            return True

        def clear_cookies_fn(url):
            jar = self._jar_data()
            if not url:
                jar.clear()
            else:
                host = (urlparse(url).hostname or "").lower()
                for domain in [d for d in jar if self._domain_match(host, d)]:
                    jar.pop(domain, None)
            self._save_jar()
            return True

        # 名称带 __ 前缀，JS 侧只经 host 门面访问
        return [
            ("__getString", get_string), ("__getHtml", get_html),
            ("__fetch", fetch), ("__finalUrl", final_url), ("__status", status),
            ("__select", select),
            ("__uaDesktop", desktop_ua), ("__uaMobile", mobile_ua),
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
            ("__hash", hash_fn), ("__hmac", hmac_fn), ("__aes", aes_fn),
            ("__rsa", rsa_fn),
            ("__hexOf", lambda t: t.encode("utf-8").hex()),
            ("__hexStr", lambda h: _from_bytes(_to_bytes(str(h), "hex"), "utf8")),
            ("__hexToB64", lambda h: _from_bytes(_to_bytes(str(h), "hex"), "base64")),
            ("__b64ToHex", lambda b: _from_bytes(_to_bytes(str(b), "base64"), "hex")),
            ("__uuid", lambda: str(uuid_module.uuid4())),
            ("__randomHex", lambda n: secrets.token_hex(
                int(_clamp(float(n), 1, 64)))),
            ("__deviceId", device_id),
            ("__cookies", cookies_fn), ("__setCookie", set_cookie_fn),
            ("__clearCookies", clear_cookies_fn),
        ]

    def _cache_path(self, name: str) -> str:
        safe = os.path.basename(name)
        if not safe or safe.startswith("."):
            raise ValueError(f"缓存名不合法: {name!r}")
        if safe == _COOKIE_FILE:
            raise ValueError(f"缓存名不合法: {name!r}（cookie 罐由 host.setCookie 维护）")
        return os.path.join(self._cache_dir, safe)

    # ── 本包 cookie 罐 ──
    #
    # 落在包自己的缓存目录，随包启停/删除一起走；只有本包发出的请求会带上它，
    # 不写进解析会话的 cookie 罐——那是同一次解析里所有规则共用的。

    @property
    def _jar_path(self) -> str:
        return os.path.join(self._cache_dir, _COOKIE_FILE)

    def _jar_data(self) -> dict:
        if self._jar is not None:
            return self._jar
        data: dict = {}
        try:
            with open(self._jar_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                for domain, pairs in list(raw.items())[:_MAX_COOKIE_DOMAINS]:
                    if isinstance(pairs, dict):
                        keep = list(pairs.items())[:_MAX_COOKIE_PER_DOMAIN]
                        data[str(domain)] = {str(k): str(v) for k, v in keep}
        except (OSError, ValueError) as e:
            logger.info("[%s] cookie 罐读不出来，按空处理: %s", self.plugin_name, e)
        self._jar = data
        return data

    def _save_jar(self) -> None:
        path = self._jar_path
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._jar or {}, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning("[%s] cookie 罐落盘失败: %s", self.plugin_name, e)

    @staticmethod
    def _domain_match(host: str, domain: str) -> bool:
        d = (domain or "").lstrip(".").lower()
        h = (host or "").lower()
        return bool(d) and (h == d or h.endswith("." + d))

    def _jar_cookies(self, url: str) -> dict:
        host = (urlparse(url).hostname or "").lower()
        out: dict = {}
        for domain, pairs in self._jar_data().items():
            if self._domain_match(host, domain):
                out.update(pairs)
        return out

    def _session_cookies(self, url: str) -> dict:
        """本次解析会话里该域名已有的 cookie（抓页拿到的，比罐里的新鲜）"""
        host = (urlparse(url).hostname or "").lower()
        out: dict = {}
        session = self._session
        if not session:
            return out
        for cookie in getattr(session, "cookies", None) or []:
            if self._domain_match(host, getattr(cookie, "domain", "")):
                out[cookie.name] = cookie.value
        return out

    def _cookies_for(self, url: str) -> dict:
        """发请求时带的本包 cookie：会话已有的赢（同名不重复，避免一个名字发两个值）

        会话自己的 cookie 由 requests 自动合并，这里只补罐子里多出来的那些。
        """
        live = set(self._session_cookies(url))
        out = {}
        for name, value in self._jar_cookies(url).items():
            if name not in live:
                out[name] = value
        return out

    def _prune_jar(self, jar: dict) -> None:
        """域名只被删空了就摘掉；总量超上限就淘汰最早的域"""
        for domain in [d for d, pairs in jar.items() if not pairs]:
            jar.pop(domain)
        while len(jar) > _MAX_COOKIE_DOMAINS:
            jar.pop(next(iter(jar)))

    def _harvest_cookies(self, response) -> bool:
        """把本包这次请求收到的 Set-Cookie 存进罐子（含重定向链）"""
        jar = self._jar_data()
        changed = False
        for resp in [response, *(getattr(response, "history", None) or [])]:
            for cookie in list(getattr(resp, "cookies", None) or []):
                domain = (getattr(cookie, "domain", "") or "").lower()
                if not domain:
                    continue
                bucket = jar.setdefault(domain, {})
                # 过期就当场删掉：罐子里留一枚死 cookie，下次带上只会被站点判异常
                # （Cookie 的过期判定在 stdlib 里叫过两个名字，按现存的取）
                expired = (getattr(cookie, "is_expired", None)
                           or getattr(cookie, "has_expired", None))
                if callable(expired) and expired():
                    if bucket.pop(cookie.name, None) is not None:
                        changed = True
                    continue
                if (cookie.name not in bucket
                        and len(bucket) >= _MAX_COOKIE_PER_DOMAIN):
                    continue
                if bucket.get(cookie.name) != cookie.value:
                    bucket[cookie.name] = cookie.value
                    changed = True
        self._prune_jar(jar)
        if changed:
            self._save_jar()
        return changed
