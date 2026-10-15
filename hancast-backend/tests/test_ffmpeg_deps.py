"""FFmpeg 运行依赖检测与按需下载（utils/ffmpeg_deps.py）

自 v2.3.11 起程序不再捆绑 FFmpeg，这个模块是「检测 → 缺失清单 → 安装命令 →
按需下载」的唯一实现。这里不碰真实网络也不 spawn 真实 ffmpeg：
`read_version` / `find_binary` / `_open_stream` 全部替身，只验契约与解包行为。
"""

import gzip
import io
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.utils import ffmpeg_deps as fd


def _make_fake_bin(dirs):
    """在给定目录里放一个可执行占位文件，返回其路径"""
    os.makedirs(dirs, exist_ok=True)
    path = os.path.join(dirs, fd.exe("ffmpeg"))
    with open(path, "wb") as f:
        f.write(b"#!/bin/sh\necho fake\n")
    os.chmod(path, 0o755)
    return path


class FindBinaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.system_dir = os.path.join(self.tmp.name, "system")
        self.managed = os.path.join(self.tmp.name, "managed")
        self.p_walk = patch.object(fd, "_walk_dirs", return_value=[self.system_dir])
        self.p_managed = patch.object(fd, "managed_dir", return_value=self.managed)
        # 真跑 `ffmpeg -version` 会把测试绑在本机的构建上，这里一律当"能跑"
        self.p_ver = patch.object(fd, "read_version", lambda p: "ffmpeg version fake")
        self.p_walk.start()
        self.p_managed.start()
        self.p_ver.start()
        self.addCleanup(patch.stopall)

    def test_prefers_system_over_downloaded(self):
        syspath = _make_fake_bin(self.system_dir)
        _make_fake_bin(self.managed)
        path, source = fd.find_binary("ffmpeg")
        self.assertEqual(path, syspath)
        self.assertEqual(source, "system")

    def test_falls_back_to_downloaded(self):
        dl = _make_fake_bin(self.managed)
        path, source = fd.find_binary("ffmpeg")
        self.assertEqual(path, dl)
        self.assertEqual(source, "downloaded")

    def test_missing_returns_none(self):
        self.assertEqual(fd.find_binary("ffmpeg"), (None, None))

    def test_unrunnable_binary_is_not_found(self):
        """装了但跑不起来（架构不对/依赖缺失）不能算检测到"""
        _make_fake_bin(self.system_dir)
        with patch.object(fd, "read_version", return_value=None):
            self.assertEqual(fd.find_binary("ffmpeg"), (None, None))


