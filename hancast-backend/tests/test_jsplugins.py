"""JS 解析插件：沙箱运行时、zip 导入、注册表接入、管线联动 — 全部离线，不触网

覆盖:
- JsRuntime：host 网络函数（假会话）、select/at/str、编码工具、缓存、prefs
- 死循环中断 / 内存上限 / JS 异常消息透传
- zip 导入：合法包安装、路径穿越/多余文件/双 manifest/坏 name 拒收
- 规则注册表：命中、via 命名、停用开关持久化、坏脚本只损伤自己
- 兜底作用域 ``hosts:['*']``：不认域名的包规则排在站点专属规则之后试
- 真实解析管线联动：resolve → 插件规则 → follow/publish/info
- 全新安装零规则：程序不编译进任何站点规则，装包之前解析不了 B 站链接
- 规则包分发：fetch_index 过滤、check_updates 分堆、pull 安装 + sha256 校验、
  _version_key 版本序、index_url 唯一来源（后台参数 rules.index_url）
- 规则总览：只剩 脚本包/声明式 JSON 两源，包规则 priority 钳在 4-8
"""

import hashlib
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


class FakeJsonResponse:
    def __init__(self, doc, status=200):
        self._doc = doc
        self.status_code = status

    def json(self):
        return self._doc

    def iter_content(self, chunk_size=65536):
        yield json.dumps(self._doc).encode("utf-8")

    def close(self):
        pass


class FakeNet:
    """requests.Session 替身：按 URL 派发假响应（dict→索引 JSON，bytes→zip）

    fetch_index / import_url 都走 `with requests.Session() as s: s.get(...)`，
    一个对象同时当会话与上下文管理器用。
    """

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []
        self.kw: list[dict] = []

    def new_session(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kw):
        self.calls.append(url)
        self.kw.append(kw)
        if url not in self.routes:
            raise AssertionError(f"用例未桩掉该地址（保持离线）: {url}")
        doc = self.routes[url]
        if isinstance(doc, dict):
            return FakeJsonResponse(doc)
        return FakeResponse(body=doc)


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

    def test_wildcard_pack_rule_defers_to_site_rule(self):
        """包规则同样认 hosts:['*'] 兜底，且排在站点专属规则之后试

        兜底那条名字取 a-generic：注册表按 (priority, name) 排它本来在前，
        「专属在前」只能靠 matching 的分组排序实现。
        """
        self.resolver.plugin_mgr.import_zip_bytes(make_zip(
            name="a-generic",
            js="registerRule({name:'a-generic',hosts:['*'],stage:'page',"
               "resolve:function(c){return {url:'https://g.example/g.m3u8'};}});"))
        url = "https://demo.example/play/1"
        self.assertEqual([r.name for r in rules_base.matching(url, "page")],
                         ["demo", "a-generic"])
        html = ('<html><script>var cfg={"playUrl":'
                '"https://cdn.example/v/1.m3u8?token=a"};</script></html>')
        result = self._resolve(html, url)
        self.assertEqual(result["via"], "demo/demo")

    def test_wildcard_pack_rule_catches_unknown_site(self):
        """没有精确规则的站：兜底包规则接手，不必为每个采集站单独发包"""
        self.resolver.plugin_mgr.import_zip_bytes(make_zip(
            name="cms",
            js="registerRule({name:'cms',hosts:['*'],stage:'page',"
               "resolve:function(c){return {url:'https://g.example/g.m3u8'};}});"))
        result = self._resolve("<html>随便什么正文</html>",
                               "https://some-new-clone.example/play/9")
        self.assertEqual(result["via"], "cms/cms")
        self.assertEqual(result["uri"], "https://g.example/g.m3u8")

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


