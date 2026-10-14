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
        ├── .hancast-builtin           ← 内置包标记（内容是随包的版本号）
        └── *.js

内置包住在包里（``hancast_sidecar/rule_plugins/``），随 bundle 出货，
启动时按版本播种进上面的目录——这样「内置规则」和「用户导入的规则」
是同一份清单、同一套开关，不再有代码里的黑箱规则。

插件规则经 ``base.set_plugin_rules`` 整体替换进注册表，
priority 缺省 5（与外置声明式规则同层，先于内置规则命中）。
"""

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
                   absolutize, title_from_html, guess_mime,
                   set_builtin_disabled)
from .jsruntime import JsRuntime

logger = logging.getLogger("hancast.media.jsplugin")

_MAX_ZIP_BYTES = 2 * 1024 * 1024
_MAX_ZIP_FILES = 128
_MAX_ZIP_UNCOMPRESSED = 8 * 1024 * 1024
_MAX_FILE_BYTES = 2 * 1024 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# zip 内只收「一层目录（可选）下的 manifest.json / *.js」，其余一律拒收
_ENTRY_RE = re.compile(r"^(?:[^/\\]+/)?(?:manifest\.json|[A-Za-z0-9._-]+\.js)$")

# 内置包标记文件名（由播种写入，zip 导入永远造不出它）
BUILTIN_MARKER = ".hancast-builtin"


def bundled_plugins_dir() -> str:
    """随包的内置规则包目录。

    锚点与 ``protocol/xml`` 同款（``__file__`` 相对），Nuitka 用
    ``--include-data-dir`` 把整个目录放到可执行文件旁边的同名路径下。
    """
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "rule_plugins"))


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
        # 非空 = 这个目录是随包内置的，内容是随包的版本号（不可删，只能停用）
        self.bundled_version = self._read_marker_version()
        self.runtime: JsRuntime | None = None
        self._runtime_lock = threading.Lock()

    def _marker_path(self) -> str:
        return os.path.join(self.dir, BUILTIN_MARKER)

    def _read_marker_version(self) -> str:
        try:
            with open(self._marker_path(), "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    @property
    def builtin(self) -> bool:
        return bool(self.bundled_version)

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
        obj.bundled_version = obj._read_marker_version()
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
                "builtin": self.builtin, "bundled_version": self.bundled_version,
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

    def __init__(self, plugins_root: str, bundled_dir: str | None = None):
        self.root = plugins_root
        self.bundled_dir = bundled_dir if bundled_dir is not None \
            else bundled_plugins_dir()
        self.state_path = os.path.join(plugins_root, "state.json")
        self._plugins: dict[str, JsPlugin] = {}
        self._fingerprint: tuple | None = None
        self._lock = threading.Lock()
        self._state = self._read_state()
        # 关掉内置规则的决定存在同一个 state.json 里，进程重启要跟着恢复
        set_builtin_disabled(self._state["disabled_builtin"])
        # 播种失败绝不能拖坏解析管线，内置包坏了自己会显示在插件页上
        try:
            self.seed_bundled()
        except Exception as e:
            logger.warning("内置解析包播种异常: %s", e)

    # ── 状态（停用清单 + prefs） ──

    def _read_state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict):
                state.setdefault("disabled", [])
                state.setdefault("prefs", {})
                state.setdefault("disabled_builtin", [])
                return state
        except (OSError, ValueError):
            pass
        return {"disabled": [], "prefs": {}, "disabled_builtin": []}

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

    # ── 内置 Python 规则的开关（和包规则共用这一份状态） ──

    def set_builtin_enabled(self, rule_name: str, enabled: bool) -> None:
        key = str(rule_name)
        disabled = self._state["disabled_builtin"]
        if enabled and key in disabled:
            disabled.remove(key)
        elif not enabled and key not in disabled:
            disabled.append(key)
        set_builtin_disabled(disabled)
        self.save_state()

    def builtin_enabled(self, rule_name: str) -> bool:
        return rule_name not in self._state["disabled_builtin"]

    # ── 内置包播种 ──

    def seed_bundled(self) -> list[dict]:
        """把随包的规则包装进插件目录：缺失即装，随包版本更高即升级

        用户自己导入的同名包不覆盖（升级不是他的义务）；停用清单和 prefs 存在
        根目录的 state.json 里，覆盖升级不会丢。
        """
        actions: list[dict] = []
        if not os.path.isdir(self.bundled_dir):
            return actions
        os.makedirs(self.root, exist_ok=True)
        for entry in sorted(os.listdir(self.bundled_dir)):
            src = os.path.join(self.bundled_dir, entry)
            if not os.path.isdir(src) or not os.path.isfile(
                    os.path.join(src, "manifest.json")):
                continue
            try:
                with open(os.path.join(src, "manifest.json"),
                          "r", encoding="utf-8-sig") as f:
                    manifest = json.load(f)
                name = str(manifest.get("name") or "")
                version = str(manifest.get("version") or "")
                if not _NAME_RE.match(name):
                    raise PluginError(f"内置包 manifest.name 不合法: {name!r}")
                target = os.path.join(self.root, name)
                marker = os.path.join(target, BUILTIN_MARKER)
                installed = ""
                if os.path.isfile(marker):
                    with open(marker, "r", encoding="utf-8") as f:
                        installed = f.read().strip()
                elif os.path.exists(target):
                    actions.append({"name": name, "action": "skipped",
                                    "reason": "已有同名用户包，不覆盖"})
                    continue
                if installed and _version_key(version) <= _version_key(installed):
                    continue
                self._copy_pack(src, target, version)
                actions.append({"name": name, "version": version,
                                "action": "upgraded" if installed else "installed"})
            except Exception as e:
                logger.warning("内置解析包播种失败 %s: %s", entry, e)
        if any(a["action"] != "skipped" for a in actions):
            self._fingerprint = None
            logger.info("内置解析包播种: " + "，".join(
                f"{a['name']} {a['action']} {a.get('version', '')}".rstrip()
                for a in actions if a["action"] != "skipped"))
        return actions

    def _copy_pack(self, src: str, target: str, version: str) -> None:
        names = [n for n in os.listdir(src)
                 if n == "manifest.json" or n.endswith(".js")]
        if os.path.isdir(target):
            shutil.rmtree(target)
        os.makedirs(target, exist_ok=True)
        for n in names:
            shutil.copy2(os.path.join(src, n), os.path.join(target, n))
        with open(os.path.join(target, BUILTIN_MARKER),
                  "w", encoding="utf-8") as f:
            f.write(version)

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
        """刚播种完、或进程刚起来时，读视图/取插件不必等第一次解析"""
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
            if os.path.isfile(os.path.join(target, BUILTIN_MARKER)):
                raise PluginError(
                    f"「{name}」是内置解析包，导入不会覆盖它；"
                    "想改这份规则请换一个 name，或先在规则页停用内置包")
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

    def import_url(self, url: str, timeout: float = 60.0) -> dict:
        if not url.lower().startswith(("http://", "https://")):
            raise PluginError("只支持 http(s) 下载地址")
        with requests.Session() as s:
            r = s.get(url, timeout=timeout, stream=True, headers={
                "User-Agent": "Mozilla/5.0 (HanCast plugin import)"})
            if r.status_code >= 400:
                raise PluginError(f"下载失败（HTTP {r.status_code}）")
            data = b""
            for chunk in r.iter_content(chunk_size=64 * 1024):
                data += chunk
                if len(data) > _MAX_ZIP_BYTES:
                    raise PluginError("插件包超过 2MB 上限")
        return self.import_zip_bytes(data)

    def remove(self, name: str) -> None:
        plugin = self.get(name)
        if plugin.builtin:
            raise PluginError(f"内置解析包不可删除，可在规则页停用: {name}")
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
