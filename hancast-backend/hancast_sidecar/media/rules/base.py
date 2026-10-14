"""站点解析规则底座：一个站点一个模块，注册表按阶段命中

规则分两个阶段（stage）：
  ``url``   拿到链接就能解析（B 站这类有官方接口/签名算法的站点）
  ``page``  必须先抓到页面正文（抖音分享页、采集站这类把播放地址藏在正文里的）

``ShareResolver._resolve_url`` 的执行顺序：
  url 阶段规则 → 抓页 → （展开短链后再跑一次）url 阶段规则 →
  直链/播放列表判定 → page 阶段规则（逐条，失败则回退下一条）→ 通用正文提取兜底。

外置声明式规则（``rules/declarative.py``）优先级高于内置规则，
用户加站点不用改代码、不用重新打包。
"""

import os
import re
import json
import logging
from dataclasses import dataclass
from html import unescape as html_unescape
from typing import Optional
from urllib.parse import urlparse, urljoin

import requests

from ...models.media import MediaInfo

logger = logging.getLogger("hancast.media.rules")

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

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
    session: requests.Session
    deadline: float
    source_url: str = ""      # 用户粘贴的那条候选地址（交付信息里回指用）

    def open(self, url: str, ua: str = DESKTOP_UA,
             referer: Optional[str] = None):
        """GET → (最终URL, Content-Type, Content-Length, 正文或None)"""
        return self.resolver._open(url, self.deadline, ua=ua,
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
             source_url: str = "", thumbnail: Optional[str] = None) -> dict:
        media_info = MediaInfo(
            media_type="url",
            uri=uri,
            title=(title or urlparse(uri).netloc)[:80],
            mime_type=mime,
            thumbnail=thumbnail,
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
    priority = 100           # 数字小者先试；外置规则统一 5（可覆盖内置）

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def resolve(self, url: str, html: str, ctx: RuleContext) -> dict:
        raise NotImplementedError


# ── 注册表 ──

_RULES: list = []
_EXT_RULES: list = []
_EXT_FINGERPRINT: Optional[tuple] = None
_PLUGIN_RULES: list = []          # JS 脚本插件规则（整体替换式管理）
# 被用户关掉的内置 Python 规则名：内置规则与 JS 包是同一件事的两份实现，
# 包跑通之后要能在同一处把兜底的那份关掉，不然「统一管理」只是看着统一。
_DISABLED_BUILTIN: set = set()

# 由 declarative.py 注入（避免 rules 包内部循环导入）
_ext_loader = None


def register(rule) -> None:
    """登记规则；同阶段按 priority 排序，priority 小者优先命中"""
    _RULES.append(rule)
    _RULES.sort(key=lambda r: (r.priority, r.name))


def registered() -> list:
    return list(_RULES)


def matching(url: str, stage: str) -> list:
    injected = {id(r) for r in _PLUGIN_RULES} | {id(r) for r in _EXT_RULES}
    return [r for r in _RULES
            if r.stage == stage
            and r.matches(url)
            # 停用集合只管内置 Python 规则：用户包里同名的规则不能被它误杀
            and (id(r) in injected or r.name not in _DISABLED_BUILTIN)]


def set_builtin_disabled(names) -> None:
    """整体替换「被关掉的内置规则」集合（状态由插件管理器的 state.json 持久化）"""
    global _DISABLED_BUILTIN
    _DISABLED_BUILTIN = {str(n) for n in (names or ())}


def builtin_disabled() -> list:
    return sorted(_DISABLED_BUILTIN)


def builtin_rules_info() -> list:
    """内置 Python 规则清单（设置页「规则总览」用）

    JS 包与外置 JSON 规则是运行期注入注册表的，这里按对象身份排除，
    剩下的就是随包编译进去的那几条（B 站、抖音）。
    """
    injected = {id(r) for r in _PLUGIN_RULES} | {id(r) for r in _EXT_RULES}
    return [{"name": r.name,
             "hosts": list(r.hosts),
             "stage": r.stage,
             "priority": r.priority,
             "enabled": r.name not in _DISABLED_BUILTIN}
            for r in _RULES if id(r) not in injected]


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
    "DESKTOP_UA", "MOBILE_UA", "RuleContext", "SiteRule", "register",
    "registered", "matching", "refresh_external", "set_external_loader",
    "set_plugin_rules", "decode_body", "decode_js_string", "normalize_text",
    "template_apply", "absolutize", "title_from_html", "guess_mime",
]
