"""
屏幕投屏统一管理（DLNA 电视 / 浏览器观看 双通道）

生命周期：
  start({target: "dlna"|"browser", device_id?, source, audio, video})
  stop()
  status()

架构：
  - FfmpegCapture 独立 FFmpeg 子进程（不阻塞 sidecar 主循环）
  - ScreenCastHttpd 独立 asyncio 线程（观看页 / HLS 静态）
  - 两个通道共用同一份 HLS：每次会话一个独立分片目录（hls/<session_id>/），
    URL 随会话变化，电视/浏览器都不会拉到上一轮的死播放列表
  - DLNA 推流复用 CommandHandler 的 DeviceCastSession（play HLS URL）
"""

import os
import shutil
import time
import logging
import tempfile
import secrets
import threading
from typing import Dict, Optional

from ..utils import protocol_io
from .ffmpeg_manager import FfmpegManager
from .capture import FfmpegCapture
from .httpd import ScreenCastHttpd

logger = logging.getLogger("hancast.screencast.manager")

# DLNA 起流后等待电视拉取播放列表的窗口（秒）。多数电视 2-6 秒开始拉流，
# 超过这个窗口还没拉，基本可以判定是网络/防火墙或地址问题而不是"还在加载"
DLNA_VERIFY_SECONDS = 12.0
DLNA_VERIFY_POLL = 1.0


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
        self._session_id: Optional[str] = None
        self._state: Dict = {
            "running": False,
            "target": None,
            "source": None,
            "device_id": None,
            "session_id": None,
            "watch_url": None,
            "url": None,
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
          target: "dlna" | "browser"
          device_id: dlna 目标设备（必填，target=dlna）
          source: {"type": "screen"|"window", "id": "...", "title": "..."}
          audio: {"mic": bool, "mic_device": str, "system": bool, "system_device": str}
          video: {"width": int, "height": int, "fps": int, "bitrate": str}
        """
        if self._state["running"]:
            return {"ok": False, "error": "已有投屏正在进行，请先停止"}

        target = params.get("target", "browser")
        if target not in ("dlna", "browser"):
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

        # 每次会话一个全新的分片目录 + 全新的 URL
        self._session_id = secrets.token_hex(3)
        try:
            result = self._start(target, params, source, audio, video)
        except Exception as e:
            logger.exception("[ScreenCast] start failed")
            self._teardown()
            return {"ok": False, "error": f"启动失败: {e}"}

        if result.get("ok"):
            self._emit()
        else:
            self._teardown()
        return result

    def stop(self) -> Dict:
        """停止屏幕投屏（幂等）"""
        if not self._state.get("running"):
            self._teardown()
            return {"ok": True, "already_stopped": True}

        target = self._state.get("target")
        device_id = self._state.get("device_id")
        if target == "dlna":
            try:
                self.handler._stop_cast(
                    {"device_id": device_id} if device_id else {})
            except Exception as e:
                logger.warning(f"[ScreenCast] dlna stop: {e}")

        self._teardown()
        self._emit()
        return {"ok": True}

    def status(self) -> Dict:
        """当前投屏状态"""
        state = dict(self._state)
        if state.get("session_id") and state.get("target") == "browser":
            state["viewers"] = self.httpd.viewers(state["session_id"])
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
    # 通道
    # ────────────────────────────────────────

    def _start(self, target: str, params: Dict, source: Dict,
               audio: Dict, video: Dict) -> Dict:
        device_id = None
        if target == "dlna":
            device_id = params.get("device_id")
            if not device_id:
                return {"ok": False, "error": "缺少目标设备 (device_id)"}
            device = self.handler.ssdp.get_device(device_id)
            if not device:
                return {"ok": False, "error": f"设备不存在或已离线: {device_id}"}

        if not self.httpd.start():
            return {"ok": False, "error": "HTTP 服务启动失败"}
        if not self.httpd.register_session(
                self._session_id, on_viewers=self._on_viewer_change):
            return {"ok": False, "error": "会话注册失败"}

        hls_dir = os.path.join(_hls_root(), self._session_id)
        capture = FfmpegCapture(self.ffmpeg.get_path())
        result = capture.start_hls(
            source, audio, video, hls_dir,
            on_error=lambda msg: self._on_capture_error(msg))
        if not result.get("ok"):
            self.httpd.unregister_session(self._session_id)
            return {"ok": False, "error": result.get("error", "屏幕捕获失败")}

        self._capture = capture
        m3u8_url = (f"http://{self._local_ip()}:{self.httpd.port}"
                    f"/hls/{self._session_id}/live.m3u8")

        if target == "dlna":
            logger.info(f"[ScreenCast] DLNA push: {device_id} <- {m3u8_url}")
            session = self.handler._get_or_create_session(device_id)
            try:
                session.play(m3u8_url, "application/vnd.apple.mpegurl")
            except Exception as e:
                logger.error(f"[ScreenCast] DLNA play failed: {e}")
                try:
                    self.handler._stop_cast({"device_id": device_id})
                except Exception:
                    pass
                return {"ok": False,
                        "error": f"电视拒绝播放（{e.__class__.__name__}），"
                                 f"可换较低分辨率/码率后重试"}
            self._state.update({
                "running": True, "target": "dlna", "source": source,
                "device_id": device_id, "session_id": self._session_id,
                "url": m3u8_url, "watch_url": None, "viewers": 0,
                "error": None, "started_at": time.time(),
            })
            threading.Thread(
                target=self._verify_dlna_pull,
                args=(self._session_id, device.display_name),
                name="screencast-dlna-verify", daemon=True).start()
            return {"ok": True, "target": "dlna", "device_id": device_id,
                    "session_id": self._session_id, "url": m3u8_url}
        watch_url = f"http://{self._local_ip()}:{self.httpd.port}/r/{self._session_id}"
        self._state.update({
            "running": True, "target": "browser", "source": source,
            "device_id": None, "session_id": self._session_id,
            "watch_url": watch_url, "url": m3u8_url, "viewers": 0,
            "error": None, "started_at": time.time(),
        })
        logger.info(f"[ScreenCast] browser share started: {watch_url}")
        return {"ok": True, "target": "browser",
                "session_id": self._session_id, "watch_url": watch_url,
                "width": result.get("width"), "height": result.get("height"),
                "fps": result.get("fps")}

    # ────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────

    def _local_ip(self) -> str:
        return self.handler._get_local_ip()

    def _verify_dlna_pull(self, session_id: str, device_name: str):
        """DLNA 电视不会把"播不出来"回报给控制器：黑屏时既可能是根本没拉到
        播放列表（网段/防火墙/地址错），也可能是拉到了但解码不了（码率过高）。
        这两种情况的处置方式完全不同，只能靠本机侧的拉流证据来区分。"""
        deadline = time.monotonic() + DLNA_VERIFY_SECONDS
        while time.monotonic() < deadline:
            time.sleep(DLNA_VERIFY_POLL)
            if self._session_id != session_id or not self._state.get("running"):
                return
            stats = self.httpd.pull_stats(session_id)
            if stats["pulls"] and stats["misses"] >= 6:
                self._state["error"] = (
                    f"{device_name} 跟不上当前码率（多个分片已被删除），"
                    f"请降低分辨率或码率后重试")
                logger.warning(f"[ScreenCast] dlna falling behind: {stats}")
                self._emit()
                return
            if stats["pulls"]:
                logger.info(
                    f"[ScreenCast] {device_name} pulling "
                    f"({stats['pulls']} requests from {stats['clients']})")
                return
        self._state["error"] = (
            f"{device_name} 在 {DLNA_VERIFY_SECONDS:.0f} 秒内没有拉取播放列表："
            f"请确认电视与本机在同一网段、允许 HanCast 接收局域网连接，"
            f"或换较低分辨率重试")
        logger.warning(f"[ScreenCast] dlna never pulled the playlist: "
                       f"{device_name} <- {self._state.get('url')}")
        self._emit()

    def _on_viewer_change(self, session_id: str, count: int):
        """httpd 事件循环线程回调：浏览器观众数变化"""
        if self._session_id == session_id and self._state.get("running"):
            self._state["viewers"] = count
            self._emit()

    def _on_capture_error(self, msg: str):
        """捕获线程错误回调（FFmpeg 退出/卡流）"""
        logger.error(f"[ScreenCast] capture error: {msg}")
        if not self._state.get("running"):
            return
        target = self._state.get("target")
        device_id = self._state.get("device_id")
        # _teardown 里的 _reset 会清空 error，必须先拆再写，否则前端拿不到
        # 失败原因，只剩一个「不知道为什么不投了」的黑屏
        self._teardown()
        self._state["error"] = msg
        # 通道一并释放：否则电视继续拉一条死流，前端也起不了下一次
        if target == "dlna":
            try:
                self.handler._stop_cast(
                    {"device_id": device_id} if device_id else {})
            except Exception as e:
                logger.warning(f"[ScreenCast] dlna stop after error: {e}")
        self._emit()

    def _teardown(self):
        """停捕获、注销会话、删除分片目录。

        幂等，且任何一步失败都不能挡住其余步骤——这一步冒异常的话，用户
        点「停止投屏」就永远没有回应（曾真的发生过：会话注销抛 TypeError
        直接冒到命令循环）。
        """
        if self._capture:
            try:
                self._capture.stop()
            except Exception as e:
                logger.warning(f"[ScreenCast] capture stop: {e}")
            self._capture = None
        sid = self._session_id
        self._session_id = None
        if sid:
            try:
                self.httpd.unregister_session(sid)
            except Exception as e:
                logger.warning(f"[ScreenCast] unregister session {sid}: {e}")
            shutil.rmtree(os.path.join(_hls_root(), sid), ignore_errors=True)
        # 没有会话在跑就把服务停下来，下次 start 才能拿回固定端口 8765
        try:
            self.httpd.stop()
        except Exception as e:
            logger.warning(f"[ScreenCast] httpd stop: {e}")
        self._reset()

    def _reset(self):
        self._state.update({
            "running": False,
            "target": None,
            "source": None,
            "device_id": None,
            "session_id": None,
            "watch_url": None,
            "url": None,
            "viewers": 0,
            "error": None,
            "started_at": None,
        })

    def _emit(self):
        """推送状态事件到前端"""
        try:
            protocol_io.emit_event("screen_cast_state_changed", self.status())
        except Exception as e:
            logger.warning(f"[ScreenCast] emit event: {e}")
