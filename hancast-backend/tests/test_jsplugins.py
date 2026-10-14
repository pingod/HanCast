"""JS 解析插件：沙箱运行时、zip 导入、注册表接入、管线联动 — 全部离线，不触网

覆盖:
- JsRuntime：host 网络函数（假会话）、select/at/str、编码工具、缓存、prefs
- 死循环中断 / 内存上限 / JS 异常消息透传
- zip 导入：合法包安装、路径穿越/多余文件/双 manifest/坏 name 拒收
- 规则注册表：命中、via 命名、停用开关持久化、坏脚本只损伤自己
- 真实解析管线联动：resolve → 插件规则 → follow/publish/info
"""

import io
import json
import os
import shutil
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

import hancast_sidecar.media.rules.jsruntime as jsruntime_mod
from hancast_sidecar.media.rules.jsplugins import (PluginError, PluginManager,
                                                   _MAX_ZIP_BYTES)
from hancast_sidecar.media.share_resolver import (
    ShareResolver, _EXT_MIME, _ext_of,
)


class FakeMediaServer:
    def __init__(self):
        self.proxies: list[str] = []

    def register_proxy(self, remote_url, headers=None, suffix=""):
        self.proxies.append(remote_url)
        return f"http://127.0.0.1:9/proxy/{len(self.proxies)}{suffix}"


def _no_follow(url, session, referer, deadline, _hop=0):
    return url, _EXT_MIME.get(_ext_of(url)) or "video/mp4"


def _no_publish(uri, mime, session, referer, deadline, via, extra_headers=None):
    return uri, mime, via


def _isolated_rules_dir(test) -> str:
    d = tempfile.mkdtemp(prefix="hancast-jsplug-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


class FakeResponse:
    def __init__(self, status=200, url="https://fetched.example/x",
                 ctype="text/html; charset=utf-8", body=b""):
        self.status_code = status
        self.url = url
        self.headers = {"Content-Type": ctype}
        self._body = body

    def iter_content(self, chunk_size=65536):
        yield self._body

    def close(self):
        pass


class FakeSession:
    def __init__(self, resp: FakeResponse):
        self.resp = resp
        self.calls: list[tuple] = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.resp


DEMO_JS = """
registerRule({
  id: 'demo-rule', name: 'demo', hosts: ['demo.example'], stage: 'page',
  resolve: function (c) {
    var m = /"playUrl"\\s*:\\s*"([^"]+)"/.exec(c.body);
    if (!m) throw new Error('no playUrl');
    if (c.body.indexOf('NEED-HEADER') >= 0)
      return { url: m[1], headers: { Referer: c.url } };
    return { url: m[1], title: 'JS标题' };
  }
});
"""


def make_zip(name="demo", js=DEMO_JS, manifest=None, extra_entries=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest or {
            "name": name, "version": "1.0.0", "author": "t",
            "description": "d", "scripts": ["site.js"],
        }))
        zf.writestr("site.js", js)
        for entry_name, data in (extra_entries or {}).items():
            zf.writestr(entry_name, data)
    return buf.getvalue()


