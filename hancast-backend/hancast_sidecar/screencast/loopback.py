"""Windows WASAPI loopback 采集（系统声音）

为什么不是 FFmpeg 直接采：上游 FFmpeg 到 9.0 都没有 WASAPI 输入设备
（ticket #9408 仍开着），Windows 侧只有 dshow，而 dshow 只能看见驱动自己
声明的「立体声混音」——绝大多数家用声卡根本没注册这个设备，于是「系统声音」
这一路在界面上永远是「未检测到」。

PortAudio 的 WASAPI 分支提供了渲染环回（用默认输出设备的 IAudioClient
LOOPBACK 标志采集），PyAudioWPatch 是它的 Python 绑定，不需要装任何驱动。

实测（.68）：各渲染端点的环回行为不一致——FxSound 虚拟设备静音期
照样按实时节奏交付数据（2.00s 读到 380928 字节 ≈ 实时 384000），
而「扬声器 (Realtek(R) Audio)」那条环回在没人出声时 read() 直接卡住，
一个字节都不给。后者会把整条投屏憋死，所以交付节奏不能指望驱动。

采到的 PCM 从 ffmpeg 的 stdin 灌进去（-f s16le -i pipe:0），
和 dshow 麦克风输入用 amix 合并。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger("hancast.screencast.loopback")

# 每次读的帧数：1024/48000 ≈ 21ms，比一个 AAC 帧大不了太多，
# 读到一次阻塞久一点也不会把 ffmpeg 的输入憋住
CHUNK_FRAMES = 1024

# 消费侧等一块数据的时长：环回一停，最多憋这么久就去补静音
POLL_SECONDS = 0.05

# 一次最多补 4 块（≈85ms）。补太长的突发会让音频冲到视频前面，
# HLS 的交织器反过来把视频憋住，等于换个姿势卡死
MAX_PAD_CHUNKS = 4


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

    返回 [{"name","index","rate","channels","default"}]；非 Windows / 无 PortAudio → []。
    当前默认渲染设备的那一路排在最前面并标 default=True：环回镜像是按设备索引排的，
    机器上挂着 HDMI/虚拟声卡时第一位往往是一块没在出声的喇叭，选它等于「投出去永远没声音」。
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
                "default": False,
            })
        try:
            default_index = pa.get_default_wasapi_loopback()["index"]
        except Exception as e:  # noqa: BLE001 - 无渲染设备时仍要给出其余选项
            logger.debug(f"[ScreenCast] 默认环回设备未知: {e}")
        else:
            for d in out:
                d["default"] = d["index"] == default_index
            out.sort(key=lambda d: not d["default"])
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
    """一路 s16le 环回采集：read() 保证按实时节奏返回可写进 ffmpeg stdin 的字节

    为什么不能直接把 PortAudio 的字节转给 ffmpeg：amix 要凑齐每一路输入的帧
    才出帧，环回一饿死（.68 实测「扬声器 (Realtek(R) Audio)」的环回在没人出声
    时 read() 一直阻塞，零字节）整条管线就停在 0.4 秒，8 秒内不落播放列表，
    表现就是「一选系统声音屏幕投屏就起不来」。所以采集放独立线程，消费侧按
    单调时钟补欠账的静音：既不饿死 ffmpeg，也不会把音频冲到视频前面。
    """

    def __init__(self, device: Dict):
        pyaudio = _import_pyaudio()
        if not pyaudio:
            raise RuntimeError("PortAudio(WASAPI loopback) 不可用")
        self.device = dict(device)
        self._lock = threading.Lock()
        self._closed = False
        self._eof = False
        self._stop = threading.Event()
        # PortAudio 的 C 调用不允许并发，也不允许重入关闭：read() 阻塞时另一个
        # 线程 stop_stream/close/terminate，pyd 会踩已释放的 stream——.68 实测
        # 异常代码 0xc0000005、出错模块 ntdll.dll，整个 sidecar 进程直接没了
        # （表现为「选系统声音后投屏崩了」）。流的释放因此固定归采集线程自己，
        # 别人只负责把「该收尾了」这件事告诉它。
        self._q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=64)
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
        self._chunk_bytes = CHUNK_FRAMES * self.channels * 2
        self._bytes_per_sec = self.rate * self.channels * 2
        self._align = self.channels * 2
        self._started = time.monotonic()
        self._delivered = 0
        self._pump = threading.Thread(target=self._pump_loop,
                                      name="loopback-pump", daemon=True)
        self._pump.start()

    @property
    def rate(self) -> int:
        return self.device["rate"]

    @property
    def channels(self) -> int:
        return self.device["channels"]

    def _pump_loop(self):
        try:
            while not self._stop.is_set():
                try:
                    # exception_on_overflow=False：消费侧慢了一点（GIL/管道堵塞）
                    # 时丢掉溢出帧，也不要抛异常把整条投屏打断——丢 21ms 音频
                    # 远比投屏中断好
                    data = self._stream.read(CHUNK_FRAMES,
                                             exception_on_overflow=False)
                except Exception as e:  # noqa: BLE001 - 设备被拔出/独占
                    logger.warning(f"[ScreenCast] 环回读取中断: {e}")
                    break
                self._offer(data)
        finally:
            self._release()
            self._eof = True

    def _offer(self, data: bytes):
        """队列满时丢最旧：投屏要的是「现在的声音」，不是两秒前的"""
        while not self._stop.is_set():
            try:
                self._q.put(data, timeout=0.2)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass

    def read(self) -> Optional[bytes]:
        try:
            data = self._q.get(timeout=POLL_SECONDS)
        except queue.Empty:
            if self._eof:
                return None      # 采集线程已收尾：调用方据此结束，不是「这帧没数据」
            return self._pad()
        with self._lock:
            if self._closed:
                return None
            self._delivered += len(data)
        return data

    def _pad(self) -> bytes:
        """补齐「按实时节奏本该已经交付」但还没到手的那段静音"""
        expected = int((time.monotonic() - self._started) * self._bytes_per_sec)
        owed = expected - self._delivered
        if owed < self._align:
            return b""           # 还欠不到一个采样帧，等着，别把音频推过快
        n = min(owed, self._chunk_bytes * MAX_PAD_CHUNKS) // self._align * self._align
        with self._lock:
            if self._closed:
                return None
            self._delivered += n
        return b"\x00" * n

    def _release(self):
        """只由采集线程调用"""
        for fn in (self._stream.stop_stream, self._stream.close, self._pa.terminate):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[ScreenCast] loopback release: {e}")

    def close(self):
        """通知采集线程收尾；PaStream 仍由它自己释放（见类注释）"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        # 设备在正常交付时采集线程 21ms 内就回来；等不到说明它卡在没有数据的
        # 端点上，继续等只会拖慢「停止投屏」，交给它自己收尾
        self._pump.join(timeout=0.5)
        if self._pump.is_alive():
            logger.info("[ScreenCast] 环回仍在读取，流由采集线程自己关闭")


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
