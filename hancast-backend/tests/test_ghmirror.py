"""GitHub 国内镜像开关：只改写 GitHub 主机，任何一条路失败都必须回退直连

覆盖 ``utils/ghmirror.py`` 本身与它的三个接入点：
- 规则包索引与包下载（``media/rules/jsplugins``）
- 应用检查新版本（``update/manager``）
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
from pathlib import Path
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
# 自更新取清单走的是**列表**接口，不是 `/releases/latest`：后者把 "latest" 给
# 「最近一次 publish」的那条而不是版本号最大的那条，为旧版补一条说明 release 就会
# 把指针拽回去（2026-11-06 实测）。判据见 tools/audit-releases.py 的 latest 指针那条。
GH_LIST = "https://api.github.com/repos/pingod/HanCast/releases?per_page=100&page=1"
# 下面两条只是「非 GitHub 主机不改写 / GitHub 三种地址形态都改写」的夹具，
# 不代表更新链路真会去访问它们（gitee 那条源已在 v2.4.12 从更新链路里删掉，
# 见 TestUpdateCheckMirror 的说明）。
GH_API = "https://api.github.com/repos/pingod/HanCast/releases/latest"
GITEE_API = "https://gitee.com/api/v5/repos/buxiangqumingzi/han-cast/releases/latest"
GYAN = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
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
            # 两条路都得喂：`doc=` 给 `resp.json()`（更新检查走那条，commands.py:1012），
            # body 给流式读（规则包索引 v2.3.20 起经 `_download` 逐块取字节再
            # json.loads，jsplugins.py:63-92）。只塞 json() 的话流式侧拿到空字节，
            # ValueError 会被当成「这条镜像路走不通」而多退一次直连——
            # 桩坏了却看起来像被测代码坏了。
            return FakeResp(doc=doc, body=json.dumps(doc).encode("utf-8"))
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


def _release_json(tag: str = "v9.9.9") -> list:
    """一条最小可用的 Release 列表响应：一个安装包 + 一个规则包 zip"""
    return [{
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "html_url": "https://github.com/pingod/HanCast/releases/tag/" + tag,
        "body": "### 变更\n- 一条",
        "published_at": "2026-11-06T00:00:00Z",
        "assets": [
            {"name": "HanCast_9.9.9_aarch64.dmg",
             "browser_download_url":
                 "https://github.com/pingod/HanCast/releases/download/" + tag +
                 "/HanCast_9.9.9_aarch64.dmg",
             "size": 1234, "digest": "sha256:" + "a" * 64},
            {"name": "hancast-demo-1.0.0.zip",
             "browser_download_url":
                 "https://github.com/pingod/HanCast/releases/download/x/y.zip",
             "size": 10, "digest": "sha256:" + "b" * 64},
        ],
    }]


class TestUpdateCheckMirror(unittest.TestCase):
    """检查新版本：镜像优先、失败退直连；来源只有 GitHub

    v2.4.11 及之前这里还有一条 gitee 源，本轮删掉：`gitee.com/buxiangqumingzi/han-cast`
    属**原作者**那条线，它的 `releases/latest` 停在 v2.1.0，GitHub 一不通它就顶上来，
    用户看到的现象是「提示有新版 2.1.0，点进去是原作者的下载页」。
    """

    def setUp(self):
        from hancast_sidecar.update import manager
        self.m = manager
        self.m.invalidate_release_cache()
        self.addCleanup(self.m.invalidate_release_cache)

    def _patch_http(self, fail_urls=(), payload=None):
        """记录每次实际请求的 URL；命中 fail_urls 的抛连接异常"""
        calls = []

        def fake(url, timeout=8.0):
            calls.append(url)
            if any(f in url for f in fail_urls):
                raise requests.ConnectionError("桩：连不上")
            return _release_json() if payload is None else payload

        patcher = patch.object(self.m, "_http_json", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_mirror_first_then_direct(self):
        calls = self._patch_http(fail_urls=(PREFIX,))
        with MirrorEnv():
            out = self.m.check_for_update("0.0.0")
        self.assertEqual(calls, [PREFIX + GH_LIST, GH_LIST])
        self.assertTrue(out["has_update"])
        self.assertEqual(out["source"], "github")

    def test_result_marks_mirror_source(self):
        calls = self._patch_http()
        with MirrorEnv():
            out = self.m.check_for_update("0.0.0")
        self.assertEqual(calls, [PREFIX + GH_LIST])
        self.assertEqual(out["via"], "镜像")

    def test_off_keeps_single_direct_attempt(self):
        calls = self._patch_http()
        with MirrorEnv(enabled=False):
            out = self.m.check_for_update("0.0.0")
        self.assertEqual(calls, [GH_LIST])
        self.assertNotIn("via", out)
        self.assertTrue(out["has_update"])

    def test_rate_limit_says_so(self):
        """403 必须说成「限流」而不是「连不上」：两者的下一步动作完全不同"""
        def fake(url, timeout=8.0):
            raise requests.HTTPError("403 Client Error: rate limit exceeded for url: " + url)
        patcher = patch.object(self.m, "_http_json", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        with MirrorEnv(enabled=False):
            out = self.m.check_for_update("0.0.0")
        self.assertFalse(out["has_update"])
        self.assertIn("限流", out["error"])

    def test_no_gitee_anywhere_in_the_update_chain(self):
        """更新链路的源码与请求里都不许再出现 gitee（那是原作者的仓库）"""
        calls = self._patch_http()
        with MirrorEnv():
            self.m.check_for_update("0.0.0")
        self.assertTrue(calls)
        self.assertFalse([c for c in calls if "gitee" in c])
        root = Path(__file__).resolve().parents[1]
        for rel in ("hancast_sidecar/commands.py",
                    "hancast_sidecar/update/manager.py"):
            text = (root / rel).read_text(encoding="utf-8")
            self.assertNotIn("gitee.com", text, rel)


if __name__ == "__main__":
    unittest.main()
