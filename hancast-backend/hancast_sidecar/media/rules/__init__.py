"""站点解析规则包

导入本包即完成注册：内置规则在各自模块底部 ``register()``，
外置声明式规则的加载器由 ``declarative`` 注入 ``base`` 的注册表。
新增内置站点 = 在这里加一行 import，解析主流程不用改。
"""

from .base import (DESKTOP_UA, MOBILE_UA, RuleContext, SiteRule,
                   matching, refresh_external, registered)
from . import bili as _bili                    # noqa: F401  注册 BilibiliRule
from . import douyin as _douyin                # noqa: F401  注册 DouyinRule
from . import declarative as _declarative      # noqa: F401  注入外置规则加载器

__all__ = ["DESKTOP_UA", "MOBILE_UA", "RuleContext", "SiteRule",
           "matching", "refresh_external", "registered"]
