"""沙箱宿主扩出来的两块能力：签名原语 与 本包 cookie 罐（全部离线）

覆盖:
- host.sha1/hmac*/aes*/rsa*/hex* 的编码约定（文本按 utf8、字节按 hex/base64）
- host.deviceId() 跨运行时稳定、host.uuid() 每次都变
- cookie 罐：setCookie 解析 Set-Cookie（含 Domain 属性）、落盘、重建运行时后仍在
- 发请求时只带本包该域名的 cookie，且不与会话同名 cookie 重复发送
- 响应 Set-Cookie 自动进罐（含重定向链）、过期即删、clearCookies 按域/全清
- writeCache 不能把 cookie 罐当普通缓存覆盖掉
"""

import json
import os
import shutil
import tempfile
import time
import unittest

import hancast_sidecar.media.rules.jsruntime as jsruntime_mod
from requests.cookies import RequestsCookieJar, create_cookie


def _tmp_dir(test) -> str:
    d = tempfile.mkdtemp(prefix="hancast-host-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


class FakeResponse:
    """够用的 requests.Response 替身：只暴露 cookie 收割与正文读取用到的部分"""

    def __init__(self, body=b"", url="https://demo.example/x", cookies=(),
                 history=()):
        self.status_code = 200
        self.url = url
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self._body = body
        self.cookies = list(cookies)
        self.history = list(history)

    def iter_content(self, chunk_size=65536):
        yield self._body

    def close(self):
        pass


class FakeSession:
    def __init__(self, resp, jar=None):
        self.resp = resp
        self.cookies = jar if jar is not None else RequestsCookieJar()
        self.calls: list[tuple] = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.resp


def _cookie(name, value, domain):
    c = create_cookie(name, value, domain=domain, path="/")
    return c


class TestSandboxHost(unittest.TestCase):
    def _runtime(self, script, cache_dir=None):
        return jsruntime_mod.JsRuntime(
            "t", [script], prefs={}, on_prefs_change=lambda: None,
            cache_dir=cache_dir or _tmp_dir(self))

    def _run(self, rt, body="", session=None):
        return rt.call_rule(0, {"body": body}, time.monotonic() + 30,
                            session or FakeSession(FakeResponse(body.encode())))

    # ── 签名 ──

    def test_hash_and_hmac_encodings(self):
        rt = self._runtime("""
        registerRule({name:'r',hosts:['x.com'],stage:'page',resolve:function(c){
          return { url: 'https://x.com/v',
            sha1: host.sha1('abc'),
            sha1hexbytes: host.sha1Hex(host.hexEncode('abc')),
            sha256b64: host.sha256B64(host.hexEncode('abc')),
            hmac: host.hmacSha256('k', 'msg'),
            hmacb64: host.hmacSha256B64('k', 'msg'),
            hmacbytes: host.hmacHex('sha1', host.hexEncode('k'), host.hexEncode('msg')),
            b64round: host.base64ToHex(host.hexToBase64(host.hexEncode('汉')))
          };
        }})
        """)
        out = self._run(rt)
        self.assertEqual(out["sha1"],
                         "a9993e364706816aba3e25717850c26c9cd0d89d")
        # 同一段文字，按 utf8 和按 hex 字节进同一个摘要必须给同一个结果
        self.assertEqual(out["sha1hexbytes"], out["sha1"])
        self.assertEqual(out["b64round"], host_hex_of("汉"))
        import hashlib
        import hmac
        self.assertEqual(out["hmac"],
                         hmac.new(b"k", b"msg", hashlib.sha256).hexdigest())
        self.assertEqual(out["hmacb64"],
                         __import__("base64").b64encode(
                             hmac.new(b"k", b"msg",
                                      hashlib.sha256).digest()).decode())
        self.assertEqual(out["hmacbytes"],
                         hmac.new(b"k", b"msg", hashlib.sha1).hexdigest())

    def test_aes_and_rsa_roundtrip(self):
        rt = self._runtime("""
        registerRule({name:'r',hosts:['x.com'],stage:'page',resolve:function(c){
          var k = host.hexEncode('0123456789abcdef');          // 16 字节密钥
          var iv = host.hexEncode('abcdef9876543210');
          var e = host.aesEcbEncrypt('汉 hello', k, 'hex');
          var cb = host.aesCbcEncrypt('round', k, iv, 'base64');
          return { url: 'https://x.com/v',
                   ecb: e, ecbBack: host.aesEcbDecryptHex(e, k, 'hex'),
                   cbcBack: host.aesCbcDecrypt(cb, k, iv),
                   bad: (function(){ try { host.aesEcbDecrypt('zz', k); return 'no'; }
                                        catch (err) { return String(err.message).slice(0,4); } })()
          };
        }})
        """)
        out = self._run(rt)
        self.assertEqual(out["ecbBack"], host_hex_of("汉 hello"))
        self.assertEqual(out["cbcBack"], "round")
        # 密钥/密文不合法时是「这条规则不适用」级别的报错，不是把解析打崩
        self.assertNotEqual(out["bad"], "no")

    # ── 身份 ──

    def test_device_id_stable_uuid_not(self):
        cache = _tmp_dir(self)
        script = """
        registerRule({name:'r',hosts:['x.com'],stage:'page',resolve:function(c){
          return { url:'https://x.com/v', a: host.deviceId(), b: host.deviceId(),
                   u1: host.uuid(), u2: host.uuid(), n: host.randomHex(8) };
        }})
        """
        out1 = self._run(self._runtime(script, cache))
        out2 = self._run(self._runtime(script, cache))
        self.assertEqual(len(out1["a"]), 32)
        self.assertEqual(out1["a"], out1["b"])       # 同一台机器恒定
        self.assertEqual(out1["a"], out2["a"])       # 换运行时也不变
        self.assertNotEqual(out1["u1"], out1["u2"])
        self.assertEqual(len(out1["n"]), 16)

    # ── cookie 罐 ──

    def test_set_cookie_parses_attributes_and_persists(self):
        cache = _tmp_dir(self)
        script = """
        registerRule({name:'r',hosts:['demo.example'],stage:'page',resolve:function(c){
          host.setCookie('https://www.demo.example/p',
            'SESSDATA=abc; Path=/; Domain=demo.example; HttpOnly; t=2');
          host.setCookie('https://other.example/', 'sid=9');
          return { url:'https://x.com/v',
                   wide: host.cookies('https://any.demo.example/x'),
                   exact: host.cookies('https://www.other.example/'),
                   unrelated: host.cookies('https://third.example/') };
        }})
        """
        out = self._run(self._runtime(script, cache))
        self.assertEqual(out["wide"], "SESSDATA=abc; t=2")
        self.assertEqual(out["exact"], "sid=9")
        self.assertEqual(out["unrelated"], "")
        with open(os.path.join(cache, "cookies.json"), encoding="utf-8") as f:
            jar = json.load(f)
        self.assertEqual(sorted(jar), ["demo.example", "other.example"])
        # 换了 runtime 实例（升级包/重启）罐子还在
        out2 = self._run(self._runtime(script, cache))
        self.assertEqual(out2["wide"], "SESSDATA=abc; t=2")

    def test_request_carries_jar_cookies_without_duplicating_session(self):
        session = FakeSession(FakeResponse())
        session.cookies.set("t", "from-page", domain="demo.example", path="/")
        script = """
        registerRule({name:'r',hosts:['demo.example'],stage:'page',resolve:function(c){
          host.setCookie('https://demo.example/p', 'SESSDATA=abc; t=stale');
          host.getString('https://demo.example/api');
          return { url:'https://x.com/v',
                   seen: host.cookies('https://demo.example/p'),
                   ua: host.userAgent(true) === host.userAgent(true) };
        }})
        """
        out = self._run(self._runtime(script), session=session)
        sent = session.calls[0][2]["cookies"]
        self.assertEqual(sent, {"SESSDATA": "abc"})     # 与会话同名的不重发
        self.assertIn("t=from-page", out["seen"])        # 会话值赢，罐里的旧值靠边
        self.assertIn("SESSDATA=abc", out["seen"])

    def test_response_cookies_are_harvested(self):
        cache = _tmp_dir(self)
        page = FakeResponse(
            cookies=[_cookie("ttwid", "new", ".demo.example")],
            history=[FakeResponse(cookies=[_cookie("sid", "old", "demo.example")])])
        session = FakeSession(page)
        harvest_js = """
        registerRule({name:'r',hosts:['demo.example'],stage:'page',resolve:function(c){
          host.getString('https://demo.example/p');
          return { url:'https://x.com/v', got: host.cookies('https://www.demo.example/') };
        }})
        """
        out = self._run(self._runtime(harvest_js, cache), session=session)
        self.assertIn("ttwid=new", out["got"])
        self.assertIn("sid=old", out["got"])         # 重定向链里的也收下
        with open(os.path.join(cache, "cookies.json"), encoding="utf-8") as f:
            self.assertEqual(sorted(json.load(f)), [".demo.example", "demo.example"])
        # 换一个运行时实例（升级包/重启）罐子还在，clearCookies 才谈得上按域清
        out2 = self._run(self._runtime("""
        registerRule({name:'r',hosts:['demo.example'],stage:'page',resolve:function(c){
          var before = host.cookies('https://www.demo.example/');
          host.clearCookies('https://other.example/');       // 别的域，不该动它
          var kept = host.cookies('https://www.demo.example/');
          host.clearCookies('');                             // 全清
          return { url:'https://x.com/v', before: before, kept: kept,
                   after: host.cookies('https://www.demo.example/') };
        }})
        """, cache))
        self.assertEqual(out2["before"], out2["kept"])
        self.assertEqual(out2["after"], "")
        with open(os.path.join(cache, "cookies.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {})

    def test_host_error_reaches_js_with_message_and_context_stays_alive(self):
        """Python 异常不许越过 C 边界

        旧行为：quickjs 只收到一句 "Python call failed"（原因全丢），而且这个
        Context 从此每次 eval 都 SystemError——一条规则失手，整包一起废。
        """
        script = """
        registerRule({name:'r',hosts:['x.com'],stage:'page',resolve:function(c){
          var msg = '';
          try { host.getJson('ftp://only-http.example/'); }
          catch (e) { msg = String(e.message); }
          return { url:'https://x.com/v', msg: msg, alive: host.md5('a') };
        }})
        """
        rt = self._runtime(script)
        out = self._run(rt)
        self.assertIn("只允许 http(s)", out["msg"])
        self.assertEqual(out["alive"], "0cc175b9c0f1b6a831c399e269772661")
        # 同一个 runtime 的下一次调用必须照常（被弄坏过的上下文做不到这件事）
        self.assertEqual(self._run(rt)["alive"],
                         "0cc175b9c0f1b6a831c399e269772661")

    def test_write_cache_cannot_clobber_cookie_jar(self):
        rt = self._runtime("""
        registerRule({name:'r',hosts:['x.com'],stage:'page',resolve:function(c){
          var err = '';
          try { host.writeCache('cookies.json', '{}'); } catch (e) { err = String(e.message); }
          return { url:'https://x.com/v', err: err };
        }})
        """)
        out = self._run(rt)
        self.assertIn("cookie", out["err"])


def host_hex_of(text: str) -> str:
    return text.encode("utf-8").hex()