class TestFreshInstallRegistersNothing(unittest.TestCase):
    """程序不编译进任何站点规则：装包之前，注册表必须是空的（这是产品行为，不是缺陷）"""

    def setUp(self):
        self.rules_dir = _isolated_rules_dir(self)
        self.resolver = ShareResolver(FakeMediaServer(), rules_dir=self.rules_dir)

    def test_empty_dirs_register_zero_rules(self):
        # rule_inventory 会触发两源整体重载，顺带清掉本文件其它用例留下的全局注册
        inv = self.resolver.rule_inventory()
        self.assertEqual(inv["script_packs"], [])
        self.assertEqual(inv["declarative"], [])
        self.assertEqual(rules_base.registered(), [])
        for url in ("https://www.bilibili.com/video/BV1xx411c7mD",
                    "https://v.douyin.com/abc123/",
                    "https://www.douyin.com/video/74123456789"):
            for stage in ("url", "page"):
                self.assertEqual(rules_base.matching(url, stage), [])

    def test_bilibili_link_fails_without_any_pack(self):
        html = "<html><body>no streams here</body></html>"
        with patch.object(ShareResolver, "_open",
                          side_effect=lambda u, d, session=None, **kw:
                              (u, "text/html; charset=utf-8", None, html.encode())):
            with self.assertRaises(ValueError) as cm:
                self.resolver.resolve("看看 https://b23.tv/AbCdEf 这个视频")
        self.assertIn("无法解析出可播放地址", str(cm.exception))


INDEX_URL = "https://rules.example/index.json"
ZIP_URL = "https://dl.example/demo.zip"


def _entry(name, version, url, **kw) -> dict:
    return {"name": name, "version": version, "url": url, "description": "d", **kw}