class TestJsRuntime(unittest.TestCase):
    def _runtime(self, script, **kw):
        return jsruntime_mod.JsRuntime(
            "t", [script], prefs=kw.get("prefs", {}),
            on_prefs_change=kw.get("on_prefs_change", lambda: None),
            cache_dir=kw.get("cache_dir") or _isolated_rules_dir(self))

    def test_resolve_string_and_object_return(self):
        rt = self._runtime("registerRule({name:'r',hosts:['x.com'],stage:'page',"
                           "resolve:function(c){ return c.echo; }})")
        self.assertEqual(rt.call_rule(0, {"echo": "https://a/x.m3u8"},
                                      time.monotonic() + 30, None),
                         {"url": "https://a/x.m3u8"})
        self.assertIsNone(rt.call_rule(0, {"echo": None},
                                       time.monotonic() + 30, None))

    def test_host_network_and_json_helpers(self):
        session = FakeSession(FakeResponse(
            body=json.dumps({"data": {"list": [{"u": "/v/1.m3u8"}]}}).encode()))
        rt = self._runtime("""
        registerRule({name:'r',hosts:['x.com'],stage:'page',resolve:function(c){
          var j = host.getJson('https://api.example/list');
          var u = str(j, 'data.list.0.u');
          var doc = host.select('<div><a href="/p/1">t</a></div>', 'a', 'https://x.com/');
          return { url: 'https://x.com' + u, ref: doc[0].abs.href + '|' + doc[0].text + '|' + at(j,'data.list.-1.u') };
        }})
        """)
        out = rt.call_rule(0, {}, time.monotonic() + 30, session)
        self.assertEqual(out["url"], "https://x.com/v/1.m3u8")
        self.assertEqual(out["ref"], "https://x.com/p/1|t|/v/1.m3u8")
        self.assertEqual(session.calls[0][0], "GET")

    def test_host_tools_and_prefs(self):
        saved = []
        rt = self._runtime("""
        registerRule({name:'r',hosts:['x.com'],stage:'page',resolve:function(c){
          host.setPref('k', 'v1');
          var enc = host.base64Encode('汉'), dec = host.base64Decode(enc);
          return { url: 'https://x.com/v', info: host.md5('a') + '|' + dec + '|' +
                  host.htmlDecode('A&amp;B') + '|' + host.getPref('k', '') };
        }})
        """, prefs={}, on_prefs_change=lambda: saved.append(1))
        out = rt.call_rule(0, {}, time.monotonic() + 30, None)
        self.assertTrue(saved)
        self.assertEqual(out["info"], "0cc175b9c0f1b6a831c399e269772661|汉|A&B|v1")

    def test_infinite_loop_interrupted(self):
        with patch.object(jsruntime_mod, "_MIN_WATCHDOG", 0.3):
            rt = self._runtime("registerRule({name:'r',hosts:['x.com'],stage:'page',"
                               "resolve:function(c){ while(true){} }})")
            with self.assertRaises(TimeoutError):
                rt.call_rule(0, {}, time.monotonic() + 30, None)

    def test_js_error_message_survives(self):
        rt = self._runtime("registerRule({name:'r',hosts:['x.com'],stage:'page',"
                           "resolve:function(c){ throw new Error('站点改版了'); }})")
        with self.assertRaises(Exception) as cm:
            rt.call_rule(0, {}, time.monotonic() + 30, None)
        self.assertIn("站点改版了", str(cm.exception))

    def test_memory_limit(self):
        rt = self._runtime("registerRule({name:'r',hosts:['x.com'],stage:'page',"
                           "resolve:function(c){ var a=[]; for(var i=0;i<40;i++)"
                           " a.push(new Array(1024*1024).fill('x')); }})")
        with self.assertRaises(Exception) as cm:
            rt.call_rule(0, {}, time.monotonic() + 30, None)
        self.assertIn("memory", str(cm.exception).lower())


class TestPluginImport(unittest.TestCase):
    def _manager(self) -> PluginManager:
        root = os.path.join(_isolated_rules_dir(self), "plugins")
        return PluginManager(root)

    def test_import_and_list(self):
        mgr = self._manager()
        info = mgr.import_zip_bytes(make_zip())
        self.assertEqual(info["name"], "demo")
        self.assertEqual(len(info["rules"]), 1)
        self.assertEqual(info["rules"][0]["id"], "demo-rule")
        self.assertTrue(info["rules"][0]["enabled"])
        self.assertTrue(os.path.isfile(
            os.path.join(mgr.root, "demo", "site.js")))

    def test_reimport_overwrites(self):
        mgr = self._manager()
        mgr.import_zip_bytes(make_zip())
        mgr.import_zip_bytes(make_zip(manifest={
            "name": "demo", "version": "2.0.0", "scripts": ["site.js"]}))
        self.assertEqual(mgr.list_info()[0]["version"], "2.0.0")

    def test_path_traversal_rejected(self):
        mgr = self._manager()
        with self.assertRaises(PluginError):
            mgr.import_zip_bytes(make_zip(extra_entries={"../evil.js": "x"}))

    def test_disallowed_entry_rejected(self):
        mgr = self._manager()
        with self.assertRaises(PluginError):
            mgr.import_zip_bytes(make_zip(extra_entries={"bin.exe": "MZ"}))

    def test_two_manifests_rejected(self):
        mgr = self._manager()
        with self.assertRaises(PluginError):
            mgr.import_zip_bytes(make_zip(extra_entries={"sub/manifest.json": "{}"}))

    def test_bad_name_rejected(self):
        mgr = self._manager()
        with self.assertRaises(PluginError):
            mgr.import_zip_bytes(make_zip(manifest={
                "name": "../evil", "scripts": ["site.js"]}))

    def test_missing_script_rejected(self):
        mgr = self._manager()
        with self.assertRaises(PluginError):
            mgr.import_zip_bytes(make_zip(manifest={
                "name": "demo", "scripts": ["ghost.js"]}))

    def test_oversized_zip_rejected(self):
        mgr = self._manager()
        big = os.urandom(_MAX_ZIP_BYTES + 1024)   # 随机数据不可压
        with self.assertRaises(PluginError):
            mgr.import_zip_bytes(big)

    def test_remove_cleans_state(self):
        mgr = self._manager()
        mgr.import_zip_bytes(make_zip())
        mgr.prefs_for("demo")["token"] = "t"
        mgr.save_state()
        mgr.remove("demo")
        self.assertEqual(mgr.list_info(), [])
        self.assertNotIn("demo", mgr._state["prefs"])
        self.assertFalse(os.path.exists(os.path.join(mgr.root, "demo")))


