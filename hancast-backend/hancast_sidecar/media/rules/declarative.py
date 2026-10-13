"""外置声明式规则：``<配置目录>/share_rules/*.json``，加站点不用改代码

一条 JSON 描述「这个站的播放地址怎么取」，不执行任意代码：

.. code-block:: json

    {
      "name": "maccms-box",
      "hosts": ["dyrsxm.cc"],
      "path_regex": "^/play/",
      "stage": "page",
      "headers": {"Referer": "{url}"},
      "steps": [
        {"fetch": "{url}"},
        {"regex": "player_aaaa\\\\s*=\\\\s*(\\\\{.*?\\\\})", "group": 1, "as": "cfg"},
        {"json": "url", "from": "cfg", "as": "play"},
        {"set": "https://api.example.com/vod?id={id}", "as": "api"},
        {"fetch": "{api}"}
      ],
      "output": ["{play}", "{alt}"],
      "title_regex": "<title>([^<]+)</title>"
    }

``steps`` 顺序执行，结果写进变量池。内置变量：``url``（命中的页面地址）、
``host``、``scheme``、``path``、``base``（最后一次 fetch 的最终地址）、
``body``（最后一次 fetch 的正文）。``fetch`` / ``regex`` / ``json`` / ``set``
四种操作，值里的 ``{name}`` 用变量池插值。

正文在交给规则之前已归一化 ``\\/``、``\\u002F``、``\\u0026`` 与 HTML 实体，
和通用提取看到的是同一份文本，一条正则就能同时吃到 JS 转义与实体写法。

``output`` 可以是一条也可以是一个列表（逐个试，第一个能播的交付）；
省略时从最后一次 ``body`` 里自动找流地址（含 ``&token=`` 这类尾巴）。
``enabled: false`` 整条停用，不必删文件。

完整说明与更多例子见 ``docs/分享解析规则.md``。
"""

import os
import re
import json
import logging
from urllib.parse import urlparse

from .base import (SiteRule, RuleContext, set_external_loader, template_apply,
                   absolutize, title_from_html, guess_mime, normalize_text,
                   decode_body)

logger = logging.getLogger("hancast.media.rules")

_MAX_STEPS = 12
# 流地址后面常跟 &token=… 形式的参数（正文里已归一化，可能没有 ?）
_OUTPUT_SCAN_RE = re.compile(
    r"https?://[^\s'\"<>\\]+?\.(?:m3u8|mp4|mkv|flv|ts)(?:[?&][^\s'\"<>\\]*)?", re.I)


