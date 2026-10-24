"""规则包后台任务：单执行、可取消、提交原子、退出后不许迟到落盘

覆盖的是那条真实故障：索引/下载在串行命令通道里跑，壳层 60s 超时只是
「不再等这条响应」，后端却继续把包装完 —— 用户看到失败，包却悄悄换了。
任务化之后响应窗口与执行时长解耦；这里验的是取消/超时/并发准入这些
只有真并发才会露馅的部分。全程假会话，不触网、不写用户配置。
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import hancast_sidecar.media.rules.jsplugins as jsplugins_mod
from hancast_sidecar.media.rules.jsplugins import PluginError, PluginManager
from hancast_sidecar.media.rules.tasks import (PluginTaskRunner, TaskCancelled, TaskDeadline,
                                               TaskContext)

# 复用规则包用例里的假网络与打包助手（同一套离线约定）
from test_jsplugins import (FakeJsonResponse, FakeResponse, DEMO_JS,
                            INDEX_URL, ZIP_URL, make_zip)


def _index_doc(name="demo", version="9.9.9", url=ZIP_URL, sha=""):
    return {"packs": [{"name": name, "version": version, "url": url,
                       "description": "d", "sha256": sha}]}


class GatedNet:
    """按 URL 派发包/索引；进到下载里就通知用例，等它放行才返回"""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []
        self.inside = threading.Event()
        self.release = threading.Event()
        self._gate = True

    def route(self, url):
        doc = self.routes[url]
        return FakeJsonResponse(doc) if isinstance(doc, dict) else FakeResponse(body=doc)

    def new_session(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kw):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"用例未桩掉该地址（保持离线）: {url}")
        if self._gate:
            self.inside.set()
            if not self.release.wait(5):
                raise AssertionError("用例没有放行这次下载，任务会一直挂着")
        return self.route(url)


class SlowNet:
    """每条路都固定慢 delay 秒：用来证明总预算而不是单次 socket 超时"""

    def __init__(self, routes, delay):
        self.routes = routes
        self.delay = delay
        self.calls: list[str] = []

    def new_session(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kw):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"用例未桩掉该地址（保持离线）: {url}")
        time.sleep(self.delay)
        doc = self.routes[url]
        return FakeJsonResponse(doc) if isinstance(doc, dict) else FakeResponse(body=doc)


class TaskFixture(unittest.TestCase):
    def setUp(self):
        self.root = os.path.join(tempfile.mkdtemp(prefix="hancast-plugintask-"),
                                 "plugins")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.root), True)
        self.mgr = PluginManager(self.root)
        self.runner = PluginTaskRunner(self.mgr, budget=5.0, close_timeout=1.0)
        self.addCleanup(self.runner.close)

    def _install(self, name="demo", version="1.0.0"):
        self.mgr.import_zip_bytes(make_zip(
            name=name, manifest={"name": name, "version": version,
                                 "author": "t", "description": "d",
                                 "scripts": ["site.js"]}))

    def _wait(self, task_id, timeout=5.0, want=("done", "error", "cancelled")):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            entry = self.runner.result(task_id)
            if entry["status"] in want:
                return entry
            time.sleep(0.02)
        self.fail(f"任务未在 {timeout}s 内落终态：{self.runner.result(task_id)}")


class TestTaskContract(TaskFixture):
    def test_check_updates_returns_the_original_shape(self):
        net = GatedNet({INDEX_URL: _index_doc()})
        net._gate = False
        with patch.object(jsplugins_mod.requests, "Session", net.new_session), \
             patch.object(jsplugins_mod, "index_url", return_value=INDEX_URL):
            started = self.runner.start("plugin_check_updates",
                                        {"index_url": INDEX_URL})
            self.assertEqual(started["status"], "pending")
            entry = self._wait(started["task_id"])
        self.assertEqual(entry["status"], "done")
        # 本机还没装过：包落在 available，而不是 updates
        self.assertEqual([p["name"] for p in entry["result"]["available"]], ["demo"])
        self.assertEqual(entry["result"]["updates"], [])
        self.assertEqual(entry["result"]["index_url"], INDEX_URL)

    def test_pull_returns_full_plugin_list_like_the_sync_command(self):
        routes = {INDEX_URL: _index_doc(version="2.0.0"),
                  ZIP_URL: make_zip(name="demo")}
        net = GatedNet(routes)
        with patch.object(jsplugins_mod.requests, "Session", net.new_session), \
             patch.object(jsplugins_mod, "index_url", return_value=INDEX_URL):
            started = self.runner.start("plugin_pull",
                                        {"name": "demo", "url": INDEX_URL})
            self.assertTrue(net.inside.wait(2), "任务没进到下载步骤")
            net.release.set()
            entry = self._wait(started["task_id"])
        self.assertEqual(entry["status"], "done")
        self.assertEqual([p["name"] for p in entry["result"]["plugins"]], ["demo"])
        self.assertEqual(self.mgr.get("demo").version, "1.0.0")

    def test_unknown_operation_and_bad_params_are_rejected(self):
        with self.assertRaises(ValueError):
            self.runner.start("plugin_remove", {})
        with self.assertRaises(ValueError):
            self.runner.start("plugin_pull", "not-a-dict")
        with self.assertRaises(ValueError):
            self.runner.result("nope")

    def test_one_slot_zero_queue_and_sync_entry_yields(self):
        routes = {INDEX_URL: _index_doc(version="2.0.0"),
                  ZIP_URL: make_zip(name="demo")}
        net = GatedNet(routes)
        with patch.object(jsplugins_mod.requests, "Session", net.new_session), \
             patch.object(jsplugins_mod, "index_url", return_value=INDEX_URL):
            started = self.runner.start("plugin_pull",
                                        {"name": "demo", "url": INDEX_URL})
            self.assertTrue(net.inside.wait(2))
            with self.assertRaises(ValueError):
                self.runner.start("plugin_check_updates", {})
            with self.assertRaises(ValueError):
                with self.runner.synchronous():
                    pass
            net.release.set()
            self.assertEqual(self._wait(started["task_id"])["status"], "done")
            # 名额在 worker 真正退出后才释放
            with self.runner.synchronous():
                pass

    def test_cancel_before_commit_leaves_the_installed_pack_alone(self):
        self._install(version="1.0.0")
        routes = {INDEX_URL: _index_doc(version="2.0.0"),
                  ZIP_URL: make_zip(name="demo", manifest={
                      "name": "demo", "version": "2.0.0", "author": "t",
                      "description": "d", "scripts": ["site.js"]})}
        net = GatedNet(routes)
        with patch.object(jsplugins_mod.requests, "Session", net.new_session), \
             patch.object(jsplugins_mod, "index_url", return_value=INDEX_URL):
            started = self.runner.start("plugin_pull",
                                        {"name": "demo", "url": INDEX_URL})
            self.assertTrue(net.inside.wait(2))
            pending = self.runner.cancel(started["task_id"])
            self.assertEqual(pending["status"], "pending")  # 已请求，尚未退出
            net.release.set()
            entry = self._wait(started["task_id"])
        self.assertEqual(entry["status"], "cancelled")
        self.assertEqual(self.mgr.get("demo").version, "1.0.0")

    def test_deadline_is_shared_across_mirror_and_direct(self):
        """镜像+直连各 40s 超时，相加会远超总预算：预算必须是整任务一份"""
        mirror = "https://mirror.example/demo.zip"
        ctx = TaskContext(budget=0.2)
        net = SlowNet({mirror: make_zip(name="demo"),
                       ZIP_URL: make_zip(name="demo")}, delay=0.5)
        started_at = time.monotonic()
        with patch.object(jsplugins_mod.requests, "Session", net.new_session), \
             patch.object(jsplugins_mod.ghmirror, "candidates",
                          lambda url: [("镜像", mirror), ("直连", ZIP_URL)]):
            with self.assertRaises(TaskDeadline) as caught:
                self.mgr.import_url(ZIP_URL, timeout=40.0, task=ctx)
        self.assertIn("总预算", str(caught.exception))
        # 预算耗尽就退出，不再拿第二条路继续烧
        self.assertEqual(net.calls, [mirror])
        self.assertLess(time.monotonic() - started_at, 1.0)
        self.assertEqual(self.mgr.list_info(), [])

    def test_close_admits_nothing_and_waits_for_the_worker(self):
        routes = {INDEX_URL: _index_doc(version="2.0.0"),
                  ZIP_URL: make_zip(name="demo")}
        net = GatedNet(routes)
        with patch.object(jsplugins_mod.requests, "Session", net.new_session), \
             patch.object(jsplugins_mod, "index_url", return_value=INDEX_URL):
            started = self.runner.start("plugin_pull",
                                        {"name": "demo", "url": INDEX_URL})
            self.assertTrue(net.inside.wait(2))
            self.runner.close()          # 请求取消但有界等待，不无限 join
            net.release.set()
            entry = self._wait(started["task_id"])
        self.assertIn(entry["status"], ("cancelled", "done"))
        with self.assertRaises(ValueError):
            self.runner.start("plugin_check_updates", {})
        with self.assertRaises(ValueError):
            with self.runner.synchronous():
                pass


class TestAtomicInstall(TaskFixture):
    """换代失败必须原样退回：旧实现先删目标目录，半路报错就只剩一个坏壳"""

    def test_commit_failure_restores_the_previous_pack(self):
        self._install(version="1.0.0")
        before = self.mgr.get("demo").dir
        new_zip = make_zip(name="demo", manifest={
            "name": "demo", "version": "2.0.0", "author": "t",
            "description": "d", "scripts": ["site.js"]})
        real_rename = os.rename
        boom = threading.Event()

        def flaky_rename(src, dst):
            if dst.endswith("demo") and not boom.is_set():
                boom.set()
                raise OSError("disk full")
            return real_rename(src, dst)

        with patch("os.rename", side_effect=flaky_rename):
            with self.assertRaises(OSError):
                self.mgr.import_zip_bytes(new_zip)

        self.assertEqual(self.mgr.get("demo").version, "1.0.0")
        self.assertEqual(self.mgr.get("demo").dir, before)
        # 暂存/备份目录不能留在插件根里污染列表与指纹
        leftovers = [n for n in os.listdir(self.mgr.root)
                     if n.startswith("_") or n.endswith(".backup")]
        self.assertEqual(leftovers, [])
        self.assertEqual([p["name"] for p in self.mgr.list_info()], ["demo"])

    def test_cancel_after_commit_reports_the_real_result(self):
        """提交已经赢了：这时取消不能谎报「已撤销」，包也确实换了"""
        self._install(version="1.0.0")
        ctx = TaskContext(budget=5.0)
        new_zip = make_zip(name="demo", manifest={
            "name": "demo", "version": "2.0.0", "author": "t",
            "description": "d", "scripts": ["site.js"]})
        with ctx.commit():
            self.mgr.import_zip_bytes(new_zip)   # 提交已由外层裁决
        ctx.request_cancel()
        self.assertEqual(ctx._finish(None, TaskCancelled("late"))["status"],
                         "done")
        self.assertEqual(self.mgr.get("demo").version, "2.0.0")

    def test_refresh_never_publishes_an_empty_snapshot(self):
        """慢构建期间并发的读视图必须仍看到旧的那一份完整快照"""
        self._install(version="1.0.0")
        entered = threading.Event()
        release = threading.Event()
        real_build = jsplugins_mod.JsPlugin.build_rules

        def slow_build(self_plugin):
            entered.set()
            self.assertTrue(release.wait(5), "用例未放行重建")
            return real_build(self_plugin)

        observed = []

        def reader():
            while not entered.wait(0.02):
                pass
            for _ in range(40):
                with self.mgr._lock:
                    observed.append((self.mgr._loaded,
                                     len(self.mgr._plugins), len(self.mgr._rules)))
                time.sleep(0.002)
            release.set()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        self.addCleanup(release.set)
        with patch.object(jsplugins_mod.JsPlugin, "build_rules", slow_build):
            self.mgr._fingerprint = None      # 强制重建
            rules = self.mgr.refresh()
        thread.join(5)
        self.assertEqual(len(rules), 1)
        self.assertTrue(observed, "读线程没赶上重建窗口")
        # 重建期间读侧要么看到旧快照、要么看到新快照，绝不能是「一个包都没有」
        self.assertTrue(all(loaded and packs == 1 for loaded, packs, _ in observed),
                        observed[:4])


if __name__ == "__main__":
    unittest.main()
