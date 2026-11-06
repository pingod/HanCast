"""应用内自更新：产物挑选、摘要校验、断点续传、取消、安装方式

判据分两层，缺一条都说明这条链路不可信：

1. **离线桩层**（本文件前半）——`classify_asset` / `describe_release` /
   `check_for_update` / `install_plan` 的语义。用桩 `_http_json` 喂真实形状的
   Release JSON，不碰网络。
2. **真下载层**（本文件后半）——起一个本地 `http.server`，让 manager 的下载线程
   真的把字节拉完、真的算 sha256、真的落盘。理由与 `test_ffmpeg_download_e2e.py`
   同一句：桩掉的 `_open_stream` 证明不了流式读、`os.replace`、Range 续传这些
   只有真连接才会露馅的部分。

全程不写用户的配置目录：`update_dir()` 被指到 `tempfile.mkdtemp()`。
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.update import manager as um

PAYLOAD = b"HanCast-installer-bytes" * 4096          # ~85 KB，够切好几片
SHA = hashlib.sha256(PAYLOAD).hexdigest()


def asset(name: str, url: str, size: int = len(PAYLOAD), digest: str = "sha256:" + SHA) -> dict:
    return {"name": name, "browser_download_url": url, "size": size, "digest": digest}


def release(assets=None, tag: str = "v9.9.9", draft: bool = False,
            prerelease: bool = False) -> dict:
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "html_url": f"https://github.com/pingod/HanCast/releases/tag/{tag}",
        "body": "### 变更\n- 一条\n- 两条",
        "published_at": "2026-11-06T00:00:00Z",
        "assets": assets if assets is not None else [
            asset("HanCast_9.9.9_aarch64.dmg",
                  "https://gh.example/d/HanCast_9.9.9_aarch64.dmg"),
            asset("HanCast_9.9.9_x64-setup.exe",
                  "https://gh.example/d/HanCast_9.9.9_x64-setup.exe"),
            asset("HanCast_9.9.9_amd64.deb", "https://gh.example/d/HanCast_9.9.9_amd64.deb"),
            asset("HanCast-9.9.9-1.x86_64.rpm", "https://gh.example/d/HanCast-9.9.9-1.x86_64.rpm"),
            asset("HanCast_9.9.9_amd64.AppImage",
                  "https://gh.example/d/HanCast_9.9.9_amd64.AppImage"),
            asset("hancast-9.9.9-1-x86_64.pkg.tar.zst",
                  "https://gh.example/d/hancast-9.9.9-1-x86_64.pkg.tar.zst"),
            asset("hancast-demo-1.0.0.zip", "https://gh.example/d/hancast-demo-1.0.0.zip",
                  size=10, digest="sha256:" + "b" * 64),
        ],
    }


class DirCase(unittest.TestCase):
    """把下载目录指进临时目录，并让每个用例从空任务表开始"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hancast-update-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        patcher = patch.object(um, "update_dir", lambda: self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        um._cancel_event.clear()
        with um._download_lock:
            um._download_task.clear()
        um.invalidate_release_cache()
        self.addCleanup(um.invalidate_release_cache)


# ── 1. 产物识别 ──

class TestClassify(unittest.TestCase):
    def test_installers_recognised(self):
        cases = {
            "HanCast_2.4.11_aarch64.dmg": ("dmg", "macos", "arm64"),
            "HanCast_2.4.11_x64-setup.exe": ("nsis", "windows", "x86_64"),
            "HanCast_2.4.11_amd64.deb": ("deb", "linux", "x86_64"),
            "HanCast-2.4.11-1.x86_64.rpm": ("rpm", "linux", "x86_64"),
            "HanCast_2.4.11_amd64.AppImage": ("appimage", "linux", "x86_64"),
            "hancast-2.4.11-1-x86_64.pkg.tar.zst": ("arch", "linux", "x86_64"),
        }
        for name, (kind, os_name, arch) in cases.items():
            got = um.classify_asset(name)
            self.assertIsNotNone(got, name)
            self.assertEqual((got["kind"], got["os"], got["arch"]), (kind, os_name, arch), name)

    def test_rule_pack_zip_is_not_an_installer(self):
        """规则包 zip 混进产物清单的话，「下载最新版本」会下回一个 7 KB 的 JS 包"""
        self.assertIsNone(um.classify_asset("hancast-bilibili-1.1.0.zip"))
        self.assertIsNone(um.classify_asset("README.txt"))

    def test_arch_token_needs_separators(self):
        # 「2.4.11」里的数字不能被当成架构；分隔符两侧都得对
        self.assertEqual(um.classify_asset("HanCast_2.4.11_aarch64.dmg")["arch"], "arm64")
        self.assertEqual(um.classify_asset("HanCast_2411_aarch64.dmg")["arch"], "arm64")


