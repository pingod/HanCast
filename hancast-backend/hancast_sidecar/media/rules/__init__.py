"""站点解析规则包

程序本身不带任何站点规则：注册表里的规则全部来自用户安装的规则包，
JS 脚本包由 ``jsplugins`` 注入，声明式 JSON 规则由 ``declarative`` 注入。
"""

from .base import (DESKTOP_UA, MOBILE_UA, RuleContext, SiteRule, desktop_ua,
                   matching, mobile_ua, refresh_external, registered)
from . import declarative as _declarative      # noqa: F401  注入外置规则加载器

__all__ = ["DESKTOP_UA", "MOBILE_UA", "desktop_ua", "mobile_ua",
           "RuleContext", "SiteRule",
           "matching", "refresh_external", "registered"]
