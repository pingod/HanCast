"""测试期的落盘隔离

两类历史污染，都真实发生过：

1. **解析失败台账**：`ShareResolver.resolve()` 解不出来会往台账记一条（media/failures.py），
   台账默认写在用户配置目录。以前只有 test_parse_failures 自己换了路径，
   test_share_resolver / test_jsplugins 是直接调 `resolve()` 的，
   于是测试用的假地址（b23.tv/AbCdEf、ads.example.com/lp）会出现在用户
   设置页的「解析失败台账」里，把真实失败冲淡。

2. **用户设置本身**（2026-10-30 查出）：`tests/test_commands.py` 直接 `CommandHandler()`，
   而 `Config` 的路径来自 `utils/config.py` 的模块级 `SETTING_DIR`（真实
   `~/Library/Application Support/HanCast`），`tearDown` 又调 `cleanup()` →
   `config.save()`。结果每跑一次全量测试，用户配置里的 `default_device`
   就被写成 `"test-id"`、还多一条 `test_key: "test_value"`——本机实测已被覆盖了
   几十轮。默认设备被改成不存在的 id，等于用户自己设的默认设备每次跑测试就丢一次。

做法：在**导入任何 hancast_sidecar 模块之前**把 HOME / APPDATA 指到临时目录。
`config.SETTING_DIR`、`identity._IDENTITY_FILE`、`logger.LOG_DIR` 三处都是
`import` 时由 `expanduser('~')` / `APPDATA` 算出来的，改环境变量是唯一能一次
覆盖三处的办法（只 patch `config.SETTING_DIR` 会漏掉 identity 与日志，因为
`identity.py:18` 是 `from .config import SETTING_DIR` 按值导入的）。
"""
import os
import shutil
import sys
import tempfile

import pytest

# ── 必须在任何 hancast_sidecar import 之前生效 ──
_TMP_HOME = tempfile.mkdtemp(prefix="hancast-test-home-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME          # Windows 侧 expanduser 读这个
os.environ["APPDATA"] = os.path.join(_TMP_HOME, "AppData", "Roaming")
os.environ["LOCALAPPDATA"] = os.path.join(_TMP_HOME, "AppData", "Local")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hancast_sidecar.media import failures  # noqa: E402
from hancast_sidecar.utils import config as config_mod  # noqa: E402
from hancast_sidecar.utils import identity as identity_mod  # noqa: E402

# 自检：路径真的落在临时 HOME 里才继续。哪天有人在 conftest 之前又 import 了侧车
# 模块（那行常量的求值就会发生在真实目录上），这里当场失败，
# 而不是安静地写回用户配置——本轮要修的就是这种"安静"。
_REAL_HOME = os.path.expanduser("~")
if not _REAL_HOME.startswith(tempfile.gettempdir()):
    raise RuntimeError(
        f"测试隔离失效：HOME 展开成 {_REAL_HOME!r}，不是临时目录。"
        "检查是否有插件在 conftest 之前导入了 hancast_sidecar。")
if not config_mod.SETTING_DIR.startswith(_REAL_HOME):
    raise RuntimeError(
        f"测试隔离失效：config.SETTING_DIR={config_mod.SETTING_DIR!r} 不在临时 HOME 下。")
if not identity_mod._IDENTITY_FILE.startswith(_REAL_HOME):
    raise RuntimeError(
        f"测试隔离失效：identity._IDENTITY_FILE={identity_mod._IDENTITY_FILE!r} "
        "不在临时 HOME 下（它是在 import 时定值的）。")


@pytest.fixture(autouse=True, scope="session")
def isolated_setting_dir():
    """台账目录跟着临时 HOME 走（保留显式 patch，不依赖 import 顺序）"""
    tmp = os.path.join(_TMP_HOME, "failures")
    failures.SETTING_DIR = tmp
    try:
        yield tmp
    finally:
        shutil.rmtree(_TMP_HOME, ignore_errors=True)
