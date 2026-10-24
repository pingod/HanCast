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
import tempfile
import threading
import zipfile
from contextlib import nullcontext
from urllib.parse import urlparse

import requests

from .base import (SiteRule, RuleContext, set_plugin_rules, normalize_text,
                   absolutize, title_from_html, guess_mime)
from .jsruntime import JsRuntime
from .tasks import TaskCancelled, TaskContext, TaskDeadline
from ...utils import ghmirror

logger = logging.getLogger("hancast.media.jsplugin")

_MAX_ZIP_BYTES = 2 * 1024 * 1024
_MAX_ZIP_FILES = 128
_MAX_ZIP_UNCOMPRESSED = 8 * 1024 * 1024
_MAX_FILE_BYTES = 2 * 1024 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# zip 内只收「一层目录（可选）下的 manifest.json / *.js」，其余一律拒收
_ENTRY_RE = re.compile(r"^(?:[^/\\]+/)?(?:manifest\.json|[A-Za-z0-9._-]+\.js)$")

_IMPORT_UA = "Mozilla/5.0 (HanCast plugin import)"


def _checkpoint(task: TaskContext | None) -> None:
    if task is not None:
        task.checkpoint()


def _download(url: str, timeout: float, task: TaskContext | None,
              limit: int | None = None) -> bytes:
    """流式读取；每块检查总预算。socket timeout 不能打断 DNS 等系统等待。"""
    _checkpoint(task)
    with requests.Session() as session:
        response = session.get(
            url, timeout=task.timeout(timeout) if task else timeout,
            stream=True, headers={"User-Agent": _IMPORT_UA})
        try:
            _checkpoint(task)
            if response.status_code >= 400:
                raise PluginError(f"HTTP {response.status_code}")
            data = bytearray()
            chunks = iter(response.iter_content(chunk_size=16 * 1024))
            while True:
                _checkpoint(task)
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                _checkpoint(task)
                data.extend(chunk)
                if limit is not None and len(data) > limit:
                    raise PluginError("插件包超过 2MB 上限")
            _checkpoint(task)
            return bytes(data)
        finally:
            response.close()


def index_url() -> str:
    """当前生效的规则包索引地址（唯一来源：后台参数 rules.index_url 的登记表）"""
    try:
        from ...params import get
        return str(get("rules.index_url") or "").strip()
    except Exception:
        return ""


def index_timeout() -> float:
    """拉索引的单次等待上限（唯一来源：后台参数 rules.index_timeout_s）"""
    try:
        from ...params import get
        return float(get("rules.index_timeout_s"))
    except Exception:
        return 40.0


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
                        thumbnail=absolutize(thumb, referer) if thumb else None,
                        qualities=_clean_qualities(ret),
                        quality=str(ret.get("quality") or "").strip())

    def _via(self) -> str:
        return f"{self.plugin_name}/{self.name}"


