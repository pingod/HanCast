"""GitHub 加速镜像：把「墙内访问 GitHub」这一个动作收敛到一处可开关的改写

所有运行时对 GitHub 的请求都从这里过：规则包索引与 zip（``media/rules/jsplugins``）、
应用自更新检查（``commands``）、FFmpeg 按需下载（``utils/ffmpeg_deps``）。
加新站点时**不要**自己拼镜像地址——调 :func:`candidates` 就能拿到「先镜像、后直连」。

前缀形态是 gh-proxy 家族的通用写法 ``<prefix><完整原地址>``，实测
（2026-10-15，本机不挂代理）``https://gh-proxy.com/`` 对三种地址形态都成立：

- ``raw.githubusercontent.com/…/index.json`` → 200，正文与直连逐字节一致；
- ``…/dist/<pack>-<version>.zip`` → 200，sha256 与索引条目一致（所以下载侧的
  摘要校验照旧生效，镜像想换包会被当场拦下）；
- ``github.com/<owner>/<repo>/releases/download/<tag>/<file>`` → 206，
  服务端替我们跟完到 ``objects.githubusercontent.com`` 的二跳。

``api.github.com`` 只有部分镜像代理（gh-proxy.com 可，ghfast.top 回 403），
所以镜像前缀是用户可改的参数，而不是写死常量。

失败必须如实回退：镜像是加速器不是新真相源。前缀挂了就走直连，两条路的失败
原因都进日志——否则用户只会看到「检查更新失败」，不知道断在哪一段。
"""

import logging
from typing import List, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("hancast.github")

# 只有这些主机会被改写；站点自己的地址（gitee、gyan.dev、johnvansickle.com…）原样通过
_GITHUB_HOSTS = ("github.com", "raw.githubusercontent.com", "api.github.com",
                 "codeload.github.com", "objects.githubusercontent.com")

PARAM_ENABLED = "github.mirror_enabled"
PARAM_PREFIX = "github.mirror_prefix"


def _get(key: str):
    try:
        from ..params import get
        return get(key)
    except Exception as e:                       # noqa: BLE001
        # 参数表读不到（例如单元测试只装了半个环境）时按「不开镜像」处理，
        # 绝不能让一个加速开关把解析链路整个拖崩
        logger.debug(f"镜像参数读取失败 {key}: {e}")
        return None


def is_github(url: str) -> bool:
    try:
        host = (urlparse(str(url).strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in _GITHUB_HOSTS or host.endswith(".githubusercontent.com")


def enabled() -> bool:
    return bool(_get(PARAM_ENABLED))


def prefix() -> str:
    p = str(_get(PARAM_PREFIX) or "").strip()
    return p if p.lower().startswith(("http://", "https://")) else ""


def rewrite(url: str) -> str:
    """这条地址该走的镜像形态；不适用（未开关/非 GitHub/前缀没填）就返回空串"""
    p = prefix()
    if not p or not enabled() or not is_github(url):
        return ""
    return p + str(url).strip()


def candidates(url: str) -> List[Tuple[str, str]]:
    """按尝试顺序返回 ``[(来源标签, 地址), …]``

    开着镜像就是「先镜像、再直连」，其余情况只有原地址一条——调用方不需要
    知道开关的存在，循环里逐个试、失败换下一个即可。
    """
    original = str(url).strip()
    mirrored = rewrite(original)
    if not mirrored:
        return [("直连", original)]
    return [("镜像", mirrored), ("直连", original)]
