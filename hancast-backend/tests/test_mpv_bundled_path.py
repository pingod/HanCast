"""打包版随包 mpv 的定位（重点是 macOS 的 mpv.app 形态，以及随包那份跑不起来时让位）

对应缺陷一：HanCast.app/Contents/Resources/mpv/mpv.app 一直在隔壁，但 _get_base_path()
只探 mpv/mpv.exe，于是没装 Homebrew mpv 的干净机器上打包版找不到播放器。

对应缺陷二（v2.3.17，Arch/CachyOS）：随包 mpv 是 CI 的 Ubuntu runner 那份构建，
滚动发行版的 ffmpeg soname 对不上（ldd 报 libavcodec.so.60 => not found），
文件在但跑不起来。这一层以前不验就返回 READY 并写进配置，
系统里装了能用的 mpv 也永远走不到兜底那一步。
"""

import os
import shutil
import sys
import tempfile
import unittest

from hancast_sidecar.models.mpv import MpvStatus
from hancast_sidecar.utils import mpv_manager
from hancast_sidecar.utils.mpv_manager import MpvManager


class _StubConfig:
    def get_mpv(self):
        return {}

    def set_mpv(self, value):
        pass


def _write(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n")
    return path


def _write_mpv(path, exit_code=None):
    """造一个可执行的假 mpv：不给 exit_code 时 `--version` 正常返回 0（跑得起），
    给了就模拟「动态库缺失」那种立刻非 0 退出。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = "#!/bin/sh\n" + (f"exit {exit_code}\n" if exit_code is not None else "")
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path


class TestBundledMpvLocation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # 「Rust 把 current_dir 设成 resource_dir」：把这一层搬到临时目录，
        # 另一个候选（sidecar exe 所在目录）指向同目录，保证两个候选都可控。
        self.cwd = os.path.join(self.tmp, "Contents", "Resources")
        os.makedirs(self.cwd)
        real_cwd = os.getcwd
        real_exec = sys.executable
        mpv_manager.os.getcwd = lambda: self.cwd
        sys.executable = os.path.join(self.cwd, "hancast-sidecar")
        self.addCleanup(setattr, mpv_manager.os, "getcwd", real_cwd)
        self.addCleanup(setattr, sys, "executable", real_exec)

    def test_macos_app_bundle_is_found(self):
        binary = _write(os.path.join(self.cwd, "mpv", "mpv.app",
                                     "Contents", "MacOS", "mpv"))
        manager = MpvManager(_StubConfig())
        self.assertEqual(manager.base_path, self.cwd)
        self.assertEqual(manager._find_in_program_dir(), binary)

    def test_flat_bundled_mpv_is_found(self):
        binary = _write(os.path.join(self.cwd, "mpv", "mpv"))
        manager = MpvManager(_StubConfig())
        self.assertEqual(manager.base_path, self.cwd)
        self.assertEqual(manager._find_in_program_dir(), binary)

    def test_empty_mpv_dir_does_not_win(self):
        """空的 mpv/ 目录不算随包 mpv，否则会停在找不到二进制的这一层"""
        os.makedirs(os.path.join(self.cwd, "mpv"))
        manager = MpvManager(_StubConfig())
        dev_path = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(mpv_manager.__file__)))))
        self.assertEqual(manager.base_path, dev_path)

    def _with_system_mpv(self):
        """在 PATH 前面挂一个能跑的 mpv，返回它的期望路径"""
        bindir = os.path.join(self.tmp, "sysbin")
        good = _write_mpv(os.path.join(bindir, "mpv"))
        real_path = os.environ.get("PATH", "")
        os.environ["PATH"] = bindir + os.pathsep + real_path
        self.addCleanup(os.environ.__setitem__, "PATH", real_path)
        return good

    def test_runnable_bundled_mpv_still_wins(self):
        """随包那份跑得起时优先级不变：bundled 仍然压过系统 mpv"""
        _write_mpv(os.path.join(self.cwd, "mpv", "mpv"))
        good = self._with_system_mpv()
        info = MpvManager(_StubConfig()).check()
        self.assertEqual(info.status, MpvStatus.READY)
        self.assertEqual(info.source, "bundled")
        self.assertNotEqual(info.path, good)

    def test_unrunnable_bundled_mpv_falls_back_to_system(self):
        """随包 mpv 在 Arch 上因 ffmpeg soname 缺失跑不起来 —— 必须让位给系统 mpv"""
        broken = _write_mpv(os.path.join(self.cwd, "mpv", "mpv"), exit_code=127)
        good = self._with_system_mpv()
        info = MpvManager(_StubConfig()).check()
        self.assertEqual(info.status, MpvStatus.READY)
        self.assertEqual(info.path, good, f"broken={broken} 不该被当成就绪")
        self.assertEqual(info.source, "system")

    def test_only_unrunnable_bundled_mpv_reports_not_found(self):
        """随包跑不起来、系统也没有：NOT_FOUND，而不是一个自报 READY 的坏路径"""
        _write_mpv(os.path.join(self.cwd, "mpv", "mpv"), exit_code=127)
        real_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.path.join(self.tmp, "empty-bin")
        self.addCleanup(os.environ.__setitem__, "PATH", real_path)
        info = MpvManager(_StubConfig()).check()
        self.assertEqual(info.status, MpvStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
