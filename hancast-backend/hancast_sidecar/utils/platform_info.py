"""
平台 / 版本信息

UPnP 的 SERVER 头格式为 `{OS}/{OSVersion} UPnP/1.0 {App}/{AppVersion}`，
控制点会依据它选择兼容策略，所以必须如实反映运行平台和真实版本号。

历史上这个字符串在三个文件里各硬编码了一份
（`protocol/server.py`、`ssdp.py`、`protocol/dlna.py`），内容都是
`Windows/10 UPnP/1.0 HanCast/2.0`——在 macOS/Linux 上谎报操作系统，
版本号也长期停在 2.0 不再跟随发布版本。这里收敛成唯一实现。
"""

import platform
import sys

APP_NAME = "HanCast"


def os_token() -> str:
    """返回 `{OS}/{OSVersion}` 片段"""
    if sys.platform == 'win32':
        return f"Windows/{platform.release() or '10'}"
    if sys.platform == 'darwin':
        return f"macOS/{platform.mac_ver()[0] or platform.release()}"
    return f"Linux/{platform.release()}"


def build_server_info(version: str) -> str:
    """组装完整的 UPnP SERVER 头"""
    return f"{os_token()} UPnP/1.0 {APP_NAME}/{version}"
