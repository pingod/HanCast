"""
FFmpeg 管理器 — 检测 / 路径管理 / 捕获源枚举

查找顺序与 MPV 一致：配置路径 → 捆绑目录（ffmpeg/）→ 系统 PATH。
同时提供屏幕/窗口/音频设备的枚举能力（供前端选择投屏源）。
"""

import os
import re
import io
import sys
import json
import wave
import ctypes
import logging
import threading
import subprocess
from typing import Dict, List, Optional, Tuple

from ..params import get as param

logger = logging.getLogger("hancast.screencast.ffmpeg")

# avfoundation 屏幕设备索引缓存（ffmpeg_path -> [index…]，空表表示没找到）
# 用哨兵区分「没查过」和「查过但没有屏幕设备」
_avf_screen_cache: Dict[str, Optional[List[int]]] = {}
_AVF_UNSET = object()
_avf_screen_lock = threading.Lock()

# 未指定采集尺寸时的兜底上限（可在设置页调整，见 params 登记表
# screencast.max_width / screencast.max_height）。多屏机器的「整个屏幕」是所有
# 显示器的并集（现场实测 4000x2571），既超出电视解码能力（Main profile 到
# 1080p 最稳），又会被 2M 码率压成马赛克；奇数高度还会让 x264 直接拒绝初始化。


def _run_utf8(cmd: List[str], timeout: int) -> subprocess.CompletedProcess:
    """跑 ffmpeg 枚举命令。

    ffmpeg 往管道写的是 UTF-8，而 Windows 上 text=True 会按当前代码页
    （简体中文 = cp936）解码，设备名/窗口标题里的中文直接抛
    UnicodeDecodeError 并把读取线程干掉——枚举结果是空的。
    """
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _parse_screen_region(source_id: str) -> Optional[tuple]:
    """解析显示器源 id "screen:x,y,w,h"，得到 gdigrab 的抓取区域。

    解析不出（"desktop" 或脏数据）就返回 None —— 退回抓整个虚拟桌面。
    """
    if not source_id.startswith("screen:"):
        return None
    parts = source_id[len("screen:"):].split(",")
    if len(parts) != 4:
        return None
    try:
        x, y, w, h = (int(p) for p in parts)
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def _avf_screen_no(source_id: str) -> int:
    """macOS 屏幕源 id "avf:<屏号>" → 屏号；其它（desktop/脏数据）按主屏 0"""
    if source_id.startswith("avf:"):
        try:
            return max(0, int(source_id[4:]))
        except ValueError:
            return 0
    return 0


