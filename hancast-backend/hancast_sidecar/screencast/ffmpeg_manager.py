"""
FFmpeg 管理器 — 检测 / 路径管理 / 捕获源枚举

查找顺序与 MPV 一致：配置路径 → 捆绑目录（ffmpeg/）→ 系统 PATH。
同时提供屏幕/窗口/音频设备的枚举能力（供前端选择投屏源）。
"""

import os
import re
import sys
import json
import logging
import threading
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger("hancast.screencast.ffmpeg")

# avfoundation 屏幕设备索引缓存（ffmpeg_path -> index，None 表示未找到）
_avf_screen_cache: Dict[str, Optional[int]] = {}
_avf_screen_lock = threading.Lock()

# 默认输出上限。多屏机器的「整个屏幕」是所有显示器的并集（现场实测
# 4000x2571），既超出电视解码能力（Main profile 到 1080p 最稳），又会被
# 2M 码率压成马赛克；奇数高度还会让 x264 直接拒绝初始化。
DEFAULT_MAX_WIDTH = 1920
DEFAULT_MAX_HEIGHT = 1080


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


def avfoundation_screen_index(ffmpeg_path: str) -> Optional[int]:
    """枚举 avfoundation 视频设备，返回 "Capture screen" 屏幕设备的索引。

    macOS 上摄像头排在屏幕前面，索引随外接摄像头/虚拟相机数量变化，
    不能硬编码（曾固定用 1，在装有 OBS Virtual Camera 的机器上抓到的是
    摄像头而不是屏幕）。找不到屏幕设备返回 None。
    """
    with _avf_screen_lock:
        if ffmpeg_path in _avf_screen_cache:
            return _avf_screen_cache[ffmpeg_path]

    idx: Optional[int] = None
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
                idx = int(match.group(1))
                break
    except Exception as e:
        logger.warning(f"Failed to detect avfoundation screen device: {e}")

    with _avf_screen_lock:
        _avf_screen_cache[ffmpeg_path] = idx
    if idx is None:
        logger.warning("avfoundation 未枚举到屏幕捕获设备（Capture screen）")
    else:
        logger.info(f"avfoundation screen capture device index: {idx}")
    return idx


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
        }

    def _list_monitors(self) -> List[Dict]:
        """枚举单块显示器（Windows，ctypes EnumDisplayMonitors）

        多屏机器上「整个屏幕」抓的是所有显示器的并集——现场实测
        4000x2571：既超出电视解码能力，又是奇数高度（x264 直接拒绝初始化），
        两块屏还会被压扁进同一帧。必须能只投其中一块屏。
        """
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
        """枚举可见顶层窗口（Windows，ctypes EnumWindows）"""
        if os.name != "nt":
            return []
        import ctypes
        from ctypes import wintypes

        windows = []

        def _callback(hwnd, _lparam):
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            if ctypes.windll.user32.IsIconic(hwnd):  # 最小化
                return True
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if not title:
                return True
            # 跳过无尺寸的窗口（隐藏/不可见辅助窗口）
            rect = wintypes.RECT()
            if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            if (rect.right - rect.left) <= 0 or (rect.bottom - rect.top) <= 0:
                return True
            windows.append({"id": f"window:{title}", "title": title})
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        ctypes.windll.user32.EnumWindows(WNDENUMPROC(_callback), 0)
        return windows

    def _list_audio_devices(self) -> List[Dict]:
        """枚举音频输入设备（Windows: dshow；macOS: avfoundation）"""
        ffmpeg = self.get_path()
        if not ffmpeg:
            return []

        devices: List[Dict] = []
        if os.name == "nt":
            # ffmpeg -list_devices true -f dshow -i dummy
            try:
                result = _run_utf8(
                    [ffmpeg, "-hide_banner", "-list_devices", "true",
                     "-f", "dshow", "-i", "dummy"], 8)
                output = (result.stderr or "") + (result.stdout or "")
                lines = output.splitlines()
                current_kind = None
                for line in lines:
                    stripped = line.strip()
                    if "DirectShow video devices" in stripped:
                        current_kind = "video"
                        continue
                    if "DirectShow audio devices" in stripped:
                        current_kind = "audio"
                        continue
                    if current_kind == "audio" and '"' in stripped:
                        # 形如: "麦克风 (Realtek)" "Microphone (Realtek Audio)"
                        import re
                        match = re.search(r'"([^"]+)"', stripped)
                        if match:
                            name = match.group(1)
                            kind = self._classify_audio_device(name)
                            devices.append({"name": name, "kind": kind})
            except Exception as e:
                logger.warning(f"Failed to enumerate audio devices: {e}")
        elif sys.platform == "darwin":
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
                        devices.append({"name": f"{match.group(1)}:{name}", "kind": kind})
            except Exception as e:
                logger.warning(f"Failed to enumerate avfoundation devices: {e}")
        return devices

    @staticmethod
    def _classify_audio_device(name: str) -> str:
        """粗分类音频设备：麦克风 vs 系统声音/立体声混音/虚拟声卡"""
        lower = name.lower()
        sys_keywords = [
            "stereo mix", "立体声混音", "what u hear", "loopback",
            "virtual", "虚拟", "vb-audio", "cable", "wave out",
            "blackhole", "black hole",  # macOS BlackHole 虚拟声卡
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
        w = int(width) if int(width or 0) > 0 else DEFAULT_MAX_WIDTH
        h = int(height) if int(height or 0) > 0 else DEFAULT_MAX_HEIGHT
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease"
                ":force_divisible_by=2")

    @staticmethod
    def build_video_input(source: Dict, fps: int, vf: str = "",
                          ffmpeg_path: str = "") -> List[str]:
        """构造视频捕获输入参数（gdigrab / avfoundation / x11grab）

        source: {"type": "screen"|"window",
                 "id": "desktop"|"screen:x,y,w,h"|"window:<title>", "title": str}
        fps: 目标帧率
        vf: 完整 -vf 滤镜串（见 build_scale_filter）
        ffmpeg_path: 用于 macOS 动态枚举屏幕设备索引
        返回参数列表，不包含输出/编码段。
        """
        fps = int(fps or 15)
        args: List[str] = []
        if os.name == "nt":
            args += ["-f", "gdigrab"]
            if source.get("type") == "window":
                title = source.get("title") or source.get("id", "").replace("window:", "", 1)
                args += ["-i", f"title={title}"]
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
            idx = avfoundation_screen_index(
                ffmpeg_path) if ffmpeg_path else None
            if idx is None:
                idx = 1
                logger.warning(
                    "[ScreenCast] 未探测到 avfoundation 屏幕设备，回退索引 1（可能是摄像头）")
            # 屏幕设备的缓冲区时间戳不可靠（实测可让 raw 管道冲到
            # 5000fps），改用到达墙钟时间作为时间戳
            args += ["-use_wallclock_as_timestamps", "1",
                     "-f", "avfoundation", "-framerate", str(
                     fps), "-i", f"{idx}:none"]
        else:
            args += ["-f", "x11grab", "-framerate", str(fps),
                     "-i", os.environ.get("DISPLAY", ":0")]

        if vf:
            args += ["-vf", vf]
        return args

    @staticmethod
    def build_audio_input(audio_cfg: Dict, input_offset: int = 0) -> List[str]:
        """构造音频捕获输入参数（含多路混音 filter）

        audio_cfg: {"mic": bool, "mic_device": str|None, "system": bool, "system_device": str|None}
        input_offset: 音频输入在整条命令中的输入序号（HLS 模式视频占 0，
        音频从 1 起；raw 模式音频是独立进程，从 0 起）——amix 标签须按
        此偏移引用，否则会指向无音频流的视频输入导致 FFmpeg 启动失败。
        返回 [] 表示不捕获音频；返回的最后若含 -filter_complex 则由调用方拼接。
        """
        use_mic = bool(audio_cfg.get("mic"))
        use_system = bool(audio_cfg.get("system"))
        if not use_mic and not use_system:
            return []

        args: List[str] = []
        if os.name == "nt":
            devices = []
            if use_mic and audio_cfg.get("mic_device"):
                devices.append(("mic", audio_cfg["mic_device"]))
            if use_system and audio_cfg.get("system_device"):
                devices.append(("sys", audio_cfg["system_device"]))
            for _kind, dev in devices:
                args += ["-f", "dshow", "-i", f"audio={dev}"]
            if len(devices) > 1:
                # 两路音频混音；amix 输入标签按音频起始序号引用
                args += ["-filter_complex",
                         f"[{input_offset}:a][{input_offset + 1}:a]"
                         f"amix=inputs=2:duration=longest:dropout_transition=0"]
        elif sys.platform == "darwin":
            # avfoundation 音频：
            #   - 麦克风：物理输入设备（如 "0:Built-in Microphone"）
            #   - 系统声音：需安装 BlackHole 虚拟声卡，并在"音频 MIDI 设置"中把
            #     系统输出路由到 BlackHole（多输出设备）；检测到后直接捕获其输入。
            mic_idx = None
            sys_idx = None
            if use_mic and audio_cfg.get("mic_device"):
                mic_idx = str(audio_cfg["mic_device"]).split(":")[0]
            if use_system and audio_cfg.get("system_device"):
                sys_idx = str(audio_cfg["system_device"]).split(":")[0]

            if mic_idx and sys_idx:
                # 双路输入（avfoundation 单输入仅支持一个音频设备，需两个输入 + amix）
                args += ["-f", "avfoundation", "-i", f":{mic_idx}",
                         "-f", "avfoundation", "-i", f":{sys_idx}"]
                args += ["-filter_complex",
                         f"[{input_offset}:a][{input_offset + 1}:a]"
                         f"amix=inputs=2:duration=longest:dropout_transition=0"]
            elif mic_idx:
                args += ["-f", "avfoundation", "-i", f":{mic_idx}"]
            elif sys_idx:
                logger.info(f"[ScreenCast] Capturing system audio via BlackHole device {sys_idx}")
                args += ["-f", "avfoundation", "-i", f":{sys_idx}"]
            else:
                logger.warning("[ScreenCast] macOS audio requested but no valid device selected")
        else:
            if use_mic:
                args += ["-f", "pulse", "-i", "default"]
        return args
