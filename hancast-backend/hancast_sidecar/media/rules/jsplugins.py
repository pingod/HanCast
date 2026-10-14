"""JS 脚本插件：``<share_rules>/plugins/<name>/``（manifest.json + *.js）

机制对标听书类播放器的脚本源（机制复刻，实现独立）：zip 包导入即用、
改动热生效、逐条规则可停用。规则通过 ``registerRule({...})`` 声明，
执行环境见 ``jsruntime.py``。

目录布局::

    <share_rules>/plugins/
    ├── state.json                     ← 停用清单 + 各插件 prefs（重装不丢）
    ├── _cache/<plugin>/…              ← host.readCache/writeCache 落点
    └── <plugin_name>/
        ├── manifest.json
        └── *.js

**这个目录就是解析规则的全部家当**：程序里没有编译进去的站点规则，也没有
「内置包」这种特殊身份——想让某个站被解析，就得往这里装包。包从哪来：设置页
导入 ZIP / 填 URL，或点「检查规则包更新」从官方索引拉（索引地址见后台参数
``rules.index_url``，raw.githubusercontent 不通就换成镜像）。

插件规则经 ``base.set_plugin_rules`` 整体替换进注册表，
priority 缺省 5（与外置声明式规则同层）。
"""

import hashlib
import io
import json
import logging
import os
import re
import shutil
import threading
import zipfile
from urllib.parse import urlparse

import requests

from .base import (SiteRule, RuleContext, set_plugin_rules, normalize_text,
                   absolutize, title_from_html, guess_mime)
from .jsruntime import JsRuntime

logger = logging.getLogger("hancast.media.jsplugin")

_MAX_ZIP_BYTES = 2 * 1024 * 1024
_MAX_ZIP_FILES = 128
_MAX_ZIP_UNCOMPRESSED = 8 * 1024 * 1024
_MAX_FILE_BYTES = 2 * 1024 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# zip 内只收「一层目录（可选）下的 manifest.json / *.js」，其余一律拒收
_ENTRY_RE = re.compile(r"^(?:[^/\\]+/)?(?:manifest\.json|[A-Za-z0-9._-]+\.js)$")

_IMPORT_UA = "Mozilla/5.0 (HanCast plugin import)"


def index_url() -> str:
    """当前生效的规则包索引地址（唯一来源：后台参数 rules.index_url 的登记表）"""
    try:
        from ...params import get
        return str(get("rules.index_url") or "").strip()
    except Exception:
        return ""


def _version_key(version: str) -> tuple:
    """``1.2.3`` → 可比较的元组；非数字段按 0 处理，缺段不报错"""
    parts = re.split(r"[._-]+", str(version or "").strip())
    nums = tuple(int(p) if p.isdigit() else 0 for p in parts)
    return nums or (0,)


class PluginError(ValueError):
    """插件安装/校验失败（给用户看的消息）"""