class TestDescribe(unittest.TestCase):
    def test_only_platform_assets_and_priority(self):
        with patch.object(um, "_os_name", lambda: "linux"), \
             patch.object(um, "_linux_installed_kind", lambda: "appimage"):
            d = um.describe_release(release())
        kinds = [a["kind"] for a in d["assets"]]
        self.assertEqual(set(kinds), {"appimage", "deb", "rpm", "arch"})
        self.assertNotIn("dmg", kinds)          # 别把 mac 的包列给 Linux 用户
        self.assertEqual(d["selected"]["kind"], "appimage")
        self.assertEqual(d["no_asset_for_platform"], "")

    def test_preferred_kind_switches_the_asset(self):
        with patch.object(um, "_os_name", lambda: "linux"), \
             patch.object(um, "_linux_installed_kind", lambda: "deb"):
            self.assertEqual(um.describe_release(release())["selected"]["kind"], "deb")

    def test_windows_arm_sees_x64_with_arch_flag_off(self):
        with patch.object(um, "_os_name", lambda: "windows"), \
             patch.object(um, "platform_machine", lambda: "arm64"):
            d = um.describe_release(release())
        self.assertFalse(d["selected"]["arch_match"])
        self.assertEqual(d["platform"]["arch"], "arm64")

    def test_platform_without_asset_says_so(self):
        r = release(assets=[asset("HanCast_9.9.9_aarch64.dmg", "https://gh.example/d/x.dmg")])
        with patch.object(um, "_os_name", lambda: "linux"), \
             patch.object(um, "_linux_installed_kind", lambda: "deb"):
            d = um.describe_release(r)
        self.assertEqual(d["assets"], [])
        self.assertEqual(d["no_asset_for_platform"], "linux")
        self.assertIsNone(d["selected"])


# ── 2. 版本比较与清单选择 ──

class TestVersionParse(unittest.TestCase):
    def test_prerelease_loses_to_stable(self):
        # 这条是原 commands._parse_version 的历史教训，搬过来必须照样成立
        self.assertGreater(um.parse_version("2.1.0"), um.parse_version("2.1.0-beta.1"))
        self.assertGreater(um.parse_version("2.1.0-rc.2"), um.parse_version("2.1.0-rc.1"))
        self.assertEqual(um.parse_version("2.1.0+build.5"), um.parse_version("2.1.0"))
        self.assertGreater(um.parse_version("2.10.0"), um.parse_version("2.9.0"))

    def test_newer_semantics(self):
        self.assertFalse(um._newer("2.4.11", "2.4.11"))
        self.assertTrue(um._newer("2.4.12", "2.4.11"))
        self.assertFalse(um._newer("2.4.12-rc.1", "2.4.12"))


