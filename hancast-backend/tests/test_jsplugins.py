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
import hancast_sidecar.media.rules.jsplugins as jsplugins_mod
from hancast_sidecar.media.rules import base as rules_base
from hancast_sidecar.media.rules.jsplugins import (PluginError, PluginManager,
                                                   _MAX_ZIP_BYTES)
from hancast_sidecar.media.share_resolver import (
    ShareResolver, _EXT_MIME, _ext_of,
)


_bundled_patcher = None

# 打补丁前先留一份真的内置包目录引用，供「随包真包必须装得上」那条用例用
_REAL_BUNDLED_DIR = jsplugins_mod.bundled_plugins_dir


def setUpModule():
    """其余用例必须与仓库里随包的内置规则包隔离（播种专项另有 TestBundledSeeding）"""
    global _bundled_patcher
    _bundled_patcher = patch.object(jsplugins_mod, "bundled_plugins_dir",
                                    return_value="")
    _bundled_patcher.start()


def tearDownModule():
    if _bundled_patcher:
        _bundled_patcher.stop()


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


class TestBundledSeeding(unittest.TestCase):
    """内置规则包随包出货 → 启动播种进插件目录，与用户包同一套清单和开关"""

    def _dirs(self):
        base = _isolated_rules_dir(self)
        bundled = os.path.join(base, "bundled")
        os.makedirs(bundled)
        return os.path.join(base, "plugins"), bundled

    def _pack(self, bundled, name, version, rule_name="r"):
        d = os.path.join(bundled, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"name": name, "version": version,
                       "author": "HanCast", "scripts": ["main.js"]}, f)
        with open(os.path.join(d, "main.js"), "w", encoding="utf-8") as f:
            f.write("registerRule({name:'%s',hosts:['s.example'],stage:'page',"
                    "resolve:function(c){return {url:'https://s.example/a.mp4'};}});"
                    % rule_name)
        return d

    def test_seed_installs_and_marks_builtin(self):
        root, bundled = self._dirs()
        self._pack(bundled, "pack_a", "1.0.0")
        mgr = PluginManager(root, bundled)
        rules = mgr.refresh()
        self.assertEqual([r.plugin_name for r in rules], ["pack_a"])
        info = mgr.list_info()[0]
        self.assertTrue(info["builtin"])
        self.assertEqual(info["bundled_version"], "1.0.0")
        self.assertTrue(os.path.isfile(
            os.path.join(root, "pack_a", jsplugins_mod.BUILTIN_MARKER)))

    def test_upgrade_only_when_bundled_version_higher(self):
        root, bundled = self._dirs()
        pack = self._pack(bundled, "pack_a", "1.0.0", rule_name="old")
        PluginManager(root, bundled).refresh()

        # 同版本重启：不得覆盖（用户就地改过的脚本要留住）
        edited = os.path.join(root, "pack_a", "main.js")
        with open(edited, "w", encoding="utf-8") as f:
            f.write("// 用户就地改动\n")
        PluginManager(root, bundled)
        self.assertIn("用户就地改动", open(edited, encoding="utf-8").read())

        # 随包版本更高：覆盖并推进标记
        with open(os.path.join(pack, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "pack_a", "version": "1.2.0", "scripts": ["main.js"]}, f)
        self._pack(bundled, "pack_a", "1.2.0", rule_name="new")
        mgr = PluginManager(root, bundled)
        self.assertEqual([r.name for r in mgr.refresh()], ["new"])
        self.assertEqual(mgr.get("pack_a").bundled_version, "1.2.0")

    def test_user_pack_with_same_name_survives(self):
        root, bundled = self._dirs()
        self._pack(bundled, "pack_a", "1.0.0")
        mgr = PluginManager(root, "")                      # 先不播种，造用户包
        mgr.import_zip_bytes(make_zip(name="pack_a", js=(
            "registerRule({name:'mine',hosts:['u.example'],stage:'page',"
            "resolve:function(c){return null}});")))
        mgr2 = PluginManager(root, bundled)                # 再播种
        self.assertFalse(mgr2.get("pack_a").builtin)
        self.assertEqual([r.name for r in mgr2.refresh()], ["mine"])

    def test_builtin_cannot_be_removed_or_imported_over(self):
        root, bundled = self._dirs()
        self._pack(bundled, "pack_a", "1.0.0")
        mgr = PluginManager(root, bundled)
        with self.assertRaises(PluginError) as cm:
            mgr.remove("pack_a")
        self.assertIn("内置", str(cm.exception))
        with self.assertRaises(PluginError):
            mgr.import_zip_bytes(make_zip(name="pack_a"))
        self.assertTrue(os.path.isdir(os.path.join(root, "pack_a")))

    def test_state_survives_upgrade(self):
        root, bundled = self._dirs()
        self._pack(bundled, "pack_a", "1.0.0")
        mgr = PluginManager(root, bundled)
        mgr.refresh()
        mgr.set_rule_enabled("pack_a", "r", False)
        self._pack(bundled, "pack_a", "2.0.0")
        mgr2 = PluginManager(root, bundled)
        self.assertEqual(mgr2.refresh(), [])               # 停用清单没被升级冲掉
        self.assertFalse(mgr2.rule_enabled("pack_a", "r"))

    def test_all_shipped_packs_load(self):
        """仓库里随包的每个规则包都必须能装、能注册出规则——防手滑写坏 JS"""
        bundled = _REAL_BUNDLED_DIR()
        self.assertTrue(os.path.isdir(bundled), f"内置包目录不存在: {bundled}")
        shipped = sorted(n for n in os.listdir(bundled)
                         if os.path.isfile(os.path.join(bundled, n, "manifest.json")))
        self.assertTrue(shipped, "随包规则包为空")
        root = os.path.join(_isolated_rules_dir(self), "plugins")
        mgr = PluginManager(root, bundled)
        infos = {p["name"]: p for p in mgr.list_info()}
        self.assertEqual(sorted(infos), shipped)
        for name, info in infos.items():
            self.assertFalse(info["error"], f"{name} 加载失败: {info['error']}")
            self.assertTrue(info["rules"], f"{name} 一条规则都没注册出来")
            self.assertTrue(info["builtin"])
        # 模板包只匹配保留 TLD，绝不能抢真实站点的解析
        tpl = [r for r in mgr.refresh() if r.plugin_name == "hancast-template"]
        self.assertTrue(tpl)
        for rule in tpl:
            self.assertEqual(rule.hosts, ("hancast-template.invalid",))
        self.assertTrue(any(r.plugin_name == "hancast-douyin" for r in mgr.refresh()))


