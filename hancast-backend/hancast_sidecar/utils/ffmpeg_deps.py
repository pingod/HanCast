"""FFmpeg 运行依赖：本机检测 + 缺失时补齐

自 v2.3.11 起程序**不再捆绑 FFmpeg**（安装包体积一大半是它，且永远落后于系统版本）。
这里的职责只有两件事：

1. **检测**：按「系统已装（PATH + 各平台常见安装目录）→ 本程序下载的那份」找
   ffmpeg / ffprobe，用户手动指定的路径优先级最高（由 `ffmpeg_manager` 负责）。
2. **补齐**：检测不到时给出各平台的包管理器命令，或按需把官方构建下载到
   `<配置目录>/bin/`，下载失败必须把**真实网络异常**原样交出去，
   不能只回一句「下载失败」——用户要能自己判断是代理、DNS 还是源站挂了。
"""

import gzip
import lzma
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
import logging
from typing import Callable, Dict, List, Optional, Tuple

from . import config, ghmirror

logger = logging.getLogger("hancast.ffmpeg_deps")

# 单次下载的硬上限：正常包 40–200MB，超过这个数说明源站给了别的东西
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024

# 检测结果缓存：status() 会被投屏状态轮询反复调用，每次都 spawn 一次
# `ffmpeg -version` 太浪费；手动「重新检测」用 refresh=True 绕过。
CACHE_TTL = 30.0
_version_cache: Dict[str, Tuple[float, Optional[str]]] = {}
_status_cache: Dict[str, Dict] = {}

_LABELS = {"ffmpeg": "FFmpeg", "ffprobe": "FFprobe"}

# FFprobe 只有「本地文件时长」这一处用到，缺了不影响屏幕投屏，所以是可选件
REQUIRED = ("ffmpeg",)
OPTIONAL = ("ffprobe",)


def exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def managed_dir() -> str:
    """本程序自己下载的 FFmpeg 落点（可写，不碰安装目录）"""
    return os.path.join(config.SETTING_DIR, "bin")


def _path_dirs() -> List[str]:
    return [p for p in os.environ.get("PATH", "").split(os.pathsep) if p.strip()]


def _extra_dirs() -> List[str]:
    """各平台「装了但没进 GUI 进程 PATH」的常见位置。

    Windows 上 winget 的可变包目录根本不在 PATH 里，Links 目录也常常只有
    新开终端才有；macOS 的 Homebrew 在 Apple Silicon 上是 /opt/homebrew。
    只查 PATH 会把「用户明明装了」判成缺失。
    """
    home = os.path.expanduser("~")
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        prog = os.environ.get("ProgramFiles", r"C:\Program Files")
        dirs = [
            os.path.join(local, "Microsoft", "WinGet", "Links"),
            os.path.join(local, "Microsoft", "WinGet", "Packages"),
            os.path.join(prog, "ffmpeg", "bin"),
            r"C:\ffmpeg\bin",
            r"C:\tools\ffmpeg\bin",
            os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                         "chocolatey", "bin"),
        ]
        # winget 的包目录带版本号，逐个试不如先展开一层
        pkg_root = os.path.join(local, "Microsoft", "WinGet", "Packages")
        for name in _child_dirs(pkg_root):
            if name.lower().startswith(("gyan.ffmpeg", "yt-dlp.ffmpeg")):
                dirs.append(os.path.join(pkg_root, name))
        return dirs
    if sys_platform() == "darwin":
        return ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin"]
    return ["/usr/local/bin", "/usr/bin", "/snap/bin", "/var/lib/flatpak/exports/bin",
            os.path.join(home, ".local", "bin")]


def sys_platform() -> str:
    """跟仓库其它模块同一口径（测试用 @patch("sys.platform", …) 切平台）"""
    return sys.platform


def _child_dirs(root: str) -> List[str]:
    try:
        return sorted(os.listdir(root))
    except OSError:
        return []


def search_dirs() -> List[str]:
    """检测顺序里的「系统」一层：PATH 在前，常见目录兜底，去重保序"""
    out: List[str] = []
    seen = set()
    for d in _path_dirs() + _extra_dirs():
        try:
            key = os.path.normcase(os.path.realpath(d))
        except OSError:
            key = d
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _walk_dirs() -> List[str]:
    """winget 的包目录要再下一层才能看见 bin/"""
    dirs = search_dirs()
    expanded: List[str] = []
    for d in dirs:
        expanded.append(d)
        if os.name != "nt":
            continue
        low = os.path.basename(d).lower()
        if low.startswith(("gyan.ffmpeg", "yt-dlp.ffmpeg")):
            for sub in ("bin", "."):
                cand = os.path.join(d, sub)
                if os.path.isdir(cand):
                    expanded.append(cand)
            # ffmpeg-xx.x-essentials_build\bin 这种带版本前缀的子目录
            for name in _child_dirs(d):
                cand = os.path.join(d, name, "bin")
                if os.path.isdir(cand):
                    expanded.append(cand)
    return expanded