class DeclarativeRule(SiteRule):
    """由 JSON 描述文件实例化的规则"""

    stage = "page"
    priority = 5               # 先于内置规则命中，用户可覆盖

    def __init__(self, spec: dict, source_file: str = ""):
        self.name = str(spec.get("name") or os.path.basename(source_file))
        self.hosts = tuple(str(h).lower().lstrip(".") for h in spec.get("hosts", ()))
        self.stage = spec.get("stage") or "page"
        self.path_regex = spec.get("path_regex")
        self.headers = spec.get("headers") or {}
        self.steps = spec.get("steps") or []
        self.output = spec.get("output")
        self.title_regex = spec.get("title_regex")
        self.source_file = source_file
        self._path_re = re.compile(self.path_regex) if self.path_regex else None
        self._title_re = re.compile(self.title_regex, re.I) if self.title_regex else None

    def matches(self, url: str) -> bool:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if not self.hosts or not any(
                host == h or host.endswith("." + h) for h in self.hosts):
            return False
        return not self._path_re or bool(self._path_re.search(p.path or "/"))

    def resolve(self, url: str, html: str, ctx: RuleContext) -> dict:
        values = _initial_values(url, html)
        base = url
        body = values["body"]
        for step in self.steps[:_MAX_STEPS]:
            if not isinstance(step, dict):
                raise ValueError(f"步骤必须是对象: {step!r}")
            if "fetch" in step:
                target = absolutize(template_apply(str(step["fetch"]), values), base)
                referer = self._header_value("Referer", values) or base
                final, ctype, _clen, raw = ctx.open(
                    target, referer=referer)
                base = final
                body = _decode(ctype, raw)
                values["url_last"] = final
                values["base"] = final
                values["host"] = urlparse(final).netloc.lower()
                values["body"] = body
            elif "regex" in step:
                src = values.get(str(step.get("from", "body")), body)
                m = re.search(str(step["regex"]), src, _flags(step))
                if not m:
                    raise ValueError(f"正则未命中: {step['regex'][:60]}")
                group = int(step.get("group", 1))
                values[str(step.get("as") or f"m{group}")] = m.group(group)
            elif "json" in step:
                src = values.get(str(step.get("from", "body")), body)
                try:
                    node = json.loads(src)
                except (ValueError, TypeError) as e:
                    raise ValueError(f"正文不是合法 JSON: {e}") from e
                values[str(step.get("as") or "json")] = _json_path(
                    node, str(step["json"]))
            elif "set" in step:
                values[str(step.get("as"))] = template_apply(
                    str(step["set"]), values)
            else:
                raise ValueError(f"未知步骤（可用 fetch/regex/json/set）: "
                                 f"{list(step)[:3]}")

        outputs = self._outputs(values, body, base)
        if not outputs:
            raise ValueError("规则未取到播放地址（检查 output / 正则）")

        last_err = None
        for cand in outputs:
            try:
                uri, mime = ctx.follow(cand, base)
            except Exception as e:
                # 网络故障无法判定（电视端可能反而拉得通），按原样交付
                logger.info(f"{self.name} 规则候选验证失败，按原样交付 {cand}: {e}")
                uri, mime = cand, guess_mime(cand)
            uri, mime, via = ctx.publish(uri, mime, base, self.name)
            title = self._title(body)
            return ctx.info(uri, mime, title, via, source_url=ctx.source_url)

    def _outputs(self, values: dict, body: str, base: str) -> list:
        raw = self.output
        if raw is None:
            found = []
            for u in _OUTPUT_SCAN_RE.findall(body or ""):
                absu = absolutize(u, base)
                if absu not in found:
                    found.append(absu)
            return found
        items = raw if isinstance(raw, list) else [raw]
        out = []
        for item in items:
            cand = absolutize(template_apply(str(item), values), base)
            if cand.lower().startswith(("http://", "https://")) and cand not in out:
                out.append(cand)
        return out

    def _header_value(self, key: str, values: dict):
        for k, v in (self.headers or {}).items():
            if k.lower() == key.lower():
                return template_apply(str(v), values)
        return None

    def _title(self, body: str) -> str:
        if self._title_re:
            m = self._title_re.search(body or "")
            if m:
                return m.group(1).strip()
        return title_from_html(body or "")


def _initial_values(url: str, html: str) -> dict:
    p = urlparse(url)
    return {
        "url": url,
        "base": url,
        "host": (p.netloc or "").lower(),
        "scheme": (p.scheme or "https").lower(),
        "path": p.path or "/",
        # 与通用提取同一份视图：先解 \/ \u002F &amp;，规则作者不用为每种转义写两遍正则
        "body": normalize_text(html or ""),
    }


def _flags(step: dict) -> int:
    return re.I if step.get("ignorecase", True) else 0


def _decode(ctype: str, raw) -> str:
    if raw is None:
        return ""
    return normalize_text(decode_body(raw, ctype or "text/html"))


def _json_path(node, path: str):
    """``data.list.0.url`` 形式的取值；取不到就抛，让调用方换下一条规则"""
    cur = node
    for part in [p for p in (path or "").split(".") if p]:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                raise ValueError(f"JSON 路径 {path}: 下标 {part} 越界/非数字")
        if not isinstance(cur, dict) or part not in cur:
            raise ValueError(f"JSON 路径 {path}: 缺少 {part}")
        cur = cur[part]
    if isinstance(cur, (dict, list)):
        return json.dumps(cur, ensure_ascii=False)
    return str(cur)


def load_rules_dir(rules_dir: str) -> list:
    """读取目录下所有 ``*.json``；单条规则写坏只跳过它，不影响其余站点"""
    rules: list = []
    try:
        names = sorted(n for n in os.listdir(rules_dir)
                       if n.lower().endswith(".json"))
    except OSError as e:
        logger.warning(f"规则目录不可读 {rules_dir}: {e}")
        return rules
    for name in names:
        path = os.path.join(rules_dir, name)
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning(f"解析规则文件无效，已跳过 {name}: {e}")
            continue
        specs = data if isinstance(data, list) else [data]
        for spec in specs:
            if not isinstance(spec, dict):
                logger.warning(f"解析规则条目格式不对，已跳过 {name}")
                continue
            if spec.get("enabled") is False:
                logger.info(f"解析规则已禁用: {spec.get('name') or name}")
                continue
            try:
                rules.append(DeclarativeRule(spec, source_file=path))
            except Exception as e:
                logger.warning(f"解析规则构建失败，已跳过 {name}: {e}")
    return rules


set_external_loader(load_rules_dir)
