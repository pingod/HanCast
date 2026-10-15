"""Windows「投屏指定窗口」真机探针（在 .68 的桌面会话里跑，用仓库真实代码）

验证的四件事：
  1. _list_windows() 交出来的窗口，任务栏里是不是真的有 —— 实测裸 EnumWindows 会
     混进 CHeadBarWindow、com.han.hancast-siw 这类 Tauri/框架的影子输入窗口：
     它们可见、有尺寸、有标题、无 owner、没被遮挡，用「任务栏那套判据」滤不掉，
     选到了就是黑屏。--survey 把每个窗口的 style/exstyle/矩形全打出来，判据
     从实测数据里挑，不靠猜；
  2. 列表 id 的形状是 "window:0x<hwnd>"（hwnd 编进 id，标题只作显示）。起播时按
     hwnd 换算窗口矩形、用 `-offset_x/-offset_y/-video_size` 抓 desktop 区域
     （见 ffmpeg_manager._window_capture_rect）——gdigrab 的窗口模式
     （`-i title=`，FindWindowW 精确匹配 + GetDC(hwnd)）在 DWM 下对浏览器这类
     GPU 合成窗口只能抓到纯黑/纯白，且标题一变就 I/O error（.68 实测）；
  3. 抓到的画面尺寸 ≈ 窗口矩形（DWM 可见边框），且不是全黑（探针窗口底色纯白，
     YAVG 应接近 235，任何「抓到黑屏」都藏不住）。注意区域抓取读的是屏幕上那块
     矩形，不是窗口自己的 buffer —— 别的窗口压在它上面，投出去的就是那个压着的
     窗口（实测不置顶时同一份代码两轮亮度 235 vs 45）。抓取前生产代码会尽力把
     窗口置前，探针也会自己置顶；
  4. 已关闭的窗口必须让起播快速失败（可读的中文报错），而不是安静地产出黑屏。

窗口由探针进程自己 RegisterClass + CreateWindowExW 开出来：标题直接进 Win32 API，
不经 shell —— 用 cmd 的 title 命令造带引号的标题会先把探针自己截断。

ssh 会话在 session 0，抓不到桌面，所以本脚本必须由计划任务丢进交互会话：
    schtasks /create /f /tn HANCAST_WINPICK /sc once /sd 2999-01-01 /st 00:00 ^
        /ru Administrator /it /tr "D:\\Temp\\run-winpick.cmd"
    schtasks /run /tn HANCAST_WINPICK
用法：
    run-winpick.cmd --survey     只枚举并打印属性表（快，用来定过滤判据）
    run-winpick.cmd              枚举 + 真抓窗口
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
# 远端是铺平放的：D:\Temp\probe-win-window-picker.py + D:\Temp\pkg\hancast_sidecar
SRC = os.environ.get("HANCAST_SRC") or os.path.join(HERE, "pkg")
sys.path.insert(0, SRC)

from hancast_sidecar.screencast.ffmpeg_manager import FfmpegManager  # noqa: E402

SURVEY = "--survey" in sys.argv
_FF_MAVS = [a for a in sys.argv if a.startswith("--ffmpeg")]
FFMPEG_OVERRIDE = _FF_MAVS[0].split("=", 1)[1] if _FF_MAVS else ""
FAIL: list[str] = []
OUT = ""
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
dwmapi = ctypes.windll.dwmapi
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
# 窗口过程里 WPARAM/LPARAM 在 win64 上会超出 c_int，不声明签名时
# DefWindowProcW 收到高位消息（如鼠标坐标）直接 OverflowError，
# 异常被 ctypes 吞进 stderr，日志里全是假栈。
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                  wintypes.WPARAM, ctypes.c_ssize_t]
GWL_STYLE, GWL_EXSTYLE, GW_OWNER = -16, -20, 4
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 13, 14
WS_EX = {"APPWINDOW": 0x00040000, "TOOLWINDOW": 0x00000080,
         "NOACTIVATE": 0x08000000, "LAYERED": 0x00080000,
         "TRANSPARENT": 0x00000020, "WINDOWEDGE": 0x00000100,
         "CLIENTEDGE": 0x00000200, "NOREDIRECTION": 0x00200000,
         "TOPMOST": 0x00000008, "MDICHILD": 0x00000040,
         "STATICEDGE": 0x00020000, "CONTPARENT": 0x00010000}
WS = {"CAPTION": 0x00C00000, "THICKFRAME": 0x00040000, "POPUP": 0x80000000,
      "CHILD": 0x40000000, "VISIBLE": 0x10000000, "MINIMIZE": 0x20000000,
      "SYSMENU": 0x00080000, "MAXIMIZE": 0x00010000}

for _fn, _res in ((user32.GetWindowLongW, ctypes.c_int),
                  (user32.GetWindow, wintypes.HWND),
                  (user32.GetParent, wintypes.HWND)):
    _fn.restype = _res


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def flags(style: int, table: dict) -> str:
    return "|".join(n for n, bit in table.items() if style & bit) or "0"


def frame_bounds(hwnd) -> tuple:
    """DWM 合成出来的整窗边界（含标题栏）"""
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(hwnd),
                                    DWMWA_EXTENDED_FRAME_BOUNDS,
                                    ctypes.byref(r), ctypes.sizeof(r)) == 0:
        return (r.right - r.left, r.bottom - r.top)
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.right - r.left, r.bottom - r.top)


def client_bounds(hwnd) -> tuple:
    """客户区尺寸（gdigrab 默认抓的就是这块 —— 实测少 16x39，正是边框+标题栏）"""
    r = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(r)):
        return (0, 0)
    pts = (wintypes.POINT * 2)((0, 0), (r.right, r.bottom))
    user32.MapWindowPoints(hwnd, None, pts, 2)
    return (pts[1].x - pts[0].x, pts[1].y - pts[0].y)


def window_table() -> dict:
    """{title: hwnd 属性表}，同名只留第一个（选择器也是按标题取第一个）"""
    out: dict = {}

    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        cloak = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(hwnd), DWMWA_CLOAKED,
                                     ctypes.byref(cloak), ctypes.sizeof(cloak))
        out.setdefault(buf.value, {
            "hwnd": hwnd,
            "style": ctypes.c_int(user32.GetWindowLongW(hwnd, GWL_STYLE)).value,
            "ex": ctypes.c_int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)).value,
            "rect": (r.left, r.top, r.right - r.left, r.bottom - r.top),
            "fb": frame_bounds(hwnd),
            "pid": _pid(hwnd),
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "iconic": bool(user32.IsIconic(hwnd)),
            "owned": bool(user32.GetWindow(hwnd, GW_OWNER)),
            "parent": bool(user32.GetParent(hwnd)),
            "cloaked": cloak.value != 0,
        })
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def _pid(hwnd) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def dump(table: dict, kept: set) -> None:
    print(f"\n{'标题':<26} {'留':<2} {'style':<28} {'exstyle':<38} "
          f"{'rect(x,y,w,h)':<26} {'DWM':<12} own/par/clo")
    for t, v in sorted(table.items(), key=lambda kv: (kv[0] not in kept, kv[0])):
        if not v["visible"] or not t.strip() or min(v["fb"]) <= 0:
            continue
        print(f"  {t[:24]!r:<26} {'留' if t in kept else '·':<2} "
              f"{flags(v['style'], WS):<28} {flags(v['ex'], WS_EX):<38} "
              f"{str(v['rect']):<26} {str(v['fb']):<12} "
              f"{int(v['owned'])}/{int(v['parent'])}/{int(v['cloaked'])} "
              f"iconic={int(v['iconic'])} pid={v['pid']}")


class ProbeWindows:
    """本进程开的纯白窗口，标题精确到每一个字符"""

    CLASS = "HANCAST_WINPICK"
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                                 wintypes.WPARAM, ctypes.c_ssize_t)

    def __init__(self, specs):
        """specs: [(标题, (宽, 高))]；构造完 self.hwnds 里是开成功的窗口"""
        self.specs = specs
        self.hwnds: dict = {}
        self._quit = False
        self._ready = threading.Event()
        self._proc_ref = self.WNDPROC(self._wndproc)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self._ready.wait(15)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self):
        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
                ("lpfnWndProc", self.WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON)]

        # GetModuleHandleW 在 kernel32，不在 user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        hinst = kernel32.GetModuleHandleW(None)
        # GDI 句柄是 64 位指针宽，默认 restype(c_int) 会把它截断
        gdi32.GetStockObject.restype = ctypes.c_void_p
        user32.CreateWindowExW.restype = wintypes.HWND
        style = 0x00CF0000 | 0x10000000  # WS_OVERLAPPEDWINDOW | WS_VISIBLE
        wc = WNDCLASSEXW(ctypes.sizeof(WNDCLASSEXW), 0, self._proc_ref, 0, 0,
                         ctypes.c_void_p(hinst), None, None,
                         ctypes.c_void_p(gdi32.GetStockObject(0)),  # WHITE_BRUSH
                         None, self.CLASS, None)
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            print(f"  RegisterClassExW 失败（GetLastError={user32.GetLastError()}）")
            self._ready.set()
            return
        # 摆在互不重叠的四个角落：gdigrab 读的是屏幕上的那块区域，窗口互相
        # 叠住时先被创建的会被挡住，亮度判据就成了测 Z 序而不是测画面
        spots = [(40, 40), (1000, 40), (40, 700), (1000, 700)]
        for i, (title, (w, h)) in enumerate(self.specs):
            x, y = spots[i % len(spots)]
            hwnd = user32.CreateWindowExW(
                0, self.CLASS, title, style, x, y, w, h, None, None,
                ctypes.c_void_p(hinst), None)
            if hwnd:
                self.hwnds[title] = hwnd
            else:
                print(f"  CreateWindowExW({title!r}) 失败"
                      f"（GetLastError={user32.GetLastError()}）")
        self._ready.set()
        msg = wintypes.MSG()
        while not self._quit:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.05)

    def close(self):
        self._quit = True
        for hwnd in self.hwnds.values():
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        self.thread.join(3)


def analyze(ffmpeg: str, path: str) -> tuple:
    """(帧数, 宽, 高, YAVG 最大值) —— 只用 ffmpeg，不依赖 ffprobe 在不在 PATH"""
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", path, "-vf",
                        "signalstats,metadata=print:file=-", "-f", "null", "-"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    found = re.findall(r"frame=\s*(\d+)", r.stderr)
    frames = int(found[-1]) if found else 0
    w = h = 0
    for line in r.stderr.splitlines():
        if "Video:" in line and "Stream" in line:
            m = re.search(r"(\d{2,5})x(\d{2,5})", line)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
            break
    luma = [float(x) for x in re.findall(r"YAVG=([\d.]+)", r.stdout)]
    return frames, w, h, (max(luma) if luma else 0.0)


def grab(ffmpeg: str, src_id: str, title: str, seconds: float, out: str) -> tuple:
    """按生产代码同一条命令行抓窗口"""
    src = {"type": "window", "id": src_id, "title": title}
    # build_video_input 只给输入段参数，滤镜要排在所有 -i 之后（抓屏不缩放，忽略第二项）
    args, _vf = FfmpegManager.build_video_input(src, 15, ffmpeg_path=ffmpeg)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args,
           "-t", f"{seconds}", "-an", out]
    print(f"  命令行: {' '.join(args)}")
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       timeout=int(seconds * 8) + 60)
    err = (r.stderr or "").strip()
    if not os.path.isfile(out) or os.path.getsize(out) == 0:
        return r.returncode, 0, 0, 0, 0.0, err
    frames, w, h, lum = analyze(ffmpeg, out)
    return r.returncode, frames, w, h, lum, err


def raise_topmost(hwnd) -> None:
    """把窗口提到最上层。

    gdigrab 抓的是屏幕上那块矩形，不是窗口自己的 buffer：别的窗口压在它上面，
    投出去就是那个压着的窗口。实测同一份代码连跑两遍，6.1.1 那轮三个探针窗口
    被桌面上的其它窗口挡住，亮度从 235 掉到 45~60 —— 不置顶的话「黑屏判据」
    测的是 Z 序运气，不是抓取本身。
    """
    HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = -1, 0x0002, 0x0001, 0x0010
    user32.SetWindowPos(ctypes.c_void_p(hwnd), HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
    time.sleep(0.4)


def report(ffmpeg: str, title: str, hwnd, luma_min: float = 4.0) -> None:
    """抓一次窗口，核对「抓到的是整窗还是客户区」——两种都算投屏成功，
    但必须如实写出来：用户选「投屏指定窗口」时预期看到的是那个窗口，
    少掉标题栏可以接受，少掉半块画面不行。"""
    out = os.path.join(OUT, f"{abs(hash(title)) % 10**8}.mp4")
    full, client = frame_bounds(hwnd), client_bounds(hwnd)
    raise_topmost(hwnd)
    rc, frames, w, h, lum, err = grab(ffmpeg, f"window:0x{int(hwnd):X}", title,
                                      2.0, out)
    print(f"\n── {title!r} 整窗 {full} / 客户区 {client} → rc={rc} 帧={frames} "
          f"实得={(w, h)} YAVG={lum:.1f}")
    if err:
        print(f"  stderr: {err[:220]}")
    check(rc == 0 and frames >= 10,
          f"{title!r} 抓到连续帧（2 秒 15fps 应约 30 帧，实得 {frames}）")
    near = [(n, e) for n, e in (("整窗", full), ("客户区", client))
            if e and abs(w - e[0]) <= 2 and abs(h - e[1]) <= 2]
    check(bool(near), f"{title!r} 抓取尺寸是整窗或客户区之一"
                      f"（整窗 {full}、客户区 {client}、实得 {(w, h)}）")
    if near:
        print(f"  → 抓到的是 {near[0][0]}")
    check(lum >= luma_min, f"{title!r} 画面不是黑屏（YAVG={lum:.1f}，需 ≥{luma_min}）")


def main() -> int:
    global OUT
    if os.name != "nt":
        print("本探针只在 Windows 上跑")
        return 2

    fm = FfmpegManager()
    info = fm.check()
    print(f"ffmpeg: {info.get('path')} {info.get('version')} ok={info.get('ok')}")
    if not info.get("ok"):
        print("SUMMARY: FAIL: FFmpeg 不可用")
        return 1
    ffmpeg = FFMPEG_OVERRIDE or info["path"]
    if FFMPEG_OVERRIDE:
        print(f"改用指定 ffmpeg: {ffmpeg}")
    sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    print(f"桌面 {sw}x{sh}（为 0 说明不在交互会话，gdigrab 抓不到东西）")

    table = window_table()
    wins = fm._list_windows()
    kept = {w["title"] for w in wins}
    raw = {t: v for t, v in table.items()
           if v["visible"] and not v["iconic"] and min(v["fb"]) > 0 and t.strip()}
    print(f"\n── 可见带标题窗口 {len(raw)} 个 → 生产过滤后 {len(wins)} 个 ──")
    dump(table, kept)
    check(sw > 0, "取到桌面分辨率（脚本跑在交互会话里）")
    check(len(wins) > 0, "过滤后仍有可投窗口")
    check(all(re.fullmatch(r"window:0x[0-9A-F]+", str(w["id"])) and w["title"]
              for w in wins),
          "每项 id 都是 window:0x<hwnd> 且有标题")
    check(not [t for t in kept if table.get(t, {}).get("iconic")],
          "列表里没有最小化窗口")
    check(not [t for t in kept if table.get(t, {}).get("owned")],
          "列表里没有带 owner 的子窗口")
    check(not [t for t in kept if table.get(t, {}).get("cloaked")],
          "列表里没有其它虚拟桌面上的遮挡窗口")
    bad_na = [t for t in kept
              if table.get(t, {}).get("ex", 0) & WS_EX["NOACTIVATE"]]
    check(not bad_na, f"列表里没有 NOACTIVATE 影子窗口（实得 {bad_na[:3]}）")
    bad_tw = [t for t in kept if table.get(t, {}).get("ex", 0) & WS_EX["TOOLWINDOW"]
              and not table.get(t, {}).get("ex", 0) & WS_EX["APPWINDOW"]]
    check(not bad_tw, f"列表里没有 TOOLWINDOW 悬浮条（实得 {bad_tw[:3]}）")
    bad_shape = [t for t in kept if not table.get(t, {}).get(
        "style", 0) & (WS["CAPTION"] | WS["THICKFRAME"])]
    check(not bad_shape, f"列表里每项都是主窗口形态（实得 {bad_shape[:3]}）")
    offscreen = [t for t in kept if table.get(t, {}).get("rect", (0, 0, 1, 1))[0]
                 <= -30000]
    check(not offscreen, f"列表里没有停在 -32000 的隐形窗口（实得 {offscreen[:3]}）")

    if SURVEY:
        print("\nSUMMARY: SURVEY（只枚举，未抓窗口）")
        return 0

    OUT = tempfile.mkdtemp(prefix="winpick-")
    specs = [("HanCast 探针:冒号", (900, 620)),
             ("HanCast 探针&与号", (802, 500)),
             ('HanCast 探针"引号"和空格', (702, 402)),
             ("HanCast 探针（括号）,逗号", (1002, 702))]
    host = ProbeWindows(specs)
    try:
        check(len(host.hwnds) == len(specs),
              f"探针窗口都开出来了（{len(host.hwnds)}/{len(specs)}）")
        time.sleep(1.0)
        fresh = window_table()
        listed = {w["title"] for w in fm._list_windows()}
        for title, size in specs:
            check(title in listed, f"选择器列表里有 {title!r}")
            if title in listed:
                report(ffmpeg, title, fresh[title]["hwnd"], luma_min=150.0)

        # 再抓一个本来就开着的真实窗口，结论不能全建立在自造窗口上
        real = None
        for w in fm._list_windows():
            t = w["title"]
            if t in dict(specs):
                continue
            v = window_table().get(t)
            if v and v["fb"][0] * v["fb"][1] > 200 * 200 and v["fb"][0] < sw:
                real = (t, v["hwnd"])
                print(f"\n附加真实窗口: {t[:60]!r} 整窗 {v['fb']}")
                break
        if real:
            report(ffmpeg, real[0], real[1])

        # 已关闭的窗口：真关掉一个探针窗口，再拿它的 hwnd 去起播 —— 必须在拼
        # 命令行之前就报「窗口已关闭」，而不是等 ffmpeg 安静地产出黑屏
        dead = int(host.hwnds["HanCast 探针:冒号"])
        user32.PostMessageW(ctypes.c_void_p(dead), 0x0010, 0, 0)  # WM_CLOSE
        time.sleep(0.8)
        dead_err = None
        try:
            FfmpegManager.build_video_input(
                {"type": "window", "id": f"window:0x{dead:X}"}, 15)
        except ValueError as e:
            dead_err = str(e)
        print(f"\n── 已关闭窗口 → {dead_err!r}")
        check(dead_err is not None,
              "已关闭的窗口快速报错（否则用户选错窗口只会看到黑屏）")
    finally:
        host.close()
        print(f"\n产物目录: {OUT}")

    print("\nSUMMARY:", "FAIL: " + "; ".join(FAIL) if FAIL else "PASS")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
