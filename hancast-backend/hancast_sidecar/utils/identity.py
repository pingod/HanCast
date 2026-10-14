"""对外请求身份：UA 与设备号只有一个来源

以前桌面 UA 写在 ``media/rules/base.py``（Chrome/124），本机代理另写一份在
``media/server.py``（Chrome/120）——同一次投屏里，电视直连和本机代理向源站报的是
两个不同的浏览器。站点风控盯的正是这种不一致，所以身份必须单点定义、全链路共用。

UA 走后台参数（``http.ua_desktop`` / ``http.ua_mobile``），改了不用重新编译，
下一次解析即生效；留空则回落到内置默认值。
"""

import json
import logging
import os
import secrets
import threading
import uuid

from .config import SETTING_DIR
from ..params import (DEFAULT_UA_DESKTOP, DEFAULT_UA_MOBILE,
                      get as _param)

logger = logging.getLogger("hancast.identity")

# 默认值登记在参数表里，这里只是沿用老名字对外提供（rules/base.py 等仍在 import）
DESKTOP_UA = DEFAULT_UA_DESKTOP
MOBILE_UA = DEFAULT_UA_MOBILE

_IDENTITY_FILE = os.path.join(SETTING_DIR, "device_identity.json")
_id_lock = threading.Lock()
_device_id: str = ""


def _from_param(key: str, fallback: str) -> str:
    try:
        value = str(_param(key) or "").strip()
    except KeyError:          # 参数表还没登记这一项（单测直接构造时）
        return fallback
    return value or fallback


def desktop_ua() -> str:
    return _from_param("http.ua_desktop", DESKTOP_UA)


def mobile_ua() -> str:
    return _from_param("http.ua_mobile", MOBILE_UA)


def device_id() -> str:
    """本机固定设备号：规则包拿它钉死「设备参数」，同一台机器每次解析必须给同一个值

    站点把随机设备号当账号的一部分，每次变就会被当成新设备风控；持久化到
    ``device_identity.json`` 后，重装规则包、改 UA 都不会换掉它。
    """
    global _device_id
    with _id_lock:
        if _device_id:
            return _device_id
        try:
            with open(_IDENTITY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved = str(data.get("device_id") or "").strip()
            if saved:
                _device_id = saved
                return _device_id
        except (OSError, ValueError):
            pass
        _device_id = secrets.token_hex(16)
        try:
            os.makedirs(SETTING_DIR, exist_ok=True)
            with open(_IDENTITY_FILE, "w", encoding="utf-8") as f:
                json.dump({"device_id": _device_id,
                           "created_usn": str(uuid.uuid4())}, f)
        except OSError as e:
            logger.warning("设备号落盘失败（本次仍可用，重启后会重新生成）: %s", e)
        return _device_id