class JsRule(SiteRule):
    """把 JS 插件里的一条规则适配进 SiteRule 注册表"""

    def __init__(self, plugin: "JsPlugin", index: int, meta: dict):
        self.plugin_name = plugin.name
        self.name = str(meta.get("name") or f"rule{index}")
        self.rule_id = str(meta.get("id") or self.name)
        hosts = meta.get("hosts") or []
        self.hosts = tuple(str(h).lower().lstrip(".") for h in hosts)
        stage = str(meta.get("stage") or "page").lower()
        self.stage = stage if stage in ("url", "page") else "page"
        try:
            self.priority = int(_clamp(int(meta.get("priority", 5)), 4, 8))
        except (TypeError, ValueError):
            self.priority = 5
        self._plugin = plugin
        self._index = index

    def resolve(self, url: str, html: str, ctx: RuleContext) -> dict:
        ret = self._plugin.call_resolve(self._index, url, html, ctx)
        if not ret or not ret.get("url"):
            raise ValueError("插件未返回播放地址")
        cand = absolutize(str(ret["url"]).strip(), url)
        if not cand.lower().startswith(("http://", "https://")):
            raise ValueError(f"插件返回的地址不是 http(s): {cand[:80]}")
        referer = str(ret.get("base") or url)
        extra_headers = ret.get("headers") if isinstance(ret.get("headers"), dict) else None
        try:
            uri, mime = ctx.follow(cand, referer)
        except Exception as e:
            # 网络故障无法判定（电视端可能反而拉得通），按原样交付
            logger.info(f"{self._via()} 候选验证失败，按原样交付 {cand[:80]}: {e}")
            uri, mime = cand, guess_mime(cand)
        uri, mime, via = ctx.publish(uri, mime, referer, self._via(),
                                     extra_headers=extra_headers)
        title = str(ret.get("title") or "").strip() or \
            title_from_html(html or "") or urlparse(cand).netloc
        thumb = str(ret.get("thumbnail") or "").strip()
        return ctx.info(uri, mime, title[:80], via, source_url=ctx.source_url,
                        thumbnail=absolutize(thumb, referer) if thumb else None)

    def _via(self) -> str:
        return f"{self.plugin_name}/{self.name}"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class JsPlugin:
    """一个已安装的插件包：磁盘目录 + QuickJS 运行时（按需构建）"""

    def __init__(self, manager: "PluginManager", plugin_dir: str):
        self.manager = manager
        self.dir = plugin_dir
        self.name = os.path.basename(plugin_dir)
        self.manifest: dict = {}
        self.error: str = ""
        with open(os.path.join(plugin_dir, "manifest.json"),
                  "r", encoding="utf-8-sig") as f:
            self.manifest = json.load(f)
        if not isinstance(self.manifest, dict):
            raise PluginError("manifest.json 顶层必须是对象")
        self.version = str(self.manifest.get("version") or "")
        self.author = str(self.manifest.get("author") or "")
        self.description = str(self.manifest.get("description") or "")
        # 官方索引里没有这个包时，作者可以自己声明更新地址（同一个 zip 语义）
        self.update_url = str(self.manifest.get("update_url") or "").strip()
        self.runtime: JsRuntime | None = None
        self._runtime_lock = threading.Lock()

    @classmethod
    def _placeholder(cls, manager: "PluginManager", plugin_dir: str,
                     name: str, error: str) -> "JsPlugin":
        """manifest 都读不出来的坏插件：只留名字和错误，供插件页展示"""
        obj = cls.__new__(cls)
        obj.manager = manager
        obj.dir = plugin_dir
        obj.name = name
        obj.manifest = {}
        obj.version = obj.author = obj.description = ""
        obj.update_url = ""
        obj.error = error
        obj.runtime = None
        obj._runtime_lock = threading.Lock()
        return obj

    # ── 运行时（懒构建：解析首次命中才付 QuickJS 成本） ──

    def _ensure_runtime(self) -> JsRuntime:
        with self._runtime_lock:
            if self.runtime is None:
                scripts = self.manifest.get("scripts") or []
                if not scripts and self.manifest.get("main"):
                    scripts = [self.manifest["main"]]
                if not scripts:
                    scripts = sorted(n for n in os.listdir(self.dir)
                                     if n.lower().endswith(".js"))
                texts = []
                for s in scripts:
                    rel = str(s)
                    if rel != os.path.basename(rel) or rel.startswith("."):
                        raise PluginError(f"manifest scripts 条目不合法: {rel}")
                    with open(os.path.join(self.dir, rel),
                              "r", encoding="utf-8-sig") as f:
                        texts.append(f.read())
                self.runtime = JsRuntime(
                    self.name, texts,
                    prefs=self.manager.prefs_for(self.name),
                    on_prefs_change=self.manager.save_state,
                    cache_dir=self.manager.cache_dir_for(self.name))
                logger.info("解析插件已加载 %s %s（%d 条规则）",
                            self.name, self.version, self.runtime.rule_count)
            return self.runtime

    def rules_meta(self) -> list[tuple[int, dict]]:
        runtime = self._ensure_runtime()
        return [(i, runtime.rule_meta(i)) for i in range(runtime.rule_count)]

    def call_resolve(self, index: int, url: str, html: str,
                     ctx: RuleContext) -> dict | None:
        runtime = self._ensure_runtime()
        p = urlparse(url)
        payload = {
            "url": url,
            "base": url,
            "host": (p.netloc or "").lower(),
            "scheme": (p.scheme or "https").lower(),
            "path": p.path or "/",
            # 与声明式规则同一份视图：先归一化 \/ \u002F &amp;
            "body": normalize_text(html or "") if ctx and html else "",
        }
        return runtime.call_rule(index, payload, ctx.deadline, ctx.session)

    def build_rules(self) -> list[JsRule]:
        out = []
        for index, meta in self.rules_meta():
            try:
                out.append(JsRule(self, index, meta))
            except Exception as e:
                logger.warning("插件 %s 规则 %d 构建失败: %s", self.name, index, e)
        return out

    def info(self) -> dict:
        info = {"name": self.name, "version": self.version,
                "author": self.author, "description": self.description,
                "update_url": self.update_url,
                "error": self.error, "rules": []}
        try:
            for _index, meta in self.rules_meta():
                rule_id = str(meta.get("id") or meta.get("name"))
                info["rules"].append({
                    "id": rule_id,
                    "name": str(meta.get("name") or rule_id),
                    "hosts": list(meta.get("hosts") or []),
                    "stage": str(meta.get("stage") or "page"),
                    "enabled": self.manager.rule_enabled(self.name, rule_id),
                })
        except Exception as e:
            info["error"] = str(e)
        return info


