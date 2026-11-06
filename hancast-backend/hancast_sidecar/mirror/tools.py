"""adb / scrcpy 的定位、版本探测与 adb 输出解析。

检测口径与 `utils/mpv_manager.py`、`utils/ffmpeg_deps.py` 保持一致：
**手动指定 → PATH → 平台常见安装目录**，并且候选路径必须真跑一次
`--version` 才算可用。只判断「文件存在」是不够的——本项目在随包 mpv 上
踩过这个坑（文件在、依赖缺，一启动就 exit 127），工具链检测同理。

本模块不启动任何常驻进程；`adb devices` 由调用方决定何时跑。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Optional

logger = logging.getLogger("hancast.mirror")

# 探测结果缓存：UI 每 2 秒轮询一次状态，不能每次都 spawn 两个进程。
# key 里带 manual 路径，用户改过路径后天然失效。
_CACHE_TTL = 30.0
_cache: dict[tuple[str, str], tuple[float, dict]] = {}

TOOLS = ("adb", "scrcpy")

# scrcpy 2.0 起才有音频转发；更老的版本连 --no-audio 都不认识
SCRCPY_AUDIO_MIN_VERSION = (2, 0, 0)


def _exe_name(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def _platform_dirs(name: str) -> list[str]:
    """平台常见安装目录（按优先级）。

    PATH 之外还得看这些，是因为 GUI 程序在 macOS 上拿不到用户 shell 的
    PATH（双击启动不走 .zshrc），Homebrew 的 /opt/homebrew/bin 常常不在里面。
    """
    home = os.path.expanduser("~")
    dirs: list[str] = []
    if sys.platform == "darwin":
        dirs += [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            os.path.join(home, "Library", "Android", "sdk", "platform-tools"),
            os.path.join(home, "Android", "Sdk", "platform-tools"),
        ]
        if name == "scrcpy":
            dirs += ["/Applications/scrcpy.app/Contents/MacOS"]
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        roaming = os.environ.get("APPDATA", "")
        program_data = os.environ.get("ProgramData", r"C:\ProgramData")
        dirs += [
            os.path.join(local, "Android", "Sdk", "platform-tools"),
            os.path.join(roaming, "Android", "Sdk", "platform-tools"),
            os.path.join(home, "AppData", "Local", "Android", "Sdk", "platform-tools"),
            os.path.join(program_data, "chocolatey", "bin"),
            os.path.join(home, "scoop", "shims"),
            r"C:\Program Files\scrcpy",
            r"C:\Program Files (x86)\scrcpy",
        ]
    else:
        dirs += [
            "/usr/bin",
            "/usr/local/bin",
            "/snap/bin",
            os.path.join(home, ".local", "bin"),
        ]
    return [d for d in dirs if d]


def _executable(path: str) -> bool:
    return os.path.isfile(path) and (
        sys.platform == "win32" or os.access(path, os.X_OK)
    )


def find_tool(name: str, manual: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """返回 (路径, 来源)。来源是 manual / system，找不到时 (None, None)。"""
    if manual:
        path = os.path.expanduser(manual.strip())
        if _executable(path):
            return path, "manual"
        # 手动指定错了不静默回退：否则界面显示「已找到」但用的不是用户选的那个
        return None, None

    found = shutil.which(_exe_name(name))
    if found and _executable(found):
        return found, "system"

    for directory in _platform_dirs(name):
        candidate = os.path.join(directory, _exe_name(name))
        if _executable(candidate):
            return candidate, "system"
    return None, None


def _run(path: str, args: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    kwargs: dict = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    if sys.platform == "win32":
        # 轮询式调用不能每 2 秒闪一个黑框
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run([path] + args, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)
    out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    err = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
    return proc.returncode, out, err


def parse_version(name: str, text: str) -> Optional[str]:
    if name == "adb":
        m = re.search(r"Android Debug Bridge version\s+([0-9][0-9A-Za-z.\-]*)", text)
        if not m:
            m = re.search(r"Version\s+([0-9][0-9A-Za-z.\-]*)", text)
        return m.group(1) if m else None
    m = re.search(r"scrcpy\s+([0-9][0-9A-Za-z.\-]*)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", text)
    return m.group(1) if m else None


def version_tuple(version: Optional[str]) -> tuple[int, ...]:
    if not version:
        return ()
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts[:3])


def _probe(name: str, path: str) -> dict:
    code, out, err = _run(path, ["--version"], timeout=6.0)
    text = (out + "\n" + err).strip()
    version = parse_version(name, text) if code == 0 else None
    return {
        "key": name,
        "found": code == 0,
        "path": path,
        "version": version,
        "error": None if code == 0 else (text.splitlines()[0] if text else f"退出码 {code}"),
    }


def tool_info(name: str, manual: Optional[str] = None, refresh: bool = False) -> dict:
    cache_key = (name, manual or "")
    now = time.monotonic()
    if not refresh:
        hit = _cache.get(cache_key)
        if hit and now - hit[0] < _CACHE_TTL:
            return dict(hit[1])

    path, source = find_tool(name, manual)
    if path:
        info = _probe(name, path)
        info["source"] = source
        # 探测失败说明这个文件跑不起来（架构不对 / 依赖缺）：界面要把它当缺失处理，
        # 并把 stderr 第一行带出来，否则用户只看到「未安装」会反复重装同一个坏包
        if not info["found"]:
            info["error"] = f"{info['error']}（{path}）"
    else:
        info = {
            "key": name,
            "found": False,
            "path": None,
            "version": None,
            "source": None,
            "error": "未找到可执行文件",
        }
    _cache[cache_key] = (now, info)
    return dict(info)


def install_commands() -> list[str]:
    if sys.platform == "darwin":
        return ["brew install scrcpy", "brew install android-platform-tools"]
    if sys.platform == "win32":
        return ["winget install Genymobile.scrcpy", "winget install Google.PlatformTools"]
    # Linux：按发行版给一条更贴近的命令，识别不出来就退回 Debian 系
    ident = ""
    try:
        with open("/etc/os-release", "r", encoding="utf-8", errors="replace") as fh:
            ident = fh.read().lower()
    except OSError:
        pass
    if "arch" in ident or "cachyos" in ident or "manjaro" in ident:
        return ["sudo pacman -S android-tools scrcpy"]
    if "fedora" in ident or "rhel" in ident or "centos" in ident:
        return ["sudo dnf install android-tools scrcpy"]
    return ["sudo apt install adb scrcpy"]


def tool_status(manual_paths: dict[str, Optional[str]] | None = None, refresh: bool = False) -> dict:
    """给前端的状态报告（不含设备列表，设备枚举会 spawn adb）。"""
    manual_paths = manual_paths or {}
    tools = [tool_info(name, manual_paths.get(name), refresh=refresh) for name in TOOLS]
    adb = next(t for t in tools if t["key"] == "adb")
    scrcpy = next(t for t in tools if t["key"] == "scrcpy")
    scrcpy_ver = version_tuple(scrcpy["version"])
    audio_supported = bool(scrcpy_ver) and scrcpy_ver >= SCRCPY_AUDIO_MIN_VERSION
    return {
        "ok": adb["found"] and scrcpy["found"],
        "platform": sys.platform,
        "tools": tools,
        "tools_found": {"adb": adb["found"], "scrcpy": scrcpy["found"]},
        "install_commands": install_commands(),
        "scrcpy_audio_supported": audio_supported,
        # 老版本 scrcpy 没有音频转发（2.0 之前），界面要明确告诉用户为什么没有声音
        "scrcpy_audio_min_version": ".".join(str(v) for v in SCRCPY_AUDIO_MIN_VERSION),
    }


def clear_cache() -> None:
    _cache.clear()


def parse_adb_devices(text: str) -> list[dict]:
    """解析 `adb devices -l`。

    输出形如：
        List of devices attached
        R58M1234  device product:beyond1 model:SM_G9730 device:beyond1 transport_id:2
        emulator-5554  offline

    只认 ``serial state attrs`` 三列；`* daemon started successfully *` 这类
    提示行以星号开头，必须跳过，否则会被当成一台叫 "*" 的设备。
    """
    devices: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("*") or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        attrs: dict[str, str] = {}
        for token in parts[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                attrs[key] = value
        model = attrs.get("model") or attrs.get("device") or ""
        devices.append(
            {
                "serial": serial,
                "state": state,
                "online": state == "device",
                "model": model.replace("_", " ") if model else serial,
                "product": attrs.get("product"),
                "transport_id": attrs.get("transport_id"),
                "wireless": ":" in serial or "_adb-tls" in serial,
            }
        )
    return devices


def run_tool(path: str, args: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """给 manager 用的透明转发（测试里可以整体替换）。"""
    return _run(path, args, timeout=timeout)
