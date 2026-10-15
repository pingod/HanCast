"""GitHub 国内镜像开关：只改写 GitHub 主机，任何一条路失败都必须回退直连

覆盖 ``utils/ghmirror.py`` 本身与它的三个接入点：
- 规则包索引与包下载（``media/rules/jsplugins``）
- 应用检查新版本（``commands``）
- FFmpeg 按需下载（``utils/ffmpeg_deps``）

全程假会话离线：镜像地址是「桩里的一条 URL」，用例断言的是尝试顺序与失败文案，
不碰真实网络。开关状态通过 patch ``params.get`` 注入（ghmirror 每次现读参数表）。
"""

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import requests

from hancast_sidecar import params
from hancast_sidecar.media.rules import jsplugins
from hancast_sidecar.media.rules.jsplugins import PluginManager
from hancast_sidecar.utils import ffmpeg_deps as fd
from hancast_sidecar.utils import ghmirror, httpclient

RAW_INDEX = ("https://raw.githubusercontent.com/pingod/HanCast/"
             "main/rule-packs/index.json")
GH_ZIP = ("https://github.com/pingod/HanCast/releases/download/rules-v1/"
          "hancast-demo-1.0.0.zip")
GH_API = "https://api.github.com/repos/pingod/HanCast/releases/latest"
GYAN = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
GITEE_API = "https://gitee.com/api/v5/repos/buxiangqumingzi/han-cast/releases/latest"
PREFIX = "https://gh-proxy.com/"


class MirrorEnv:
    """按用例需要打开/关闭镜像开关（前缀一并注入）"""

    def __init__(self, enabled=True, prefix=PREFIX):
        self.table = {ghmirror.PARAM_ENABLED: enabled,
                      ghmirror.PARAM_PREFIX: prefix}

    def __enter__(self):
        patcher = patch.object(params, "get",
                               lambda k: self.table.get(k))
        patcher.start()
        self._patcher = patcher
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False


class TestCandidates(unittest.TestCase):
    def test_default_off_only_direct(self):
        with MirrorEnv(enabled=False):
            self.assertEqual(ghmirror.candidates(RAW_INDEX),
                             [("直连", RAW_INDEX)])

    def test_enabled_puts_mirror_first(self):
        with MirrorEnv():
            self.assertEqual(ghmirror.candidates(RAW_INDEX),
                             [("镜像", PREFIX + RAW_INDEX), ("直连", RAW_INDEX)])

    def test_non_github_hosts_pass_through(self):
        """Gitee 更新源、gyan.dev / johnvansickle 的 FFmpeg 包不是 GitHub，原样通过"""
        with MirrorEnv():
            for url in (GYAN, GITEE_API, "https://demo.example/play/1"):
                self.assertEqual(ghmirror.candidates(url), [("直连", url)], url)

    def test_without_prefix_only_direct(self):
        """开关开着但前缀没填/不是 http(s)：等于没开，不能拼出相对地址去请求"""
        for bad in ("", "gh-proxy.com/", "ftp://gh-proxy.com/"):
            with MirrorEnv(prefix=bad):
                self.assertEqual(ghmirror.candidates(RAW_INDEX),
                                 [("直连", RAW_INDEX)], bad)

    def test_all_github_release_shapes_are_rewritten(self):
        # 三种形态都得覆盖：索引（raw）、包与 FFmpeg（releases/download）、
        # 检查更新（api）；二跳的 objects.githubusercontent.com 也在名单里
        for url in (RAW_INDEX, GH_ZIP, GH_API,
                    "https://codeload.github.com/pingod/HanCast/tar.gz/refs/tags/v1",
                    "https://objects.githubusercontent.com/x/y"):
            self.assertTrue(ghmirror.is_github(url), url)
            with MirrorEnv():
                via, target = ghmirror.candidates(url)[0]
            self.assertEqual((via, target), ("镜像", PREFIX + url))


class FakeResp:
    def __init__(self, doc=None, body=b"", status=200):
        self.status_code = status
        self._doc, self._body = doc, body
        self.headers = {"Content-Length": str(len(self._body))}
        self.closed = False

    def json(self):
        if self._doc is None:
            raise ValueError("不是 JSON")
        return self._doc

    def iter_content(self, chunk_size=65536):
        yield self._body

    def close(self):
        self.closed = True


