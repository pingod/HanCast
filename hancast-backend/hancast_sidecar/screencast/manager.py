"""
屏幕投屏统一管理（DLNA 电视 / WebRTC 浏览器 双通道）

生命周期：
  start({target: "dlna"|"webrtc", device_id?, source, audio, video})
  stop()
  status()

架构：
  - FfmpegCapture 独立 FFmpeg 子进程（不阻塞 sidecar 主循环）
  - ScreenCastHttpd 独立 asyncio 线程（信令 WS / HLS 静态 / 观看页）
  - WebRtcRoom 复用 httpd 的事件循环（aiortc 必须在 asyncio 内）
  - DLNA 推流复用 CommandHandler 的 DeviceCastSession（play HLS URL）
"""

import os
import sys
import json
import time
import tempfile
import logging
import threading
from typing import Any, Dict, Optional

from ..utils import protocol_io
from .ffmpeg_manager import FfmpegManager
from .capture import FfmpegCapture
from .httpd import ScreenCastHttpd, DEFAULT_PORT

logger = logging.getLogger("hancast.screencast.manager")


def _hls_root() -> str:
    """HLS 分片缓存目录（系统临时目录，避免网络盘 IO）"""
    return os.path.join(tempfile.gettempdir(), "hancast_screencast_hls")


