"""应用内自更新（检查 → 下载 → 装）

本程序此前只把用户送到网页上去下载：`check_update` 拿到 release 的 `html_url`，
前端 `window.open` 打开，剩下的「挑哪个安装包、下完校验、怎么装」全是用户的事。
国内直连 GitHub 时那条路经常走不通（网页打开、资产在 `objects.githubusercontent.com`、
速度掉到几十 KB/s），所以才有了「每次都要去页面下载好麻烦」这句反馈。

这里把这几步搬进应用内，判据与既有约定对齐：

- **只有一个真相源**：产物清单来自 GitHub Release API（`pingod/HanCast`），
  不再引入第二套「自建最新版本 JSON」。gitee 那一路由已删除，理由见
  `commands._check_update` 的注释与 docs/构建与发布.md §7.5。
- **镜像只改写 GitHub 主机**：所有出网地址一律过 `utils/ghmirror.candidates()`，
  与规则包下载、FFmpeg 下载同一个开关（后台参数 `github.mirror_enabled`），
  失败自动回退直连，两条路的失败原因都进日志。
- **摘要必须校验**：GitHub 每个 asset 带 `digest`（`sha256:…`），下载完逐字节比对。
  镜像换包 / 传输被截断都当场判死并如实写进任务表——与官方索引的 sha256 同理
  （`media/rules/jsplugins.import_url`）。
- **本模块不装任何东西**：安装包由 Rust 侧执行（`update_apply`）。Sidecar 只做
  「取清单、下文件、算摘要、给安装方式」，因为替换正在运行的 app 属于壳层的事。
"""

# **注意绑定点**：`commands.py` 走的是 `from .update import manager as app_update`，
# 不是这里转出的名字。原因很实际——把符号 re-export 到包一层之后再 patch 包属性，
# 原模块里的函数对象纹丝不动，测试改不到、线上也改不到（本轮的
# tests/test_update_manager.py::TestCommandSurface 就是这么抓出来第一条的）。
# 这份转出只给外部读者当一个「本模块对外就这 9 个名字」的清单看。
from .manager import (
    InstallPlanError,
    UpdateError,
    cancel_download,
    check_for_update,
    describe_release,
    download_status,
    get_release,
    install_plan,
    parse_version,
    start_download,
)

__all__ = [
    "InstallPlanError",
    "UpdateError",
    "cancel_download",
    "check_for_update",
    "describe_release",
    "download_status",
    "get_release",
    "install_plan",
    "parse_version",
    "start_download",
]