class TestPackDistribution(unittest.TestCase):
    """规则包分发：官方索引 → 检查更新 → 一键安装，全程假会话离线"""

    def _manager(self) -> PluginManager:
        root = os.path.join(_isolated_rules_dir(self), "plugins")
        return PluginManager(root)

    def test_fetch_index_filters_bad_entries(self):
        # 索引是「下载下来就要执行里面 JS」的入口：坏名字与明文地址一律丢掉，
        # 但只丢这一条，不能让一个坏条目拖垮整页检查更新
        net = FakeNet({INDEX_URL: {"packs": [
            _entry("good", "1.0.0", ZIP_URL),
            _entry("bad name", "1.0.0", ZIP_URL),
            _entry("sub/evil", "1.0.0", ZIP_URL),
            _entry("cleartext", "1.0.0", "http://dl.example/c.zip"),
            "不是对象",
        ]}})
        with patch.object(jsplugins_mod.requests, "Session", net.new_session):
            idx = self._manager().fetch_index(INDEX_URL)
        self.assertEqual(idx["url"], INDEX_URL)
        self.assertEqual(sorted(idx["packs"]), ["good"])
        self.assertEqual(idx["packs"]["good"]["url"], ZIP_URL)

    def test_fetch_index_requires_index_url(self):
        with patch.object(jsplugins_mod, "index_url", return_value=""):
            with self.assertRaises(PluginError) as cm:
                self._manager().fetch_index()
        self.assertIn("索引地址", str(cm.exception))

    def test_index_url_reads_background_param(self):
        """索引地址唯一来源是后台参数 rules.index_url（两处各写一遍早晚对不上）"""
        from hancast_sidecar import params
        with patch.object(params, "get",
                          lambda k: "https://mirror.example/index.json"
                          if k == "rules.index_url" else None):
            self.assertEqual(jsplugins_mod.index_url(),
                             "https://mirror.example/index.json")
        net = FakeNet({"https://mirror.example/index.json": {"packs": []}})
        with patch.object(params, "get",
                          lambda k: "https://mirror.example/index.json"
                          if k == "rules.index_url" else None), \
             patch.object(jsplugins_mod.requests, "Session", net.new_session):
            idx = self._manager().fetch_index()      # 不传 url，也必须落到参数配的地址
        self.assertEqual(net.calls, ["https://mirror.example/index.json"])
        self.assertEqual(idx["packs"], {})

    def test_fetch_index_timeout_follows_background_param(self):
        """单次索引请求的等待上限读 rules.index_timeout_s，不写死 15s：
        墙内冷启动 15s 必超时，而失败在界面上看起来像「没有更新」，没人会怀疑超时"""
        from hancast_sidecar import params
        net = FakeNet({INDEX_URL: {"packs": []}})
        with patch.object(params, "get",
                          lambda k: 40 if k == "rules.index_timeout_s" else None), \
             patch.object(jsplugins_mod.requests, "Session", net.new_session):
            self._manager().fetch_index(INDEX_URL)
        self.assertEqual(net.kw[0]["timeout"], 40)

        # 显式传参说话（调用方要重试或缩短时不该被登记表绑住）
        net2 = FakeNet({INDEX_URL: {"packs": []}})
        with patch.object(jsplugins_mod.requests, "Session", net2.new_session):
            self._manager().fetch_index(INDEX_URL, timeout=3)
        self.assertEqual(net2.kw[0]["timeout"], 3)

    def test_index_timeout_default_replaces_the_old_15s(self):
        """登记表默认值本身也是契约：写死 15s 时墙内冷启动点「检查更新」必失败，
        而界面只说「没有可用更新」，没人会怀疑是超时"""
        from hancast_sidecar import params
        self.assertEqual(params.get("rules.index_timeout_s"), 40)
        self.assertEqual(jsplugins_mod.index_timeout(), 40)

    def test_check_updates_buckets(self):
        mgr = self._manager()
        mgr.import_zip_bytes(make_zip(name="aged"))      # 本地 1.0.0
        mgr.import_zip_bytes(make_zip(name="uptodate"))  # 本地 1.0.0
        net = FakeNet({INDEX_URL: {"packs": [
            _entry("uptodate", "1.0.0", ZIP_URL),        # 同版本：两边都不出现
            _entry("aged", "1.10.0", ZIP_URL),           # 版本比较必须按数字段
            _entry("untried", "0.9.0", ZIP_URL),         # 官方有、本机没装 → available
        ]}})
        with patch.object(jsplugins_mod.requests, "Session", net.new_session):
            res = mgr.check_updates(INDEX_URL)
        self.assertEqual(res["index_url"], INDEX_URL)
        self.assertEqual(res["installed"], 2)
        self.assertEqual([(u["name"], u["version"], u["current"])
                          for u in res["updates"]], [("aged", "1.10.0", "1.0.0")])
        self.assertEqual([a["name"] for a in res["available"]], ["untried"])

    def test_pull_installs_from_index(self):
        mgr = self._manager()
        blob = make_zip(name="demo")
        net = FakeNet({INDEX_URL: {"packs": [
            _entry("demo", "1.0.0", ZIP_URL,
                   sha256=hashlib.sha256(blob).hexdigest())]},
            ZIP_URL: blob})
        with patch.object(jsplugins_mod.requests, "Session", net.new_session):
            info = mgr.pull("demo", url=INDEX_URL)
        self.assertEqual(info["name"], "demo")
        self.assertEqual(mgr.get("demo").version, "1.0.0")
        self.assertEqual(net.calls, [INDEX_URL, ZIP_URL])

    def test_pull_command_wraps_full_pack_list(self):
        """命令层必须回 {"plugins": 全量清单}：前端拿它整块刷新列表，
        回单个包 info 会让已装的包在界面上全消失（v2.3.3 就是踩在这里）"""
        from types import SimpleNamespace
        from hancast_sidecar.commands import CommandHandler
        mgr = self._manager()
        blob = make_zip(name="demo")
        net = FakeNet({INDEX_URL: {"packs": [
            _entry("demo", "1.0.0", ZIP_URL,
                   sha256=hashlib.sha256(blob).hexdigest())]},
            ZIP_URL: blob})
        # 命令层的规则包入口现在都过一份任务准入（与后台安装互斥）
        from hancast_sidecar.media.rules.tasks import PluginTaskRunner
        fake = SimpleNamespace(share_resolver=SimpleNamespace(plugin_mgr=mgr),
                              _plugin_tasks=PluginTaskRunner(mgr))
        with patch.object(jsplugins_mod.requests, "Session", net.new_session):
            res = CommandHandler._plugin_pull(fake, {"name": "demo", "url": INDEX_URL})
        self.assertEqual([p["name"] for p in res["plugins"]], ["demo"])
        with self.assertRaises(ValueError):
            CommandHandler._plugin_pull(fake, {"name": ""})

    def test_pull_rejects_sha256_mismatch(self):
        """摘要对不上就不装：这是「下载下来就执行」的入口，不能只信传输层"""
        mgr = self._manager()
        net = FakeNet({INDEX_URL: {"packs": [
            _entry("demo", "1.0.0", ZIP_URL, sha256="0" * 64)]},
            ZIP_URL: make_zip(name="demo")})
        with patch.object(jsplugins_mod.requests, "Session", net.new_session):
            with self.assertRaises(PluginError) as cm:
                mgr.pull("demo", url=INDEX_URL)
        self.assertIn("sha256", str(cm.exception))
        self.assertEqual(mgr.list_info(), [])
        self.assertFalse(os.path.exists(os.path.join(mgr.root, "demo")))

    def test_pull_falls_back_to_pack_update_url(self):
        """官方索引拉不动时，退回包自己声明的 update_url（作者自托管的更新路）"""
        mgr = self._manager()
        mgr.import_zip_bytes(make_zip(name="solo", manifest={
            "name": "solo", "version": "1.0.0", "scripts": ["site.js"],
            "update_url": ZIP_URL}))
        net = FakeNet({ZIP_URL: make_zip(name="solo", manifest={
            "name": "solo", "version": "1.0.1", "scripts": ["site.js"]})})
        with patch.object(jsplugins_mod, "index_url", return_value=""), \
             patch.object(jsplugins_mod.requests, "Session", net.new_session):
            mgr.pull("solo")
        self.assertEqual(mgr.get("solo").version, "1.0.1")
        self.assertEqual(net.calls, [ZIP_URL])

    def test_remove_accepts_any_pack(self):
        """「内置包不许删」的特殊身份已废除：装进来的包都能删"""
        mgr = self._manager()
        mgr.import_zip_bytes(make_zip(name="demo"))
        mgr.remove("demo")
        self.assertEqual(mgr.list_info(), [])

    def test_version_key_orders_numerically(self):
        vk = jsplugins_mod._version_key
        versions = ["1.10.0", "1.9.9", "2.0.0", "1.2"]
        self.assertEqual(sorted(versions, key=vk),
                         ["1.2", "1.9.9", "1.10.0", "2.0.0"])
        self.assertEqual(vk(""), (0,))
        self.assertEqual(vk("1.x.3"), (1, 0, 3))       # 非数字段按 0
        self.assertEqual(vk("1_2-3.4"), (1, 2, 3, 4))  # 分隔符 . _ - 通吃


