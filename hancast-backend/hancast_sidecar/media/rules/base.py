"""规则注册表底座：规则从「装进来的包」来，不从这里长出来

规则分两个阶段（stage）：
  ``url``   拿到链接就能解析（有官方接口/签名算法的站点）
  ``page``  必须先抓到页面正文（分享页、采集站这类把播放地址藏在正文里的）

``ShareResolver._resolve_url`` 的执行顺序：
  url 阶段规则 → 抓页 → （展开短链后再跑一次）url 阶段规则 →
  直链/播放列表判定 → page 阶段规则（逐条，失败则回退下一条）→ 通用正文提取兜底。

注册表里只有两种来源：JS 脚本包（``jsplugins.py``）与外置声明式 JSON
（``declarative.py``），都是运行期注入、用户可增可删可停。
程序本身不带任何站点规则——想让某个站被解析，就去装规则包。

``hosts`` 里写 ``"*"`` 是**兜底作用域**（见 ``WILDCARD_HOST``）：这条规则不认域名，
任何地址都可能命中，但 ``matching`` 保证它只在同阶段的站点专属规则全都拿不下之后
才被试。这样「通用采集站规则」和「某站的精确规则」可以共存而互不遮挡。
"""

import os
import re
import json
import logging
from dataclasses import dataclass
from html import unescape as html_unescape
from typing import Optional
from urllib.parse import urlparse, urljoin

from ...models.media import MediaInfo
from ...utils.httpclient import HttpSession

logger = logging.getLogger("hancast.media.rules")

# hosts 里写这个值 = 兜底作用域（不认域名，排在站点专属规则之后试）
WILDCARD_HOST = "*"

# 身份常量单点定义在 utils/identity.py（代理侧要用同一个 UA，不能再各写一份）
from ...utils.identity import (DESKTOP_UA, MOBILE_UA, desktop_ua, mobile_ua)


def normalize_text(text: str) -> str:
    """归一化 JS 转义与 HTML 实体：\\/ 、\\u002F → / ；\\u0026 、&amp; → &"""
    text = text.replace("\\/", "/")
    text = re.sub(r"\\u002F", "/", text, flags=re.I)
    text = re.sub(r"\\u0026", "&", text, flags=re.I)
    return html_unescape(text)


def decode_js_string(raw: str) -> str:
    """JS/JSON 字符串字面量 → 真实文本（\\/ 、\\u002F 、\\u0026 等转义一次解干净）"""
    try:
        return json.loads(f'"{raw}"')
    except (ValueError, json.JSONDecodeError):
        return normalize_text(raw)


def decode_body(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type, re.I)
    encodings = ([m.group(1)] if m else []) + ["utf-8", "gbk"]
    for enc in encodings:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


@dataclass
class RuleContext:
    """一次解析的共享底座：会话、总预算，以及向管线借用的能力"""

    resolver: object
    session: HttpSession        # requests 语义的会话，由 utils.httpclient 工厂给出
    deadline: float
    source_url: str = ""      # 用户粘贴的那条候选地址（交付信息里回指用）
    quality: str = ""         # 用户点名的画质档位 id（空＝按规则自己的默认，通常最高）

    def open(self, url: str, ua: Optional[str] = None,
             referer: Optional[str] = None):
        """GET → (最终URL, Content-Type, Content-Length, 正文或None)"""
        return self.resolver._open(url, self.deadline,
                                   ua=ua or desktop_ua(),
                                   session=self.session, referer=referer)

    def publish(self, uri: str, mime: str, referer: str, via: str,
                extra_headers: Optional[dict] = None):
        """交付前检查电视能否裸拉，必要时改走本机代理

        ``extra_headers``：规则自带的关键请求头（如插件算出的签名头），
        仅在改走代理时合并进代理请求头。
        """
        return self.resolver._publish(uri, mime, self.session, referer,
                                      self.deadline, via,
                                      extra_headers=extra_headers)

    def follow(self, url: str, referer: str):
        return self.resolver._follow_stream(url, self.session, referer, self.deadline)

    def info(self, uri: str, mime: str, title: str, via: str,
             source_url: str = "", thumbnail: Optional[str] = None,
             qualities: Optional[list] = None,
             quality: str = "") -> dict:
        media_info = MediaInfo(
            media_type="url",
            uri=uri,
            title=(title or urlparse(uri).netloc)[:80],
            mime_type=mime,
            thumbnail=thumbnail,
            qualities=qualities or [],
            quality=quality or "",
        )
        return {**media_info.to_dict(),
                "source_url": source_url or self.source_url, "via": via}