class ScreenCastManager:
    """屏幕投屏管理器"""

    def __init__(self, handler):
        self.handler = handler  # CommandHandler（复用 config/session/ssdp）
        self.config = handler.config
        self.ffmpeg = FfmpegManager(self.config)
        self.httpd = ScreenCastHttpd(hls_root=_hls_root())
        self._capture: Optional[FfmpegCapture] = None
        self._room: Optional[Any] = None
        self._state: Dict = {
            "running": False,
            "target": None,
            "source": None,
            "device_id": None,
            "room_id": None,
            "watch_url": None,
            "viewers": 0,
            "error": None,
            "started_at": None,
        }

    # ────────────────────────────────────────
    # 命令接口
    # ────────────────────────────────────────

    def list_sources(self) -> Dict:
        """枚举屏幕/窗口/音频设备 + ffmpeg 状态"""
        return self.ffmpeg.list_sources()

    def start(self, params: Dict) -> Dict:
        """开始屏幕投屏

        params:
          target: "dlna" | "webrtc"
          device_id: dlna 目标设备（必填，target=dlna）
          source: {"type": "screen"|"window", "id": "...", "title": "..."}
          audio: {"mic": bool, "mic_device": str, "system": bool, "system_device": str}
          video: {"width": int, "height": int, "fps": int, "bitrate": str}
        """
        if self._state["running"]:
            return {"ok": False, "error": "已有投屏正在进行，请先停止"}

        target = params.get("target", "webrtc")
        if target not in ("dlna", "webrtc"):
            return {"ok": False, "error": f"未知目标类型: {target}"}

        # FFmpeg 检查
        ffmpeg_info = self.ffmpeg.check()
        if not ffmpeg_info.get("ok"):
            return {"ok": False, "error": ffmpeg_info.get("error", "FFmpeg 不可用")}

        source = params.get("source") or {}
        if not source.get("type"):
            source = {"type": "screen", "id": "desktop"}
        if source.get("type") == "window" and not (source.get("title") or source.get("id")):
            return {"ok": False, "error": "窗口源缺少窗口信息"}

        audio = params.get("audio") or {}
        video = params.get("video") or {}
        if audio.get("system") and not audio.get("system_device"):
            return {"ok": False, "error": "未选择系统声音设备（需立体声混音/虚拟声卡）"}

        try:
            if target == "dlna":
                result = self._start_dlna(params)
            else:
                result = self._start_webrtc(params)
        except Exception as e:
            logger.exception("[ScreenCast] start failed")
            self._reset()
            return {"ok": False, "error": f"启动失败: {e}"}

        if result.get("ok"):
            self._emit()
        return result

    def stop(self) -> Dict:
        """停止屏幕投屏（幂等）"""
        target = self._state.get("target")
        if not self._state.get("running"):
            self._reset()
            return {"ok": True, "already_stopped": True}

        # 停止捕获
        if self._capture:
            try:
                self._capture.stop()
            except Exception as e:
                logger.warning(f"[ScreenCast] capture stop: {e}")
            self._capture = None

        # 停止通道
        if target == "dlna":
            device_id = self._state.get("device_id")
            try:
                if device_id:
                    self.handler._stop_cast({"device_id": device_id})
                else:
                    self.handler._stop_cast({})
            except Exception as e:
                logger.warning(f"[ScreenCast] dlna stop: {e}")
        elif target == "webrtc":
            self._close_room()

        self._reset()
        self._emit()
        return {"ok": True}

    def status(self) -> Dict:
        """当前投屏状态"""
        state = dict(self._state)
        state["ffmpeg"] = self.ffmpeg.check()
        return state

    def set_ffmpeg_path(self, params: Dict) -> Dict:
        path = params.get("path", "")
        if not path:
            return {"ok": False, "error": "缺少路径"}
        return self.ffmpeg.set_path(path)

    def cleanup(self):
        """退出清理（幂等）"""
        try:
            self.stop()
        except Exception as e:
            logger.warning(f"[ScreenCast] cleanup: {e}")
        try:
            self.httpd.stop()
        except Exception as e:
            logger.warning(f"[ScreenCast] httpd stop: {e}")

    # ────────────────────────────────────────
    # DLNA 通道
    # ────────────────────────────────────────

    def _start_dlna(self, params: Dict) -> Dict:
        device_id = params.get("device_id")
        if not device_id:
            return {"ok": False, "error": "缺少目标设备 (device_id)"}
        device = self.handler.ssdp.get_device(device_id)
        if not device:
            return {"ok": False, "error": f"设备不存在或已离线: {device_id}"}

        source = params.get("source") or {"type": "screen", "id": "desktop"}
        audio = params.get("audio") or {}
        video = params.get("video") or {}

        # 启动 HTTP 服务（HLS 静态）
        if not self.httpd.start():
            return {"ok": False, "error": "HTTP 服务启动失败"}

        # HLS 输出目录（按目标类型固定，每次启动清空）
        hls_dir = os.path.join(_hls_root(), "dlna")
        capture = FfmpegCapture(self.ffmpeg.get_path())
        result = capture.start_hls(source, audio, video, hls_dir)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "屏幕捕获失败")}

        # 构造 HLS URL 并推给电视
        m3u8_url = f"http://{self._local_ip()}:{self.httpd.port}/hls/dlna/live.m3u8"
        logger.info(f"[ScreenCast] DLNA push: {device_id} <- {m3u8_url}")
        session = self.handler._get_or_create_session(device_id)
        try:
            session.play(m3u8_url, "application/vnd.apple.mpegurl")
        except Exception:
            # play 失败时 self._capture 尚未赋值，必须停掉局部 capture，
            # 否则 FFmpeg 子进程成为孤儿一直编码
            capture.stop()
            raise

        self._capture = capture
        self._state.update({
            "running": True,
            "target": "dlna",
            "source": source,
            "device_id": device_id,
            "url": m3u8_url,
            "error": None,
            "started_at": time.time(),
        })
        return {"ok": True, "target": "dlna", "device_id": device_id, "url": m3u8_url}

    # ────────────────────────────────────────
    # WebRTC 通道
    # ────────────────────────────────────────

    def _start_webrtc(self, params: Dict) -> Dict:
        source = params.get("source") or {"type": "screen", "id": "desktop"}
        audio = params.get("audio") or {}
        video = params.get("video") or {}

        # 启动 HTTP 服务（信令 + 观看页）
        if not self.httpd.start():
            return {"ok": False, "error": "HTTP 服务启动失败"}

        fps = int(video.get("fps") or 15)

        # 创建房间（绑定 httpd 事件循环）
        from .webrtc_room import WebRtcRoom
        room_id = self._gen_room_id()
        room = WebRtcRoom(
            loop=self.httpd._loop,
            room_id=room_id,
            fps=fps,
            on_viewer_change=self._on_viewer_change,
        )
        if not self.httpd.register_room(room):
            return {"ok": False, "error": "房间注册失败"}

        # 启动原始帧捕获
        capture = FfmpegCapture(self.ffmpeg.get_path())
        result = capture.start_raw(
            source, audio, video,
            on_video_frame=room.set_video_frame,
            on_audio_frame=room.push_audio,
            on_error=lambda msg: self._on_capture_error(msg),
        )
        if not result.get("ok"):
            self._close_room()
            return {"ok": False, "error": result.get("error", "屏幕捕获失败")}

        watch_url = f"http://{self._local_ip()}:{self.httpd.port}/r/{room_id}"
        self._capture = capture
        self._room = room
        self._state.update({
            "running": True,
            "target": "webrtc",
            "source": source,
            "room_id": room_id,
            "watch_url": watch_url,
            "viewers": 0,
            "error": None,
            "started_at": time.time(),
        })
        logger.info(f"[ScreenCast] WebRTC room started: {watch_url}")
        return {
            "ok": True, "target": "webrtc",
            "room_id": room_id, "watch_url": watch_url,
            "width": result.get("width"), "height": result.get("height"), "fps": fps,
        }

    def _close_room(self):
        room = self._room
        room_id = self._state.get("room_id")
        if room is not None:
            try:
                self.httpd.run_coro(room.close()).result(timeout=5)
            except Exception as e:
                logger.warning(f"[ScreenCast] room close: {e}")
        if room_id:
            self.httpd.unregister_room(room_id)
        self._room = None

    def _on_viewer_change(self, room_id: str, count: int):
        """httpd 事件循环线程内回调"""
        if self._state.get("room_id") == room_id:
            self._state["viewers"] = count
            self._emit()

    def _on_capture_error(self, msg: str):
        """捕获线程错误回调"""
        logger.error(f"[ScreenCast] capture error: {msg}")
        if self._state.get("running"):
            target = self._state.get("target")
            self._state["error"] = msg
            # 停止捕获（保留状态供前端展示错误）
            if self._capture:
                self._capture.stop()
            self._state["running"] = False
            # 释放通道：否则旧房间留在 httpd.rooms、DLNA 会话继续拉死流
            if target == "webrtc":
                self._close_room()
            elif target == "dlna":
                try:
                    device_id = self._state.get("device_id")
                    self.handler._stop_cast(
                        {"device_id": device_id} if device_id else {})
                except Exception as e:
                    logger.warning(f"[ScreenCast] dlna stop after error: {e}")
            self._emit()

    # ────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────

    @staticmethod
    def _gen_room_id() -> str:
        import secrets
        return secrets.token_hex(3)  # 6 位十六进制

    def _local_ip(self) -> str:
        return self.handler._get_local_ip()

    def _reset(self):
        self._state.update({
            "running": False,
            "target": None,
            "source": None,
            "device_id": None,
            "room_id": None,
            "watch_url": None,
            "viewers": 0,
            "url": None,
            "error": None,
            "started_at": None,
        })

    def _emit(self):
        """推送状态事件到前端"""
        try:
            protocol_io.emit_event("screen_cast_state_changed", self.status())
        except Exception as e:
            logger.warning(f"[ScreenCast] emit event: {e}")
