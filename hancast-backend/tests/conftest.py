"""测试期的落盘隔离

`ShareResolver.resolve()` 解不出来会往解析失败台账记一条（media/failures.py），
而台账默认写在用户配置目录。以前只有 test_parse_failures 自己换了路径，
test_share_resolver / test_jsplugins 是直接调 `resolve()` 的，
于是测试用的假地址（b23.tv/AbCdEf、ads.example.com/lp）会出现在用户
设置页的「解析失败台账」里，把真实失败冲淡。这里一次性兜住。
"""
import os
import shutil
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hancast_sidecar.media import failures  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def isolated_setting_dir():
    tmp = tempfile.mkdtemp(prefix="hancast-tests-")
    real = failures.SETTING_DIR
    failures.SETTING_DIR = tmp
    try:
        yield tmp
    finally:
        failures.SETTING_DIR = real
        shutil.rmtree(tmp, ignore_errors=True)