class SiteRule:
    """站点规则基类

    子类给出 ``name`` / ``hosts`` / ``stage``，并实现 ``resolve``。
    ``resolve`` 解析不出来时抛异常即可 —— 调用方会记录并回退下一条规则，
    绝不会把「这条规则不适用」误报成「这个地址坏了」。
    """

    name = ""
    hosts: tuple = ()
    stage = "url"            # "url" | "page"
    priority = 100           # 数字小者先试；包规则统一落在 4-8

    def matches(self, url: str) -> bool:
        if WILDCARD_HOST in self.hosts:
            return True
        host = (urlparse(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def resolve(self, url: str, html: str, ctx: RuleContext) -> dict:
        raise NotImplementedError


# ── 注册表 ──

_RULES: list = []
_EXT_RULES: list = []
_EXT_FINGERPRINT: Optional[tuple] = None
_PLUGIN_RULES: list = []          # JS 脚本插件规则（整体替换式管理）

# 由 declarative.py 注入（避免 rules 包内部循环导入）
_ext_loader = None


def register(rule) -> None:
    """登记规则；同阶段按 priority 排序，priority 小者优先命中"""
    _RULES.append(rule)
    _RULES.sort(key=lambda r: (r.priority, r.name))


def registered() -> list:
    return list(_RULES)


def matching(url: str, stage: str) -> list:
    """该阶段能用的规则，按「站点专属在前、通用兜底在后」返回

    兜底规则（``hosts`` 含 ``*``）不认域名，靠排序而不是 priority 让位：
    装了精确规则的站永远先试精确的，只有全都拿不下才轮到通用规则。
    两组成员各自保持 ``(priority, name)`` 顺序。
    """
    hit = [r for r in _RULES if r.stage == stage and r.matches(url)]
    specific = [r for r in hit if WILDCARD_HOST not in r.hosts]
    fallback = [r for r in hit if WILDCARD_HOST in r.hosts]
    return specific + fallback


def set_plugin_rules(rules: list) -> None:
    """JS 插件规则整体替换（热载、启用/停用、删除都走这里）"""
    global _PLUGIN_RULES, _RULES
    for old in _PLUGIN_RULES:
        if old in _RULES:
            _RULES.remove(old)
    _PLUGIN_RULES = list(rules)
    for rule in _PLUGIN_RULES:
        register(rule)
    if _PLUGIN_RULES:
        logger.info(f"已加载解析插件规则 {len(_PLUGIN_RULES)} 条: " +
                    ", ".join(f"{r.plugin_name}/{r.name}" for r in _PLUGIN_RULES))


def set_external_loader(loader) -> None:
    """declarative.py 注册 ``(rules_dir) -> [SiteRule]`` 的加载函数"""
    global _ext_loader
    _ext_loader = loader


def refresh_external(rules_dir: str) -> list:
    """按需重载外置规则：目录内容没变就不碰磁盘

    他改完 ``share_rules/*.json`` 不用重启应用，下一次解析即生效。
    """
    global _EXT_RULES, _EXT_FINGERPRINT, _RULES
    if _ext_loader is None or not os.path.isdir(rules_dir):
        fingerprint = None
    else:
        try:
            fingerprint = tuple(sorted(
                (n, int(os.path.getmtime(os.path.join(rules_dir, n))))
                for n in os.listdir(rules_dir) if n.lower().endswith(".json")))
        except OSError as e:
            logger.warning(f"规则目录不可读 {rules_dir}: {e}")
            return []
    if fingerprint == _EXT_FINGERPRINT:
        return list(_EXT_RULES)
    new: list = []
    if fingerprint and _ext_loader is not None:
        new = _ext_loader(rules_dir) or []
    for old in _EXT_RULES:
        if old in _RULES:
            _RULES.remove(old)
    for rule in new:
        register(rule)
    _EXT_RULES = new
    _EXT_FINGERPRINT = fingerprint
    if new:
        logger.info(f"已加载外置解析规则 {len(new)} 条: " +
                    ", ".join(r.name for r in new))
    return list(new)


def template_apply(tpl: str, values: dict) -> str:
    """``{name}`` 插值；缺变量就抛，避免把半个模板串当成地址交出去"""
    def _sub(m):
        key = m.group(1)
        if key not in values:
            raise ValueError(f"规则模板缺少变量 {{{key}}}")
        return str(values[key])
    return re.sub(r"\{(\w+)}", _sub, tpl)


def absolutize(url: str, base: str) -> str:
    return urljoin(base, url)


def title_from_html(html: str) -> str:
    from html import unescape
    m = re.search(r"<title[^>]*>([^<]+)</title>", html or "", re.I)
    return unescape(m.group(1)).strip() if m else ""


def guess_mime(url: str) -> str:
    ext = _ext_of(url)
    return {
        "m3u8": "application/vnd.apple.mpegurl",
        "ts": "video/mp2t",
        "mp4": "video/mp4",
        "mkv": "video/x-matroska",
        "flv": "video/x-flv",
        "mp3": "audio/mpeg",
    }.get(ext, "video/mp4")


def _ext_of(url: str) -> str:
    name = os.path.basename(urlparse(url).path)
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


__all__ = [
    "DESKTOP_UA", "MOBILE_UA", "desktop_ua", "mobile_ua",
    "WILDCARD_HOST",
    "RuleContext", "SiteRule", "register",
    "registered", "matching", "refresh_external", "set_external_loader",
    "set_plugin_rules", "decode_body", "decode_js_string", "normalize_text",
    "template_apply", "absolutize", "title_from_html", "guess_mime",
]