class TestLatestSelection(unittest.TestCase):
    """**不**用 /releases/latest：它按「最近 publish」给结果"""

    def setUp(self):
        # 这个类不继承 DirCase，得自己清 60s 清单缓存，否则后一个用例拿到前一个
        # 用例的桩结果，看起来像「压根没发请求」
        um.invalidate_release_cache()
        self.addCleanup(um.invalidate_release_cache)

    def _many(self):
        return [
            release(tag="v2.4.11"),
            release(tag="v2.3.8", assets=[]),          # 旧版补的说明条目，无产物
            release(tag="v9.9.9"),
            release(tag="v2.5.0-rc.1", prerelease=True),
            release(tag="v2.4.6", draft=True),
        ]

    def test_picks_semantic_max_not_upload_order(self):
        calls = []

        def fake(url, timeout=8.0):
            calls.append(url)
            return self._many() if "page=" in url else []
        with patch.object(um, "_http_json", fake):
            d = um.get_release("")
        self.assertEqual(d["version"], "9.9.9")

    def test_draft_and_prerelease_excluded(self):
        pool = [r for r in self._many() if not r["draft"] and not r["prerelease"]]
        def fake(url, timeout=8.0):
            return pool
        with patch.object(um, "_http_json", fake):
            self.assertEqual(um.get_release("")["version"], "9.9.9")
        # 去掉 9.9.9 之后，2.3.8（无产物、语义更低）也不该赢过 2.4.11
        um.invalidate_release_cache()      # 上一段的结果还在 60s 缓存里
        pool2 = [r for r in self._many() if r["tag_name"] != "v9.9.9"]
        with patch.object(um, "_http_json", lambda u, timeout=8.0: pool2):
            self.assertEqual(um.get_release("")["version"], "2.4.11")

    def test_paging_uses_next_page_url(self):
        seen = []
        def fake(url, timeout=8.0):
            seen.append(url)
            return [release(tag="v2.4.11")] if url.endswith("page=1") else []
        with patch.object(um, "_http_json", fake):
            um.get_release("")
        self.assertEqual(seen, [um.API_RELEASES + "1"])

    def test_empty_release_list_is_an_error(self):
        def fake(url, timeout=8.0):
            return [release(tag="not-a-version", assets=[])]
        with patch.object(um, "_http_json", fake):
            with self.assertRaises(um.UpdateError) as cm:
                um.get_release("")
        self.assertIn("可用的已发布版本", str(cm.exception))


class TestCheckForUpdate(DirCase):
    def _patch(self, payload):
        return patch.object(um, "_http_json", lambda url, timeout=8.0: payload)

    def test_fields_when_update_exists(self):
        with self._patch([release()]), patch.object(um, "_os_name", lambda: "macos"):
            out = um.check_for_update("2.4.11")
        self.assertTrue(out["has_update"])
        self.assertEqual(out["latest"], "9.9.9")
        self.assertEqual(out["source"], "github")
        self.assertIn("releases/tag/v9.9.9", out["url"])
        self.assertTrue(out["assets"])
        self.assertEqual(out["selected"]["kind"], "dmg")

    def test_no_assets_when_up_to_date(self):
        with self._patch([release(tag="v2.4.11")]):
            out = um.check_for_update("2.4.11")
        self.assertFalse(out["has_update"])
        self.assertEqual(out["url"], "")
        self.assertNotIn("assets", out)

    def test_ignored_version_suppresses_prompt(self):
        with self._patch([release()]):
            out = um.check_for_update("2.4.11", ignored_version="9.9.9")
        self.assertFalse(out["has_update"])
        self.assertTrue(out["ignored"])
        self.assertEqual(out["latest"], "9.9.9")
        # force（设置页手点）必须无视忽略记录
        forced = um.check_for_update("2.4.11", ignored_version="9.9.9", force=True)
        self.assertTrue(forced["has_update"])

    def test_failure_returns_error_instead_of_raising(self):
        """检查失败不抛错：启动自检没人在听异常，界面要的是「为什么没查到」"""
        import requests
        def boom(url, timeout=8.0):
            raise requests.ConnectionError("全部不通")
        with patch.object(um, "_http_json", boom):
            out = um.check_for_update("2.4.11")
        self.assertFalse(out["has_update"])
        self.assertIn("取版本清单失败", out["error"])


# ── 3. 真下载：本地 HTTP 源 ──

