"""
WebRTC 观看房间（参考 Piik：浏览器免安装观看）

模型：一个房间 = 一个共享视频/音频 track（屏幕捕获帧）+ 最多 MAX_VIEWERS 个
RTCPeerConnection。观众打开观看页 → WebSocket 信令交换 offer/answer/ICE
→ P2P 直连（局域网 host candidate），浏览器播放 h264 + opus。

帧供给：捕获线程调用 set_video_frame / push_audio 线程安全写入，
观众 recv() 时构造 av 帧返回（aiortc 内部编码后经 RTP 发送）。
"""

import asyncio
import logging
import threading
from fractions import Fraction
from typing import Callable, Dict, Optional

try:
    import av
    from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
    from aiortc.rtcrtpparameters import RTCRtpCodecParameters
    from aiortc.mediastreams import MediaStreamError
except ImportError:  # 允许 import 失败以便单元测试（真实运行前依赖已装）
    av = None
    MediaStreamTrack = object
    RTCPeerConnection = None
    RTCSessionDescription = None
    MediaStreamError = Exception

logger = logging.getLogger("hancast.screencast.webrtc")

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 1920  # 20ms @48kHz


class SharedVideoTrack(MediaStreamTrack):
    """共享视频 track：捕获线程写入最新 bgr24 帧，观众 recv 时取帧编码"""

    kind = "video"

    def __init__(self, fps: int = 15):
        super().__init__()
        self._fps = int(fps or 15)
        self._frame: Optional[object] = None  # numpy bgr24 (h,w,3)
        self._pts = 0
        self._lock = threading.Lock()

    def set_frame(self, arr):
        """捕获线程调用：更新最新帧（numpy uint8 bgr24）"""
        with self._lock:
            self._frame = arr

    async def recv(self):
        if av is None:
            raise MediaStreamError("av not available")
        with self._lock:
            arr = self._frame
        if arr is None:
            raise MediaStreamError("no frame yet")
        # PyAV 会拷贝 numpy 数据，可安全复用底层缓冲区
        frame = av.VideoFrame.from_ndarray(arr, format="bgr24")
        frame.pts = self._pts
        self._pts += 1
        frame.time_base = Fraction(1, self._fps)
        return frame


class SharedAudioTrack(MediaStreamTrack):
    """共享音频 track：捕获线程 push pcm s16le 48kHz 双声道数据"""

    kind = "audio"

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._loop = loop
        self._queue: Optional[asyncio.Queue] = None
        self._pts = 0

    def _ensure_queue(self):
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=32)
        return self._queue

    def push(self, data: bytes):
        """捕获线程调用（线程安全）"""
        def _put():
            q = self._ensure_queue()
            if q.qsize() < 30:  # 丢弃过饱和数据，避免内存膨胀
                q.put_nowait(data)
        try:
            self._loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass  # loop 已关闭

    async def recv(self):
        if av is None:
            raise MediaStreamError("av not available")
        data = await self._ensure_queue().get()
        frame = av.AudioFrame(format="s16", layout="stereo", samples=SAMPLES_PER_FRAME)
        plane = frame.planes[0]
        plane.update(data)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        self._pts += SAMPLES_PER_FRAME
        return frame


class WebRtcRoom:
    """一个 WebRTC 观看房间"""

    MAX_VIEWERS = 20

    def __init__(self, loop: asyncio.AbstractEventLoop, room_id: str,
                 fps: int = 15,
                 on_viewer_change: Optional[Callable] = None):
        self.loop = loop
        self.room_id = room_id
        self.video_track = SharedVideoTrack(fps)
        self.audio_track = SharedAudioTrack(loop)
        self.on_viewer_change = on_viewer_change
        self.pcs: Dict[str, RTCPeerConnection] = {}
        self._closed = False

    # ── 帧供给（捕获线程调用）──

    def set_video_frame(self, arr):
        self.video_track.set_frame(arr)

    def push_audio(self, data: bytes):
        self.audio_track.push(data)

    # ── 观众管理 ──

    @property
    def viewer_count(self) -> int:
        return len(self.pcs)

    def can_join(self) -> bool:
        return not self._closed and self.viewer_count < self.MAX_VIEWERS

    def _emit_change(self):
        if self.on_viewer_change:
            try:
                self.on_viewer_change(self.room_id, self.viewer_count)
            except Exception:
                logger.exception("viewer change callback failed")

    # ── 信令处理（asyncio 线程内调用）──

    async def handle_offer(self, peer_id: str, offer_sdp: str,
                           ice_callback: Callable, close_callback: Callable) -> str:
        """处理观众 offer，返回 answer sdp；并注册 ICE/关闭回调

        ice_callback(candidate_dict): 发候选给该观众
        close_callback(peer_id): 观众断开
        """
        if RTCPeerConnection is None:
            raise RuntimeError("aiortc not available")
        if self._closed:
            raise RuntimeError("room closed")
        if peer_id in self.pcs:
            raise RuntimeError("peer already joined")
        if not self.can_join():
            raise RuntimeError("房间已满")

        pc = RTCPeerConnection()
        self.pcs[peer_id] = pc
        pc.addTrack(self.video_track)
        pc.addTrack(self.audio_track)

        @pc.on("icecandidate")
        def _on_ice(candidate):
            if candidate is not None:
                try:
                    ice_callback({
                        "candidate": candidate.candidate,
                        "sdpMid": candidate.sdpMid,
                        "sdpMLineIndex": candidate.sdpMLineIndex,
                    })
                except Exception:
                    logger.exception("ice callback failed")

        @pc.on("connectionstatechange")
        def _on_state():
            state = pc.connectionState
            if state in ("failed", "closed", "disconnected"):
                asyncio.ensure_future(self._remove_peer(peer_id, pc, close_callback))

        # aiortc 版本间 RTCSessionDescription 参数顺序不同（(sdp,type) 新 / (type,sdp) 旧）
        try:
            desc = RTCSessionDescription(sdp=offer_sdp, type="offer")
        except TypeError:
            desc = RTCSessionDescription("offer", offer_sdp)
        await pc.setRemoteDescription(desc)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        self._emit_change()
        return pc.localDescription.sdp

    async def add_ice(self, peer_id: str, candidate_dict: Dict):
        pc = self.pcs.get(peer_id)
        if pc is None:
            return
        try:
            from aiortc import RTCIceCandidate
            cand = RTCIceCandidate(
                candidate=candidate_dict.get("candidate", ""),
                sdpMid=candidate_dict.get("sdpMid"),
                sdpMLineIndex=candidate_dict.get("sdpMLineIndex"),
            )
            await pc.addIceCandidate(cand)
        except Exception as e:
            logger.warning(f"[ScreenCast] addIceCandidate failed: {e}")

    async def _remove_peer(self, peer_id: str, pc: RTCPeerConnection,
                           close_callback: Callable):
        if self.pcs.pop(peer_id, None) is not None:
            try:
                await pc.close()
            except Exception:
                pass
            self._emit_change()
            try:
                close_callback(peer_id)
            except Exception:
                pass

    async def disconnect(self, peer_id: str):
        pc = self.pcs.pop(peer_id, None)
        if pc is not None:
            try:
                await pc.close()
            except Exception:
                pass
            self._emit_change()

    async def close(self):
        """关闭房间（停止所有连接，幂等）"""
        if self._closed:
            return
        self._closed = True
        pcs = list(self.pcs.values())
        self.pcs.clear()
        for pc in pcs:
            try:
                await pc.close()
            except Exception:
                pass
        self._emit_change()
        logger.info(f"[ScreenCast] Room {self.room_id} closed")
