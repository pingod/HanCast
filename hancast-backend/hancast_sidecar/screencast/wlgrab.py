"""
Wayland 屏幕抓取 helper（hancast-wlgrab）的定位与会话管理

为什么需要它（2026-10-17 在 CachyOS / Hyprland 0.56.2 取证）：
    Linux 侧以前统一走 `-f x11grab -i $DISPLAY`，而 Hyprland 的 XWayland 以
    `-rootless` 启动——原生 Wayland 窗口根本不在 X 的窗口树里。x11grab 抓到的
    是那面「空的 root window」：投屏启动成功、画面流畅，每一帧却是纯黑的
    （实测 signalstats YAVG=16.03，同一时刻 grim 走 wlroots screencopy 抓到
    的是 44.28）。用户只说得出「黑屏」，日志里一点异常都没有。
    ffmpeg n9.0.2 里没有任何 Wayland 输入设备（`-demuxers` 只有 x11grab /
    kmsgrab / fbdev），pipewire 抓取要的是 portal + libcamera 那条路，本机
    ffmpeg 也没编。所以只能自带一个原生 helper：用 `zwlr_screencopy_manager_v1`
    （grim / wf-recorder 同一套协议）把帧按 BGRA 写进 stdout，ffmpeg 用
    `-f rawvideo -pixel_format bgra -video_size WxH -i pipe:0` 接上，后面整条
    HLS 管线与 Windows/macOS 完全共用。

helper 与 sidecar 的接口（源码 linux-capture/src/main.rs）：
    hancast-wlgrab list                      → stdout 一行 JSON 数组
    hancast-wlgrab capture [--output NAME] [--fps N]
                           [--x X --y Y --width W --height H] [--cursor]
        stdout：每帧 width*height*4 字节的紧凑 BGRA，无分隔
        stderr：首行 `HDR {...}`（width/height/stride/pix_fmt/output/scale/y_invert），
                结束时 `DONE n 帧`，失败时 `ERR <原因>`
    退出码：0 正常 / 2 参数错 / 3 连不上合成器（无 Wayland、无 screencopy、无 wl_shm）
            / 4 首帧抓不到 / 5 合成器给的像素格式不是 ARGB/XRGB

定位方式与随包 mpv 一样：**光看文件在不在不算数**（v2.3.17 的教训——随包 mpv
在 Arch 上缺 13 条 soname，存在性检查全绿，装端照样 exit 127）。这里对每个候选
真的跑一次 `list`，能出合法 JSON 才认。
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("hancast.screencast.wlgrab")

HELPER_NAME = "hancast-wlgrab"
_LIST_TIMEOUT = 5
_HDR_TIMEOUT = 10
_OUTPUTS_TTL = 3.0


def wayland_session() -> bool:
    """当前进程是否处在一个 Wayland 会话里（X11 会话继续走 x11grab）"""
    return sys.platform == "linux" and bool(os.environ.get("WAYLAND_DISPLAY"))


def _candidate_paths() -> List[str]:
    """helper 可能在的位置（打包布局 → 开发环境）

    装端（deb/rpm/Arch 包）把 resources 整体放进 /usr/lib/HanCast/，
    helper 与 hancast-sidecar、hancast_sidecar/ 同级；开发环境则可能躺在
    linux-capture/target/release/（本地 cargo build）或 src-tauri/（构建暂存）。
    """
    out: List[str] = []
    env = os.environ.get("HANCAST_WLGRAB")
    if env:
        out.append(env)

    def add(dir_path: str) -> None:
        if not dir_path:
            return
        p = os.path.join(dir_path, HELPER_NAME)
        if p not in out:
            out.append(p)

    add(os.getcwd())                       # Rust 设置的 current_dir（= resource_dir）
    add(os.path.dirname(sys.executable))   # sidecar 可执行文件同级
    here = os.path.abspath(__file__)
    # <root>/hancast_sidecar/screencast/wlgrab.py → <root> 就是 resources 那一层
    add(os.path.dirname(os.path.dirname(os.path.dirname(here))))
    if os.path.isabs(here):
        # 开发环境：…/HanCast/hancast-backend/hancast_sidecar/screencast/
        repo = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(here))))
        add(os.path.join(repo, "linux-capture", "target", "release"))
        add(os.path.join(os.path.dirname(repo), "src-tauri"))
    return out


def _run_list(path: str) -> Tuple[Optional[List[Dict]], str]:
    """真的跑一次 `list`：返回值 (显示器列表, 失败原因)

    ⚠️ 不要把 stderr 并进 stdout 再解析——pactl 那种坑在这里是 ffmpeg 系工具
    通用的：helper 的 stdout 只有 JSON，诊断都写在 stderr。
    """
    try:
        r = subprocess.run([path, "list"], capture_output=True,
                           timeout=_LIST_TIMEOUT, text=True,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None, "文件不存在"
    except subprocess.TimeoutExpired:
        return None, f"运行超时（>{_LIST_TIMEOUT}s）"
    except Exception as e:  # noqa: BLE001 - 探测本身不能拖垮枚举
        return None, f"{type(e).__name__}: {e}"
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()
        reason = next((l[4:] for l in tail if l.startswith("ERR ")), None)
        return None, f"退出码 {r.returncode}" + (f"：{reason}" if reason else "")
    try:
        data = json.loads((r.stdout or "").strip() or "[]")
    except Exception as e:  # noqa: BLE001
        return None, f"输出不是合法 JSON: {e}"
    if not isinstance(data, list):
        return None, "输出不是 JSON 数组"
    return data, ""


_probe_lock = threading.Lock()
# (路径, 原因) —— 路径为 None 时原因非空。跑一次 `list` 只要几十毫秒，
# 但枚举会随面板反复刷新，所以整个进程生命周期探一次即可。
_probe_result: Optional[Tuple[Optional[str], str]] = None
# (helper 路径, 采样时刻, 屏幕列表)
_outputs_cache: List = [None, 0.0, []]


def find_helper(force: bool = False) -> Tuple[Optional[str], str]:
    """返回 (可用的 helper 路径, 不可用原因)。路径为 None 时原因必然非空。"""
    global _probe_result
    with _probe_lock:
        if _probe_result is not None and not force:
            return _probe_result
        if force:
            _probe_result = None
            _outputs_cache[0] = None
        failures: List[str] = []
        for path in _candidate_paths():
            if not os.path.isfile(path):
                continue
            outputs, reason = _run_list(path)
            if outputs is not None:
                logger.info(f"[ScreenCast] Wayland helper 就绪: "
                            f"{path}（{len(outputs)} 块屏）")
                _probe_result = (path, "")
                return _probe_result
            logger.info(f"[ScreenCast] Wayland helper 不可用 {path}: {reason}")
            failures.append(f"{path} → {reason}")
        if failures:
            # 逐个候选的真实死因要带给用户：「文件在但跑不起来」（装包的 soname
            # 缺失、合成器不给 screencopy）和「压根没打进包」的解法完全不同，
            # 只说「未找到」等于把 v2.3.17 随包 mpv 那个坑又踩一遍。
            why = "hancast-wlgrab 存在但不可用（已试：" + "；".join(failures) + "）"
        else:
            why = ("未随包找到 hancast-wlgrab：安装包 resources 里缺这个 helper，"
                   "或它不在 sidecar 同级目录")
        _probe_result = (None, why)
        return _probe_result


def status() -> Dict:
    """给枚举与错误文案用的能力快照（不抛异常）"""
    wl = wayland_session()
    if not wl:
        return {"wayland": False, "helper": None, "reason": ""}
    path, reason = find_helper()
    return {"wayland": True, "helper": path, "reason": reason}


def list_outputs(force: bool = False) -> List[Dict]:
    """屏幕列表（带 3 秒缓存：面板反复刷新别每次都起一个进程问合成器）"""
    path, _ = find_helper()
    if not path:
        return []
    now = time.monotonic()
    with _probe_lock:
        cached = _outputs_cache
        if (not force and cached and cached[0] == path
                and now - cached[1] < _OUTPUTS_TTL):
            return list(cached[2])
    outputs, reason = _run_list(path)
    if outputs is None:
        logger.warning(f"[ScreenCast] Wayland 显示器枚举失败: {reason}")
        return []
    with _probe_lock:
        _outputs_cache[0] = (path, now, list(outputs))
    return outputs


def _primary_index(outputs: List[Dict]) -> int:
    """主显示器：合成器坐标原点那块（Hyprland 把主屏摆在 0,0），退化取第一块"""
    for i, o in enumerate(outputs):
        if o.get("x") == 0 and o.get("y") == 0:
            return i
    return 0


def monitor_entries() -> List[Dict]:
    """把 wl_output 列表转成前端 `screens` 的条目（不含 "desktop"）

    Wayland 下 screencopy 一次只能抓一块输出，没有「所有显示器的并集」这种
    东西——所以 "desktop" 的语义在这里等于「主显示器」，标题由调用方写清楚，
    否则多屏用户会以为它和 Windows 上那个 desktop 是一回事。
    """
    outputs = list_outputs()
    if not outputs:
        return []
    primary = _primary_index(outputs)
    entries: List[Dict] = []
    for i, o in enumerate(outputs):
        name = str(o.get("name") or f"输出{i + 1}")
        width = int(o.get("width") or 0)
        height = int(o.get("height") or 0)
        desc = str(o.get("description") or "").strip()
        # 描述形如 "AOC AG273QG3R3B ASMP59A005500 (DP-2)"：型号有用，序列号是噪音
        short = " ".join(desc.split(" (")[0].split(" ")[0:2]).strip()
        label = f"显示器 {i + 1} · {width}x{height}"
        if short:
            label += f"（{short}）"
        if i == primary:
            label += "（主屏）"
        entries.append({"id": f"wl:{name}", "title": label,
                        "width": width, "height": height,
                        "primary": i == primary})
    return entries


def desktop_title() -> Optional[str]:
    """Wayland 下「整个屏幕」这一项该怎么跟用户解释（拿不到屏幕列表就返回 None）"""
    outputs = list_outputs()
    if not outputs:
        return None
    first = outputs[_primary_index(outputs)]
    return (f"整个屏幕（Wayland 只能整块整块地投，这里投主显示器 "
            f"{first.get('name')}）")


def resolve_output(source: Dict) -> Optional[str]:
    """source.id → 要抓的 wl_output 名字（"wl:DP-2" / "desktop" / 未知取主屏）"""
    outputs = list_outputs()
    if not outputs:
        return None
    sid = str((source or {}).get("id") or "")
    if sid.startswith("wl:"):
        want = sid[3:]
        for o in outputs:
            if o.get("name") == want:
                return want
    return outputs[_primary_index(outputs)].get("name")


class WlGrabSession:
    """一路抓取会话：helper 进程 + 它的 stdout（要交给 ffmpeg 当 stdin）

    尺寸必须问合成器，不能问 `list`：wl_output 的 mode 给的是逻辑尺寸，
    缩放屏（scale=2）上 screencopy 的缓冲区是它的整数倍。`-video_size` 写错
    一个字节 ffmpeg 就会把整条流读歪，所以这里以 helper 首行 HDR 为准。
    """

    def __init__(self, path: str):
        self.path = path
        self.proc: Optional[subprocess.Popen] = None
        self.hdr: Optional[Dict] = None
        self.error: Optional[str] = None
        self.tail: List[str] = []
        self._hdr_evt = threading.Event()
        self._reader: Optional[threading.Thread] = None

    def start(self, output: str, fps: int = 15, cursor: bool = False,
              region: Optional[Tuple[int, int, int, int]] = None) -> bool:
        cmd = [self.path, "capture", "--output", output, "--fps", str(int(fps))]
        if cursor:
            cmd.append("--cursor")
        if region:
            x, y, w, h = region
            cmd += ["--x", str(x), "--y", str(y), "--width", str(w),
                    "--height", str(h)]
        env = dict(os.environ)
        # 从某些启动器（DM/服务）拉起来的进程可能没有 XDG_RUNTIME_DIR，
        # helper 的 connect_to_env() 就会判「连不上 Wayland 合成器」
        if not env.get("XDG_RUNTIME_DIR"):
            uid = os.getuid() if hasattr(os, "getuid") else None
            runtime = f"/run/user/{uid}" if uid is not None else ""
            if runtime and os.path.isdir(runtime):
                env["XDG_RUNTIME_DIR"] = runtime
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, env=env, bufsize=0)
        except Exception as e:  # noqa: BLE001
            self.error = f"启动 Wayland 抓取进程失败: {e}"
            return False
        self._reader = threading.Thread(target=self._drain, daemon=True,
                                        name="wlgrab-stderr")
        self._reader.start()
        if not self._hdr_evt.wait(_HDR_TIMEOUT):
            rc = self.proc.poll()
            self.error = ("抓取进程没报出首帧尺寸"
                          + (f"（退出码 {rc}）" if rc is not None else "（超时）")
                          + ("：" + " / ".join(self.tail[-3:]) if self.tail else ""))
            self.stop()
            return False
        if self.hdr is None:
            self.error = "抓取进程报的首帧头无法解析"
            self.stop()
            return False
        return True

    def _drain(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                if line.startswith("HDR ") and self.hdr is None:
                    try:
                        self.hdr = json.loads(line[4:])
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"[ScreenCast] wlgrab HDR 解析失败: {e}")
                    self._hdr_evt.set()
                    continue
                self.tail.append(line)
                del self.tail[:-40]
                logger.debug(f"[ScreenCast] wlgrab: {line}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[ScreenCast] wlgrab stderr 读取结束: {e}")
        finally:
            # 进程死了却没给出 HDR，也要把等待者放出去（走超时分支）
            self._hdr_evt.set()

    @property
    def stdout(self):
        """给 ffmpeg 当 stdin 的文件对象（Popen 会在子进程里 dup 这个读端）"""
        return self.proc.stdout if self.proc else None

    def close_stdout(self) -> None:
        """ffmpeg 起来之后关掉父进程这份读端。

        不关的话 helper 退出时 ffmpeg 收不到 EOF——父进程还握着同一个管道，
        于是编码端会一直等，停止投屏变成等满超时。
        """
        if self.proc is not None and self.proc.stdout is not None:
            try:
                self.proc.stdout.close()
            except Exception:
                pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def exit_code(self) -> Optional[int]:
        return self.proc.poll() if self.proc is not None else None

    def stop(self) -> None:
        """先关喂料（stdout）再收进程（幂等）"""
        proc = self.proc
        if proc is None:
            return
        self.close_stdout()
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
        if proc.stderr is not None:
            try:
                proc.stderr.close()
            except Exception:
                pass
        if self._reader is not None:
            self._reader.join(timeout=1)
            self._reader = None
