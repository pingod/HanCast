"""B 站规则：官方接口 + 签名，拿到链接即可解析（url 阶段）"""

from ..bili_resolver import BILIBILI_SUFFIXES
from .base import SiteRule, register, RuleContext


class BilibiliRule(SiteRule):
    name = "bilibili"
    # 与 BiliResolver.belongs_to 同一份主机表，两边不会各说各话
    hosts = tuple(sorted(BILIBILI_SUFFIXES))
    stage = "url"
    priority = 10

    def resolve(self, url: str, html: str, ctx: RuleContext) -> dict:
        # 具体取流/注册本机代理仍由 BiliResolver 负责（反爬 Cookie 缓存跨调用复用）
        return ctx.resolver.resolve_bilibili_media_info(url)


register(BilibiliRule())
