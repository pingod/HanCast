"""手机镜像接收（Android → 本机）

本程序此前只有「出站」的屏幕投屏（本机屏 → 电视 / 浏览器观看页）。
这里补的是反方向：把 Android 手机屏幕接到本机显示。

为什么是 ADB + scrcpy，而不是 Miracast：
- Miracast 走 Wi-Fi Direct P2P，macOS 没有可用的 P2P API，当不了 sink；
- Android 的「无线投屏」入口绝大多数仍是 Miracast / 厂商私有协议，
  Windows 的「投影到此电脑」只在 Windows 上可用；
- 全平台都能落地的一条路是 ADB（USB 或「无线调试」）把屏幕帧取出来，
  scrcpy 是这条路上事实标准，且带触控、声音、旋转。

v1 的形态：本模块只做「工具链定位 + 设备枚举 + 进程编排」，窗口由
用户本机已装的 scrcpy 自己打开（低延迟、触控、声音都现成）。不自己实现
scrcpy 的传输协议——那需要逐帧解析它的 12 字节包头并处理 codec 元数据，
没有真机时无法验证，风险远大于收益。
"""

from .manager import MirrorManager, build_scrcpy_args
from .tools import parse_adb_devices, tool_status

__all__ = [
    "MirrorManager",
    "build_scrcpy_args",
    "parse_adb_devices",
    "tool_status",
]
