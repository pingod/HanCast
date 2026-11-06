"""自更新的下半程：Release 清单 → 本平台产物 → 带校验的下载 → 安装方式

三条契约，改动前先读：

1. **本模块不装东西**，只给 `install_plan()`。替换正在运行的 app 由 Rust 侧执行
   （`src-tauri/src/lib.rs` 的 `update_apply`）：Sidecar 是壳层的孩子，孩子去替换
   父亲的二进制、再负责把父亲叫醒，顺序与权限都不对。
2. **地址一律过 `ghmirror.candidates()`**。镜像是加速器不是新真相源：先镜像后直连、
   失败就换下一条路，两条路的失败原因都进日志。摘要校验保证「退回直连」不等于放宽。
3. **任务表只有一个槽位**。同时只允许一次下载（安装包 55–204 MB，并发只会互相抢带宽），
   重复 `start_download` 幂等返回进行中的那条——与 `utils/ffmpeg_deps.py` 同形。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..utils import ghmirror, protocol_io
from ..utils import config as config_mod
from ..utils import httpclient

logger = logging.getLogger("hancast.update")

REPO = "pingod/HanCast"
# **不用** `/releases/latest`：GitHub 把那个接口的 "latest" 给的是**最近一次 publish**
# 的 release，不是版本号最大的那条。2026-11-06 对账时实测踩中——为 v2.3.8 补一条
# 无产物的说明 release，`/releases/latest` 当场回指 2.3.8，正在跑 2.4.11 的客户端
# 从此报「已是最新版本」，界面一行错都不报。所以这里自己拉列表、按语义版本挑。
# 判据与修法记在 tools/audit-releases.py 的「`releases/latest` 指针」那条里。
API_RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=100&page="
API_RELEASE_TAG = f"https://api.github.com/repos/{REPO}/releases/tags/"
RELEASE_URL = f"https://github.com/{REPO}/releases"
#: 翻列表最多几页。一页 100 条，六页 = 600 个版本，本项目每版一个 tag 也远够不到。
MAX_PAGES = 6

#: 单个安装包的上限：AppImage 最大（实测 2.4.11 = 203.9 MB），留一倍余量。
#: 超了立即断开，免得镜像返回一个无限流把磁盘写满。
MAX_ASSET_BYTES = 512 * 1024 * 1024
#: 连接 15s、单次读 60s。读超时给到 60s 是因为墙内镜像首字节偶发很慢，
#: 而这里下的是整包不是首帧，慢一点没关系，断在半路有续传兜底。
TIMEOUT = (15, 60)
PROGRESS_INTERVAL = 0.4
#: release 清单的内存缓存时长。用户从「发现新版本」点到「立即下载」通常在几秒内，
#: 60 秒足够让这一步不再打第二次 API；手动「重新检查」走 commands._check_update，
#: 那里不经这个缓存，所以不会出现「缓存里还是旧版本」。
RELEASE_CACHE_TTL = 60.0

UPDATE_DIR_NAME = "updates"


class UpdateError(RuntimeError):
    """给用户看的失败原因（文案里带下一步该干什么）"""


class InstallPlanError(UpdateError):
    """产物已下载完，但本机没有可自动执行的安装方式"""


# ── 平台与产物识别 ──

_ARCH_TOKENS = (
    (("x86_64", "amd64", "x64"), "x86_64"),
    (("aarch64", "arm64"), "arm64"),
)

#: 后缀 → (kind, 归属平台, 本平台内的优先级)。数字越小越优先。
#: AppImage 在 Linux 优先级最高：它不需要 root，下载完 chmod +x 就能跑；
#: deb/rpm/Arch 都要 sudo，只能把命令交给用户（见 install_plan 的 kind="command"）。
_SUFFIX_TABLE = (
    (".appimage", "appimage", "linux", 0),
    (".deb", "deb", "linux", 1),
    (".rpm", "rpm", "linux", 2),
    (".pkg.tar.zst", "arch", "linux", 3),
    (".dmg", "dmg", "macos", 0),
    ("-setup.exe", "nsis", "windows", 0),
    (".exe", "nsis", "windows", 1),
    (".msi", "msi", "windows", 2),
)

#: kind → 该 kind 能不能由壳层自动执行
AUTO_APPLY = {"nsis", "dmg", "appimage"}


def _os_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _machine_arch() -> str:
    raw = (platform_machine() or "").lower()
    for tokens, canon in _ARCH_TOKENS:
        if raw in tokens:
            return canon
    return raw or "unknown"


def platform_machine() -> str:
    """单独抽出来，好让测试能改成 x86_64 / arm64 两台机器而不必换解释器"""
    import platform
    return platform.machine()


def classify_asset(name: str) -> Optional[Dict[str, Any]]:
    """文件名 → ``{kind, os, arch}``；不是安装包（规则包 zip 等）返回 None"""
    low = name.strip().lower()
    if low.endswith(".zip"):        # 规则包 zip 与 app 更新无关
        return None
    for suffix, kind, os_name, prio in _SUFFIX_TABLE:
        if not low.endswith(suffix):
            continue
        arch = ""
        for tokens, canon in _ARCH_TOKENS:
            if any(re.search(rf"[-_.]({re.escape(t)})[-_.]", " " + low + " ") for t in tokens):
                arch = canon
                break
        return {"kind": kind, "os": os_name, "arch": arch, "priority": prio}
    return None


def _linux_installed_kind(dest_candidates: Optional[List[str]] = None) -> str:
    """Linux 上「这份 HanCast 是怎么装进来的」→ 该给哪一种产物

    优先级：AppImage（免 root）→ 包管理器归属 → deb。之所以先猜 AppImage，是
    `APPIMAGE` 环境变量由 AppImage 运行时注入，命中即确定；包管理器那条只有
    在能证明归属时才敢说，猜错等于让用户 `sudo apt install` 一个 rpm 装的东西。
    """
    if os.environ.get("APPIMAGE"):
        return "appimage"
    markers = (
        ("/var/lib/dpkg/info/hancast.list", "deb"),
        ("/var/lib/rpm/rpmdb.sqlite", "rpm"),
        ("/usr/share/pacman", "arch"),
    )
    hits = {k for p, k in markers if os.path.exists(p)}
    # 真正的判据是包数据库里有没有 hancast 这个包，存在性检查只用来决定
    # 「值不值得去问一句」——三条命令都很便宜，失败就跳过。
    probes = (
        (["dpkg-query", "-W", "-f", "${db:Status-Abbrev}", "hancast"], "deb", "install ok installed"),
        (["rpm", "-q", "hancast"], "rpm", "hancast"),
        (["pacman", "-Q", "hancast"], "arch", "hancast"),
    )
    for cmd, kind, needle in probes:
        if kind not in hits and not os.path.exists({
            "deb": "/var/lib/dpkg", "rpm": "/var/lib/rpm", "arch": "/usr/share/pacman"}[kind]):
            continue
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and needle in (out.stdout or ""):
            return kind
    return "deb"


def _preferred_kind() -> str:
    """本平台默认该下哪一种产物"""
    if _os_name() == "linux":
        return _linux_installed_kind()
    return "nsis" if _os_name() == "windows" else "dmg"


def describe_release(data: Dict[str, Any]) -> Dict[str, Any]:
    """把一条 Release API 响应压成前端要的形状（含本平台可用产物清单）

    只回**本平台**能装的东西，并把每项的 `kind` 与 `arch_match` 标出来：
    ARM Windows 用户会看到 x64 的 `arch_match=False`——能装（系统跑 x64 模拟），
    但界面必须说实话，而不是让他以为这是原生包。
    """
    tag = str(data.get("tag_name") or "")
    assets_out: List[Dict[str, Any]] = []
    for a in data.get("assets") or []:
        name = str(a.get("name") or "")
        info = classify_asset(name)
        if not info:
            continue
        digest = str(a.get("digest") or "")
        assets_out.append({
            "name": name,
            "url": str(a.get("browser_download_url") or ""),
            "size": a.get("size"),
            "sha256": digest.replace("sha256:", "").lower(),
            "kind": info["kind"],
            "os": info["os"],
            "arch": info["arch"],
            "priority": info["priority"],
            "arch_match": bool(info["arch"]) and info["arch"] == _machine_arch(),
        })

    os_name = _os_name()
    mine = sorted((a for a in assets_out if a["os"] == os_name),
                  key=lambda a: (a["priority"], 0 if a["arch_match"] else 1))
    wanted = _preferred_kind()
    selected = next((a for a in mine if a["kind"] == wanted), None) or (mine[0] if mine else None)
    return {
        "tag": tag,
        "version": tag.lstrip("vV"),
        "release_url": str(data.get("html_url") or RELEASE_URL),
        "published_at": data.get("published_at"),
        "body": (data.get("body") or "")[:2000],
        "platform": {"os": os_name, "arch": _machine_arch()},
        "assets": mine,
        "selected": selected,
        # 本平台一个产物都没有 = 这版没出这个平台的包，界面必须直说而不是「下载失败」
        "no_asset_for_platform": os_name if not mine else "",
    }


# ── release 取数（带 60s 内存缓存） ──

_release_lock = threading.Lock()
_release_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def parse_version(text: str) -> Tuple:
    """版本号 → 可比较的元组（`(major, minor, patch, prerelease_key)`）

    这是全项目**唯一**一处版本号比较实现：原来住在
    ``commands.CommandHandler._parse_version``，本轮把「检查」与「下载」都收进本模块，
    实现跟着搬过来，`commands.py` 那边只剩一条调用。规则（照搬原实现，别再发明一套）：

    * 正式版（无 `-` 后缀）的 prerelease_key 为 `(1, ())`，预发布版为 `(0, ids)`；
      首位 1 > 0，所以 ``2.1.0`` > ``2.1.0-beta.1``。
    * 预发布标识符逐段比较，数字按数值、其余按字典序，数字段优先级低于非数字段
      （SemVer 11.4），所以 ``2.1.0-rc.2`` > ``2.1.0-rc.1``。
    * ``+`` 之后的构建元数据按规范忽略，``2.1.0+build.5`` == ``2.1.0``。

    历史一句：更早的实现是 ``re.findall(r'\\d+', v)``，把 ``2.1.0-beta.1`` 解析成
    ``(2, 1, 0, 1)``，比正式版 ``(2, 1, 0)`` 还大——于是装了正式版的用户被提示
    「有新版本 2.1.0-beta.1」，被推向 beta。这条教训是原实现 docstring 里的原话。
    """
    raw = str(text or "").strip().lstrip('vV')
    # 忽略 + 之后的构建元数据（SemVer 规范：不参与优先级比较）
    core = raw.split('+', 1)[0]

    m = re.match(r'^\s*(\d+(?:\.\d+)*)\s*(?:-([0-9A-Za-z.\-]*))?\s*$', core)
    if not m:
        # 兜底：抓出所有数字，按正式版处理
        nums = tuple(int(n) for n in re.findall(r'\d+', core)) or (0,)
        nums = (nums + (0, 0, 0))[:3]
        return nums + (1, ())

    nums = tuple(int(n) for n in m.group(1).split('.'))
    nums = (nums + (0, 0, 0))[:3]

    pre = m.group(2)
    if not pre:
        # 正式版：用 (1, ()) 表示，排在所有预发布版之上
        return nums + (1, ())

    # 预发布标识符逐段比较：数字按数值比，其余按字典序；
    # 数字标识符优先级低于非数字标识符（SemVer 规则 11.4）。
    ids = []
    for part in pre.split('.'):
        if part == '':
            continue
        if part.isdigit():
            ids.append((0, int(part), ''))
        else:
            ids.append((1, 0, part))
    # 预发布版：用 (0, ids) 表示。正式版首元素 1 > 预发布版 0，
    # 所以 2.1.0 > 2.1.0-beta.1；同为预发布版时再比 ids。
    return nums + (0, tuple(ids))


def _newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def _api_urls(version: str = "") -> Tuple[str, str]:
    """(缓存键, 请求地址)；version 为空时拉**列表**而不是 `/releases/latest`（见上注释）"""
    if version:
        tag = version if version.lower().startswith("v") else f"v{version}"
        return tag, f"{API_RELEASE_TAG}{tag}"
    return "latest", f"{API_RELEASES}1"


def _friendly_transfer_error(e: Exception) -> str:
    """传输层异常 → 用户能照着做的一句话

    `curl: (18) end of response with N bytes missing` 是**断流**的准确形状（真实 CDN
    掐线就这样），原文既看不懂也没有下一步。摘要不匹配与截断是两种不同的失败：
    前者怀疑镜像换了包，后者只是网络断了，重试就可能成——文案必须分开。
    """
    text = str(e)
    low = text.lower()
    if "18" in text and "end of response" in low or "incompleteread" in low \
            or "bytes missing" in low:
        return "下载被截断（远端只给了一半，通常是连接被掐）—— 再点一次会从断点接着下"
    if "timed out" in low or "timeout" in low:
        return "下载超时（线路太慢）—— 可打开国内镜像后重试"
    name = type(e).__name__
    return f"{name}: {text[:160]}"


def _friendly_http_error(e: Exception) -> str:
    """把 GitHub API 的失败说成人话，尤其是**限流**这一种

    匿名调用只有 60 次/小时，而一次检查要 1~2 个请求。用户在测试版上多点几下
    「检查更新」就会撞到 403，如果原样抛 `HTTP Error 403` 就变成「更新服务器坏了」，
    实际只需要等一小时或走镜像。
    """
    text = str(e)
    low = text.lower()
    if "403" in text or "rate limit" in low or "too request" in low:
        return ("GitHub API 访问过于频繁（匿名限流 60 次/小时）"
                "—— 请过一会儿再检查，或在设置 → 网络里打开 GitHub 镜像")
    if "404" in text:
        return "GitHub 上找不到该版本的 Release（可能已删除或未发布）"
    return f"{type(e).__name__}: {text}"


def _http_json(url: str, timeout: float = 8.0) -> Any:
    """一次 GET 取 JSON。走 httpclient：设置里配了代理的用户，裸 requests
    只认环境变量代理，程序内代理形同不存在。"""
    session = httpclient.make_session()
    try:
        resp = session.get(url, headers={"Accept": "application/vnd.github+json"},
                           timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def _next_page_url(url: str, page: int) -> str:
    """把（可能带镜像前缀的）列表地址翻到下一页

    页码在**已改写**的地址上替换，而不是拿原地址重拼再过一次镜像：否则第二条路
    会绕过 `candidates()` 的失败回退语义，出现「直连取第 1 页、镜像取第 2 页」这种
    跨路混着拿的清单。
    """
    return re.sub(r"page=\d+", f"page={page}", url, count=1)


def _latest_release_raw(first_url: str) -> Dict[str, Any]:
    """语义版本最大的那条**已发布且非预发布**的 Release 原始 JSON

    翻页拉列表：draft 不进结果（GitHub 对未登录用户也不给），prerelease 跳过，
    版本号解析不出来的（比如手滑建的说明条目）不参与比较。
    """
    collected: List[Dict[str, Any]] = []
    url = first_url
    for page in range(1, MAX_PAGES + 1):
        chunk = _http_json(url)
        if not isinstance(chunk, list) or not chunk:
            break
        collected.extend(chunk)
        if len(chunk) < 100:
            break
        url = _next_page_url(url, page + 1)
    usable = [r for r in collected
              if not r.get("draft") and not r.get("prerelease")
              and parse_version(str(r.get("tag_name") or ""))[:3] > (0, 0, 0)]
    if not usable:
        raise UpdateError("GitHub 上一条可用的已发布版本都没有（发布面是空的？）")
    return max(usable, key=lambda r: parse_version(str(r.get("tag_name") or "")))


def get_release(version: str = "") -> Dict[str, Any]:
    """取某版本的 Release 清单（空=语义最新的已发布版）；镜像与直连两条路都试过才判失败

    返回里除了 `describe_release` 那套产物清单，还带 `has_update` / `current`：
    「有没有新版」和「新版有什么可下载」必须来自同一次取数，否则界面会出现
    「提示有 2.5.0，下载时按 2.4.11 的产物挑」。
    """
    key = version or "latest"
    now = time.time()
    with _release_lock:
        hit = _release_cache.get(key)
        if hit and now - hit[0] < RELEASE_CACHE_TTL:
            return hit[1]

    cache_key, url = _api_urls(version)
    errors: List[str] = []
    for via, fetch_url in ghmirror.candidates(url):
        try:
            data = (_latest_release_raw(fetch_url) if not version
                    else _http_json(fetch_url))
        except Exception as e:  # noqa: BLE001 - 任何取数失败都换下一条路
            detail = _friendly_http_error(e)
            errors.append(f"{via} {detail}")
            if via != "直连":
                logger.warning(f"经{via}取 Release 失败，退回直连: {detail}")
            continue
        described = describe_release(data)
        described["via"] = via
        described["source"] = "github"
        with _release_lock:
            _release_cache[cache_key] = (time.time(), described)
        return described
    detail = "；".join(errors or ["无可用地址"])
    # 镜像/限流那条提示已经说清了下一步，别再追一句重复的
    hint = "" if ("镜像" in detail or "限流" in detail) else \
        "（可在设置 → 网络里改 GitHub 镜像前缀，或关掉镜像重试）"
    raise UpdateError(f"取版本清单失败：{detail}{hint}")


def check_for_update(current_version: str, ignored_version: str = "",
                     force: bool = False) -> Dict[str, Any]:
    """有没有新版本（前端 `check_update` 的唯一实现，含被忽略版本的语义）

    返回形状保持与 v2.2 以来的 `check_update` 契约一致（`has_update/current/latest/
    url/body/source`），再往外加产物清单；老字段一个都不动，前端与既有测试不至于
    因为一次功能新增就得跟着改读取方式。
    """
    current_version = str(current_version or "0.0.0")
    try:
        release = get_release("")
    except UpdateError as e:
        # 与旧实现一致：检查失败不抛错，把原因放进返回体，让界面自己决定怎么说
        return {
            "has_update": False,
            "current": current_version,
            "latest": current_version,
            "url": "",
            "body": "",
            "source": "",
            "error": str(e),
        }
    latest = str(release.get("version") or "")
    has = _newer(latest, current_version)
    if has and not force and ignored_version and latest == ignored_version.lstrip("vV"):
        logger.info(f"Update {latest} ignored by user")
        return {
            "has_update": False,
            "ignored": True,
            "current": current_version,
            "latest": latest,
            "url": "",
            "body": "",
            "source": release.get("source") or "github",
        }
    out = {
        "has_update": has,
        "current": current_version,
        "latest": latest,
        # 有新版才给 release 页面地址；没有新版时给空串，与旧契约一致
        "url": str(release.get("release_url") or "") if has else "",
        "body": str(release.get("body") or "")[:500],
        "source": release.get("source") or "github",
    }
    if release.get("via") and release["via"] != "直连":
        out["via"] = release["via"]
    if has:
        out["assets"] = release.get("assets") or []
        out["selected"] = release.get("selected")
        out["platform"] = release.get("platform")
        out["no_asset_for_platform"] = release.get("no_asset_for_platform") or ""
    return out


def invalidate_release_cache() -> None:
    """发版后想立刻看到新版：清掉 60s 内存缓存（测试与手动检查用）"""
    with _release_lock:
        _release_cache.clear()


# ── 下载任务表 ──

_download_lock = threading.Lock()
_download_task: Dict[str, object] = {}
_cancel_event = threading.Event()


def update_dir() -> str:
    """安装包落点：配置目录下的 `updates/`（与 FFmpeg 的 `bin/` 同层，不污染用户文档）"""
    return os.path.join(config_mod.SETTING_DIR, UPDATE_DIR_NAME)


def start_download(asset_name: str = "", version: str = "") -> Dict[str, object]:
    """起一次下载，立即返回任务快照；进度靠 `download_status()` 轮询

    `asset_name` 留空 = 用本平台默认产物；给了名字就必须在本平台清单里找得到，
    不接受任意名字——否则这条命令等于「用应用的凭据去下 GitHub 上任何文件」。
    """
    with _download_lock:
        if _download_task.get("status") == "pending":
            return dict(_download_task)

    release = get_release(version)
    assets: List[Dict[str, Any]] = list(release.get("assets") or [])
    if not assets:
        raise UpdateError(f"{release.get('version') or version} 没有本平台"
                          f"（{release.get('platform', {}).get('os')}）的安装产物")
    chosen = None
    if asset_name:
        chosen = next((a for a in assets if a["name"] == asset_name), None)
        if not chosen:
            raise UpdateError(f"该版本不含产物 {asset_name}")
    else:
        chosen = release.get("selected")
    if not chosen:
        raise UpdateError("没找到可下载的产物")
    _check_asset_url(str(chosen.get("url") or ""))

    dest_dir = update_dir()
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        raise UpdateError(f"创建下载目录失败：{e}") from e
    dest = os.path.join(dest_dir, str(chosen["name"]))
    _prune(dest_dir, keep=os.path.basename(dest))
    _cancel_event.clear()

    task = {
        "status": "pending",
        "stage": "connecting",
        "task_id": f"update-{release.get('version') or version}-{chosen['name']}",
        "version": release.get("version") or version,
        "asset": chosen["name"],
        "kind": chosen.get("kind") or "",
        "url": chosen["url"],
        "dest": dest,
        "total": chosen.get("size") or 0,
        "received": 0,
        "percent": 0.0,
        "speed": 0,
        "elapsed": 0.0,
        "via": "",
        "resumed_from": 0,
        "sha256_expected": chosen.get("sha256") or "",
        "sha256_actual": "",
        "verified": False,
        "error": None,
    }
    with _download_lock:
        _download_task.clear()
        _download_task.update(task)
    # 线程在锁外起：`_set_task` 要拿同一把锁，锁内 spawn 只会白等
    threading.Thread(target=_run_download, name="UPDATE_DOWNLOAD", daemon=True).start()
    return dict(task)


def download_status() -> Dict[str, object]:
    """任务表快照；**空表也带 `dir`**

    界面上一颗「打开所在目录」在下载开始前就可点，Rust 侧的 `open_update_dir`
    是从这份返回里拿路径的。空表只回 `{"status":"idle"}` 的话，那条命令在
    「还没下过任何东西」时必定失败——目录其实一直都在（`update_dir()`）。
    """
    with _download_lock:
        task = dict(_download_task) or {"status": "idle"}
    task.setdefault("dir", update_dir())
    return task


def cancel_download() -> Dict[str, object]:
    """请求取消；**不谎报已取消**——真停由下载线程落终态

    取消信号只置位，线程在下一次分片检查时把状态写成 `cancelled` 并删掉半份文件。
    这里如果直接把任务表改成 cancelled，界面会显示「已取消」而磁盘还在写。
    """
    _cancel_event.set()
    with _download_lock:
        if not _download_task:
            return {"status": "idle"}
        out = dict(_download_task)
        if out.get("status") != "pending":
            return out
        out["stage"] = "cancelling"
        _download_task.update({"stage": "cancelling"})
    return out


def _set_task(**kwargs: Any) -> None:
    with _download_lock:
        _download_task.update(kwargs)


def _check_asset_url(url: str) -> None:
    """产物地址必须是 https；例外只给环回地址

    一个能覆盖 `/Applications/HanCast.app` 的包，走明文 HTTP 传输等于把安装权交给
    同网段的任何人。GitHub 的 asset 地址本来就是 https，所以这条对真实链路是零成本；
    放行 127.0.0.1/localhost 只为本机测试与内网代理（离线判据用例就靠它，见
    tests/test_update_manager.py）。**不**放行任意 http 镜像：内网镜像该走
    `github.mirror_prefix` 那条改写，而不是把产物地址本身降成明文。
    """
    low = url.lower()
    if not low.startswith(("http://", "https://")):
        raise UpdateError(f"产物地址不是 http(s)：{url}")
    if low.startswith("http://"):
        host = (urlparse(url).hostname or "").lower()
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise UpdateError(f"产物地址是明文 HTTP，已拒绝：{url}")


def _prune(dest_dir: str, keep: str = "") -> List[str]:
    """删掉 updates/ 里除本次目标外的旧安装包

    一次更新 55–204 MB，攒五六个就够吃掉一 GB。只删本目录自己命名形态的文件
    （`HanCast_*` / `hancast-*`），别的文件一律不碰。

    `keep` 同时保住它的 `.part`：续传靠的就是那个半份文件，早先只保最终名，
    于是「上次断在半路、这次点下载」会被自己清理掉，断点续传永远从 0 开始
    （tests/test_update_manager.py::test_resume_from_part_file 钉住这条）。
    """
    removed: List[str] = []
    keep_all = {keep, keep + ".part"}
    try:
        entries = os.listdir(dest_dir)
    except OSError:
        return removed
    for name in entries:
        if name in keep_all:
            continue
        low = name.lower()
        if not (low.startswith("hancast") or low.startswith("hancast-")):
            continue
        if not low.endswith((".dmg", ".exe", ".msi", ".deb", ".rpm", ".appimage",
                             ".zst", ".part")):
            continue
        path = os.path.join(dest_dir, name)
        try:
            os.remove(path)
            removed.append(name)
        except OSError:
            pass
    if removed:
        logger.info(f"清理旧安装包: {', '.join(removed)}")
    return removed


def _sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_download() -> None:
    with _download_lock:
        url = str(_download_task.get("url") or "")
        dest = str(_download_task.get("dest") or "")
        expected = str(_download_task.get("sha256_expected") or "")
    part = dest + ".part"
    started = time.time()
    errors: List[str] = []

    for via, fetch_url in ghmirror.candidates(url):
        _set_task(via=via, stage="connecting", error=None)
        try:
            received = _fetch(fetch_url, part, via, started)
        except _Cancelled:
            _discard(part)
            spent = round(time.time() - started, 1)
            _set_task(status="cancelled", stage="cancelled", elapsed=spent,
                      error="已取消下载（半份文件已删除）")
            return
        except UpdateError as e:
            errors.append(f"{via} {e}")
            logger.warning(f"{via} 下载安装包不成: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            errors.append(f"{via} {_friendly_transfer_error(e)}")
            logger.warning(f"{via} 下载安装包异常: {e}")
            # **留下** .part：断流/超时正是续传要救的那一种。清掉它等于让下一次
            # 从 0 重下 55~204 MB，用户看到的现象就是「每次都白下一半」。
            # 只有两种情况才真该删：取消（用户明确不要了）与摘要不符（字节坏了）。
            continue

        # 摘要校验：整包重算。续传可能跨两条路（镜像半份 + 直连半份），
        # 只有对最终字节算摘要才拦得住「镜像给过坏字节」。
        _set_task(stage="verifying", percent=100.0, speed=0)
        try:
            actual = _sha256_of(part)
        except OSError as e:
            errors.append(f"{via} 读取失败: {e}")
            continue
        _set_task(sha256_actual=actual)
        if expected and actual != expected.lower():
            # 坏了的半份没有续传价值：删掉，让重试从干净的第一字节开始
            _discard(part)
            errors.append(f"{via} sha256 不匹配（期望 {expected[:12]}…，实到 {actual[:12]}…）")
            logger.error(f"安装包摘要不符，来源 {via}，已丢弃")
            continue

        try:
            os.replace(part, dest)
        except OSError as e:
            _set_task(status="error", stage="failed",
                      error=f"下载完成但落盘失败：{e}")
            return
        spent = round(time.time() - started, 1)
        _set_task(
            status="done", stage="done", percent=100.0, received=received,
            elapsed=spent, verified=bool(expected), dest=dest, error=None,
            note=("已按 GitHub 声明的 sha256 校验" if expected
                  else "GitHub 未声明该产物的 sha256，未做摘要校验"),
        )
        logger.info(f"安装包下载完成并校验: {dest}（{via}，{spent}s）")
        protocol_io.emit_event("update_download_done", {
            "version": str(_download_task.get("version") or ""),
            "asset": os.path.basename(dest),
            "path": dest,
            "verified": bool(expected),
        })
        return

    detail = "；".join(errors or ["无可用地址"])
    hint = "" if ("镜像" in detail or "限流" in detail) else \
        "（可改 GitHub 镜像前缀或关掉镜像重试）"
    _set_task(status="error", stage="failed",
              error=f"下载安装包失败：{detail}{hint}")


class _Cancelled(Exception):
    pass


def _fetch(fetch_url: str, part: str, via: str, started: float) -> int:
    """流式下载到一个 .part，返回磁盘上已有字节数（支持 Range 续传）

    已有 `.part` 且远端认 Range 就从断点接着写；远端回 200（不支持 Range）就从零开始，
    这种情况必须显式截断，否则接着 append 会得到一份既重复又对不上摘要的文件。
    """
    have = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"User-Agent": "HanCast/app-updater", "Accept-Encoding": "identity"}
    if have:
        headers["Range"] = f"bytes={have}-"
    session = httpclient.make_session()
    try:
        r = session.get(fetch_url, headers=headers, stream=True, timeout=TIMEOUT,
                        allow_redirects=True)
        if r.status_code >= 400:
            raise UpdateError(f"HTTP {r.status_code} {r.reason}")
        status = r.status_code
        if status == 206 and have:
            mode = "ab"
        else:
            if have:
                logger.info(f"{via} 不支持续传（HTTP {status}），从头重下")
            mode, have = "wb", 0
        total = _total_bytes(r, have)
        _set_task(stage="downloading", resumed_from=have, received=have, total=total,
                  percent=_pct(have, total))
        last = time.time()
        sent = have
        with open(part, mode) as f:
            for chunk in _iter_chunks(r, 256 * 1024):
                if _cancel_event.is_set():
                    raise _Cancelled()
                if not chunk:
                    continue
                f.write(chunk)
                sent += len(chunk)
                if sent > MAX_ASSET_BYTES:
                    raise UpdateError(f"产物超出大小上限（{MAX_ASSET_BYTES // (1024 * 1024)} MB），已中止")
                now = time.time()
                if now - last >= PROGRESS_INTERVAL:
                    # speed/elapsed 一律以「本次 start_download 的时刻」为锚：续传或
                    # 换路重下时这一段可能只有几秒，按段算会让界面显示 30 MB/s 的假速度
                    _set_task(received=sent, total=total, percent=_pct(sent, total),
                              speed=int(sent / max(now - started, 0.1)),
                              elapsed=round(now - started, 1))
                    last = now
        r.close()
        if total and sent < total:
            raise UpdateError(f"下载不完整（{sent}/{total} 字节）")
        return sent
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def _total_bytes(r: Any, resumed: int) -> int:
    """Content-Length + 已有的那截；Range 命中时真正的整包长度在 Content-Range 末段"""
    cr = str(r.headers.get("Content-Range") or "")
    m = re.search(r"/(\d+)\s*$", cr)
    if m:
        return int(m.group(1))
    try:
        return int(r.headers.get("Content-Length") or 0) + resumed
    except (TypeError, ValueError):
        return 0


def _pct(done: int, total: int) -> Optional[float]:
    return round(done * 100.0 / total, 1) if total else None


def _iter_chunks(response: Any, size: int):
    """按 size 分块读响应体

    不用 `resp.iter_content(chunk_size=…)`：curl_cffi 会直接忽略这个参数并打
    `CurlCffiWarning: chunk_size is ignored, there is no way to tell curl that`
    （实测 FFmpeg 那条下载也在吃这个警告），分块大小由 curl 自己定。这里从
    `raw`/`urllib3` 流上按我们要的字节数读，既消掉噪声，也让进度节流与取消点
    落在**我们**选的粒度上——200 MB 的包按 256 KB 读，取消最多等一片。
    """
    raw = getattr(response, "raw", None)
    reader = getattr(raw, "read", None) or getattr(response, "read", None)
    if reader is None:
        # 兜底：退回 requests 语义的迭代（拿不到分块控制权，但功能不受影响）
        for chunk in response.iter_content():
            if chunk:
                yield chunk
        return
    while True:
        chunk = reader(size)
        if not chunk:
            return
        yield chunk


def _discard(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ── 安装方式 ──

def install_plan() -> Dict[str, Any]:
    """已下载完成的产物 → 交给壳层的安装方案

    `kind` 决定壳层怎么动：
      - `nsis`：静默跑安装包装机，装完重启（Windows 交给安装程序处理正在运行的 exe，
        它自己会等进程退出再覆盖，所以我们只需退出并留一条 watchdog）；
      - `dmg`：挂载 dmg → 替换同目录的 HanCast.app → 卸载 → 重启（macOS）；
      - `appimage`：chmod +x 后直接起新的 AppImage，本程序退出（Linux，不需要 root）；
      - `command`：**只给命令，不代跑**。deb/rpm/Arch 包必须提权，Sidecar 与壳层都
        不该偷偷 sudo，所以把 `sudo pacman -U <文件>` 这类原文交出去，由界面提供复制。
    """
    with _download_lock:
        task = dict(_download_task)
    if not task:
        raise InstallPlanError("还没有下载过安装包")
    if task.get("status") != "done":
        raise InstallPlanError(f"安装包尚未就绪（当前状态：{task.get('status')}）")
    path = str(task.get("dest") or "")
    if not path or not os.path.isfile(path):
        raise InstallPlanError("下载记录还在，但文件已不在磁盘上（可能被清理或手动删了）")
    kind = str(task.get("kind") or "") or (classify_asset(os.path.basename(path)) or {}).get("kind", "")
    if not kind:
        raise InstallPlanError(f"认不出这个产物的安装方式：{os.path.basename(path)}")

    plan: Dict[str, Any] = {
        "ok": True,
        "kind": kind if kind in AUTO_APPLY else "command",
        "asset_kind": kind,
        "path": path,
        "asset": os.path.basename(path),
        "version": task.get("version") or "",
        "sha256": task.get("sha256_actual") or "",
        "verified": bool(task.get("verified")),
        "auto": kind in AUTO_APPLY,
    }
    if plan["kind"] == "command":
        plan["command"] = _manual_command(kind, path)
        plan["note_zh"] = "这一类产物需要管理员权限，界面里请复制这条命令到终端执行"
        plan["note_en"] = "This package needs admin rights; copy the command below into a terminal"
    return plan


def _manual_command(kind: str, path: str) -> str:
    quoted = f'"{path}"' if sys.platform == "win32" else path
    return {
        "deb": f"sudo apt install {quoted}",
        "rpm": f"sudo rpm -Uvh --replacepkgs {quoted}",
        "arch": f"sudo pacman -U {quoted}",
        "msi": f'msiexec /i {quoted}',
    }.get(kind, f"xdg-open {quoted}")
