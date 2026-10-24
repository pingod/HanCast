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
import sys
import time
import logging
import tempfile
import secrets
import threading
from typing import Dict, List, Optional

from ..utils import protocol_io
from ..params import get as param
from ..protocol.dlna import (build_didl_item, ControlUrlUnavailable,
                             RendererUnresponsive)
from .ffmpeg_manager import FfmpegManager
from .capture import FfmpegCapture
from .httpd import ScreenCastHttpd

logger = logging.getLogger("hancast.screencast.manager")

# DLNA 起流后等待电视拉取播放列表的窗口（秒）见 params 登记表
# （screencast.dlna_verify_seconds）。多数电视 2-6 秒开始拉流，超过窗口还没拉，
# 基本可以判定是网络/防火墙或地址问题而不是"还在加载"
DLNA_VERIFY_POLL = 1.0

# 直播 HLS 的 MIME：既写进 DIDL 的 protocolInfo，也用作 HTTP 响应类型
HLS_MIME = "application/vnd.apple.mpegurl"


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
        self._dlna_session = None
        self._pending_error: Optional[str] = None
        self._operation_lock = handler._cast_lock
        self._state_lock = threading.RLock()
        self._starting = False
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
        # Ownership changes share the handler lock. Capture callbacks record
        # errors under _state_lock only; they must not wait here during startup.
        with self._operation_lock:
            return self._start_requested(params)

    def _start_requested(self, params: Dict) -> Dict:
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
        if source.get("type") == "window" and sys.platform == "darwin":
            # mac 上没有窗口抓取，只能抓它所在屏再按几何裁——坐标必须是此刻的
            err = self.ffmpeg.refresh_window(source)
            if err:
                return {"ok": False, "error": err}

        audio = params.get("audio") or {}
        video = params.get("video") or {}
        if audio.get("system") and not audio.get("system_device"):
            return {"ok": False,
                    "error": "未选择系统声音设备（Windows 选带 [Loopback] 的渲染设备；"
                             "macOS 选「HanCast 系统声音」，14.2 以下需虚拟声卡；"
                             "Linux 选带「（默认）」的系统声音，即当前输出总线的镜像）"}
        if audio.get("mic") and not audio.get("mic_device"):
            return {"ok": False, "error": "未选择麦克风设备"}
        # 勾了声学设备却拿不到对应的输入，宁可直接说清楚：以前这里静默降级成
        # -an，用户以为在收声，实际投出去的每一帧都是哑的
        devices = self.ffmpeg._list_audio_devices()
        missing = self._missing_audio_devices(audio, devices)
        if missing:
            return {"ok": False, "error": missing}
        if audio.get("mic") and self.ffmpeg.audio_input_silent(
                audio.get("mic_device")) is True:
            # 没有录音权限时系统不报错、也不弹窗（macOS 尤其如此），设备照样
            # 枚举得到，只是采回来的每个样本都是 0 —— 投出去的每一帧都没有人声，
            # 而且画面流畅、毫无异常。开录前试采一秒，把这件事如实报出来。
            if sys.platform == "linux":
                advice = ("Linux 请先在「声音」设置里确认这条输入没有被静音，"
                          "再检查 PipeWire/PulseAudio 的录音权限（沙箱或 flatpak "
                          "启动时可能拿不到麦克风）")
            elif sys.platform == "darwin":
                advice = ("macOS 请在「系统设置 → 隐私与安全性 → 麦克风」里"
                          "授予 HanCast 录音权限")
            else:
                advice = ("Windows 请在「设置 → 隐私和安全性 → 麦克风」里"
                          "授予桌面应用录音权限")
            return {"ok": False,
                    "error": f"麦克风采到的全是静音，一个非零样本都没有——{advice}。"
                             "授权后重试，或先取消勾选「麦克风」"}
        if audio.get("system"):
            # 用户选的是 WASAPI 环回（Windows 上系统声音的默认形态）时，
            # ffmpeg 里没有这个设备，得由 sidecar 自己采 PCM 灌进 stdin
            for d in devices:
                if d.get("loopback") and d["name"] == audio.get("system_device"):
                    audio["_loopback"] = {"name": d["name"], "rate": d["rate"],
                                          "channels": d["channels"]}
                    break
            # macOS 的 Audio Tap 设备是枚举时现建的，avfoundation 里的索引跟着
            # 其他设备的增减走；界面存的是用户当时看到的索引，这里换成本次
            # 枚举到的真实索引（按名称匹配，BlackHole 那类物理设备不受影响）
            sel = str(audio.get("system_device") or "").split(":", 1)[-1]
            for d in devices:
                if d.get("tap") and str(d["name"]).split(":", 1)[-1] == sel:
                    audio["system_device"] = d["name"]
                    break

        # 每次会话一个全新的分片目录 + 全新的 URL
        self._session_id = secrets.token_hex(3)
        # 采集线程在 Play 返回前后都可能报错；这段时间 running 还是 False，
        # 错误必须先记下来，由本路径统一回滚，否则就是「返回成功却没有画面」。
        with self._state_lock:
            self._starting = True
            self._pending_error = None
        try:
            result = self._start(target, params, source, audio, video)
        except Exception as e:
            logger.exception("[ScreenCast] start failed")
            result = {"ok": False, "error": f"启动失败: {e}"}

        with self._state_lock:
            self._starting = False
            pending = self._pending_error
            self._pending_error = None
        if result.get("ok") and pending:
            # 错误落在 Play 与「标记 running」之间：看门狗已经退出，
            # 不能再把这次启动报成成功
            result = {"ok": False, "error": pending}

        if not result.get("ok"):
            # 启动失败一律回滚：电视会话、FFmpeg、分片目录都不留残骸
            self._release_dlna_session()
            self._teardown()
            return result

        self._emit()
        return result

    def _missing_audio_devices(self, audio: Dict,
                               devices: Optional[List[Dict]] = None) -> Optional[str]:
        """勾了声学设备、系统里却找不到它 —— 说清楚，而不是静默降级成 -an

        以前的表现：用户在界面上勾了「系统声音」，设备早被拔了/驱动装坏了，
        投屏照旧启动、画面流畅，只是永远没声音，谁也不知道为什么。
        """
        wanted = [("麦克风", audio.get("mic_device")) if audio.get("mic") else None,
                  ("系统声音", audio.get("system_device")) if audio.get("system") else None]
        wanted = [w for w in wanted if w]
        if not wanted:
            return None
        if devices is None:
            devices = self.ffmpeg._list_audio_devices()
        try:
            names = {d["name"] for d in devices}
        except Exception as e:
            logger.warning(f"[ScreenCast] 音频设备校验跳过: {e}")
            return None
        if not names:
            return None        # 枚举本身不可用（ffmpeg 缺失等），别拦住投屏
        for label, dev in wanted:
            if not dev:
                continue
            # macOS 的设备名是 "索引:名称"，索引随设备增减变化，只比名称部分
            base = str(dev).split(":", 1)[-1] if sys.platform == "darwin" else str(dev)
            if str(dev) not in names and not any(
                    n.split(":", 1)[-1] == base for n in names):
                return (f"选中的{label}设备已不可用：{base}；请重新选择"
                        f"（Windows 系统声音用「[Loopback]」结尾的渲染设备，"
                        f"macOS 需 CoreAudio 系统音频或虚拟声卡，"
                        f"Linux 用 PipeWire/PulseAudio 的输入源与输出镜像"
                        f"「….monitor」）")
        return None

    def stop(self) -> Dict:
        """停止屏幕投屏（幂等）"""
        with self._operation_lock:
            return self._stop_requested()

    def _stop_requested(self) -> Dict:
        if not self._state.get("running"):
            self._release_dlna_session()
            self._teardown()
            return {"ok": True, "already_stopped": True}

        control_failed: list = []
        reasons: Dict[str, str] = {}
        # 只停「屏幕通道自己那份」会话：电视此时可能已被媒体投屏接管，
        # 按 device_id 停会把别人的画面一起停掉。
        session, self._dlna_session = self._dlna_session, None
        if self._state.get("target") == "dlna" and session is not None:
            try:
                res = self.handler._stop_cast(
                    {"device_id": session.device_id}, expected_session=session)
                control_failed = res.get("control_failed") or []
                reasons = res.get("reasons") or {}
            except Exception as e:
                logger.warning(f"[ScreenCast] dlna stop: {e}")
                control_failed = [session.device_id]
                reasons = {session.device_id: str(e)}

        self._teardown()
        self._emit()
        # 分片目录已删、HTTP 服务已停，电视顶多把缓冲播完；但 Stop 没送达时
        # 必须让前端知道，否则用户以为「停止」= 电视已经回到待机。
        return {"ok": True, "control_failed": control_failed, "reasons": reasons}

    def stop_for_device(self, device_id: str) -> Dict:
        """媒体投屏要接管这台电视：屏幕通道让位，关掉采集与 HLS。

        不发 SOAP Stop——紧接着的 Play 会替换电视上的内容，停一次反而闪一下。
        由 `_start_cast` 在 handler 锁内调用。
        """
        if (self._dlna_session is None
                or self._dlna_session.device_id != device_id):
            return {"ok": True, "released": False}
        logger.info(f"[ScreenCast] 让位给媒体投屏: {device_id}")
        self._release_dlna_session(stop_remote=False)
        self._teardown()
        self._emit()
        return {"ok": True, "released": True}

    def release_session(self, session) -> None:
        """handler 丢弃某个设备会话时的回调：若那正是屏幕通道的会话就收尾。

        只做本地收尾，绝不回手调 `_stop_cast`（否则递归）。
        """
        if session is None or self._dlna_session is not session:
            return
        self._dlna_session = None
        if self._state.get("running") or self._capture is not None:
            logger.info("[ScreenCast] 电视会话已被释放，同步停止屏幕采集")
            self._teardown()
            self._emit()

    def _release_dlna_session(self, stop_remote: bool = True) -> None:
        session, self._dlna_session = self._dlna_session, None
        if session is None or not stop_remote:
            return
        try:
            self.handler._stop_cast({"device_id": session.device_id},
                                    expected_session=session)
        except Exception as e:
            logger.warning(f"[ScreenCast] release dlna session: {e}")

    def status(self) -> Dict:
        """当前投屏状态"""
        state = dict(self._state)
        if state.get("session_id") and state.get("target") == "browser":
            state["viewers"] = self.httpd.viewers(state["session_id"])
        if state.get("session_id"):
            # 拉流证据：电视黑屏时「没人来拉」和「拉了但跟不上码率」是两种
            # 完全不同的故障，只能靠这个区分（也供日志面板直接看）
            state["pull"] = self.httpd.pull_stats(state["session_id"])
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
        # 没有会话时 stop() 会提前返回、走不到 _teardown，这里兜底释放环回设备
        self._release_tap()

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
        # 先登记再判成败：_teardown 才有机会回收已起起来的子进程
        capture = self._capture = FfmpegCapture(self.ffmpeg.get_path())
        sid = self._session_id
        result = capture.start_hls(
            source, audio, video, hls_dir,
            on_error=lambda msg, sid=sid: self._on_capture_error(msg, sid),
            target=target)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("error", "屏幕捕获失败")}

        m3u8_url = (f"http://{self._local_ip()}:{self.httpd.port}"
                    f"/hls/{self._session_id}/live.m3u8")

        if target == "dlna":
            logger.info(f"[ScreenCast] DLNA push: {device_id} <- {m3u8_url}")
            # owner="screen"：这条电视会话归屏幕通道，媒体投屏不会再共用它
            session = self.handler._get_or_create_session(device_id, owner="screen")
            self._dlna_session = session
            didl = build_didl_item(
                m3u8_url, HLS_MIME, f"{device.display_name} 屏幕投屏")
            try:
                session.play(m3u8_url, HLS_MIME, metadata=didl)
            except (ControlUrlUnavailable, RendererUnresponsive) as e:
                logger.error(f"[ScreenCast] DLNA push not delivered: {e}")
                return {"ok": False, "error": str(e)}
            except Exception as e:
                logger.error(f"[ScreenCast] DLNA play failed: {e}")
                return {"ok": False,
                        "error": f"电视拒绝播放（{str(e) or e.__class__.__name__}），"
                                 f"可换较低分辨率/码率后重试"}
            with self._state_lock:
                if self._pending_error:
                    # Play 期间采集就报错了：别把死流交给电视
                    return {"ok": False, "error": self._pending_error}
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
        with self._state_lock:
            if self._pending_error:
                return {"ok": False, "error": self._pending_error}
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
        deadline = time.monotonic() + param("screencast.dlna_verify_seconds")
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
            f"{device_name} 在 {param('screencast.dlna_verify_seconds'):.0f} 秒内没有拉取播放列表："
            f"请确认电视与本机在同一网段、允许 HanCast 接收局域网连接，"
            f"或换较低分辨率重试")
        stats = self.httpd.pull_stats(session_id)
        logger.warning(
            f"[ScreenCast] dlna never pulled the playlist: "
            f"{device_name} <- {self._state.get('url')} "
            f"(pulls={stats['pulls']} misses={stats['misses']} "
            f"clients={stats['clients']})")
        self._emit()

    def _on_viewer_change(self, session_id: str, count: int):
        """httpd 事件循环线程回调：浏览器观众数变化"""
        if self._session_id == session_id and self._state.get("running"):
            self._state["viewers"] = count
            self._emit()

    def _on_capture_error(self, msg: str, sid: Optional[str] = None) -> None:
        """捕获线程错误回调（FFmpeg 退出/卡流）

        `sid` 在起流时绑定：旧会话的看门狗不能来拆新会话。启动期间（running
        还没置上）只记录，由 start 路径统一回滚并如实报错——直接 return 就是
        「投屏返回成功、实际一帧都没有」。
        """
        logger.error(f"[ScreenCast] capture error: {msg}")
        with self._state_lock:
            if sid is not None and sid != self._session_id:
                return
            if self._starting or not self._state.get("running"):
                if not self._pending_error:
                    self._pending_error = msg
                return
            target = self._state.get("target")
            device_id = self._state.get("device_id")
            self._state["running"] = False
        # 以下都在 _state_lock 之外：handler 侧会取投屏锁，锁序必须一致
        # 先放电视会话（_teardown 不清 `_dlna_session`，顺序反了就停不掉远端），
        # 再拆采集；error 必须在 _teardown 的 _reset 之后写，否则前端拿不到
        # 失败原因，只剩一个「不知道为什么不投了」的黑屏
        if target == "dlna":
            self._release_dlna_session()
        self._teardown()
        self._state["error"] = msg
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
        self._release_tap()
        self._reset()

    @staticmethod
    def _release_tap():
        """销毁自建的「HanCast 系统声音」环回设备（仅 macOS，幂等）

        这台虚拟输入设备只在投屏面板开着/正在投的时候有价值，留着会让用户的
        声音 MIDI 设置里莫名多出一个设备。销毁失败不影响停流。
        """
        if sys.platform != "darwin":
            return
        try:
            from .macos_tap import destroy_device
            destroy_device()
        except Exception as e:  # noqa: BLE001
            logger.info(f"[ScreenCast] 系统声音设备释放: {e}")

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