class StatusContractTest(unittest.TestCase):
    """弹窗与投屏门禁共用的那份报告：老键不能少，新键要齐"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fd.reset_cache()
        self.addCleanup(fd.reset_cache)
        self.p_managed = patch.object(fd, "managed_dir",
                                      return_value=os.path.join(self.tmp.name, "bin"))
        self.p_managed.start()
        # 不还掉的话，后面任何用例的 managed_dir() 都会指进这个已删除的临时目录
        self.addCleanup(self.p_managed.stop)

    def _report(self, found):
        """found: {'ffmpeg': path|None, 'ffprobe': path|None}"""
        def fake_find(name):
            path = found.get(name)
            return (path, "system") if path else (None, None)
        with patch.object(fd, "find_binary", side_effect=fake_find), \
                patch.object(fd, "read_version", return_value="ffmpeg version fake"):
            return fd.status(refresh=True)

    def test_missing_ffmpeg_shape(self):
        r = self._report({})
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing"], ["FFmpeg"])
        self.assertEqual(r["optional_missing"], ["FFprobe"])
        self.assertIn("brew", " ".join(r["install_commands"]))
        self.assertTrue(r["error"])
        self.assertEqual([i["key"] for i in r["items"]], ["ffmpeg", "ffprobe"])
        self.assertFalse(r["items"][0]["found"])

    def test_ffprobe_only_missing_is_not_fatal(self):
        """FFprobe 是可选件：缺了它 ok 仍为真，投屏不该被挡住"""
        r = self._report({"ffmpeg": "/x/ffmpeg"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["missing"], [])
        self.assertEqual(r["optional_missing"], ["FFprobe"])
        self.assertEqual(r["install_commands"], [])

    def test_manual_path_wins_and_bring_its_sibling(self):
        bindir = os.path.join(self.tmp.name, "manual")
        os.makedirs(bindir, exist_ok=True)
        ff = os.path.join(bindir, fd.exe("ffmpeg"))
        fp = os.path.join(bindir, fd.exe("ffprobe"))
        for p in (ff, fp):
            open(p, "wb").close()
        with patch.object(fd, "find_binary", return_value=(None, None)), \
                patch.object(fd, "read_version", return_value="ffmpeg version fake"):
            r = fd.status(refresh=True, manual=ff)
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "manual")
        self.assertEqual(r["path"], ff)
        self.assertEqual(r["ffprobe_path"], fp)

    def test_cache_honours_refresh_and_manual_key(self):
        calls = {"n": 0}

        def fake_find(name):
            calls["n"] += 1
            return None, None

        with patch.object(fd, "find_binary", side_effect=fake_find), \
                patch.object(fd, "read_version", return_value=None):
            fd.status(refresh=True)
            self.assertEqual(calls["n"], 2)
            fd.status()                       # 命中缓存，不再扫描
            self.assertEqual(calls["n"], 2)
            fd.status(refresh=True)           # 「重新检测」必须重扫
            self.assertEqual(calls["n"], 4)
            fd.status(manual="/somewhere/ffmpeg")   # 手动路径是另一个缓存键
            self.assertEqual(calls["n"], 6)

    def test_probe_path_falls_back_to_bare_command(self):
        with patch.object(fd, "status", return_value={"ffprobe_path": None}):
            self.assertEqual(fd.probe_path(), "ffprobe")


class PlatformContractTest(unittest.TestCase):
    def test_install_commands_per_platform(self):
        with patch.object(fd, "sys_platform", return_value="win32"):
            cmds = fd.install_commands()
        self.assertIn("winget install --id Gyan.FFmpeg -e", cmds)
        with patch.object(fd, "sys_platform", return_value="darwin"):
            self.assertTrue(any("brew install ffmpeg" in c for c in fd.install_commands()))
        with patch.object(fd, "sys_platform", return_value="linux"):
            self.assertTrue(any("apt install" in c for c in fd.install_commands()))

    def test_download_target_known_and_unknown_platform(self):
        with patch.object(fd, "sys_platform", return_value="darwin"), \
                patch.object(fd.platform, "machine", return_value="arm64"):
            t = fd.download_target()
        self.assertEqual(t["kind"], "gz")
        self.assertTrue(t["available"])
        self.assertEqual(fd._wanted_names("gz"), (fd.exe("ffmpeg"),))   # 该源没有 ffprobe
        self.assertEqual(set(fd._wanted_names("zip")),
                         {fd.exe("ffmpeg"), fd.exe("ffprobe")})
        with patch.object(fd, "sys_platform", return_value="sunos"), \
                patch.object(fd.platform, "machine", return_value="sparc"):
            self.assertIsNone(fd.download_target())

    def test_format_error_chains_underlying_exception(self):
        try:
            try:
                try:
                    raise OSError(8, "nodename nor servname provided")
                except OSError as inner:
                    raise RuntimeError("连接下载源失败") from inner
            except RuntimeError as mid:
                raise fd.DependencyError("下载中断") from mid
        except Exception as e:
            text = fd.format_error(e)
        self.assertIn("下载中断", text)
        self.assertIn("nodename nor servname provided", text)
        self.assertIn("←", text)


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "bin")

    def _zip(self, entries):
        path = os.path.join(self.tmp.name, "pkg.zip")
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        return path

    def test_zip_takes_binaries_from_nested_dirs(self):
        """gyan.dev 的包是 ffmpeg-X.Y-essentials_build/bin/ffmpeg.exe（Windows 口径）"""
        archive = self._zip({
            "ffmpeg-7.1-essentials_build/README.txt": "doc",
            "ffmpeg-7.1-essentials_build/bin/" + fd.exe("ffmpeg"): "FFMPEG",
            "ffmpeg-7.1-essentials_build/bin/" + fd.exe("ffprobe"): "FFPROBE",
        })
        written = fd._extract(archive, "zip", self.out)
        names = sorted(os.path.basename(p) for p in written)
        self.assertEqual(names, sorted([fd.exe("ffmpeg"), fd.exe("ffprobe")]))
        with open(os.path.join(self.out, fd.exe("ffmpeg")), "rb") as f:
            self.assertEqual(f.read(), b"FFMPEG")
        self.assertTrue(os.stat(written[0]).st_mode & 0o111)

    def test_zip_entry_cannot_escape_target_dir(self):
        """条目名一律取 basename：`../../evil/ffmpeg` 只能落在目标目录里"""
        archive = self._zip({
            "../../../evil/" + fd.exe("ffmpeg"): "X",
            "a/b/bin/" + fd.exe("ffmpeg"): "FFMPEG",
        })
        written = fd._extract(archive, "zip", self.out)
        self.assertTrue(written)
        for p in written:
            self.assertEqual(os.path.dirname(p), self.out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "evil")))

    def test_gz_is_a_single_binary(self):
        path = os.path.join(self.tmp.name, "ffmpeg-darwin-arm64.gz")
        with gzip.open(path, "wb") as f:
            f.write(b"FFMPEG")
        written = fd._extract(path, "gz", self.out)
        self.assertEqual([os.path.basename(p) for p in written], [fd.exe("ffmpeg")])

    def test_tar_xz(self):
        path = os.path.join(self.tmp.name, "ffmpeg-static.tar.xz")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:xz") as tf:
            for name, data in (("ffmpeg-amd64-static/" + fd.exe("ffmpeg"), b"FFMPEG"),
                               ("ffmpeg-amd64-static/" + fd.exe("ffprobe"), b"FFPROBE"),
                               ("ffmpeg-amd64-static/readme.txt", b"doc")):
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        with open(path, "wb") as f:
            f.write(buf.getvalue())
        written = fd._extract(path, "tar.xz", self.out)
        self.assertEqual(sorted(os.path.basename(p) for p in written),
                         sorted([fd.exe("ffmpeg"), fd.exe("ffprobe")]))

    def test_package_without_ffmpeg_fails_loudly(self):
        archive = self._zip({"x/bin/" + fd.exe("ffprobe"): "FFPROBE"})
        with self.assertRaises(fd.DependencyError) as ctx:
            fd._extract(archive, "zip", self.out)
        self.assertIn("ffmpeg", str(ctx.exception))

    def test_unknown_kind_rejected(self):
        with self.assertRaises(fd.DependencyError):
            fd._extract("whatever", "7z", self.out)


class DownloadTaskTest(unittest.TestCase):
    def test_status_idle_before_any_download(self):
        with patch.object(fd, "_download_task", {}, create=False):
            fd._download_task.clear()
            self.assertEqual(fd.download_status(), {"status": "idle"})

    def test_start_without_source_raises_with_hint(self):
        with patch.object(fd, "download_target", return_value=None), \
                patch.object(fd, "sys_platform", return_value="sunos"):
            with self.assertRaises(fd.DependencyError) as ctx:
                fd.start_download()
        self.assertIn("重新检测", str(ctx.exception))

    def test_incomplete_download_raises(self):
        class FakeResp:
            headers = {"Content-Length": "100"}

            def iter_content(self, chunk_size=0):
                yield b"x" * 10

            def close(self):
                pass

        class FakeSession:
            def close(self):
                pass

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dest = os.path.join(tmp.name, "pkg.zip")
        with patch.object(fd, "_open_stream", return_value=(FakeSession(), FakeResp())):
            with self.assertRaises(fd.DependencyError) as ctx:
                fd._download_archive("http://x/y.zip", dest, None)
        self.assertIn("下载不完整", str(ctx.exception))

    def test_http_error_surfaces_status_code(self):
        class FakeResp:
            status_code = 404
            headers = {}

            def close(self):
                pass

        class FakeSession:
            def get(self, url, **kw):
                return FakeResp()

            def close(self):
                pass

        with patch("hancast_sidecar.utils.httpclient.make_session",
                   return_value=FakeSession()):
            with self.assertRaises(fd.DependencyError) as ctx:
                fd._open_stream("http://x/y.zip")
        self.assertIn("404", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