def avfoundation_screen_indexes(ffmpeg_path: str) -> List[int]:
    """所有 "Capture screen N" 屏幕设备的 avfoundation 索引，按 N 升序。

    macOS 上摄像头排在屏幕前面，索引随外接摄像头/虚拟相机数量变化，
    不能硬编码（曾固定用 1，在装有 OBS Virtual Camera 的机器上抓到的是
    摄像头而不是屏幕）。多屏机器要按屏序挑对应的设备，所以留全表。
    """
    with _avf_screen_lock:
        cached = _avf_screen_cache.get(ffmpeg_path, _AVF_UNSET)
    if cached is not _AVF_UNSET:
        return list(cached or [])

    idxs: List[int] = []
    try:
        result = _run_utf8(
            [ffmpeg_path, "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""], 10)
        section = None
        for line in (result.stderr or "").splitlines():
            if "AVFoundation video devices" in line:
                section = "video"
                continue
            if "AVFoundation audio devices" in line:
                break
            if section != "video":
                continue
            match = re.search(r"\[(\d+)\]\s*(.*)", line)
            if not match:
                continue
            name = match.group(2).lower()
            if "capture screen" in name or "屏幕捕捉" in name or "捕捉屏幕" in name:
                # 括号里是屏号：Capture screen 0 / 1 / …
                screen_no = re.search(r"(\d+)\s*$", name)
                idxs.append((int(screen_no.group(1)) if screen_no else len(idxs),
                             int(match.group(1))))
    except Exception as e:
        logger.warning(f"Failed to detect avfoundation screen device: {e}")

    idxs.sort()
    ordered = [i for _n, i in idxs]
    with _avf_screen_lock:
        _avf_screen_cache[ffmpeg_path] = ordered
    if not ordered:
        logger.warning("avfoundation 未枚举到屏幕捕获设备（Capture screen）")
    else:
        logger.info(f"avfoundation screen capture device indexes: {ordered}")
    return ordered


def avfoundation_screen_index(ffmpeg_path: str, screen_no: int = 0) -> Optional[int]:
    """第 screen_no 块屏（主屏为 0）对应的 avfoundation 索引；找不到返回 None"""
    idxs = avfoundation_screen_indexes(ffmpeg_path)
    if not idxs:
        return None
    if screen_no < len(idxs):
        return idxs[screen_no]
    # 屏数对不上（拔了显示器）就用最后一块，至少有画面
    return idxs[-1]


# ────────────────────────────────────────
# macOS：窗口与显示器枚举（ctypes 直调 CoreGraphics，不引 pyobjc）
#
# FFmpeg 的 avfoundation 从 6.1 到 master 都没有窗口输入（选项表里只有
# 设备索引/尺寸/帧率那几个），所以 mac 上「投某个窗口」只能是
# 抓那块屏 + 按窗口几何 crop 出来。几何就从 CoreGraphics 取。
# ────────────────────────────────────────

_CFStringEncodingUTF8 = 0x08000100
# Stage Manager / iPhone 镜像在窗口列表里塞的系统磁贴，layer 也是 0，
# 不剔除的话用户会在一堆真窗口里看到几个叫不上名字的方块
_CG_SYSTEM_OWNERS = {"WindowManager"}
_CFIndex = ctypes.c_ssize_t if hasattr(ctypes, "c_ssize_t") else ctypes.c_long


class _CGRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                ("w", ctypes.c_double), ("h", ctypes.c_double)]


def _cf_libs():
    """加载 CoreGraphics + CoreFoundation，配好返回值/参数类型"""
    cg = ctypes.CDLL(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cf = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    VP = ctypes.c_void_p
    cg.CGWindowListCopyWindowInfo.restype = VP
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32
    cg.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(VP),
                                          ctypes.POINTER(ctypes.c_uint32)]
    cg.CGDisplayBounds.restype = _CGRect
    cg.CGDisplayBounds.argtypes = [VP]
    cg.CGMainDisplayID.restype = VP
    cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
    cg.CGDisplayPixelsWide.argtypes = [VP]
    cf.CFArrayGetCount.restype = _CFIndex
    cf.CFArrayGetCount.argtypes = [VP]
    cf.CFArrayGetValueAtIndex.restype = VP
    cf.CFArrayGetValueAtIndex.argtypes = [VP, _CFIndex]
    cf.CFDictionaryGetValue.restype = VP
    cf.CFDictionaryGetValue.argtypes = [VP, VP]
    cf.CFStringGetLength.restype = _CFIndex
    cf.CFStringGetLength.argtypes = [VP]
    cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
    cf.CFStringGetCStringPtr.argtypes = [VP, ctypes.c_uint32]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [VP, ctypes.c_char_p, _CFIndex, ctypes.c_uint32]
    cf.CFNumberGetValue.restype = ctypes.c_bool
    cf.CFNumberGetValue.argtypes = [VP, ctypes.c_int, ctypes.c_void_p]
    cf.CFRelease.restype = None
    cf.CFRelease.argtypes = [VP]
    return cg, cf


def _cf_str(cf, handle) -> str:
    if not handle:
        return ""
    ptr = cf.CFStringGetCStringPtr(handle, _CFStringEncodingUTF8)
    if ptr:
        return ptr.decode("utf-8", errors="replace")
    n = cf.CFStringGetLength(handle)
    buf = ctypes.create_string_buffer(n * 4 + 4)
    if cf.CFStringGetCString(handle, buf, len(buf), _CFStringEncodingUTF8):
        return buf.value.decode("utf-8", errors="replace")
    return ""


def _cf_num(cf, handle, double=False):
    if not handle:
        return None
    if double:
        val = ctypes.c_double()
        # kCFNumberDoubleType=6、kCFNumberSInt128Type=12 —— 拿 12 去写 8 字节的
        # c_double，CFNumberGetValue 越界写完返回垃圾（CGWindowBounds 全是 5e-315
        # 这种非规格化数），窗口尺寸一律小于阈值，最后枚举结果为 0 个窗口
        ok = cf.CFNumberGetValue(handle, 6, ctypes.byref(val))
        return float(val.value) if ok else None
    val = ctypes.c_longlong()
    ok = cf.CFNumberGetValue(handle, 4, ctypes.byref(val))        # kCFNumberSInt64Type
    return int(val.value) if ok else None


def darwin_displays() -> List[Dict]:
    """活动显示器列表：主屏在前，坐标已换算成「窗口坐标系」（左上角为原点、y 向下）

    CGDisplayBounds 用的是 y 向上的全局坐标，两套坐标不换算的话副屏的裁剪区域
    会跑到屏外，投出来是一条错位的边。
    """
    import ctypes
    try:
        cg, cf = _cf_libs()
    except OSError as e:
        logger.warning(f"[ScreenCast] CoreGraphics 加载失败: {e}")
        return []
    count = ctypes.c_uint32()
    if cg.CGGetActiveDisplayList(0, None, ctypes.byref(count)) != 0 or not count.value:
        return []
    ids = (ctypes.c_void_p * count.value)()
    if cg.CGGetActiveDisplayList(count.value, ids, ctypes.byref(count)) != 0:
        return []
    main = cg.CGMainDisplayID()
    out: List[Dict] = []
    for did in list(ids)[:count.value]:
        b = cg.CGDisplayBounds(did)
        scale = 1.0
        px = cg.CGDisplayPixelsWide(did)
        if b.w > 0 and px:
            scale = px / b.w          # Retina：逻辑点 → 采集像素
        out.append({
            "id": did,
            "main": did == main,
            "points": (b.x, b.y, b.w, b.h),
            "scale": scale,
            # 窗口坐标系里该屏的上边：CGDisplayBounds 的 y 向上、窗口列表的 y 向下，
            # 主屏（y=0）上沿就是 0，位于主屏上方的副屏（y=+主屏高）上沿是负数
            "top": -b.y,
        })
    out.sort(key=lambda d: (not d["main"], d["points"][1]))
    for i, d in enumerate(out):
        d["screen_no"] = i
    return out


def darwin_windows() -> List[Dict]:
    """在屏窗口列表（含屏幕号与像素级裁剪区域）

    没有「屏幕与系统音频录制」权限时，系统会静默去掉 kCGWindowName
    （App 名和几何照给），所以标题退化成「App 名」也要能选。
    """
    import ctypes
    try:
        cg, cf = _cf_libs()
    except OSError as e:
        logger.warning(f"[ScreenCast] CoreGraphics 加载失败: {e}")
        return []
    try:
        keys = {n: ctypes.c_void_p.in_dll(cg, n) for n in
                ("kCGWindowNumber", "kCGWindowOwnerName", "kCGWindowName",
                 "kCGWindowBounds", "kCGWindowLayer")}
    except (ValueError, KeyError) as e:
        logger.warning(f"[ScreenCast] 取 CGWindow 键失败: {e}")
        return []

    displays = darwin_displays()
    arr = cg.CGWindowListCopyWindowInfo(1 | 1 << 4, 0)  # 在屏 + 排除桌面元素
    if not arr:
        return []
    out: List[Dict] = []
    try:
        for i in range(cf.CFArrayGetCount(arr)):
            info = cf.CFArrayGetValueAtIndex(arr, i)
            num = _cf_num(cf, cf.CFDictionaryGetValue(info, keys["kCGWindowNumber"]))
            owner = _cf_str(cf, cf.CFDictionaryGetValue(info, keys["kCGWindowOwnerName"]))
            name = _cf_str(cf, cf.CFDictionaryGetValue(info, keys["kCGWindowName"]))
            layer = _cf_num(cf, cf.CFDictionaryGetValue(info, keys["kCGWindowLayer"]))
            bounds = cf.CFDictionaryGetValue(info, keys["kCGWindowBounds"])
            if num is None or not owner or layer not in (None, 0):
                continue      # 菜单/提醒层一类的浮动元素不是用户要的窗口
            if owner in _CG_SYSTEM_OWNERS:
                continue      # Stage Manager / iPhone 镜像的系统磁贴，用户不会想投
            rect = _cg_bounds(cf, bounds) if bounds else None
            if not rect:
                continue
            x, y, w, h = rect
            if w < 40 or h < 40:
                continue
            win = {"num": num, "owner": owner, "name": name,
                   "x": x, "y": y, "w": w, "h": h}
            disp = _display_for(win, displays)
            if not disp:
                continue
            out.append(_window_entry(win, disp))
    finally:
        cf.CFRelease(arr)
    return out


def _cg_key(cf, text: str):
    """CFStringCreateWithCString —— CGWindowBounds 字典的键是普通字符串"""
    import ctypes
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                             ctypes.c_uint32]
    return cf.CFStringCreateWithCString(None, text.encode(), _CFStringEncodingUTF8)


