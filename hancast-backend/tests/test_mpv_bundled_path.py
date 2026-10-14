"""打包版随包 mpv 的定位（重点是 macOS 的 mpv.app 形态）

对应缺陷：HanCast.app/Contents/Resources/mpv/mpv.app 一直在隔壁，但 _get_base_path()
只探 mpv/mpv.exe，于是没装 Homebrew mpv 的干净机器上打包版找不到播放器。
"""

import os
import shutil
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
