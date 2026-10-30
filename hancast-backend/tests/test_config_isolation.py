"""钉死「测试不落真实用户目录」这条契约

起因（2026-10-30）：`tests/test_commands.py` 直接构造 `CommandHandler()`，
`tearDown` 调 `cleanup()` → `config.save()`，写的就是用户真实配置
`~/Library/Application Support/HanCast/hancast_setting.json`。每跑一次全量测试，
用户设的 `default_device` 就被覆盖成 `"test-id"`、还多一条 `test_key: "test_value"`——
本机已被覆盖几十轮，用户每跑一次测试就丢一次默认设备。

修法在 `conftest.py`：在任何侧车 import 之前把 HOME/APPDATA 指到临时目录。
本文件把这条契约变成断言——哪天隔离被绕过（有人把侧车模块提到 conftest 之前
import、或某处绕过 `SETTING_DIR` 自己拼路径），这里直接红，
而不是安静地往用户配置里写测试数据。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.utils import config as config_mod  # noqa: E402
from hancast_sidecar.utils import identity as identity_mod  # noqa: E402

TEMP = os.path.realpath(tempfile.gettempdir())


def in_temp(path: str) -> bool:
    """路径是否落在临时目录下（真实用户目录永远不可能）"""
    return os.path.realpath(path).startswith(TEMP + os.sep)


class TestNoRealUserDir:
    def test_HOME_指向临时目录(self):
        assert in_temp(os.path.expanduser("~")), \
            "HOME 展开后不在临时目录：conftest 的隔离没生效"

    def test_SETTING_DIR_跟着_HOME_走(self):
        assert in_temp(config_mod.SETTING_DIR), \
            f"config.SETTING_DIR={config_mod.SETTING_DIR!r} 指向真实用户目录"

    def test_身份文件与日志目录也在临时目录(self):
        # identity.py:18 是 `from .config import SETTING_DIR` 按值导入，
        # 只 patch config.SETTING_DIR 救不了它——必须靠 conftest 改环境变量
        assert in_temp(identity_mod._IDENTITY_FILE), \
            f"identity._IDENTITY_FILE={identity_mod._IDENTITY_FILE!r} 在真实用户目录"
        from hancast_sidecar.utils import logger as logger_mod
        log_dir = getattr(logger_mod, "LOG_DIR", None)
        if log_dir:                        # 某些平台分支不设这个常量
            assert in_temp(log_dir), f"日志目录会写进真实用户目录：{log_dir}"

    def test_写入只落临时盘(self):
        """重演当年那次污染：写默认设备并 save，落点必须在临时 HOME 里"""
        cfg = config_mod.Config()
        cfg.set_default_device("test-id")
        cfg.save()
        assert os.path.exists(cfg.setting_path)
        assert in_temp(cfg.setting_path), \
            f"Config.save() 写到了真实用户目录：{cfg.setting_path}"
        assert cfg.default_device == "test-id"

    def test_台账目录也在临时盘(self):
        from hancast_sidecar.media import failures
        assert in_temp(failures.SETTING_DIR), \
            f"解析失败台账会写进真实用户目录：{failures.SETTING_DIR}"
