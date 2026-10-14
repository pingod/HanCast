"""浏览器通道（sidecar ⇄ 宿主 WebView 反向 RPC）的 Python 侧契约

覆盖:
- render() 发出的 browser_request 形状：UA 只有一个来源、options 带预算与 settle
- 应答经 deliver() 唤醒等待者；没有归宿的迟到应答如实拒绝而不是硬塞
- 超时 → ok=False 并进熔断，冷却期内 available() 为假、再调直接抛「不可用」
- 预算：剩余不足立刻 TimeoutError，超时值收进解析总预算而不越过 25s 上限
- cookie 清洗：域名缺失的丢弃（灌错主机比不灌更糟）、Set-Cookie 字符串形状、条数上限
- inject_cookies 灌进解析会话；media_candidates 复用解析管线的判据
- 命令层 browser_result 只做一次投递
- 沙箱 host.render / host.sniff：参数透传、媒体地址挑出、Cookie 双罐同步、
  宿主异常在规则里看到的是人话而不是 "Python call failed"
"""

import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import hancast_sidecar.media.rules.jsruntime as jsruntime_mod
from hancast_sidecar import params
from hancast_sidecar.commands import CommandHandler
from hancast_sidecar.media.rules.jsruntime import JsRuntime
from hancast_sidecar.utils import browser_channel as bc
from hancast_sidecar.utils.identity import desktop_ua, mobile_ua
from requests.cookies import RequestsCookieJar