class _Handler(http.server.BaseHTTPRequestHandler):
    payload: bytes = b""
    name: str = "HanCast_9.9.9_aarch64.dmg"
    digest: str = ""
    send_range: bool = True
    serve_wrong: bool = False
    abort_mid: bool = False
    slow: bool = False            # 逐片 sleep，给「中途取消」留出能打的窗口
    requests_log: list = []

    def do_GET(self):
        type(self).requests_log.append(self.headers.get("Range"))
        data = self.payload
        if self.serve_wrong:
            data = b"tampered-by-mirror" * 4096
        start = 0
        rng = self.headers.get("Range") or ""
        if self.send_range and rng.startswith("bytes="):
            try:
                start = int(rng[6:].split("-")[0])
            except ValueError:
                start = 0
            if start >= len(data):
                self.send_error(416)
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        body = data[start:]
        # abort_mid 模拟断流：Content-Length 说完整长度，实发一半（真实 CDN 掐线就是这个
        # 形状）。连 Content-Length 一起改小，客户端就看不出截断，只会得到「摘要不匹配」，
        # 那是另一条判据（test_tampered_asset_is_discarded）。
        declared = len(body)
        if self.abort_mid:
            body = body[: len(body) // 2]
        self.send_header("Content-Length", str(declared))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        try:
            if self.slow:
                step = 64 * 1024
                for i in range(0, len(body), step):
                    self.wfile.write(body[i:i + step])
                    self.wfile.flush()
                    time.sleep(0.01)
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


class LiveSource:
    def __init__(self, test: unittest.TestCase, **over):
        cls = type("_H", (_Handler,), {})
        cls.payload = over.pop("payload", PAYLOAD)
        cls.digest = over.pop("digest", SHA)
        for k, v in over.items():
            setattr(cls, k, v)
        cls.requests_log = []
        self.cls = cls
        # 端口交给内核挑：先自己 bind 一个 socket 再拿那个号去 bind 服务器，
        # 第二次 bind 必然 EADDRINUSE（第一版就踩了）
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), cls)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        test.addCleanup(self.stop)

    def url(self, name: str = "") -> str:
        return (f"http://127.0.0.1:{self.port}/{name or self.cls.name}")

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


def _stub_release(test: DirCase, source: LiveSource, name: str, digest: str) -> None:
    """让 get_release 返回一个只含本地产物地址的版本"""
    payload = [release(assets=[asset(name, source.url(name),
                                     size=len(source.cls.payload),
                                     digest=("sha256:" + digest) if digest else "")])]
    patcher = patch.object(um, "_http_json", lambda url, timeout=8.0: payload)
    patcher.start()
    test.addCleanup(patcher.stop)