class TestRegistryAndPipeline(unittest.TestCase):
    def setUp(self):
        self.rules_dir = _isolated_rules_dir(self)
        self.resolver = ShareResolver(FakeMediaServer(), rules_dir=self.rules_dir)
        self.resolver.plugin_mgr.import_zip_bytes(make_zip())

    def _resolve(self, html: str, url="https://demo.example/play/1") -> dict:
        # 注意：patch.object 换成 MagicMock 后不再是描述符，side_effect 不接收 self
        with patch.object(ShareResolver, "_open",
                          side_effect=lambda u, d, session=None, **kw:
                              (u, "text/html; charset=utf-8", None, html.encode())):
            with patch.object(ShareResolver, "_follow_stream",
                              side_effect=lambda u, s, r, d: _no_follow(u, s, r, d)), \
                 patch.object(ShareResolver, "_publish",
                              side_effect=lambda u, m, s, r, d, v, **kw:
                                  _no_publish(u, m, s, r, d, v)):
                return self.resolver.resolve(url)

    def test_plugin_rule_wins_in_pipeline(self):
        html = ('<html><title>t</title><script>'
                'var cfg={"playUrl":"https://cdn.example/v/1.m3u8?token=a"};'
                '</script></html>')
        result = self._resolve(html)
        self.assertEqual(result["via"], "demo/demo")
        self.assertEqual(result["title"], "JS标题")
        self.assertEqual(result["uri"], "https://cdn.example/v/1.m3u8?token=a")

    def test_plugin_headers_reach_publish(self):
        captured = {}

        def pub(uri, mime, session, referer, deadline, via, extra_headers=None):
            captured["extra"] = extra_headers
            return uri, mime, via

        html = ('<html>NEED-HEADER<script>'
                'var cfg={"playUrl":"https://cdn.example/v/1.mp4"};</script></html>')
        with patch.object(ShareResolver, "_open",
                          side_effect=lambda u, d, session=None, **kw:
                              (u, "text/html; charset=utf-8", None, html.encode())), \
             patch.object(ShareResolver, "_follow_stream",
                          side_effect=lambda u, s, r, d: _no_follow(u, s, r, d)), \
             patch.object(ShareResolver, "_publish", side_effect=pub):
            self.resolver.resolve("https://demo.example/play/2")
        self.assertEqual(captured["extra"], {"Referer": "https://demo.example/play/2"})

    def test_broken_rule_yields_not_crashes(self):
        self.resolver.plugin_mgr.import_zip_bytes(make_zip(
            name="bad", js="registerRule({name:'b',hosts:['demo.example'],"
                           "stage:'page',resolve:function(c){ throw new Error('改版'); }});"))
        html = '<html><body>no streams here</body></html>'
        with self.assertRaises(ValueError):
            self._resolve(html)     # 插件让路后，通用提取也拿不到 → 明确失败而非误交付

    def test_broken_script_isolated_from_others(self):
        mgr = self.resolver.plugin_mgr
        mgr.import_zip_bytes(make_zip(name="broken",
                                      js="this is not javascript {{{"))
        infos = {p["name"]: p for p in mgr.list_info()}
        self.assertTrue(infos["broken"]["error"])
        ok = mgr.get("demo").build_rules()
        self.assertEqual(len(ok), 1)     # 好插件不受连累

    def test_disable_persists_and_unregisters(self):
        mgr = self.resolver.plugin_mgr
        mgr.set_rule_enabled("demo", "demo-rule", False)
        reloaded = PluginManager(mgr.root)
        reloaded.refresh()
        self.assertEqual([r.name for r in reloaded.refresh()], [])
        mgr.set_rule_enabled("demo", "demo-rule", True)
        reloaded2 = PluginManager(mgr.root)
        self.assertEqual([r.name for r in reloaded2.refresh()], ["demo"])

    def test_disabled_rule_skipped_in_registry(self):
        mgr = self.resolver.plugin_mgr
        mgr.set_rule_enabled("demo", "demo-rule", False)
        html = '<html><body>plain</body></html>'
        with self.assertRaises(ValueError):
            self._resolve(html)      # 规则停用 → 走不到插件 → 通用提取失败


if __name__ == "__main__":
    unittest.main()