class PluginManager:
    """插件目录的加载、导入、启停、删除；指纹变化即热重载"""

    def __init__(self, plugins_root: str):
        self.root = plugins_root
        self.state_path = os.path.join(plugins_root, "state.json")
        self._plugins: dict[str, JsPlugin] = {}
        self._fingerprint: tuple | None = None
        self._lock = threading.Lock()
        self._state = self._read_state()

    # ── 状态（停用清单 + prefs） ──

    def _read_state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict):
                state.setdefault("disabled", [])
                state.setdefault("prefs", {})
                state.pop("disabled_builtin", None)   # 内置规则这个概念已删
                return state
        except (OSError, ValueError):
            pass
        return {"disabled": [], "prefs": {}}

    def save_state(self) -> None:
        with self._lock:
            os.makedirs(self.root, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)

    def rule_enabled(self, plugin: str, rule_id: str) -> bool:
        return f"{plugin}/{rule_id}" not in self._state["disabled"]

    def prefs_for(self, plugin: str) -> dict:
        return self._state["prefs"].setdefault(plugin, {})

    def cache_dir_for(self, plugin: str) -> str:
        return os.path.join(self.root, "_cache", plugin)

    def set_rule_enabled(self, plugin: str, rule_id: str, enabled: bool) -> None:
        key = f"{plugin}/{rule_id}"
        disabled = self._state["disabled"]
        if enabled and key in disabled:
            disabled.remove(key)
        elif not enabled and key not in disabled:
            disabled.append(key)
        self.save_state()
        # 目录指纹没变，但注册表里的启用集合要跟着重建
        self._fingerprint = None
        self.refresh()

    # ── 官方索引：检查更新 / 一键安装 ──

    def fetch_index(self, url: str = "", timeout: float = 15.0) -> dict:
        """拉规则包索引 ``{"packs": [{name, version, url, sha256, …}]}``

        name 不合法或地址不是 https 的条目直接丢掉：一个坏条目不该拖垮整页
        检查更新，而索引是「下载来就要执行里面 JS」的入口，不给明文地址留门。
        """
        target = (url or index_url()).strip()
        if not target.lower().startswith(("http://", "https://")):
            raise PluginError(f"索引地址必须是 http(s)（当前：{target or '未设置'}）")
        try:
            with requests.Session() as s:
                r = s.get(target, timeout=timeout,
                          headers={"User-Agent": _IMPORT_UA})
                if r.status_code >= 400:
                    raise PluginError(f"索引拉取失败（HTTP {r.status_code}）")
                doc = r.json()
        except requests.RequestException as e:
            raise PluginError(f"索引拉取失败: {type(e).__name__}: {e}")
        except ValueError as e:
            raise PluginError(f"索引不是合法 JSON: {e}")
        packs: dict[str, dict] = {}
        entries = doc.get("packs") if isinstance(doc, dict) else None
        for item in entries or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            link = str(item.get("url") or "")
            if not _NAME_RE.match(name) or not link.lower().startswith("https://"):
                continue
            packs[name] = {"name": name, "version": str(item.get("version") or ""),
                           "url": link,
                           "description": str(item.get("description") or ""),
                           "sha256": str(item.get("sha256") or "").lower()}
        return {"url": target, "packs": packs}

    def check_updates(self, url: str = "") -> dict:
        """索引对上本地：分成「可更新」与「官方源里有、本机没装」两堆"""
        index = self.fetch_index(url)
        self._ensure_loaded()
        updates: list[dict] = []
        available: list[dict] = []
        for name, meta in sorted(index["packs"].items()):
            plugin = self._plugins.get(name)
            entry = {"name": name, "version": meta["version"],
                     "url": meta["url"], "description": meta["description"]}
            if plugin is None:
                available.append(entry)
            elif _version_key(meta["version"]) > _version_key(plugin.version):
                updates.append({**entry, "current": plugin.version})
        return {"index_url": index["url"], "updates": updates,
                "available": available, "installed": len(self._plugins)}

    def pull(self, name: str, url: str = "") -> dict:
        """按索引装/更新一个包（索引没有它时，退回包自己声明的 update_url）"""
        self._ensure_loaded()
        plugin = self._plugins.get(name)
        link = ""
        digest = ""
        try:
            meta = self.fetch_index(url)["packs"].get(name) or {}
            link, digest = str(meta.get("url") or ""), str(meta.get("sha256") or "")
        except PluginError as e:
            if plugin is None or not plugin.update_url:
                raise PluginError(f"官方索引不可用，「{name}」也没声明更新地址：{e}")
        link = link or (plugin.update_url if plugin else "")
        if not link:
            raise PluginError(f"「{name}」既不在官方索引里，也没声明 update_url")
        return self.import_url(link, expected_sha256=digest)

    # ── 指纹热载 ──

    def _fingerprint_now(self) -> tuple | None:
        if not os.path.isdir(self.root):
            return None
        items = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in ("_cache", "__pycache__")]
            for n in filenames:
                if n == "state.json" or n.endswith(".tmp"):
                    continue
                p = os.path.join(dirpath, n)
                try:
                    items.append((os.path.relpath(p, self.root),
                                  int(os.path.getmtime(p)),
                                  os.path.getsize(p)))
                except OSError:
                    continue
        return tuple(sorted(items))

    def refresh(self) -> list[JsRule]:
        """目录指纹没变就直接返回现有规则；变了重建全部插件（数量小，成本可忽略）"""
        fingerprint = self._fingerprint_now()
        if fingerprint == self._fingerprint:
            return self._enabled_rules()
        with self._lock:
            self._plugins = {}
            if os.path.isdir(self.root):
                for name in sorted(os.listdir(self.root)):
                    plugin_dir = os.path.join(self.root, name)
                    if not os.path.isdir(plugin_dir) or name.startswith(("_", ".")):
                        continue
                    if not os.path.isfile(os.path.join(plugin_dir, "manifest.json")):
                        continue
                    try:
                        self._plugins[name] = JsPlugin(self, plugin_dir)
                    except Exception as e:
                        placeholder = JsPlugin._placeholder(self, plugin_dir, name, str(e))
                        self._plugins[name] = placeholder
                        logger.warning("解析插件加载失败 %s: %s", name, e)
            self._fingerprint = fingerprint
        return self._enabled_rules()

    def _enabled_rules(self) -> list[JsRule]:
        rules: list[JsRule] = []
        for plugin in self._plugins.values():
            try:
                for rule in plugin.build_rules():
                    if self.rule_enabled(plugin.name, rule.rule_id):
                        rules.append(rule)
            except Exception as e:
                plugin.error = plugin.error or str(e)
                logger.warning("解析插件 %s 规则枚举失败: %s", plugin.name, e)
        set_plugin_rules(rules)
        return rules

    def get(self, name: str) -> JsPlugin:
        self._ensure_loaded()
        plugin = self._plugins.get(name)
        if plugin is None:
            raise PluginError(f"插件不存在: {name}")
        return plugin

    def _ensure_loaded(self) -> None:
        """进程刚起来时，读视图/取插件不必等第一次解析"""
        if not self._plugins:
            self.refresh()

    # ── 导入 ──

    def import_zip_bytes(self, data: bytes) -> dict:
        if len(data) > _MAX_ZIP_BYTES:
            raise PluginError("插件包超过 2MB 上限")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise PluginError(f"不是合法的 zip 包: {e}")
        with zf:
            entries = [i for i in zf.infolist() if not i.is_dir()]
            if len(entries) > _MAX_ZIP_FILES:
                raise PluginError("插件包文件数超过 128 上限")
            if sum(i.file_size for i in entries) > _MAX_ZIP_UNCOMPRESSED:
                raise PluginError("插件包解压后超过 8MB 上限")
            for info in entries:
                n = info.filename
                if n.startswith("/") or "\\" in n or ".." in n.split("/"):
                    raise PluginError(f"zip 内路径不合法: {n}")
                if not _ENTRY_RE.match(n):
                    raise PluginError(f"zip 内不允许的文件（只收 manifest.json 和 .js）: {n}")
                if info.file_size > _MAX_FILE_BYTES:
                    raise PluginError(f"单文件超过 2MB: {n}")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise PluginError(f"zip 内不允许软链接: {n}")
            manifests = [i for i in entries
                         if i.filename.rsplit("/", 1)[-1] == "manifest.json"]
            if len(manifests) != 1:
                raise PluginError("插件包必须恰好含一个 manifest.json")
            try:
                manifest = json.loads(zf.read(manifests[0]).decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError) as e:
                raise PluginError(f"manifest.json 不是合法 JSON: {e}")
            name = str(manifest.get("name") or "")
            if not _NAME_RE.match(name):
                raise PluginError(
                    "manifest.name 只允许字母/数字/下划线/连字符（1-64 字符）")
            scripts = manifest.get("scripts") or \
                ([manifest["main"]] if manifest.get("main") else [])
            for s in scripts:
                if not isinstance(s, str) or not s.endswith(".js"):
                    raise PluginError(f"manifest scripts 条目必须是 .js 文件名: {s!r}")
            present = {i.filename.rsplit("/", 1)[-1] for i in entries}
            for s in scripts:
                if s not in present:
                    raise PluginError(f"manifest scripts 引用了不存在的文件: {s}")

            target = os.path.join(self.root, name)
            os.makedirs(self.root, exist_ok=True)
            if os.path.exists(target):
                shutil.rmtree(target)      # 同名覆盖（ssts 同款语义）
            os.makedirs(target, exist_ok=True)
            for info in entries:
                rel = info.filename.rsplit("/", 1)[-1]
                with open(os.path.join(target, rel), "wb") as f:
                    f.write(zf.read(info))

        self._fingerprint = None
        self.refresh()
        return self.get(name).info()

    def import_url(self, url: str, timeout: float = 60.0,
                   expected_sha256: str = "") -> dict:
        if not url.lower().startswith(("http://", "https://")):
            raise PluginError("只支持 http(s) 下载地址")
        with requests.Session() as s:
            r = s.get(url, timeout=timeout, stream=True,
                      headers={"User-Agent": _IMPORT_UA})
            if r.status_code >= 400:
                raise PluginError(f"下载失败（HTTP {r.status_code}）")
            data = b""
            for chunk in r.iter_content(chunk_size=64 * 1024):
                data += chunk
                if len(data) > _MAX_ZIP_BYTES:
                    raise PluginError("插件包超过 2MB 上限")
        # 索引给了摘要就必须对上：这是「下载来就执行」的入口，不能只信传输层
        if expected_sha256:
            got = hashlib.sha256(data).hexdigest()
            if got != expected_sha256.lower():
                raise PluginError(
                    f"包校验失败（sha256 不匹配，可能被劫持或索引过期）: {got[:12]}…")
        return self.import_zip_bytes(data)

    def remove(self, name: str) -> None:
        self.get(name)
        shutil.rmtree(os.path.join(self.root, name), ignore_errors=True)
        self._state["prefs"].pop(name, None)
        self._state["disabled"] = [d for d in self._state["disabled"]
                                   if not d.startswith(f"{name}/")]
        self.save_state()
        self._fingerprint = None
        self.refresh()

    # ── 前端视图 ──

    def list_info(self) -> list[dict]:
        self._ensure_loaded()
        out = []
        for name in sorted(self._plugins):
            plugin = self._plugins[name]
            try:
                out.append(plugin.info())
            except Exception as e:
                out.append({"name": name, "version": "", "author": "",
                            "description": "", "error": str(e), "rules": []})
        return out
