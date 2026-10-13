"""Windows WASAPI loopback 采集（系统声音）

为什么不是 FFmpeg 直接采：上游 FFmpeg 到 9.0 都没有 WASAPI 输入设备
（ticket #9408 仍开着），Windows 侧只有 dshow，而 dshow 只能看见驱动自己
声明的「立体声混音」——绝大多数家用声卡根本没注册这个设备，于是「系统声音」
这一路在界面上永远是「未检测到」。

PortAudio 的 WASAPI 分支提供了渲染环回（用默认输出设备的 IAudioClient
LOOPBACK 标志采集），PyAudioWPatch 是它的 Python 绑定，不需要装任何驱动。
实测（.68，FxSound 渲染设备）：48kHz/立体声，静音期照样按实时节奏交付数据
（2.00s 读到 380928 字节 ≈ 实时 384000），所以不必自己按单调时钟补零。

采到的 PCM 从 ffmpeg 的 stdin 灌进去（-f s16le -i pipe:0），
和 dshow 麦克风输入用 amix 合并。
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger("hancast.screencast.loopback")

# 每次读的帧数：1024/48000 ≈ 21ms，比一个 AAC 帧大不了太多，
# 读到一次阻塞久一点也不会把 ffmpeg 的输入憋住
CHUNK_FRAMES = 1024


def _import_pyaudio():
    """只有 Windows 装得到（PyPI 只发 win wheel），非 nt 或缺包都当作不可用"""
    if os.name != "nt":
        return None
    try:
        import pyaudiowpatch
        return pyaudiowpatch
    except Exception as e:  # noqa: BLE001 - 打包缺 .pyd 时不能拖垮整个投屏
        logger.info(f"[ScreenCast] pyaudiowpatch 不可用，系统声音退回 dshow: {e}")
        return None


def loopback_devices() -> List[Dict]:
    """可用的 WASAPI loopback 设备（每个渲染设备一个环回镜像）。

    返回 [{"name","index","rate","channels"}]；非 Windows / 无 PortAudio → []。
    当前默认渲染设备的那一路排在最前面：环回镜像是按设备索引排的，机器上挂着
    HDMI/虚拟声卡时第一位往往是一块没在出声的喇叭，选它等于「投出去永远没声音」。
    """
    pyaudio = _import_pyaudio()
    if not pyaudio:
        return []
    pa = None
    try:
        pa = pyaudio.PyAudio()
        out: List[Dict] = []
        for info in pa.get_loopback_device_info_generator():
            out.append({
                "name": info["name"],
                "index": info["index"],
                "rate": int(info["defaultSampleRate"]),
                "channels": int(info["maxInputChannels"]) or 2,
            })
        try:
            default_index = pa.get_default_wasapi_loopback()["index"]
        except Exception as e:  # noqa: BLE001 - 无渲染设备时仍要给出其余选项
            logger.debug(f"[ScreenCast] 默认环回设备未知: {e}")
        else:
            out.sort(key=lambda d: d["index"] != default_index)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ScreenCast] loopback 设备枚举失败: {e}")
        return []
    finally:
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass


def resolve(name: Optional[str]) -> Optional[Dict]:
    """按设备名找到 loopback 设备；名字对不上时用默认渲染设备的环回兜底。

    用户在界面选的是枚举那一刻的设备名；换输出设备（耳机拔插、蓝牙切换）后
    这个名字就过期了。环回永远跟着「当前默认渲染设备」走，所以兜底到默认环回
    比直接报错更符合用户预期——他听到的就是那块喇叭。
    """
    devices = loopback_devices()
    if not devices:
        return None
    if name:
        for d in devices:
            if d["name"] == name:
                return d
    return devices[0]


class LoopbackStream:
    """一路 s16le 环回采集：read() 返回可直接写进 ffmpeg stdin 的字节"""

    def __init__(self, device: Dict):
        pyaudio = _import_pyaudio()
        if not pyaudio:
            raise RuntimeError("PortAudio(WASAPI loopback) 不可用")
        self.device = dict(device)
        self._pa = pyaudio.PyAudio()
        try:
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self.device["channels"],
                rate=self.device["rate"],
                input=True,
                input_device_index=self.device["index"],
                frames_per_buffer=CHUNK_FRAMES,
            )
        except Exception:
            self._pa.terminate()
            raise

    @property
    def rate(self) -> int:
        return self.device["rate"]

    @property
    def channels(self) -> int:
        return self.device["channels"]

    def read(self) -> bytes:
        # exception_on_overflow=False：读慢了一点（GIL/管道堵塞）时丢掉溢出帧，
        # 也不要抛异常把整条投屏打断——丢 21ms 音频远比投屏中断好
        return self._stream.read(CHUNK_FRAMES, exception_on_overflow=False)

    def close(self):
        for fn in (self._stream.stop_stream, self._stream.close, self._pa.terminate):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[ScreenCast] loopback close: {e}")


def open_stream(name: Optional[str]) -> Optional[LoopbackStream]:
    """按名打开环回流；设备消失了就退回默认渲染设备的环回"""
    device = resolve(name)
    if not device:
        return None
    try:
        return LoopbackStream(device)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[ScreenCast] 打开 loopback 失败: {e}")
        return None