def _cg_bounds(cf, bounds_dict):
    import ctypes
    vals = []
    try:
        for k in ("X", "Y", "Width", "Height"):
            key = _cg_key(cf, k)
            vals.append(_cf_num(cf, cf.CFDictionaryGetValue(bounds_dict, key),
                                double=True))
            cf.CFRelease(key)
    except Exception:
        return None
    if any(v is None for v in vals):
        return None
    return tuple(vals)


def _display_for(win: Dict, displays: List[Dict]) -> Optional[Dict]:
    """窗口中心落在哪块屏上（副屏坐标是负的，靠窗口坐标系里的上边来判）"""
    cx, cy = win["x"] + win["w"] / 2, win["y"] + win["h"] / 2
    for d in displays:
        _bx, _by, bw, bh = d["points"]
        top = d["top"]
        if _bx <= cx < _bx + bw and top <= cy < top + bh:
            return d
    return None


def _window_entry(win: Dict, disp: Dict) -> Dict:
    """把窗口几何翻译成采集参数（像素、偶数、落在屏内）"""
    s = disp["scale"]
    x = round((win["x"] - disp["points"][0]) * s)
    y = round((win["y"] - disp["top"]) * s)
    w = round(win["w"] * s)
    h = round(win["h"] * s)
    fw = round(disp["points"][2] * s)
    fh = round(disp["points"][3] * s)
    x = max(0, x)
    y = max(0, y)
    w = max(2, min(w, fw - x))
    h = max(2, min(h, fh - y))
    w -= w % 2                      # x264 拒绝奇数尺寸
    h -= h % 2
    title = win["name"] or win["owner"]
    label = f"{title} · {win['owner']}" if win["name"] else f"{win['owner']}（无标题）"
    return {
        "id": f"win:{win['num']}",
        "title": label,
        "screen_no": disp["screen_no"],
        "crop": [x, y, w, h],
        # 同一块区域的「占屏比例」形式：真正下发给 ffmpeg 的是它。
        # avfoundation 送进 crop 滤镜的坐标是它实际采集的那块缓冲区，
        # 而缓冲区尺寸在不同机器上不保证等于我们算的像素尺寸（HiDPI/缩放分辨率
        # 尤甚）。按比例裁剪的话缓冲区是 2560 还是 5120 都对，只要长宽比一致；
        # 尺寸留给后面的 scale 兜底（它一定输出偶数）。
        "crop_norm": [x / fw, y / fh, w / fw, h / fh],
    }


def build_crop_filter(crop: List[float]) -> str:
    """窗口裁剪滤镜串。

    crop 是 [fx, fy, fw, fh]（占屏比例）时下发 iw/ih 表达式：avfoundation 送进
    crop 滤镜的坐标系是它**实际采集的缓冲区**，不是我们算的那份像素 —— Retina/
    缩放分辨率下两者差 2 倍，像素版会裁到错位的一块（本机是 1:1 外接屏，量不出来）。
    按比例就与缓冲区尺寸无关，只要长宽比一致即可，偶数尺寸交给后面的 scale。
    旧的像素 [x, y, w, h] 仍按像素裁剪（w/h 在 _window_entry 里恒 >= 2，可判别）。
    """
    fx, fy, fw, fh = (float(v) for v in crop)
    if 0 < fw <= 1 and 0 < fh <= 1 and fx <= 1 and fy <= 1:
        return (f"crop=trunc(iw*{fw:.6f}/2)*2:trunc(ih*{fh:.6f}/2)*2"
                f":trunc(iw*{fx:.6f}):trunc(ih*{fy:.6f})")
    x, y, w, h = (int(round(v)) for v in crop)
    return f"crop={w}:{h}:{x}:{y}"


