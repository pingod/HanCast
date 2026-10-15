"""真跑一遍下载管线：本地 HTTP 源 → 流式下载 → 解包 → 执行校验

`DownloadTaskTest` 把 `_open_stream` 整个换掉了，验的是任务表与错误文案；这里补的是
剩下那段没被覆盖的真相：`httpclient.make_session()` 能不能流式拉完、`_extract` 落盘后
`read_version` 是否真跑得起来、`status(refresh=True)` 是否立刻把「已下载」认出来。
"""

import gzip
import http.server
import io
import os
import socket
import sys
import tarfile
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.utils import ffmpeg_deps as fd

FAKE = b'#!/bin/sh\necho "ffmpeg version 9.9-test ffmpeg_deps e2e"\n'
FAKE_PROBE = b'#!/bin/sh\necho "ffprobe version 9.9-test"\n'


def _gz():
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as fh:
        fh.write(FAKE)
    return buf.getvalue()


def _zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ffmpeg-master/bin/" + fd.exe("ffmpeg"), FAKE)
        zf.writestr("ffmpeg-master/bin/" + fd.exe("ffprobe"), FAKE_PROBE)
    return buf.getvalue()


def _tar_xz():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name, payload in ((("bin/" + fd.exe("ffmpeg")), FAKE),
                              (("bin/" + fd.exe("ffprobe")), FAKE_PROBE)):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class _Handler(http.server.BaseHTTPRequestHandler):
    payload: bytes = b""
    kind: str = "gz"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args):          # 测试里别往 stderr 刷访问日志
        pass


class DownloadPipelineTest(unittest.TestCase):
    """解包后要真的执行一次，所以只有 POSIX 能跑这套"""

    @classmethod
    def setUpClass(cls):
        if os.name != "posix":
            raise unittest.SkipTest("用 shell 脚本冒充 ffmpeg，非 POSIX 跑不起来")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.setting_dir = self.tmp.name
        fd.reset_cache()
        self.addCleanup(fd.reset_cache)
        # 下载中途的压缩包与解包出来的 bin/ 都落在 SETTING_DIR 下，整段都指到临时目录
        for p in (
            patch.object(fd.config, "SETTING_DIR", self.setting_dir),
            # 本机装了 ffmpeg 的话，系统目录会抢在托管目录之前命中，这里只留托管目录
            patch.object(fd, "_walk_dirs", return_value=[]),
            # 本机代理会被当成出口，127.0.0.1 必须强制直连
            patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*",
                                   "http_proxy": "", "https_proxy": ""}, clear=False),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _serve(self, payload: bytes, kind: str) -> str:
        handler = type("Bound", (_Handler,), {"payload": payload, "kind": kind})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/ffmpeg.{kind}"

    def _run(self, url: str, kind: str) -> dict:
        with patch.object(fd, "download_target", return_value={"url": url, "kind": kind}):
            fd.start_download()
            for _ in range(200):
                task = fd.download_status()
                if task.get("status") != "pending":
                    return task
                threading.Event().wait(0.05)
        self.fail("下载任务没有收尾")

    def test_gz_download_end_to_end(self):
        url = self._serve(_gz(), "gz")
        task = self._run(url, "gz")
        self.assertEqual(task.get("status"), "done", task.get("error"))
        binary = os.path.join(self.setting_dir, "bin", fd.exe("ffmpeg"))
        self.assertTrue(os.path.isfile(binary))
        self.assertTrue(os.access(binary, os.X_OK))
        self.assertEqual(fd.read_version(binary),
                         "ffmpeg version 9.9-test ffmpeg_deps e2e")
        # 下完必须立刻被检测链路认出来，来源标成 downloaded
        info = fd.status(refresh=True, manual=None)
        self.assertTrue(info["ok"], info)
        self.assertEqual(info["path"], binary)
        self.assertEqual(info["source"], "downloaded")

    def test_zip_download_lands_both_binaries(self):
        url = self._serve(_zip(), "zip")
        task = self._run(url, "zip")
        self.assertEqual(task.get("status"), "done", task.get("error"))
        written = [os.path.basename(p) for p in task["result"]["paths"]]
        self.assertEqual(sorted(written), sorted([fd.exe("ffmpeg"), fd.exe("ffprobe")]))
        # 下载中间产物不能留在盘上
        self.assertEqual([n for n in os.listdir(self.setting_dir) if n.endswith(".zip")], [])

    def test_tar_xz_download(self):
        url = self._serve(_tar_xz(), "tar.xz")
        task = self._run(url, "tar.xz")
        self.assertEqual(task.get("status"), "done", task.get("error"))
        self.assertEqual(len(task["result"]["paths"]), 2)

    def test_unrunnable_binary_reports_honest_failure(self):
        """包能下能解，但这份构建在本机跑不起来 —— 必须报「跑不起来」而不是假成功"""
        bad = gzip.compress(b"not an executable at all")
        url = self._serve(bad, "gz")
        task = self._run(url, "gz")
        self.assertEqual(task.get("status"), "error")
        self.assertIn("跑不起来", task["error"])

    def test_dead_source_keeps_underlying_network_error(self):
        """端口没人听：错误里要留着底层异常，不能只剩一句「下载失败」"""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()                     # 占一个确定没人用的端口，别用 server.shutdown() 造死端口
        task = self._run(f"http://127.0.0.1:{port}/ffmpeg.gz", "gz")
        self.assertEqual(task.get("status"), "error")
        self.assertIn("连接下载源失败", task["error"])
        self.assertIn(urlparse(task["url"]).hostname, task["error"])
        # curl 的原始报错必须原样传上来，否则用户只知道「失败」而不知道败在哪
        self.assertIn("ConnectionError", task["error"])


if __name__ == "__main__":
    unittest.main()