class TestRuleInventory(unittest.TestCase):
    """规则总览：脚本包 / 声明式 JSON —— 「内置 Python 规则」这一源已废除"""

    def setUp(self):
        self.rules_dir = _isolated_rules_dir(self)
        self.resolver = ShareResolver(FakeMediaServer(), rules_dir=self.rules_dir)

    def _write_json_rule(self, name="maccms-box", hosts=("demo.example",),
                         enabled=True):
        spec = {"name": name, "hosts": list(hosts), "stage": "page",
                "steps": [{"fetch": "{url}"}], "output": ["{body}"]}
        if not enabled:
            spec["enabled"] = False
        with open(os.path.join(self.rules_dir, f"{name}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(spec, f)

    def test_inventory_lists_both_sources(self):
        self.resolver.plugin_mgr.import_zip_bytes(make_zip())
        self._write_json_rule()
        with open(os.path.join(self.rules_dir, "broken.json"), "w",
                  encoding="utf-8") as f:
            f.write("{not json")
        inv = self.resolver.rule_inventory()
        # 来源只剩两路：再冒出第三个 key 就说明「内置规则」被偷偷加了回来
        self.assertEqual(set(inv), {"rules_dir", "script_packs", "declarative"})
        self.assertEqual(inv["rules_dir"], self.rules_dir)
        self.assertIn("demo", [p["name"] for p in inv["script_packs"]])
        listed = {d["name"]: d for d in inv["declarative"]}
        self.assertEqual(listed["maccms-box"]["hosts"], ["demo.example"])
        # 写坏的那条也要露面，否则用户只能靠猜
        self.assertTrue(listed["broken.json"]["error"])

    def test_pack_info_has_no_builtin_identity(self):
        info = self.resolver.plugin_mgr.import_zip_bytes(make_zip())
        self.assertNotIn("builtin", info)
        self.assertNotIn("bundled_version", info)
        self.assertIn("update_url", info)
        fresh = {p["name"]: p for p in self.resolver.plugin_mgr.list_info()}
        self.assertNotIn("builtin", fresh["demo"])

    def test_pack_rule_priority_clamped(self):
        """包规则 priority 被钳在 4-8：坏心/笔误的规则既插不了队也垫不到底"""
        mgr = self.resolver.plugin_mgr
        mgr.import_zip_bytes(make_zip(
            name="greedy", js="registerRule({name:'g',hosts:['g.example'],"
                              "stage:'page',priority:99,"
                              "resolve:function(c){return null}});"))
        mgr.import_zip_bytes(make_zip(
            name="humble", js="registerRule({name:'h',hosts:['h.example'],"
                              "stage:'page',priority:0,"
                              "resolve:function(c){return null}});"))
        self.assertEqual(mgr.get("greedy").build_rules()[0].priority, 8)
        self.assertEqual(mgr.get("humble").build_rules()[0].priority, 4)
        # 声明式规则固定 5：与包规则同段，同值按规则名决胜
        self._write_json_rule()
        ext = rules_base.refresh_external(self.rules_dir)
        self.assertEqual([r.priority for r in ext], [5])


class TestShippedDouyinPack(unittest.TestCase):
    """官方抖音包对着「假抖音」跑：没领券就只有验证码中间页

    现场取证（2026-10-14）：分享页只对带 ttwid 的请求回吐内嵌 JSON，
    没有这张券时 6/6 全是中间页；先 GET iesdouyin 根路径领券再取分享页则 6/6 命中。
    """

    CAPTCHA = ("<html><title>验证码中间页</title>"
               "<script src=\"https://.../captcha/index.js\"></script></html>")
    SHARE_JSON = (
        '{"video":{"play_addr":{"url_list":['
        '"https:\\u002F\\u002Fwww.douyin.com\\u002Faweme\\u002Fv1\\u002Fplaywm'
        '\\u002F?video_id=v0200f\\u0026ratio=1080p"}]},"desc":"抖音标题"}')

    class Net:
        def __init__(self, give_captcha_first=False):
            self.seeded = False
            self.shares = 0
            self.captcha_first = give_captcha_first
            self.calls = []

        def request(self, method, url, **kw):
            self.calls.append(url)
            if url.rstrip("/").endswith("iesdouyin.com"):
                self.seeded = True          # 根路径下发 ttwid
                return FakeResponse(url=url, body=b"<html>home</html>")
            if "/share/video/" in url:
                self.shares += 1
                if not self.seeded or (self.captcha_first and self.shares == 1):
                    return FakeResponse(url=url, body=TestShippedDouyinPack.CAPTCHA.encode())
                return FakeResponse(url=url,
                                    body=TestShippedDouyinPack.SHARE_JSON.encode())
            raise AssertionError(f"用例未桩掉该地址（保持离线）: {url}")

    def _resolve(self, net):
        pack_js = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "..", "rule-packs", "hancast-douyin", "main.js")
        with open(pack_js, encoding="utf-8") as f:
            js = f.read()
        rt = jsruntime_mod.JsRuntime(
            "hancast-douyin", [js], prefs={},
            on_prefs_change=lambda: None, cache_dir=_isolated_rules_dir(self))
        return rt.call_rule(
            0, {"url": "https://www.iesdouyin.com/share/video/7696270412111416585/",
                "body": "", "base": "https://www.iesdouyin.com/"},
            time.monotonic() + 30, net)

    def test_seeds_ttwid_before_share_page(self):
        net = self.Net()
        net.seeded = True                 # 券已经在会话里 → 首发的 body 也带券
        out = self._resolve(net)
        self.assertEqual(net.calls[0], "https://www.iesdouyin.com/")
        self.assertEqual(out["url"],
                         "https://www.douyin.com/aweme/v1/play/?video_id=v0200f&ratio=1080p")
        self.assertEqual(out["title"], "抖音标题")

    def test_retries_until_share_page_has_payload(self):
        """抖音按请求抽风：第一发给了中间页，重试就该拿到正文"""
        net = self.Net(give_captcha_first=True)
        out = self._resolve(net)
        self.assertEqual(net.shares, 2)
        self.assertIn("/aweme/v1/play/", out["url"])

    def test_persistent_captcha_reports_verification(self):
        """一直只给中间页时如实说「要人机验证」，不再谎报「未找到播放地址」"""
        class NoCookieNet(self.Net):
            def request(self, method, url, **kw):
                self.calls.append(url)
                if "/share/video/" in url:
                    self.shares += 1
                    return FakeResponse(url=url,
                                        body=TestShippedDouyinPack.CAPTCHA.encode())
                return FakeResponse(url=url, body=b"<html>home</html>")

        net = NoCookieNet()
        with self.assertRaises(Exception) as ctx:
            self._resolve(net)
        self.assertIn("人机验证", str(ctx.exception))
        self.assertEqual(net.shares, 3)


if __name__ == "__main__":
    unittest.main()