class FfmpegManager:
    """FFmpeg 检测、路径管理、源枚举"""

    def __init__(self, config=None):
        self.config = config
        self._path: Optional[str] = None
        self._version: Optional[str] = None

    # ────────────────────────────────────────
    # 路径发现 / 校验
    # ────────────────────────────────────────

    def _get_base_path(self) -> str:
        """获取程序根目录（含 ffmpeg/ 子目录的层级），与 mpv_manager 一致"""
        candidates = [
            os.getcwd(),                       # Rust 设置的 current_dir
            os.path.dirname(sys.executable),   # sidecar exe 所在目录
        ]
        for path in candidates:
            exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            if os.path.isfile(os.path.join(path, "ffmpeg", exe)):
                return path
        # 开发环境：向上 4 级到项目根
        return os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))

    def _find_in_program_dir(self) -> Optional[str]:
        base = self._get_base_path()
        exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        candidates = [
            os.path.join(base, "ffmpeg", exe),
            os.path.join(base, "ffmpeg", "bin", exe),
        ]
        for p in candidates:
            if os.path.isfile(p):
                return os.path.abspath(p)
        # 递归查找 ffmpeg/ 目录
        ffmpeg_dir = os.path.join(base, "ffmpeg")
        if os.path.isdir(ffmpeg_dir):
            for root, _, files in os.walk(ffmpeg_dir):
                for name in files:
                    if name.lower() == exe:
                        return os.path.join(root, name)
        return None

    def _find_in_path(self) -> Optional[str]:
        exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        for dir_path in os.environ.get("PATH", "").split(os.pathsep):
            candidate = os.path.join(dir_path, exe)
            if os.path.isfile(candidate):
                return candidate
        return None

    def check(self) -> Dict:
        """检查 FFmpeg 是否可用，返回 {path, version, ok}"""
        # 1. 配置路径
        if self.config:
            cfg = self.config.get("screencast", {})
            if cfg.get("ffmpeg_path"):
                if self._validate(cfg["ffmpeg_path"]):
                    self._path = cfg["ffmpeg_path"]
                    self._version = self._get_version(self._path)
                    return {"path": self._path, "version": self._version, "ok": True}
                logger.warning(f"Configured FFmpeg path no longer valid: {cfg['ffmpeg_path']}")

        # 2. 捆绑目录
        local = self._find_in_program_dir()
        if local and self._validate(local):
            self._path = local
            self._version = self._get_version(local)
            return {"path": local, "version": self._version, "ok": True, "source": "bundled"}

        # 3. 系统 PATH
        system = self._find_in_path()
        if system and self._validate(system):
            self._path = system
            self._version = self._get_version(system)
            return {"path": system, "version": self._version, "ok": True, "source": "system"}

        logger.warning("[ScreenCast] FFmpeg not found; screen casting unavailable")
        return {"path": None, "version": None, "ok": False, "error": "FFmpeg 未找到，请在设置中指定 ffmpeg 路径"}

    def get_path(self) -> Optional[str]:
        if self._path is None:
            self.check()
        return self._path

    def set_path(self, path: str) -> Dict:
        """手动指定 FFmpeg 路径"""
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return {"ok": False, "error": f"文件不存在: {path}"}
        if not self._validate(path):
            return {"ok": False, "error": "不是有效的 FFmpeg 可执行文件"}
        self._path = path
        self._version = self._get_version(path)
        if self.config:
            screencast_cfg = dict(self.config.get("screencast", {}))
            screencast_cfg["ffmpeg_path"] = path
            self.config.set("screencast", screencast_cfg)
            self.config.save()
        return {"ok": True, "path": path, "version": self._version}

    def _validate(self, path: str) -> bool:
        try:
            result = _run_utf8([path, "-version"], 5)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def _get_version(path: str) -> Optional[str]:
        try:
            result = _run_utf8([path, "-version"], 5)
            first = (result.stdout or "").splitlines()
            if first:
                return first[0].strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        return None

    # ────────────────────────────────────────
    # 捕获源枚举
    # ────────────────────────────────────────

    def list_sources(self) -> Dict:
        """枚举屏幕 / 窗口 / 音频设备

        返回:
            {
              "screens": [{"id": "desktop"|"screen:x,y,w,h", "title": str, ...}],
              "windows": [{"id": "window:<title>", "title": "<title>"}],
              "audio_inputs": [{"name": "...", "kind": "mic|system"}],
              "ffmpeg": {...check()},
            }
        """
        screens = [{"id": "desktop", "title": "整个屏幕（所有显示器）"}]
        screens += self._list_monitors()
        windows = self._list_windows()
        audio_inputs = self._list_audio_devices()
        return {
            "screens": screens,
            "windows": windows,
            "audio_inputs": audio_inputs,
            "ffmpeg": self.check(),
            # 前端要靠它决定「macOS 才需要预热麦克风授权」这类平台差异
            "platform": sys.platform,
        }

    def _list_monitors(self) -> List[Dict]:
        """枚举单块显示器（Windows: EnumDisplayMonitors；macOS: CGGetActiveDisplayList）

        多屏机器上「整个屏幕」抓的是所有显示器的并集——现场实测
        4000x2571：既超出电视解码能力，又是奇数高度（x264 直接拒绝初始化），
        两块屏还会被压扁进同一帧。必须能只投其中一块屏。
        """
        if sys.platform == "darwin":
            out: List[Dict] = []
            for d in darwin_displays():
                px_w = round(d["points"][2] * d["scale"])
                px_h = round(d["points"][3] * d["scale"])
                label = f"显示器 {d['screen_no'] + 1} · {px_w}x{px_h}"
                if d["main"]:
                    label += "（主屏）"
                out.append({"id": f"avf:{d['screen_no']}", "title": label,
                            "width": px_w, "height": px_h,
                            "primary": d["main"]})
            return out
        if os.name != "nt":
            return []
        import ctypes
        from ctypes import wintypes

        primary_flag = 1
        monitors: List[Dict] = []

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        ENUM_PROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC,
            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def _callback(hmon, _hdc, _lprc, _lparam):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if not ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                return True
            rect = info.rcMonitor
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width <= 0 or height <= 0:
                return True
            is_primary = bool(info.dwFlags & primary_flag)
            label = f"显示器 {len(monitors) + 1} · {width}x{height}"
            if is_primary:
                label += "（主屏）"
            monitors.append({
                "id": f"screen:{rect.left},{rect.top},{width},{height}",
                "title": label,
                "width": width,
                "height": height,
                "primary": is_primary,
            })
            return True

        try:
            ctypes.windll.user32.EnumDisplayMonitors(
                0, 0, ENUM_PROC(_callback), 0)
        except Exception as e:
            logger.warning(f"[ScreenCast] 显示器枚举失败: {e}")
            return []
        return monitors

    def _list_windows(self) -> List[Dict]:
        """枚举可投的顶层窗口（Windows: EnumWindows；macOS: CGWindowList）"""
        if sys.platform == "darwin":
            # mac 上给的不是标题而是窗口号 + 该窗口的裁剪区域：avfoundation
            # 没有窗口输入，只能抓它所在那块屏再 crop
            return darwin_windows()
        if os.name != "nt":
            return []
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        GW_STYLE, GW_EXSTYLE, GW_OWNER = -16, -20, 4
        WS_CAPTION, WS_THICKFRAME = 0x00C00000, 0x00040000
        WS_EX_APPWINDOW, WS_EX_TOOLWINDOW = 0x00040000, 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
        DWMWA_CLOAKED = 14
        try:
            # 64 位才有 GetWindowLongPtrW；32 位 Python 上退回 GetWindowLongW
            # （STYLE/EXSTYLE 本身是 32 位值，两个 API 读出来一样）
            _get_long = user32.GetWindowLongPtrW
        except AttributeError:
            _get_long = user32.GetWindowLongW
        _get_long.restype = ctypes.c_uint32
        _get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindow.restype = wintypes.HWND
        try:
            dwm = ctypes.windll.dwmapi
        except Exception:  # noqa: BLE001 - 没有 dwmapi 就当没有虚拟桌面遮挡
            dwm = None

        vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

        def _castable(hwnd, rect) -> bool:
            """这窗口是不是用户眼里「一个能投的程序窗口」——判据全部来自 .68 实测

            裸 EnumWindows 交出来的东西比想象脏：Tauri/框架的影子输入窗口
            （com.han.hancast-siw 之类，16x16、WS_EX_NOACTIVATE|TOOLWINDOW|LAYERED
            |TRANSPARENT）、CHeadBarWindow（16x120 悬浮条）、WinDisplay（停在
            -32000,-32000 的隐形窗口）……它们 IsWindowVisible 为真、有标题、有尺寸，
            用户在任务栏里一个都看不到，选到了就是一片黑。
            反过来，真在用的窗口（cmd、浏览器、抖音、HanCast）都带 WS_CAPTION 或
            WS_THICKFRAME，且不是 NOACTIVATE/TOOLWINDOW。
            """
            ex = _get_long(hwnd, GW_EXSTYLE)
            style = _get_long(hwnd, GW_STYLE)
            if ex & WS_EX_NOACTIVATE:
                return False        # 框架垫片：点不着，也就不是用户想投的那个窗口
            if (ex & WS_EX_TOOLWINDOW) and not (ex & WS_EX_APPWINDOW):
                return False        # 工具窗口/悬浮条（任务栏同样不显示）
            if not (style & (WS_CAPTION | WS_THICKFRAME)):
                return False        # 没有标题栏也没有可缩放边框 = 不是主窗口形态
            x, y, w, h = rect
            if w < 80 and h < 80:
                return False        # 16x16 那类隐形垫片
            if vw and vh and (x + w <= vx or y + h <= vy
                              or x >= vx + vw or y >= vy + vh):
                return False        # 整块在虚拟桌面之外（--32000 的隐形窗口）
            if dwm:
                cloaked = ctypes.c_int(0)
                if dwm.DwmGetWindowAttribute(
                        ctypes.c_void_p(hwnd), DWMWA_CLOAKED,
                        ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0:
                    # 另一个虚拟桌面上的窗口对 gdigrab 是不可见的（BitBlt 全黑）
                    return cloaked.value == 0
            if user32.GetWindow(hwnd, GW_OWNER):
                # 子对话框默认不进任务栏，除非自己声明了 APPWINDOW
                return bool(ex & WS_EX_APPWINDOW)
            return True

        windows = []

        def _callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.IsIconic(hwnd):  # 最小化：gdigrab 抓不到内容
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if not title:
                return True
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            size = (rect.right - rect.left, rect.bottom - rect.top)
            if size[0] <= 0 or size[1] <= 0:
                return True
            if not _castable(hwnd, (rect.left, rect.top, *size)):
                return True
            windows.append({"id": f"window:{title}", "title": title})
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(_callback), 0)
        return windows

    def refresh_window(self, source: Dict) -> Optional[str]:
        """macOS：出发前重新取一次窗口几何，返回错误说明（能投则 None）

        列表是用户几分钟前打开面板时枚举的，窗口随时可能被关闭、最小化
        或挪到另一块屏 —— 拿旧坐标 crop 出来的是错位的一块。
        """
        num = str(source.get("id") or "").split(":")[-1]
        for w in darwin_windows():
            if str(w["id"]).endswith(f":{num}"):
                source["crop"] = w["crop"]
                source["crop_norm"] = w["crop_norm"]
                source["screen_no"] = w["screen_no"]
                return None
        return "选中的窗口已关闭或被最小化，请重新选择"

    def _list_audio_devices(self) -> List[Dict]:
        """枚举音频输入设备（Windows: dshow；macOS: avfoundation）"""
        ffmpeg = self.get_path()
        if not ffmpeg:
            return []

        devices: List[Dict] = []
        if os.name == "nt":
            # ffmpeg -list_devices true -f dshow -i dummy
            #
            # 设备行有两种形态，.68 实测（打包的 6.1.1 与系统 8.1.2 一模一样）：
            # 现在的 build 根本不打印 "DirectShow audio devices" 分段头 —— 那条
            # 字符串压根不在 exe 里（findstr 验过），只有设备行自带类型后缀
            #     [dshow @ ..] "麦克风 (Realtek(R) Audio)" (audio)
            # 早先只按分段头切段，Windows 上永远切不进 audio 段，选择器里一个
            # 麦克风都没有，用户看到的「麦克风没声音」就是这么来的。
            try:
                result = _run_utf8(
                    [ffmpeg, "-hide_banner", "-list_devices", "true",
                     "-f", "dshow", "-i", "dummy"], 8)
                output = (result.stderr or "") + (result.stdout or "")
                lines = output.splitlines()
                current_kind = None
                import re
                # 名称里可以带括号和引号，所以贪婪取「首个引号到类型后缀前的末引号」
                typed = re.compile(r'"(.+)"\s*\((audio|video|none)\)\s*$')
                named = re.compile(r'"([^"]+)"')
                for line in lines:
                    stripped = line.strip()
                    if "DirectShow video devices" in stripped:
                        current_kind = "video"
                        continue
                    if "DirectShow audio devices" in stripped:
                        current_kind = "audio"
                        continue
                    # Alternative name 行也带引号，分段头格式下会伪装成设备混进来
                    if "Alternative name" in stripped:
                        continue
                    m = typed.search(stripped)
                    if m:
                        if m.group(2) != "audio":
                            continue
                        name = m.group(1)
                    elif current_kind == "audio":
                        # 老/其它 build：靠分段头认类型
                        match = named.search(stripped)
                        if not match:
                            continue
                        name = match.group(1)
                    else:
                        continue
                    if name.lower() == "dummy":
                        continue
                    devices.append({"name": name,
                                    "kind": self._classify_audio_device(name)})
            except Exception as e:
                logger.warning(f"Failed to enumerate audio devices: {e}")

            # dshow 只能看见驱动自己注册的「立体声混音」，绝大多数机器没有；
            # 补上 WASAPI 环回（每个渲染设备一个镜像），用户选到的就是
            # 「此刻真正在出声的那块喇叭」，不用装任何驱动
            try:
                from .loopback import loopback_devices
                for d in loopback_devices():
                    devices.append({"name": d["name"], "kind": "system",
                                    "loopback": True, "rate": d["rate"],
                                    "channels": d["channels"],
                                    "default": bool(d.get("default"))})
            except Exception as e:
                logger.warning(f"Failed to enumerate WASAPI loopback devices: {e}")
        elif sys.platform == "darwin":
            # macOS 14.2+ 用 CoreAudio Audio Tap 现建一个「HanCast 系统声音」输入
            # 设备（见 screencast/macos_tap.py）：不装驱动也能抓到本机正在出声的
            # 声音。建不出来（14.1 及以下、被系统拒绝）就照旧只列物理设备，
            # 装了 BlackHole 的用户那条路不受影响。
            tap_ok = False
            try:
                from .macos_tap import ensure_device, is_tap_device
                tap_ok = ensure_device()
            except Exception as e:  # noqa: BLE001 - 采集能力探测不能拖垮枚举
                logger.info(f"[ScreenCast] Audio Tap 探测失败: {e}")
                tap_ok = False

                def is_tap_device(_name: str) -> bool:
                    return False
            # avfoundation: ffmpeg -f avfoundation -list_devices true -i ""
            try:
                result = _run_utf8(
                    [ffmpeg, "-hide_banner", "-f", "avfoundation",
                     "-list_devices", "true", "-i", ""], 8)
                output = (result.stderr or "") + (result.stdout or "")
                section = None
                for line in output.splitlines():
                    stripped = line.strip()
                    if "AVFoundation video devices" in stripped:
                        section = "video"
                        continue
                    if "AVFoundation audio devices" in stripped:
                        section = "audio"
                        continue
                    if section != "audio":
                        continue
                    # 形如: [1] BlackHole 2ch（前面可能带 [AVFoundation indev @ ...] 前缀）
                    import re
                    match = re.search(r"\[(\d+)\]\s*(.*)", stripped)
                    if match:
                        name = match.group(2).strip()
                        if not name:
                            continue
                        kind = self._classify_audio_device(name)
                        entry = {"name": f"{match.group(1)}:{name}", "kind": kind}
                        # 环回设备的索引随其他设备增减而变，标记出来让调用方
                        # 每次按名称重新解析
                        if tap_ok and is_tap_device(name):
                            entry["tap"] = True
                        devices.append(entry)
            except Exception as e:
                logger.warning(f"Failed to enumerate avfoundation devices: {e}")
        return devices

    def audio_input_silent(self, device: str, seconds: float = 1.0) -> Optional[bool]:
        """试采一小段，判断这条输入到底有没有信号

        True  = 采到的每一个字节都是 0（数字静音）；权限没给、设备被系统静音时
                就是这个形态——设备照样枚举得到，投出去却永远是哑的；
        False = 有信号；
        None  = 探测本身没跑成（ffmpeg 缺失、设备被占用、超时），调用方不该据此拦人。
        """
        ffmpeg = self.get_path()
        if not ffmpeg or not device:
            return None
        if os.name == "nt":
            src = ["-f", "dshow", "-i", f"audio={device}"]
        elif sys.platform == "darwin":
            # 设备名是 "索引:名称"，avfoundation 只认索引
            src = ["-f", "avfoundation", "-i", f":{str(device).split(':')[0]}"]
        else:
            return None
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *src,
               "-t", f"{seconds}", "-ar", "48000", "-ac", "1",
               "-c:a", "pcm_s16le", "-f", "wav", "-"]
        try:
            r = subprocess.run(
                cmd, capture_output=True, timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except Exception as e:  # noqa: BLE001 - 探测失败不拦投屏
            logger.info(f"[ScreenCast] 音频信号探测异常: {e}")
            return None
        if r.returncode != 0 or not r.stdout:
            logger.info(f"[ScreenCast] 音频信号探测不可用: "
                        f"{(r.stderr or b'').decode('utf-8', 'replace').strip()[-160:]}")
            return None
        try:
            with wave.open(io.BytesIO(r.stdout), "rb") as w:
                data = w.readframes(w.getnframes())
        except Exception:  # noqa: BLE001
            return None
        if len(data) < 8000:      # 不到 0.08 秒，样本太少不作判据
            return None
        return not any(data)

    @staticmethod
    def _classify_audio_device(name: str) -> str:
        """粗分类音频设备：麦克风 vs 系统声音/立体声混音/虚拟声卡"""
        lower = name.lower()
        sys_keywords = [
            "stereo mix", "立体声混音", "what u hear", "loopback",
            "virtual", "虚拟", "vb-audio", "cable", "wave out",
            "blackhole", "black hole",  # macOS BlackHole 虚拟声卡
            "hancast", "系统声音",  # macOS Audio Tap 自建环回（macos_tap.py）
            "混音", "输出", "speaker", "扬声器", "音响",
        ]
        for kw in sys_keywords:
            if kw in lower:
                return "system"
        return "mic"

    # ────────────────────────────────────────
    # 构造 FFmpeg 命令行（输入段）
    # ────────────────────────────────────────

    @staticmethod
    def build_scale_filter(width: int = 0, height: int = 0) -> str:
        """构造缩放滤镜：压到电视可解的上限，并强制宽高为偶数。

        x264 拒绝奇数尺寸（"width not divisible by 2" 后整个编码进程退出），
        而桌面尺寸经常就是奇数——多屏并集实测 4000x2571，Windows 显示缩放
        也会给出 1917x1081 这类值；force_divisible_by=2 是唯一兜底，
        decrease 保证比上限小的源不被放大重编码。
        """
        w = int(width) if int(width or 0) > 0 else int(param("screencast.max_width"))
        h = int(height) if int(height or 0) > 0 else int(param("screencast.max_height"))
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease"
                ":force_divisible_by=2")

    @staticmethod
    def build_video_input(source: Dict, fps: int, scale_vf: str = "",
                          ffmpeg_path: str = "") -> Tuple[List[str], str]:
        """构造视频捕获输入参数（gdigrab / avfoundation / x11grab）

        source: {"type": "screen"|"window",
                 "id": "desktop"|"screen:x,y,w,h"|"window:<title>", "title": str}
        fps: 目标帧率
        scale_vf: 缩放滤镜串（见 build_scale_filter），窗口抓取时 crop 排在它前面
        ffmpeg_path: 用于 macOS 动态枚举屏幕设备索引

        返回 (输入参数, 视频滤镜串)。滤镜串**必须由调用方放到所有 -i 之后**
        当输出选项下发：ffmpeg 把 `-i` 之前的选项判给「下一个输入」，夹在中间的
        -vf 会落到麦克风输入上，直接
        「Option vf cannot be applied to input url audio=…」EINVAL 退出——
        这正是选了麦克风/系统声音就投不起来的原因（2026-10-14 .68 实据）。
        """
        fps = int(fps or 15)
        args: List[str] = []
        vf = scale_vf
        if os.name == "nt":
            args += ["-f", "gdigrab"]
            if source.get("type") == "window":
                title = source.get("title") or source.get("id", "").replace("window:", "", 1)
                # -framerate 是 gdigrab 的选项，必须排在 -i 前面：不给的话
                # gdigrab 默认 1fps，窗口投屏就变成幻灯片
                args += ["-framerate", str(fps), "-i", f"title={title}"]
            else:
                args += ["-framerate", str(fps)]
                region = _parse_screen_region(source.get("id") or "")
                if region:
                    x, y, w, h = region
                    args += ["-offset_x", str(x), "-offset_y", str(y),
                             "-video_size", f"{w}x{h}"]
                args += ["-i", "desktop"]
        elif sys.platform == "darwin":
            # avfoundation 屏幕设备索引不固定（摄像头排在屏幕前面），动态枚举
            crop = ((source.get("crop_norm") or source.get("crop"))
                    if source.get("type") == "window" else None)
            screen_no = (int(source.get("screen_no") or 0) if crop
                         else _avf_screen_no(source.get("id") or ""))
            idx = avfoundation_screen_index(
                ffmpeg_path, screen_no) if ffmpeg_path else None
            if idx is None:
                idx = 1
                logger.warning(
                    "[ScreenCast] 未探测到 avfoundation 屏幕设备，回退索引 1（可能是摄像头）")
            # 屏幕设备的缓冲区时间戳不可靠（实测可让 raw 管道冲到
            # 5000fps），改用到达墙钟时间作为时间戳
            args += ["-use_wallclock_as_timestamps", "1",
                     "-f", "avfoundation", "-framerate", str(
                     fps), "-i", f"{idx}:none"]
            if crop:
                # 窗口抓取 = 抓它所在那块屏再裁出窗口区域（FFmpeg 在 mac 上
                # 没有窗口输入，见 avfoundation 选项表）
                vf = build_crop_filter(crop) + (f",{vf}" if vf else "")
        else:
            args += ["-f", "x11grab", "-framerate", str(fps),
                     "-i", os.environ.get("DISPLAY", ":0")]

        return args, vf

    @staticmethod
    def _mix_filter(labels: List[str]) -> List[str]:
        """多路音频混音的 -filter_complex 串

        每路先 aformat 规整成 48k/立体声/s16 再 amix：amix 要求各输入的采样率、
        声道、采样格式一致，而 dshow 麦克风给的是设备原生格式（44.1k 单声道很
        常见），WASAPI loopback 又跟着渲染设备走（48k 立体声），直接串起来
        混音图建不起来，FFmpeg 报 "Input formats not supported"。
        """
        graph = ";".join(
            f"{lab}aformat=sample_fmts=s16:sample_rates=48000"
            f":channel_layouts=stereo[m{i}]"
            for i, lab in enumerate(labels))
        graph += ";" + "".join(f"[m{i}]" for i in range(len(labels)))
        # normalize=0：默认的归一会把两路各砍一半音量，实测人声明显发虚
        graph += (f"amix=inputs={len(labels)}:duration=longest"
                  ":dropout_transition=0:normalize=0")
        return ["-filter_complex", graph]

    @staticmethod
    def build_audio_input(audio_cfg: Dict, input_offset: int = 0) -> List[str]:
        """构造音频捕获输入参数（含多路混音 filter）

        audio_cfg: {"mic": bool, "mic_device": str|None, "system": bool,
                    "system_device": str|None, "_loopback": dict|None}
        input_offset: 音频输入在整条命令中的输入序号（HLS 模式视频占 0，
        音频从 1 起）——amix 标签须按实际输入序号引用，否则会指向无音频流的
        视频输入导致 FFmpeg 启动失败。
        返回 [] 表示不捕获音频；返回的最后若含 -filter_complex 则由调用方拼接。
        """
        use_mic = bool(audio_cfg.get("mic"))
        use_system = bool(audio_cfg.get("system"))
        if not use_mic and not use_system:
            return []

        args: List[str] = []
        labels: List[str] = []
        idx = input_offset

        def take(input_args: List[str]) -> None:
            """追加一个输入，并记下它的音频流标签（amix 要按实际序号引用）"""
            nonlocal idx
            args.extend(input_args)
            labels.append(f"[{idx}:a]")
            idx += 1

        if os.name == "nt":
            if use_mic and audio_cfg.get("mic_device"):
                take(["-f", "dshow", "-i", f"audio={audio_cfg['mic_device']}"])
            if use_system:
                lb = audio_cfg.get("_loopback")
                if lb:
                    # WASAPI 环回不是一种输入设备，ffmpeg 里没有它——由 sidecar
                    # 自己采成 s16le，从 stdin 灌进来（见 screencast/loopback.py）
                    take(["-f", "s16le", "-ar", str(int(lb["rate"])),
                          "-ac", str(int(lb["channels"])), "-i", "pipe:0"])
                elif audio_cfg.get("system_device"):
                    take(["-f", "dshow", "-i",
                          f"audio={audio_cfg['system_device']}"])
            if len(labels) > 1:
                args += FfmpegManager._mix_filter(labels)
        elif sys.platform == "darwin":
            # avfoundation 音频：
            #   - 麦克风：物理输入设备（如 "1:MacBook Pro麦克风"）
            #   - 系统声音：优先 「HanCast 系统声音」（Audio Tap，枚举时自建，
            #     见 macos_tap.py）；没装 BlackHole 也不用装虚拟声卡。
            #     仍是 BlackHole 的用户照旧可选：它需要自己在「音频 MIDI 设置」
            #     里把系统输出路由进去。
            mic_idx = None
            sys_idx = None
            if use_mic and audio_cfg.get("mic_device"):
                mic_idx = str(audio_cfg["mic_device"]).split(":")[0]
            if use_system and audio_cfg.get("system_device"):
                sys_idx = str(audio_cfg["system_device"]).split(":")[0]

            if mic_idx and sys_idx:
                # 双路输入（avfoundation 单输入仅支持一个音频设备，需两个输入 + amix）
                take(["-f", "avfoundation", "-i", f":{mic_idx}"])
                take(["-f", "avfoundation", "-i", f":{sys_idx}"])
                args += FfmpegManager._mix_filter(labels)
            elif mic_idx:
                take(["-f", "avfoundation", "-i", f":{mic_idx}"])
            elif sys_idx:
                logger.info(f"[ScreenCast] 捕获系统声音设备 {sys_idx}")
                take(["-f", "avfoundation", "-i", f":{sys_idx}"])
            else:
                logger.warning("[ScreenCast] macOS audio requested but no valid device selected")
        else:
            if use_mic:
                take(["-f", "pulse", "-i", "default"])
        return args