class TestUnifiedRuleManagement(unittest.TestCase):
    """规则总览：脚本包 / 声明式 JSON / 内置 Python 规则一屏列全，开关共用一份状态"""

    def setUp(self):
        rules_base.set_builtin_disabled(set())
        self.rules_dir = _isolated_rules_dir(self)
        self.resolver = ShareResolver(FakeMediaServer(), rules_dir=self.rules_dir)

    def tearDown(self):
        rules_base.set_builtin_disabled(set())

    def _write_json_rule(self, name="maccms-box", hosts=("demo.example",),
                         enabled=True):
        spec = {"name": name, "hosts": list(hosts), "stage": "page",
                "steps": [{"fetch": "{url}"}], "output": ["{body}"]}
        if not enabled:
            spec["enabled"] = False
        with open(os.path.join(self.rules_dir, f"{name}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(spec, f)

    def test_inventory_lists_all_three_sources(self):
        self.resolver.plugin_mgr.import_zip_bytes(make_zip())
        self._write_json_rule()
        with open(os.path.join(self.rules_dir, "broken.json"), "w",
                  encoding="utf-8") as f:
            f.write("{not json")
        inv = self.resolver.rule_inventory()
        self.assertEqual(inv["rules_dir"], self.rules_dir)
        self.assertIn("demo", [p["name"] for p in inv["script_packs"]])
        listed = {d["name"]: d for d in inv["declarative"]}
        self.assertEqual(listed["maccms-box"]["hosts"], ["demo.example"])
        # 写坏的那条也要露面，否则用户只能靠猜
        self.assertTrue(listed["broken.json"]["error"])
        builtin = {b["name"]: b for b in inv["builtin"]}
        self.assertIn("douyin", builtin)
        self.assertTrue(builtin["douyin"]["enabled"])
        # 内置清单里不能再混进包规则/JSON 规则
        self.assertNotIn("demo", builtin)

    def test_disabling_builtin_rule_takes_effect(self):
        html = '<html><body>plain</body></html>'
        with patch.object(ShareResolver, "_open",
                          side_effect=lambda u, d, session=None, **kw:
                              (u, "text/html; charset=utf-8", None, html.encode())):
            # 关掉内置抖音规则后，page 阶段就不该再命中它
            self.resolver.plugin_mgr.set_builtin_enabled("douyin", False)
            self.assertEqual([r.name for r in rules_base.matching(
                "https://www.douyin.com/video/1", "page")], [])
            self.resolver.plugin_mgr.set_builtin_enabled("douyin", True)
            self.assertIn("douyin", [r.name for r in rules_base.matching(
                "https://www.douyin.com/video/1", "page")])

    def test_builtin_switch_persists_and_reloads(self):
        self.resolver.plugin_mgr.set_builtin_enabled("bilibili", False)
        reloaded = PluginManager(self.resolver.plugin_mgr.root)
        self.assertEqual(reloaded._state["disabled_builtin"], ["bilibili"])
        self.assertFalse(reloaded.builtin_enabled("bilibili"))
        self.assertTrue(reloaded.builtin_enabled("douyin"))

    def test_inventory_shows_disabled_builtin_state(self):
        self.resolver.plugin_mgr.set_builtin_enabled("douyin", False)
        builtin = {b["name"]: b for b in self.resolver.rule_inventory()["builtin"]}
        self.assertFalse(builtin["douyin"]["enabled"])
        self.assertTrue(builtin["bilibili"]["enabled"])


if __name__ == "__main__":
    unittest.main()