def _tmp_dir(test) -> str:
    d = tempfile.mkdtemp(prefix="hancast-browser-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


class FakeResponse:
    def __init__(self, body=b"", url="https://x.com/v"):
        self.status_code = 200
        self.url = url
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self._body = body
        self.cookies = []
        self.history = []

    def iter_content(self, chunk_size=65536):
        yield self._body

    def close(self):
        pass


class FakeSession:
    def __init__(self, resp=None):
        self.resp = resp or FakeResponse()
        self.cookies = RequestsCookieJar()

    def request(self, method, url, **kw):
        return self.resp


def _ok_payload(request_id: str, **over) -> dict:
    payload = {
        "request_id": request_id,
        "ok": True,
        "error": "",
        "html": "<html><video src='https://v.douyin.com/a.mp4'></video></html>",
        "finalUrl": "https://www.douyin.com/video/1",
        "title": "标题",
        "ua": desktop_ua(),
        "candidates": ["https://v.douyin.com/a.mp4", "https://www.douyin.com/js/app.js"],
        "cookies": [{"name": "ttwid", "value": "T", "domain": ".douyin.com", "path": "/"}],
    }
    payload.update(over)
    return payload


class TestRenderRoundtrip(unittest.TestCase):
    def setUp(self):
        params.load_from({})
        bc.reset()
        self.addCleanup(bc.reset)

    def test_request_shape_and_ua_single_source(self):
        seen = {}

        def emit(event, data):
            seen["event"] = event
            seen["data"] = data
            self.assertEqual(bc.deliver(_ok_payload(data["request_id"]))["acknowledged"], True)

        with patch.object(bc, "emit_event", emit):
            res = bc.render("https://v.douyin.com/x/", mobile=True)

        self.assertEqual(seen["event"], "browser_request")
        opts = seen["data"]["options"]
        # 浏览器身份 = HTTP 身份：同一台机器同一个 UA，站点才认得出「一个客户端」
        self.assertEqual(opts["ua"], mobile_ua())
        self.assertEqual(opts["settle_ms"], int(params.get("browser.settle_ms")))
        self.assertEqual(opts["want_html"], True)
        self.assertLessEqual(opts["timeout_s"], float(params.get("browser.timeout_s")))
        self.assertTrue(res.ok)
        self.assertEqual(res.final_url, "https://www.douyin.com/video/1")
        self.assertEqual(res.title, "标题")
        self.assertEqual(res.ua, desktop_ua())          # 宿主回报的那一份
        self.assertEqual(res.candidates[1], "https://www.douyin.com/js/app.js")
        self.assertEqual(res.cookies[0]["name"], "ttwid")
        self.assertEqual(bc.stats()["calls"], 1)

    def test_desktop_ua_used_when_not_mobile(self):
        with patch.object(bc, "emit_event",
                          lambda e, d: bc.deliver(_ok_payload(d["request_id"]))):
            self.assertEqual(bc.render("https://x.com/").ua, desktop_ua())

    def test_non_http_url_rejected(self):
        with patch.object(bc, "emit_event", lambda *a: None):
            with self.assertRaises(ValueError):
                bc.render("file:///etc/passwd")
            with self.assertRaises(ValueError):
                bc.render("javascript:alert(1)")

    def test_timeout_cools_channel_down(self):
        with patch.object(bc, "emit_event", lambda *a: None), \
                patch.object(bc, "_budget", lambda deadline: 0.2), \
                patch.object(bc, "_SLACK_S", 0.0):
            res = bc.render("https://x.com/")
        self.assertFalse(res.ok)
        self.assertIn("超时", res.error)
        self.assertFalse(bc.available())               # 熔断中，不再让每条规则重等
        st = bc.stats()
        self.assertEqual(st["timeouts"], 1)
        self.assertGreater(st["cooldown_s"], 0)
        with patch.object(bc, "emit_event", lambda *a: None):
            with self.assertRaises(ValueError):
                bc.render("https://x.com/")

    def test_disabled_channel_is_not_available(self):
        params.apply({"browser.enabled": False})
        with patch.object(bc, "emit_event", lambda *a: None):
            self.assertFalse(bc.available())
            with self.assertRaises(ValueError):
                bc.render("https://x.com/")

    def test_late_answer_has_no_home(self):
        out = bc.deliver({"request_id": "no-such-request"})
        self.assertEqual(out["acknowledged"], False)

    def test_budget_follows_resolve_deadline(self):
        now = time.monotonic()
        self.assertLessEqual(bc._budget(None), 25.0)
        self.assertAlmostEqual(bc._budget(now + 5.0), 4.0, places=1)
        self.assertAlmostEqual(bc._budget(now + 60.0),
                               float(params.get("browser.timeout_s")))
        with self.assertRaises(TimeoutError):
            bc._budget(now + 0.5)
        params.apply({"browser.timeout_s": 4})
        self.assertAlmostEqual(bc._budget(now + 60.0), 4.0)


class TestCookiesAndCandidates(unittest.TestCase):
    def test_clean_cookies_keeps_only_usable_ones(self):
        cleaned = bc._clean_cookies([
            "ttwid=abc; Domain=.DOUYIN.COM; Path=/; HttpOnly",
            {"name": "novalue", "value": "", "domain": "x.com"},
            {"name": "nodomain", "value": "v"},
            {"name": "ok", "value": "1", "domain": "x.com", "path": "/p"},
            "not-a-cookie",
        ])
        self.assertEqual([(c["name"], c["domain"], c["path"]) for c in cleaned],
                         [("ttwid", "douyin.com", "/"), ("ok", "x.com", "/p")])

    def test_clean_cookies_caps_total_size(self):
        big = [{"name": f"n{i}", "value": "x" * 200, "domain": "x.com"} for i in range(100)]
        self.assertLess(len(bc._clean_cookies(big)), 100)

    def test_inject_cookies_into_session_jar(self):
        class Jar:
            def __init__(self):
                self.set_calls = []

            def set(self, name, value, domain=None, path=None):
                self.set_calls.append((name, value, domain, path))

        class S:
            cookies = Jar()

        s = S()
        n = bc.inject_cookies(s, [{"name": "a", "value": "1", "domain": "x.com", "path": "/"}])
        self.assertEqual(n, 1)
        self.assertEqual(s.cookies.set_calls, [("a", "1", "x.com", "/")])
        self.assertEqual(bc.inject_cookies(object(), [{"name": "a", "value": "1"}]), 0)
        # 单条灌不进去不该把整次解析炸掉
        self.assertEqual(bc.inject_cookies(s, [{"value": "无名字"}]), 0)

    def test_media_candidates_reuse_resolver_tables(self):
        picked = bc.media_candidates([
            "https://v.x.com/a.m3u8?sign=1",
            "https://v.x.com/b.mp4",
            "https://v.x.com/app.js",
            "https://v.x.com/page",
        ])
        self.assertEqual(picked, ["https://v.x.com/a.m3u8?sign=1", "https://v.x.com/b.mp4"])


class TestCommandRouting(unittest.TestCase):
    def setUp(self):
        bc.reset()

    def test_browser_result_delivers_once(self):
        got = []
        handler = CommandHandler()
        self.addCleanup(handler.cleanup)
        with patch.object(bc, "deliver",
                          lambda p: got.append(p) or {"acknowledged": True}):
            out = handler.execute("browser_result", {"request_id": "r1", "ok": True})
        self.assertEqual(got, [{"request_id": "r1", "ok": True}])
        self.assertEqual(out["acknowledged"], True)
        # 没有等待者时如实拒绝，绝不把渲染内容当成功塞给谁
        self.assertEqual(handler.execute("browser_result", None)["acknowledged"], False)


class TestSandboxBrowserChannel(unittest.TestCase):
    def _runtime(self, script, cache_dir=None):
        return JsRuntime("t", [script], prefs={},
                         on_prefs_change=lambda: None,
                         cache_dir=cache_dir or _tmp_dir(self))

    def _run(self, rt, session=None):
        return rt.call_rule(0, {"body": ""}, time.monotonic() + 30,
                            session or FakeSession())

    SCRIPT = """
    registerRule({name:'r',hosts:['x.com'],stage:'page',resolve:function(c){
      var r = host.render('https://www.douyin.com/video/1',
                          {html:false, mobile:true, settle_ms: 200});
      return { url: 'https://x.com/v', title: r.title, ua: r.ua, media: r.media,
               cand: r.candidates.length, jar: host.cookies('https://v.douyin.com/a.mp4'),
               htmlLen: (r.html || '').length,
               sniff: host.sniff('https://www.douyin.com/video/1', {mobile:true}) };
    }})
    """

    def test_render_and_sniff_through_sandbox(self):
        calls = []
        result = bc.RenderResult(
            ok=True, html="<html><video src='https://v.douyin.com/a.mp4'></video></html>",
            final_url="https://www.douyin.com/video/1", title="标题", ua=mobile_ua(),
            # 第二条不是媒体也不是已知媒体域名
            candidates=["https://v.douyin.com/a.mp4", "https://s.x.com/js/app.js"],
            # _clean_cookies 的产物形态：域名已去过前导点
            cookies=[{"name": "ttwid", "value": "T", "domain": "douyin.com", "path": "/"}])

        def fake_render(url, **kw):
            calls.append((url, kw))
            return result

        session = FakeSession()
        with patch.object(bc, "render", fake_render):
            out = self._run(self._runtime(self.SCRIPT), session)

        # 沙箱选项 → browser_channel kwargs
        self.assertEqual(calls[0][0], "https://www.douyin.com/video/1")
        self.assertEqual(calls[0][1]["want_html"], False)
        self.assertEqual(calls[0][1]["mobile"], True)
        self.assertEqual(calls[0][1]["settle_ms"], 200)
        self.assertEqual(len(calls), 2)                 # render + sniff 各一次
        # sniff 只要地址：不能让「省掉正文」停留在注释里
        self.assertEqual(calls[1][1]["want_html"], False)
        self.assertEqual(calls[1][1]["mobile"], True)

        self.assertEqual(out["title"], "标题")
        self.assertEqual(out["ua"], mobile_ua())
        self.assertEqual(out["htmlLen"], len(result.html))  # html:false 只是不喂给宿主
        self.assertEqual(out["media"], ["https://v.douyin.com/a.mp4"])
        self.assertEqual(out["sniff"], ["https://v.douyin.com/a.mp4"])
        self.assertEqual(out["cand"], 2)
        # WebView 罐子 → HTTP 罐子：规则接着 fetch 时就是同一个访客
        self.assertIn("ttwid=T", out["jar"])
        self.assertEqual(session.cookies.get("ttwid"), "T")

    def test_disabled_channel_raises_human_message(self):
        params.load_from({})
        params.apply({"browser.enabled": False})
        self.addCleanup(params.load_from, {})
        rt = self._runtime("""
        registerRule({name:'r',hosts:['x.com'],stage:'page',resolve:function(c){
          var msg = '';
          try { host.render('https://x.com/play'); } catch (e) { msg = String(e.message); }
          return { url: 'https://x.com/v', msg: msg, alive: host.md5('a') };
        }})
        """)
        out = self._run(rt)
        self.assertIn("浏览器通道不可用", out["msg"])
        # 宿主异常不能把上下文弄坏（下一句还在跑，且 MD5 正确）
        self.assertEqual(out["alive"], "0cc175b9c0f1b6a831c399e269772661")

    def test_render_failure_surfaces_host_error_text(self):
        params.load_from({})
        self.addCleanup(params.load_from, {})
        with patch.object(bc, "render",
                          lambda url, **kw: bc.RenderResult(False, error="页面加载超时（12s）")):
            rt = self._runtime("""
            registerRule({name:'r',hosts:['x.com'],stage:'page',resolve:function(c){
              var msg = '';
              try { host.render('https://x.com/play'); } catch (e) { msg = String(e.message); }
              return { url: 'https://x.com/v', msg: msg };
            }})
            """)
            self.assertIn("页面加载超时", self._run(rt)["msg"])


if __name__ == "__main__":
    unittest.main()