class FakeNet:
    """requests.Session / httpclient 会话替身：url → 索引 dict、zip bytes、
    现成的 FakeResp（流式）或要抛的异常

    没桩到的地址直接 KeyError 炸开——那说明代码去请求了用例没预期的路径。
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def new_session(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kw):
        self.calls.append(url)
        doc = self.routes[url]
        if isinstance(doc, Exception):
            raise doc
        if isinstance(doc, FakeResp):       # FFmpeg 那条路要的是流式响应，用例直接给对象
            return doc
        if isinstance(doc, dict):
            return FakeResp(doc=doc)
        return FakeResp(body=doc)

    def close(self):
        pass


def _dir(test) -> str:
    d = tempfile.mkdtemp(prefix="hancast-ghmirror-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


def make_zip(name="demo") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({
            "name": name, "version": "1.0.0", "author": "t",
            "description": "d", "scripts": ["site.js"]}))
        zf.writestr("site.js", f"registerRule({{name:'{name}',hosts:['demo.example'],"
                               f"stage:'page',resolve:function(){{return null;}}}});")
    return buf.getvalue()


class TestPackIndexMirror(unittest.TestCase):
    """规则包索引：先镜像，失败退回直连，两条路的理由都要留痕"""

    def _manager(self):
        return PluginManager(os.path.join(_dir(self), "plugins"))

    def test_index_taken_from_mirror(self):
        net = FakeNet({PREFIX + RAW_INDEX: {"packs": []}})
        with MirrorEnv(), patch.object(jsplugins.requests, "Session", net.new_session):
            idx = self._manager().fetch_index(RAW_INDEX)
        self.assertEqual(net.calls, [PREFIX + RAW_INDEX])
        self.assertEqual(idx["packs"], {})

    def test_mirror_failure_falls_back_to_direct(self):
        net = FakeNet({PREFIX + RAW_INDEX: requests.ConnectionError("镜像 502"),
                       RAW_INDEX: {"packs": []}})
        with MirrorEnv(), patch.object(jsplugins.requests, "Session", net.new_session):
            idx = self._manager().fetch_index(RAW_INDEX)
        self.assertEqual(net.calls, [PREFIX + RAW_INDEX, RAW_INDEX])
        self.assertEqual(idx["url"], RAW_INDEX)

    def test_both_paths_failed_names_both_reasons(self):
        net = FakeNet({PREFIX + RAW_INDEX: requests.ConnectionError("镜像挂了"),
                       RAW_INDEX: requests.Timeout("直连超时")})
        with MirrorEnv(), patch.object(jsplugins.requests, "Session", net.new_session):
            with self.assertRaises(jsplugins.PluginError) as cm:
                self._manager().fetch_index(RAW_INDEX)
        text = str(cm.exception)
        self.assertIn("镜像", text)
        self.assertIn("直连", text)

    def test_off_means_no_mirror_request_at_all(self):
        net = FakeNet({RAW_INDEX: {"packs": []}})
        with MirrorEnv(enabled=False), \
             patch.object(jsplugins.requests, "Session", net.new_session):
            self._manager().fetch_index(RAW_INDEX)
        self.assertEqual(net.calls, [RAW_INDEX])


class TestPackDownloadMirror(unittest.TestCase):
    """规则包 zip：镜像给的字节摘要对不上就退回直连重取，官方摘要这道关不放宽"""

    def _manager(self):
        return PluginManager(os.path.join(_dir(self), "plugins"))

    def test_sha_mismatch_on_mirror_falls_back_to_direct(self):
        good = make_zip("demo")
        net = FakeNet({PREFIX + GH_ZIP: b"swapped-by-mirror",
                       GH_ZIP: good})
        mgr = self._manager()
        with MirrorEnv(), patch.object(jsplugins.requests, "Session", net.new_session):
            info = mgr.import_url(GH_ZIP, expected_sha256=hashlib.sha256(good).hexdigest())
        self.assertEqual(net.calls, [PREFIX + GH_ZIP, GH_ZIP])
        self.assertEqual(info["name"], "demo")

    def test_sha_mismatch_on_last_path_still_rejected(self):
        net = FakeNet({PREFIX + GH_ZIP: b"bad-1", GH_ZIP: b"bad-2"})
        real = hashlib.sha256(make_zip("demo")).hexdigest()
        with MirrorEnv(), patch.object(jsplugins.requests, "Session", net.new_session):
            with self.assertRaises(jsplugins.PluginError) as cm:
                self._manager().import_url(GH_ZIP, expected_sha256=real)
        self.assertIn("sha256", str(cm.exception))

    def test_download_through_mirror_when_checksum_matches(self):
        good = make_zip("demo")
        net = FakeNet({PREFIX + GH_ZIP: good})
        with MirrorEnv(), patch.object(jsplugins.requests, "Session", net.new_session):
            self._manager().import_url(GH_ZIP, expected_sha256=hashlib.sha256(good).hexdigest())
        self.assertEqual(net.calls, [PREFIX + GH_ZIP])


class TestFfmpegSourceMirror(unittest.TestCase):
    """FFmpeg 按需下载：GitHub 系源先镜像，非 GitHub 源（gyan.dev）不动"""

    def _net(self, routes):
        net = FakeNet(routes)
        return net, patch.object(httpclient, "make_session", net.new_session)

    def test_github_source_prefers_mirror(self):
        net, p = self._net({PREFIX + GH_ZIP: FakeResp(body=b"x")})
        with MirrorEnv(), p:
            session, r = fd._open_stream(GH_ZIP)
        r.close(); session.close()
        self.assertEqual(net.calls, [PREFIX + GH_ZIP])

    def test_mirror_failure_falls_back_to_direct(self):
        net, p = self._net({PREFIX + GH_ZIP: requests.ConnectionError("镜像 403"),
                            GH_ZIP: FakeResp(body=b"x")})
        with MirrorEnv(), p:
            session, r = fd._open_stream(GH_ZIP)
        r.close(); session.close()
        self.assertEqual(net.calls, [PREFIX + GH_ZIP, GH_ZIP])

    def test_http_error_message_lists_both_paths(self):
        net, p = self._net({PREFIX + GH_ZIP: FakeResp(status=403),
                            GH_ZIP: FakeResp(status=503)})
        with MirrorEnv(), p:
            with self.assertRaises(fd.DependencyError) as cm:
                fd._open_stream(GH_ZIP)
        text = str(cm.exception)
        self.assertIn("镜像：HTTP 403", text)
        self.assertIn("直连：HTTP 503", text)

    def test_non_github_source_single_call(self):
        """gyan.dev 不在改写名单：开着镜像也只发一条，且地址原样"""
        net, p = self._net({GYAN: FakeResp(body=b"x")})
        with MirrorEnv(), p:
            session, r = fd._open_stream(GYAN)
        r.close(); session.close()
        self.assertEqual(net.calls, [GYAN])


class TestUpdateCheckMirror(unittest.TestCase):
    """检查新版本：走镜像时放宽等待（1s 是给直连启动检查留的），失败退回直连"""

    def setUp(self):
        from hancast_sidecar.commands import CommandHandler
        self.handler = CommandHandler()
        self.addCleanup(self.handler.cleanup)

    def _release(self, api_url, current_version, timeout=2):
        self.calls.append((api_url, timeout))
        if api_url.startswith(PREFIX):
            raise requests.ConnectionError("镜像不通")
        return {"has_update": True, "current": current_version, "latest": "9.9.9",
                "url": "https://github.com/pingod/HanCast/releases/tag/v9.9.9",
                "body": ""}

    def _run(self, sources, enabled=True):
        self.calls = []
        with MirrorEnv(enabled=enabled), \
             patch.object(self.handler, "_fetch_github_release", self._release):
            out = self.handler.execute("check_update",
                                       {"current_version": "0.0.0", "sources": sources})
        return out, self.calls

    def test_mirror_first_then_direct(self):
        out, calls = self._run(["github"])
        self.assertEqual(calls, [(PREFIX + GH_API, 3), (GH_API, 1)])
        self.assertTrue(out["has_update"])

    def test_result_marks_mirror_source(self):
        def ok(api_url, current_version, timeout=2):
            self.calls.append((api_url, timeout))
            return {"has_update": True, "current": current_version, "latest": "9.9.9",
                    "url": "", "body": ""}
        self.calls = []
        with MirrorEnv(), patch.object(self.handler, "_fetch_github_release", ok):
            out = self.handler.execute("check_update",
                                       {"current_version": "0.0.0", "sources": ["github"]})
        self.assertEqual(out["via"], "镜像")
        self.assertEqual(self.calls, [(PREFIX + GH_API, 3)])

    def test_off_keeps_single_direct_attempt_with_1s(self):
        out, calls = self._run(["github"], enabled=False)
        self.assertEqual(calls, [(GH_API, 1)])
        self.assertNotIn("via", out)

    def test_gitee_source_never_rewritten(self):
        _, calls = self._run(["gitee"])
        self.assertEqual(calls, [(GITEE_API, 3)])


if __name__ == "__main__":
    unittest.main()