def read_version(path: str) -> Optional[str]:
    """跑一次 `-version` 取首行；坏文件在这里就会被识破"""
    now = time.time()
    hit = _version_cache.get(path)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    version: Optional[str] = None
    try:
        result = subprocess.run(
            [path, "-version"], capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode == 0:
            lines = (result.stdout or "").splitlines()
            head = lines[0].strip() if lines else ""
            version = head or None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        version = None
    _version_cache[path] = (now, version)
    return version


def find_binary(name: str) -> Tuple[Optional[str], Optional[str]]:
    """返回 (path, source)，source ∈ {'system', 'downloaded', None}"""
    target = exe(name)
    for d in _walk_dirs():
        cand = os.path.join(d, target)
        if os.path.isfile(cand) and read_version(cand):
            return cand, "system"
    cand = os.path.join(managed_dir(), target)
    if os.path.isfile(cand) and read_version(cand):
        return cand, "downloaded"
    return None, None


def install_commands() -> List[str]:
    plat = sys_platform()
    if plat == "win32":
        return ["winget install --id Gyan.FFmpeg -e",
                "choco install ffmpeg -y"]
    if plat == "darwin":
        return ["brew install ffmpeg", "sudo port install ffmpeg"]
    return ["sudo apt update && sudo apt install -y ffmpeg",
            "sudo dnf install -y ffmpeg"]


def status(refresh: bool = False, manual: Optional[str] = None) -> Dict:
    """检测结果，供「需要下载运行依赖」弹窗与投屏门禁共用。

    顶层 `ok/path/version/source/error` 是老契约（投屏状态轮询与既有测试都在读），
    `items/missing/install_commands` 是给弹窗渲染的新字段。
    `manual` 是用户在设置里指定的路径，优先级最高，不参与缓存键以外的排序。
    """
    now = time.time()
    cache_key = manual or ""
    hit = _status_cache.get(cache_key)
    if not refresh and hit and now - hit["ts"] < CACHE_TTL:
        return dict(hit["data"])

    found: Dict[str, Dict] = {}
    for name in ("ffmpeg", "ffprobe"):
        path, source = find_binary(name)
        found[name] = {
            "key": name,
            "label": _LABELS[name],
            "required": name in REQUIRED,
            "found": path is not None,
            "path": path,
            "source": source,
            "version": read_version(path) if path else None,
        }

    if manual:
        found["ffmpeg"].update({"found": True, "path": manual,
                                "source": "manual",
                                "version": read_version(manual)})
        # 手动指了 ffmpeg 却没找到 ffprobe：同一目录里那份就是它的搭档
        if not found["ffprobe"]["found"]:
            sibling = os.path.join(os.path.dirname(manual), exe("ffprobe"))
            if os.path.isfile(sibling) and read_version(sibling):
                found["ffprobe"].update({"found": True, "path": sibling,
                                         "source": "manual",
                                         "version": read_version(sibling)})

    ok = found["ffmpeg"]["found"]
    missing = [found[n]["label"] for n in REQUIRED if not found[n]["found"]]
    optional_missing = [found[n]["label"] for n in OPTIONAL if not found[n]["found"]]
    data: Dict = {
        "ok": ok,
        "path": found["ffmpeg"]["path"],
        "version": found["ffmpeg"]["version"],
        "source": found["ffmpeg"]["source"],
        "ffprobe_path": found["ffprobe"]["path"],
        "items": [found["ffmpeg"], found["ffprobe"]],
        "missing": missing,
        "optional_missing": optional_missing,
        "managed_dir": managed_dir(),
        "platform": sys_platform(),
        "arch": platform.machine().lower(),
        "install_commands": install_commands() if missing else [],
        "download": dict(download_target() or {}),
    }
    if not ok:
        data["error"] = "未检测到 FFmpeg：屏幕投屏需要它，可自己安装后点「重新检测」，" \
                        "或让本程序下载一份"
    else:
        data["error"] = None
    _status_cache[cache_key] = {"ts": now, "data": dict(data)}
    return data


def probe_path() -> Optional[str]:
    """给 media/parser.py 用的 ffprobe 路径（找不到就退回裸命令，行为同旧版）"""
    path = status().get("ffprobe_path")
    return path or "ffprobe"


# ── 按需下载 ──

# 各平台的官方静态构建。macOS 只有 ffmpeg-static 给单文件 ffmpeg（无 ffprobe，
# 所以 FFprobe 是可选件）；Windows/Linux 的包里有 ffmpeg + ffprobe 两个。
_SOURCES = {
    ("win32", "amd64"): {
        "kind": "zip",
        "url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    },
    ("win32", "arm64"): {
        "kind": "zip",
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/latest/"
               "download/ffmpeg-master-latest-winarm64-gpl.zip",
    },
    ("darwin", "arm64"): {
        "kind": "gz",
        "url": "https://github.com/eugeneware/ffmpeg-static/releases/download/"
               "b6.1.1/ffmpeg-darwin-arm64.gz",
    },
    ("darwin", "x86_64"): {
        "kind": "gz",
        "url": "https://github.com/eugeneware/ffmpeg-static/releases/download/"
               "b6.1.1/ffmpeg-darwin-x64.gz",
    },
    ("linux", "x86_64"): {
        "kind": "tar.xz",
        "url": "https://johnvansickle.com/ffmpeg/releases/"
               "ffmpeg-release-amd64-static.tar.xz",
    },
    ("linux", "aarch64"): {
        "kind": "tar.xz",
        "url": "https://johnvansickle.com/ffmpeg/releases/"
               "ffmpeg-release-arm64-static.tar.xz",
    },
}


def download_target() -> Optional[Dict]:
    src = _SOURCES.get((sys_platform(), platform.machine().lower()))
    if not src:
        return None
    return {"available": True, "kind": src["kind"], "url": src["url"]}


class DependencyError(RuntimeError):
    """带「真实原因」的依赖失败：文案要能直接给用户看"""


def format_error(exc: BaseException) -> str:
    """把异常链拉直：代理端口死了这类问题，只看最外层是看不出来的"""
    parts: List[str] = []
    cur: Optional[BaseException] = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur).strip()
        parts.append(f"{type(cur).__name__}: {text}" if text else type(cur).__name__)
        cur = cur.__cause__ or cur.__context__
    out, dup = [], set()
    for p in parts:
        if p not in dup:
            dup.add(p)
            out.append(p)
    return " ← ".join(out[:3])


def _open_stream(url: str):
    """先按系统/环境代理走，代理不通就直连（和解析链路同一套策略）

    GitHub 系的源在「GitHub 走国内镜像」打开时先试镜像前缀，失败自动回退直连。
    两条路的失败原因都要写进文案：只说「连接下载源失败」，用户分不清是镜像挂了
    还是线路挂了，而这两种情况的处理动作完全不同。
    """
    from . import httpclient
    paths = ghmirror.candidates(url)
    errors: List[str] = []
    for via, target in paths:
        session = httpclient.make_session()
        try:
            r = session.get(target, timeout=(15, 30), stream=True,
                            headers={"User-Agent": "HanCast/dependency-installer",
                                     "Accept-Encoding": "identity"},
                            allow_redirects=True)
        except Exception as e:                       # noqa: BLE001
            session.close()
            errors.append(f"{via}：{format_error(e)}")
            if len(paths) > 1:
                logger.warning(f"依赖下载经{via}失败，回退下一条路径（{target}）")
            continue
        if r.status_code >= 400:
            r.close()
            session.close()
            errors.append(f"{via}：HTTP {r.status_code}")
            if len(paths) > 1:
                logger.warning(f"依赖下载经{via}返回 {r.status_code}，回退下一条路径")
            continue
        return session, r
    raise DependencyError(
        f"连接下载源失败（{url}）— {'；'.join(errors)}") from None


def _total_bytes(r) -> int:
    for key in ("Content-Length", "content-length"):
        val = r.headers.get(key) if hasattr(r, "headers") else None
        if val:
            try:
                return int(val)
            except ValueError:
                return 0
    return 0


def _download_archive(url: str, dest: str,
                      on_progress: Optional[Callable[[Dict], None]]) -> int:
    session, r = _open_stream(url)
    total = _total_bytes(r)
    received = 0
    started = time.time()
    last_tick = 0.0
    try:
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=128 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                received += len(chunk)
                if received > MAX_ARCHIVE_BYTES:
                    raise DependencyError(
                        f"下载包超过 {MAX_ARCHIVE_BYTES // 1024 // 1024}MB 上限，已中止")
                now = time.time()
                if on_progress and now - last_tick >= 0.4:
                    last_tick = now
                    elapsed = max(now - started, 0.001)
                    on_progress({
                        "received": received,
                        "total": total,
                        "percent": round(received * 100.0 / total, 1) if total else None,
                        "speed": int(received / elapsed),
                        "elapsed": round(elapsed, 1),
                    })
    except DependencyError:
        raise
    except Exception as e:                        # noqa: BLE001
        raise DependencyError(f"下载中断 — {format_error(e)}") from e
    finally:
        try:
            r.close()
        except Exception:                         # noqa: BLE001
            pass
        session.close()
    if total and received < total:
        raise DependencyError(f"下载不完整（{received}/{total} 字节）")
    return received


def _wanted_names(kind: str) -> Tuple[str, ...]:
    names = ["ffmpeg"] if kind == "gz" else ["ffmpeg", "ffprobe"]
    return tuple(exe(n) for n in names)


def _extract(archive: str, kind: str, target_dir: str) -> List[str]:
    """只取 ffmpeg/ffprobe 两个文件，条目名一律取 basename（防目录穿越）"""
    wanted = set(_wanted_names(kind))
    os.makedirs(target_dir, exist_ok=True)
    written: List[str] = []
    if kind == "zip":
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                base = os.path.basename(info.filename)
                if base in wanted and not info.is_dir():
                    _write_out(zf.open(info), os.path.join(target_dir, base), written)
    elif kind == "gz":
        with gzip.open(archive, "rb") as fh:
            _write_out(fh, os.path.join(target_dir, exe("ffmpeg")), written)
    elif kind == "tar.xz":
        with lzma.open(archive) as raw, tarfile.open(fileobj=raw, mode="r|") as tf:
            for member in tf:
                base = os.path.basename(member.name)
                if base in wanted and member.isfile():
                    handle = tf.extractfile(member)
                    if handle is not None:
                        _write_out(handle, os.path.join(target_dir, base), written)
    else:
        raise DependencyError(f"不支持的包格式: {kind}")
    if exe("ffmpeg") not in [os.path.basename(p) for p in written]:
        raise DependencyError("下载包里没找到 ffmpeg 可执行文件")
    for p in written:
        os.chmod(p, 0o755)
    return written


def _write_out(src, dest: str, written: List[str]) -> None:
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        shutil.copyfileobj(src, f, length=256 * 1024)
    os.replace(tmp, dest)
    written.append(dest)


# ── 任务表（下载可能几百 MB，命令循环不能被它卡满 60s）──

_download_lock = threading.Lock()
_download_task: Dict[str, object] = {}


def start_download() -> Dict:
    with _download_lock:
        if _download_task.get("status") == "pending":
            return dict(_download_task)
        target = download_target()
        if not target:
            raise DependencyError(
                f"当前平台（{sys_platform()}/{platform.machine()}）没有可用的下载源，"
                "请用它推荐的命令安装 FFmpeg 后点「重新检测」")
        task = {"task_id": f"ffmpeg_{int(time.time() * 1000) % 1000000:06d}",
                "status": "pending", "stage": "connecting",
                "received": 0, "total": 0, "percent": None, "speed": 0,
                "url": target["url"], "error": None, "result": None}
        _download_task.clear()
        _download_task.update(task)

    thread = threading.Thread(target=_run_download, name="FFMPEG_DOWNLOAD",
                              daemon=True)
    thread.start()
    return dict(task)


def download_status() -> Dict:
    with _download_lock:
        return dict(_download_task) or {"status": "idle"}


def _set_task(**kwargs) -> None:
    with _download_lock:
        _download_task.update(kwargs)


def _run_download() -> None:
    url = str(_download_task.get("url") or "")
    target_dir = managed_dir()
    archive = os.path.join(
        config.SETTING_DIR, f"ffmpeg-download{os.path.splitext(url)[-1] or '.bin'}")
    try:
        os.makedirs(config.SETTING_DIR, exist_ok=True)
        if os.path.exists(archive):
            os.remove(archive)

        def _progress(info: Dict) -> None:
            _set_task(stage="downloading", **info)

        _set_task(stage="downloading")
        _download_archive(url, archive, _progress)
        _set_task(stage="extracting")
        kind = str(download_target()["kind"])
        written = _extract(archive, kind, target_dir)
        _set_task(stage="verifying")
        status(refresh=True)
        ff = os.path.join(target_dir, exe("ffmpeg"))
        if not read_version(ff):
            raise DependencyError(
                f"下载并解包成功，但这个 ffmpeg 跑不起来（{ff}）——"
                "多半是当前平台用不了这份构建，请改用上面推荐的安装命令")
        result = {"ok": True, "paths": written, "dir": target_dir}
        _set_task(status="done", stage="done", result=result, error=None)
        logger.info(f"FFmpeg 已下载到 {target_dir}: {', '.join(written)}")
    except Exception as e:                        # noqa: BLE001
        # 真实原因（含底层网络异常）必须原样交给弹窗，别压成一句「下载失败」
        message = e if isinstance(e, DependencyError) else DependencyError(format_error(e))
        logger.warning(f"FFmpeg 下载失败: {message}")
        _set_task(status="error", stage="failed", error=str(message))
    finally:
        try:
            if os.path.exists(archive):
                os.remove(archive)
        except OSError:
            pass


def reset_cache() -> None:
    """测试用：清掉 TTL 缓存，别把上一用例的结果带给下一用例"""
    _version_cache.clear()
    _status_cache.clear()