def _wait(statuses=("done", "error", "cancelled"), timeout=15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = um.download_status()
        if task.get("status") in statuses:
            return task
        time.sleep(0.05)
    raise AssertionError(f"下载任务没落终态：{um.download_status()}")


class TestDownload(DirCase):
    def test_happy_path_verifies_and_lands(self):
        src = LiveSource(self)
        _stub_release(self, src, "HanCast_9.9.9_aarch64.dmg", SHA)
        started = um.start_download()
        self.assertEqual(started["stage"], "connecting")
        task = _wait()
        self.assertEqual(task["status"], "done", task.get("error"))
        dest = os.path.join(self.dir, "HanCast_9.9.9_aarch64.dmg")
        self.assertTrue(os.path.isfile(dest))
        self.assertFalse(os.path.exists(dest + ".part"))
        self.assertEqual(task["dest"], dest)
        self.assertTrue(task["verified"])
        self.assertEqual(task["sha256_actual"], SHA)
        self.assertEqual(task["percent"], 100.0)

    def test_tampered_asset_is_discarded(self):
        """镜像/CDN 换了字节必须当场判死，且不留下半份文件（与规则包同一道关）"""
        src = LiveSource(self, serve_wrong=True)
        _stub_release(self, src, "HanCast_9.9.9_aarch64.dmg", SHA)
        um.start_download()
        task = _wait()
        self.assertEqual(task["status"], "error")
        self.assertIn("sha256 不匹配", task["error"])
        self.assertEqual(os.listdir(self.dir), [])

    def test_missing_digest_reports_unverified(self):
        src = LiveSource(self)
        _stub_release(self, src, "HanCast_9.9.9_aarch64.dmg", "")
        um.start_download()
        task = _wait()
        self.assertEqual(task["status"], "done", task.get("error"))
        self.assertFalse(task["verified"])
        self.assertIn("未声明", task["note"])

    def test_truncated_stream_is_not_done(self):
        """半份包最危险：能挂载、能打开，装到一半才炸"""
        src = LiveSource(self, abort_mid=True)
        _stub_release(self, src, "HanCast_9.9.9_aarch64.dmg", SHA)
        um.start_download()
        task = _wait()
        self.assertEqual(task["status"], "error")
        self.assertTrue("下载被截断" in task["error"] or "下载不完整" in task["error"],
                        task["error"])

    def test_resume_from_part_file(self):
        """已有 .part 就带 Range 续传，且整包摘要仍然算得过"""
        src = LiveSource(self)
        name = "HanCast_9.9.9_aarch64.dmg"
        _stub_release(self, src, name, SHA)
        part = os.path.join(self.dir, name + ".part")
        with open(part, "wb") as f:
            f.write(PAYLOAD[: 4000])
        um.start_download()
        task = _wait()
        self.assertEqual(task["status"], "done", task.get("error"))
        self.assertEqual(task["resumed_from"], 4000)
        self.assertTrue([r for r in src.cls.requests_log if r and r.startswith("bytes=4000-")],
                        src.cls.requests_log)
        self.assertEqual(task["sha256_actual"], SHA)
        self.assertEqual(os.path.getsize(os.path.join(self.dir, name)), len(PAYLOAD))

    def test_server_without_range_support_restarts_clean(self):
        """远端不认 Range 时必须从零重写：append 半份会既重复又对不上摘要"""
        src = LiveSource(self, send_range=False)
        name = "HanCast_9.9.9_aarch64.dmg"
        _stub_release(self, src, name, SHA)
        with open(os.path.join(self.dir, name + ".part"), "wb") as f:
            f.write(PAYLOAD[: 4000])
        um.start_download()
        task = _wait()
        self.assertEqual(task["status"], "done", task.get("error"))
        self.assertEqual(task["resumed_from"], 0)
        self.assertEqual(task["sha256_actual"], SHA)

    def test_tampered_part_is_dropped_not_resumed(self):
        """摘要不符的半份没有续传价值：必须删干净，否则重试永远卡在同一份坏字节上"""
        name = "HanCast_9.9.9_aarch64.dmg"
        src = LiveSource(self, serve_wrong=True)
        _stub_release(self, src, name, SHA)
        um.start_download()
        task = _wait()
        self.assertEqual(task["status"], "error")
        self.assertEqual(os.listdir(self.dir), [])

    def test_cancel_stops_and_clears_part(self):
        # 85 KB 在本地环回上几毫秒就下完，取消点永远打不中：这里用 6 MB 慢源
        big = PAYLOAD + b"z" * (6 * 1024 * 1024)
        big_sha = hashlib.sha256(big).hexdigest()
        src = LiveSource(self, payload=big, digest=big_sha, slow=True)
        _stub_release(self, src, "HanCast_9.9.9_aarch64.dmg", big_sha)
        um.start_download()
        # 等它真的开始写盘再点取消，否则取消的是「还没开始的连接」
        deadline = time.time() + 10
        while time.time() < deadline and um.download_status().get("received", 0) == 0:
            time.sleep(0.02)
        mid = um.cancel_download()
        self.assertIn(mid["stage"], ("cancelling", "downloading"), mid)
        task = _wait(statuses=("cancelled", "done", "error"))
        self.assertEqual(task["status"], "cancelled", task.get("error"))
        self.assertEqual(os.listdir(self.dir), [])

    def test_second_start_is_idempotent(self):
        src = LiveSource(self)
        _stub_release(self, src, "HanCast_9.9.9_aarch64.dmg", SHA)
        a = um.start_download()
        b = um.start_download()
        self.assertEqual(a["task_id"], b["task_id"])
        task = _wait()
        self.assertEqual(task["status"], "done", task.get("error"))
        # 两条路只该有一次 GET：起第二条任务时第一条还在跑，幂等返回而不是并发抢带宽
        self.assertEqual(len(src.cls.requests_log), 1, src.cls.requests_log)

    def test_unknown_asset_name_rejected(self):
        src = LiveSource(self)
        _stub_release(self, src, "HanCast_9.9.9_aarch64.dmg", SHA)
        with self.assertRaises(um.UpdateError) as cm:
            um.start_download("HanCast_9.9.9_aarch64.iso")
        self.assertIn("不含产物", str(cm.exception))

    def test_plaintext_asset_from_remote_host_rejected(self):
        """环回之外的 http 一律拒绝：一个能覆盖 /Applications 的包不该走明文"""
        with patch.object(um, "_http_json", lambda url, timeout=8.0: [release(assets=[
                asset("HanCast_9.9.9_aarch64.dmg",
                      "http://example.com/HanCast_9.9.9_aarch64.dmg")])]):
            with self.assertRaises(um.UpdateError) as cm:
                um.start_download()
        self.assertIn("明文 HTTP", str(cm.exception))

    def test_non_http_scheme_rejected(self):
        with patch.object(um, "_http_json", lambda url, timeout=8.0: [release(assets=[
                asset("HanCast_9.9.9_aarch64.dmg", "file:///tmp/x.dmg")])]):
            with self.assertRaises(um.UpdateError) as cm:
                um.start_download()
        self.assertIn("不是 http(s)", str(cm.exception))

    def test_old_installers_pruned(self):
        src = LiveSource(self)
        name = "HanCast_9.9.9_aarch64.dmg"
        _stub_release(self, src, name, SHA)
        stale = os.path.join(self.dir, "HanCast_2.4.11_aarch64.dmg")
        with open(stale, "wb") as f:
            f.write(b"x")
        um.start_download()
        _wait()
        self.assertFalse(os.path.exists(stale))

    def test_foreign_files_in_dir_untouched(self):
        """清理只动本程序命名形态的文件，用户在配置目录里的东西一律不碰"""
        src = LiveSource(self)
        keep = os.path.join(self.dir, "我的录像.mp4")
        with open(keep, "wb") as f:
            f.write(b"x")
        _stub_release(self, src, "HanCast_9.9.9_aarch64.dmg", SHA)
        um.start_download()
        _wait()
        self.assertTrue(os.path.isfile(keep))


class TestStatusShape(DirCase):
    """`download_status` 的空表也要带目录：壳层的 `open_update_dir` 就靠它"""

    def test_idle_still_reports_dir(self):
        out = um.download_status()
        self.assertEqual(out["status"], "idle")
        self.assertEqual(out["dir"], self.dir)

    def test_dir_survives_when_task_exists(self):
        with um._download_lock:
            um._download_task.update({"status": "pending", "dest": os.path.join(self.dir, "x.dmg")})
        out = um.download_status()
        self.assertEqual(out["status"], "pending")
        self.assertEqual(out["dir"], self.dir)


class TestPrune(DirCase):
    """清理只动自己的文件，且不得动本次的半份包（断点续传的前提）"""

    def test_partial_of_current_target_survives(self):
        part = os.path.join(self.dir, "HanCast_9.9.9_aarch64.dmg.part")
        with open(part, "wb") as f:
            f.write(b"x")
        um._prune(self.dir, keep="HanCast_9.9.9_aarch64.dmg")
        self.assertTrue(os.path.exists(part))

    def test_other_installers_removed(self):
        stale = os.path.join(self.dir, "HanCast_2.4.11_aarch64.dmg")
        with open(stale, "wb") as f:
            f.write(b"x")
        um._prune(self.dir, keep="HanCast_9.9.9_aarch64.dmg")
        self.assertFalse(os.path.exists(stale))


class TestInstallPlan(DirCase):
    def _finish(self, kind: str, name: str) -> str:
        with um._download_lock:
            um._download_task.update({"status": "done", "kind": kind, "version": "9.9.9",
                                      "sha256_actual": SHA, "verified": True})
        dest = os.path.join(self.dir, name)
        with open(dest, "wb") as f:
            f.write(PAYLOAD)
        um._download_task["dest"] = dest
        return dest

    def test_auto_kinds_are_auto(self):
        for kind, name in (("dmg", "HanCast_9.9.9_aarch64.dmg"),
                           ("nsis", "HanCast_9.9.9_x64-setup.exe"),
                           ("appimage", "HanCast_9.9.9_amd64.AppImage")):
            self._finish(kind, name)
            plan = um.install_plan()
            self.assertTrue(plan["auto"], kind)
            self.assertEqual(plan["kind"], kind)
            self.assertNotIn("command", plan)

    def test_package_managers_return_command_only(self):
        """deb/rpm/Arch 必须提权，Sidecar 与壳层都不该偷偷 sudo"""
        for kind, name, needle in (("deb", "HanCast_9.9.9_amd64.deb", "sudo apt install"),
                                   ("rpm", "HanCast-9.9.9-1.x86_64.rpm", "sudo rpm -Uvh"),
                                   ("arch", "hancast-9.9.9-1-x86_64.pkg.tar.zst",
                                    "sudo pacman -U")):
            dest = self._finish(kind, name)
            plan = um.install_plan()
            self.assertFalse(plan["auto"], kind)
            self.assertEqual(plan["kind"], "command")
            self.assertIn(needle, plan["command"])
            self.assertIn(dest, plan["command"])

    def test_missing_file_is_not_ok(self):
        with um._download_lock:
            um._download_task.update({"status": "done", "kind": "dmg",
                                      "dest": os.path.join(self.dir, "没了.dmg")})
        with self.assertRaises(um.InstallPlanError) as cm:
            um.install_plan()
        self.assertIn("已不在磁盘上", str(cm.exception))

    def test_incomplete_task_refuses(self):
        with um._download_lock:
            um._download_task.update({"status": "pending", "dest": self.dir})
        with self.assertRaises(um.InstallPlanError):
            um.install_plan()

    def test_no_task_at_all(self):
        with self.assertRaises(um.InstallPlanError):
            um.install_plan()


class TestCommandSurface(unittest.TestCase):
    """命令注册与返回体形状：壳层是**一行透传**，任何包装都会把判据键藏起来"""

    def setUp(self):
        from hancast_sidecar.commands import CommandHandler
        self.handler = CommandHandler()
        self.addCleanup(self.handler.cleanup)
        um.invalidate_release_cache()

    def test_update_commands_registered(self):
        for cmd in ("update_release_info", "update_download_start", "update_download_status",
                    "update_download_cancel", "update_install_plan"):
            self.assertIn(cmd, self.handler._commands, cmd)

    def test_install_plan_returned_verbatim(self):
        """`lib.rs` 的 `update_apply` 直接 `plan.get("kind")`/`get("path")`

        这条断言存在的理由：如果哪天 handler 把结果包成 `{"plan": {...}}`，
        Rust 那边每个 `get()` 都落空、`update_apply` 只会回一句
        「更新产物路径为空」，而 Sidecar 与界面各自看起来都正常。
        """
        fake = {"ok": True, "kind": "dmg", "path": "/tmp/HanCast_9.9.9_aarch64.dmg",
                "asset": "HanCast_9.9.9_aarch64.dmg", "version": "9.9.9",
                "sha256": SHA, "verified": True, "auto": True}
        with patch.object(um, "install_plan", lambda: fake):
            out = self.handler.execute("update_install_plan", {})
        self.assertEqual(out, fake)
        self.assertEqual(out.get("kind"), "dmg")
        self.assertEqual(out.get("path"), "/tmp/HanCast_9.9.9_aarch64.dmg")

    def test_download_start_passes_asset_and_version(self):
        seen = {}

        def fake(asset: str = "", version: str = "") -> dict:
            seen["asset"], seen["version"] = asset, version
            return {"status": "pending", "stage": "connecting"}
        with patch.object(um, "start_download", fake):
            out = self.handler.execute("update_download_start",
                                      {"asset": "a.dmg", "version": "9.9.9"})
        self.assertEqual(seen, {"asset": "a.dmg", "version": "9.9.9"})
        self.assertEqual(out["stage"], "connecting")

    def test_update_release_info_defaults_to_latest(self):
        seen = {}

        def fake(version: str = "") -> dict:
            seen["version"] = version
            return {"version": "2.4.11", "assets": []}
        with patch.object(um, "get_release", fake):
            self.handler.execute("update_release_info", {})
        self.assertEqual(seen, {"version": ""})

    def test_update_download_start_accepts_missing_params(self):
        """壳层在无产物名时发的就是两个空串；handler 不许 KeyError"""
        with patch.object(um, "start_download", lambda asset="", version="": {
                "status": "pending", "stage": "connecting", "asset": asset}):
            out = self.handler.execute("update_download_start", {})
        self.assertEqual(out["stage"], "connecting")


class TestSourceIsOurGithubOnly(unittest.TestCase):
    """更新链路的地址只能是 pingod/HanCast：原作者那条线不许出现"""

    def test_repo_constants(self):
        self.assertEqual(um.REPO, "pingod/HanCast")
        self.assertIn("pingod/HanCast", um.API_RELEASES)
        self.assertIn("pingod/HanCast", um.RELEASE_URL)

    def test_no_latest_endpoint_used(self):
        """/releases/latest 会把指针给「最近 publish」的那条，不能当版本真相源"""
        self.assertNotIn("releases/latest", um.API_RELEASES + um.API_RELEASE_TAG)


if __name__ == "__main__":
    unittest.main()