def _clean_qualities(ret: dict) -> list[dict]:
    """把包返回的档位表收成 [{id, label}]；形状不对就当没有

    档位是「重解析的钥匙」而不是地址：切换画质时按 id 再解一次，
    所以这里只保留能重新喂回 resolve 的标识，不落任何会过期的直链。
    """
    raw = ret.get("qualities")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[:12]:
        if isinstance(item, str):
            item = {"id": item, "label": item}
        if not isinstance(item, dict):
            continue
        qid = str(item.get("id") or "").strip()[:24]
        if not qid:
            continue
        label = str(item.get("label") or qid).strip()[:24]
        out.append({"id": qid, "label": label})
    return out


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class JsPlugin:
    """一个已安装的插件包：磁盘目录 + QuickJS 运行时（按需构建）"""

    def __init__(self, manager: "PluginManager", plugin_dir: str,
                 name: str | None = None):
        self.manager = manager
        self.dir = plugin_dir
        self.name = name or os.path.basename(plugin_dir)
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
        self._meta: list[tuple[int, dict]] | None = None
        # 在 manager 的目录写锁内固定源文本。旧 parser 即使延迟构建 runtime，
        # 也不会再读已经换代/删除的包目录。
        scripts = self.manifest.get("scripts") or []
        if not scripts and self.manifest.get("main"):
            scripts = [self.manifest["main"]]
        if not scripts:
            scripts = sorted(n for n in os.listdir(self.dir)
                             if n.lower().endswith(".js"))
        texts = []
        for script in scripts:
            rel = str(script)
            if rel != os.path.basename(rel) or rel.startswith("."):
                raise PluginError(f"manifest scripts 条目不合法: {rel}")
            with open(os.path.join(self.dir, rel), "r", encoding="utf-8-sig") as f:
                texts.append(f.read())
        self._texts = tuple(texts)

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
        obj._texts = ()
        obj._meta = []
        return obj

    # ── 运行时（源文本与元数据均属于这一代 snapshot） ──

    def _ensure_runtime(self) -> JsRuntime:
        with self._runtime_lock:
            if self.runtime is None:
                if self.error:
                    raise PluginError(self.error)
                self.runtime = JsRuntime(
                    self.name, list(self._texts),
                    prefs=self.manager.prefs_for(self.name),
                    on_prefs_change=self.manager.save_state,
                    cache_dir=self.manager.cache_dir_for(self.name))
                logger.info("解析插件已加载 %s %s（%d 条规则）",
                            self.name, self.version, self.runtime.rule_count)
            return self.runtime

    def rules_meta(self) -> list[tuple[int, dict]]:
        if self._meta is None:
            try:
                runtime = self._ensure_runtime()
                self._meta = [(i, runtime.rule_meta(i))
                              for i in range(runtime.rule_count)]
            except Exception as exc:
                self.error = str(exc)
                self._meta = []
                raise
        return list(self._meta)

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
            # 用户点名的画质档位（空＝规则自己定，通常是最高）；包按它取流
            "quality": (ctx.quality if ctx else "") or "",
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
        self._lock = threading.RLock()  # 短状态锁；绝不跨网络/QuickJS 等待
        self._write_lock = threading.RLock()  # 目录与 snapshot 构建的单写者
        self._rules: list[JsRule] = []
        self._loaded = False
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
        with self._lock:
            return f"{plugin}/{rule_id}" not in self._state["disabled"]

    def prefs_for(self, plugin: str) -> dict:
        with self._lock:
            return self._state["prefs"].setdefault(plugin, {})

    def cache_dir_for(self, plugin: str) -> str:
        return os.path.join(self.root, "_cache", plugin)

    def set_rule_enabled(self, plugin: str, rule_id: str, enabled: bool) -> None:
        with self._write_lock:
            key = f"{plugin}/{rule_id}"
            with self._lock:
                disabled = self._state["disabled"]
                if enabled and key in disabled:
                    disabled.remove(key)
                elif not enabled and key not in disabled:
                    disabled.append(key)
                self.save_state()
            self._fingerprint = None
            self.refresh()

    # ── 官方索引：检查更新 / 一键安装 ──

    def fetch_index(self, url: str = "", timeout: float | None = None,
                    task: TaskContext | None = None) -> dict:
        """拉规则包索引 ``{"packs": [{name, version, url, sha256, …}]}``

        name 不合法或地址不是 https 的条目直接丢掉：一个坏条目不该拖垮整页
        检查更新，而索引是「下载来就要执行里面 JS」的入口，不给明文地址留门。

        不显式传 timeout 时按后台参数 ``rules.index_timeout_s`` 走：写死 15s 的话，
        墙内冷启动点「检查规则包更新」必然失败，而失败看起来像「没有更新」。

        地址按 ``utils/ghmirror.candidates`` 给的顺序试（开了镜像就是「先镜像、
        后直连」）；两条路的失败原因都拼进异常，用户才分得清「镜像挂了」
        和「根本没网」。``task`` 带上时，镜像+直连共用同一份总预算，
        取消/超时在每次回退前都能退出。
        """
        target = (url or index_url()).strip()
        if timeout is None:
            timeout = index_timeout()
        if not target.lower().startswith(("http://", "https://")):
            raise PluginError(f"索引地址必须是 http(s)（当前：{target or '未设置'}）")
        doc = None
        errors: list[str] = []
        for via, fetch_url in ghmirror.candidates(target):
            try:
                raw = _download(fetch_url, timeout, task)
                doc = json.loads(raw.decode("utf-8", "replace"))
                if via != "直连":
                    logger.info(f"规则包索引经{via}取得: {fetch_url[:90]}")
                break
            except (PluginError, requests.RequestException, ValueError) as e:
                # 取消/超时不是「这条路走不通」，不能退回下一条路继续烧预算
                if isinstance(e, (TaskCancelled, TaskDeadline)):
                    raise
                detail = str(e) if isinstance(e, PluginError) else f"{type(e).__name__}: {e}"
                errors.append(f"{via} {detail}")
                if task is not None:
                    task.checkpoint()
                if via != "直连":
                    logger.info(f"索引走镜像失败，回退直连（{fetch_url[:80]}）: {e}")
        if doc is None:
            raise PluginError("索引拉取失败: " + "；".join(errors))
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

    def check_updates(self, url: str = "",
                      task: TaskContext | None = None) -> dict:
        """索引对上本地：分成「可更新」与「官方源里有、本机没装」两堆"""
        index = self.fetch_index(url, task=task)
        self._ensure_loaded()
        updates: list[dict] = []
        available: list[dict] = []
        with self._lock:
            installed = dict(self._plugins)
        for name, meta in sorted(index["packs"].items()):
            plugin = installed.get(name)
            entry = {"name": name, "version": meta["version"],
                     "url": meta["url"], "description": meta["description"]}
            if plugin is None:
                available.append(entry)
            elif _version_key(meta["version"]) > _version_key(plugin.version):
                updates.append({**entry, "current": plugin.version})
        return {"index_url": index["url"], "updates": updates,
                "available": available, "installed": len(installed)}

    def pull(self, name: str, url: str = "",
             task: TaskContext | None = None) -> dict:
        """按索引装/更新一个包（索引没有它时，退回包自己声明的 update_url）"""
        self._ensure_loaded()
        with self._lock:
            plugin = self._plugins.get(name)
        link = ""
        digest = ""
        try:
            meta = self.fetch_index(url, task=task)["packs"].get(name) or {}
            link, digest = str(meta.get("url") or ""), str(meta.get("sha256") or "")
        except (TaskCancelled, TaskDeadline):
            # 取消/超时不是「这条索引不通」，不能接着退回 update_url 继续烧预算
            raise
        except PluginError as e:
            if plugin is None or not plugin.update_url:
                raise PluginError(f"官方索引不可用，「{name}」也没声明更新地址：{e}")
            # 索引不通、但包自己声明了 update_url：这是作者自托管的更新路
            logger.info(f"官方索引不可用，改用「{name}」声明的更新地址: {e}")
        link = link or (plugin.update_url if plugin else "")
        if not link:
            raise PluginError(f"「{name}」既不在官方索引里，也没声明 update_url")
        return self.import_url(link, expected_sha256=digest, task=task)

    # ── 指纹热载 ──

    def _fingerprint_now(self) -> tuple | None:
        if not os.path.isdir(self.root):
            return None
        items = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            # `_` 前缀是内部目录：_cache / __pycache__ / 安装中的暂存与备份。
            # 把暂存目录算进指纹会让「装到一半」被别的线程判成「目录变了」
            dirnames[:] = [d for d in dirnames if not d.startswith("_")]
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
        """目录指纹没变就用现有快照；变了则先建完整快照再一次性发布

        不能先清空 `_plugins` 再逐个装：那段时间里并发的解析会看到「一个包
        都没有」，把用户正在投的链接判成「没有规则」。
        """
        if self._fingerprint_now() == self._fingerprint:
            with self._lock:
                if self._loaded:
                    return list(self._rules)
        with self._write_lock:
            fingerprint = self._fingerprint_now()
            with self._lock:
                if self._loaded and fingerprint == self._fingerprint:
                    return list(self._rules)
            plugins: dict[str, JsPlugin] = {}
            if os.path.isdir(self.root):
                for name in sorted(os.listdir(self.root)):
                    plugin_dir = os.path.join(self.root, name)
                    if not os.path.isdir(plugin_dir) or name.startswith(("_", ".")):
                        continue
                    if not os.path.isfile(os.path.join(plugin_dir, "manifest.json")):
                        continue
                    try:
                        plugins[name] = JsPlugin(self, plugin_dir)
                    except Exception as e:
                        plugins[name] = JsPlugin._placeholder(
                            self, plugin_dir, name, str(e))
                        logger.warning("解析插件加载失败 %s: %s", name, e)
            # QuickJS 构建可能很慢（首次要编译脚本）——这段时间不持任何状态锁
            rules = self._collect_rules(plugins)
            with self._lock:
                self._plugins = plugins
                self._rules = rules
                self._fingerprint = fingerprint
                self._loaded = True
            set_plugin_rules(rules)
            return list(rules)

    def _collect_rules(self, plugins: dict) -> list[JsRule]:
        rules: list[JsRule] = []
        for plugin in plugins.values():
            try:
                for rule in plugin.build_rules():
                    if self.rule_enabled(plugin.name, rule.rule_id):
                        rules.append(rule)
            except Exception as e:
                plugin.error = plugin.error or str(e)
                logger.warning("解析插件 %s 规则枚举失败: %s", plugin.name, e)
        return rules

    def get(self, name: str) -> JsPlugin:
        self._ensure_loaded()
        with self._lock:
            plugin = self._plugins.get(name)
        if plugin is None:
            raise PluginError(f"插件不存在: {name}")
        return plugin

    def _ensure_loaded(self) -> None:
        """进程刚起来时，读视图/取插件不必等第一次解析"""
        if not self._loaded:
            self.refresh()

    # ── 导入 ──

    def import_zip_bytes(self, data: bytes,
                         task: TaskContext | None = None) -> dict:
        """校验并落盘一个包：先解到暂存目录，成功后才换代

        旧实现是「先 rmtree 目标目录再逐个写文件」——中途报错（磁盘满、
        zip 半路坏）就把用户原来能用的包删了，只剩一个加载失败的壳。
        """
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

            os.makedirs(self.root, exist_ok=True)
            staging = tempfile.mkdtemp(prefix=f"_{name}.staging.", dir=self.root)
            backup = f"{staging}.backup"
            try:
                for info in entries:
                    _checkpoint(task)
                    rel = info.filename.rsplit("/", 1)[-1]
                    with open(os.path.join(staging, rel), "wb") as f:
                        f.write(zf.read(info))
                # 提交：换代之后取消就不再算「撤销成功」
                commit_ctx = task.commit() if task else nullcontext()
                with self._write_lock, commit_ctx:
                    target = os.path.join(self.root, name)
                    if os.path.exists(target):
                        os.rename(target, backup)
                    try:
                        os.rename(staging, target)
                    except OSError:
                        if os.path.exists(backup):
                            os.rename(backup, target)   # 原样退回旧包
                        raise
                    self._fingerprint = None
            finally:
                shutil.rmtree(staging, ignore_errors=True)
                if os.path.isdir(backup):
                    shutil.rmtree(backup, ignore_errors=True)

        self.refresh()
        return self.get(name).info()

    def import_url(self, url: str, timeout: float = 60.0,
                   expected_sha256: str = "",
                   task: TaskContext | None = None) -> dict:
        """下载并安装一个包；摘要对不上就换下一条路，最后一条也不对才判死

        走 ``ghmirror.candidates``：开了镜像先试镜像、失败或摘要不符再退回直连。
        退回的理由是「镜像给错了字节」与「镜像挂了」在用户侧看起来一模一样，
        而直连重取一次就能分开这两种；sha256 始终按索引里那份官方摘要校验，
        所以退回直连不构成放宽——被换掉的包照样过不了这道关。

        带 ``task`` 时所有路共用同一份总预算，取消/超时不会被当成
        「这条路线不通」而继续往下试。
        """
        if not url.lower().startswith(("http://", "https://")):
            raise PluginError("只支持 http(s) 下载地址")
        paths = ghmirror.candidates(url)
        data = None
        errors: list[str] = []
        for via, fetch_url in paths:
            try:
                buf = _download(fetch_url, timeout, task, limit=_MAX_ZIP_BYTES)
                if expected_sha256:
                    _checkpoint(task)
                    got = hashlib.sha256(buf).hexdigest()
                    if got != expected_sha256.lower():
                        raise PluginError(
                            f"包校验失败（sha256 不匹配，可能是镜像过期）: {got[:12]}…")
                data = buf
                if via != "直连":
                    logger.info(f"规则包经{via}下载: {fetch_url[:90]}")
                break
            except (PluginError, requests.RequestException) as e:
                detail = str(e) if isinstance(e, PluginError) else f"{type(e).__name__}: {e}"
                errors.append(f"{via} {detail}")
                if task is not None:
                    task.checkpoint()
                if len(paths) > 1:
                    logger.info(f"{via} 下载规则包不成，换下一条路: {e}")
        if data is None:
            raise PluginError("；".join(errors) or "下载失败")
        return self.import_zip_bytes(data, task=task)

    def remove(self, name: str) -> None:
        self.get(name)
        with self._write_lock:
            shutil.rmtree(os.path.join(self.root, name), ignore_errors=True)
            with self._lock:
                self._state["prefs"].pop(name, None)
                self._state["disabled"] = [d for d in self._state["disabled"]
                                           if not d.startswith(f"{name}/")]
                self.save_state()
            self._fingerprint = None
        self.refresh()

    # ── 前端视图 ──

    def list_info(self) -> list[dict]:
        self._ensure_loaded()
        with self._lock:
            plugins = dict(self._plugins)
        out = []
        for name in sorted(plugins):
            plugin = plugins[name]
            try:
                out.append(plugin.info())
            except Exception as e:
                out.append({"name": name, "version": "", "author": "",
                            "description": "", "error": str(e), "rules": []})
        return out
