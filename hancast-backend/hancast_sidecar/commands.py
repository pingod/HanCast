"""
命令路由 - 将 Sidecar 命令分发到对应模块
"""

import os
import sys
import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, Optional
from .ssdp import SSDPService, list_network_interfaces
from .protocol import dlna
from .protocol.dlna import DLNAProtocol
from .protocol.server import DLNAServer, DLNAHandler
from .renderer.mpv import MPVRenderer
from .media.parser import MediaParser
from .media.server import MediaServer
from .media.share_resolver import ShareResolver
from .models.device import Device
from .models.cast import CastState
from .models.session import DeviceCastSession
from .utils.config import Config
from .security.guard import DeviceGuard
from .utils.mpv_manager import MpvManager
from .utils import protocol_io
from .screencast import ScreenCastManager

import requests

logger = logging.getLogger("hancast.commands")


def find_mpv_path() -> str:
    """查找 MPV 可执行文件路径"""
    # 1. 检查环境变量
    mpv_path = os.environ.get("MPV_PATH")
    if mpv_path and os.path.exists(mpv_path):
        return mpv_path

    # 2. 确定基础路径
    if getattr(sys, 'frozen', False):
        # 打包后：sidecar 的 current_dir 已设为资源根目录
        base_path = os.getcwd()
    else:
        # 开发环境：向上 4 级到项目根
        base_path = os.path.dirname(
            os.path.dirname(
                os.path.dirname(
                    os.path.dirname(os.path.abspath(__file__))
                )
            )
        )

    # Windows
    if os.name == 'nt':
        project_mpv = os.path.join(base_path, 'mpv', 'mpv.exe')
        if os.path.exists(project_mpv):
            return os.path.abspath(project_mpv)
    else:
        # macOS / Linux
        project_mpv = os.path.join(base_path, 'mpv', 'mpv')
        if os.path.exists(project_mpv):
            return os.path.abspath(project_mpv)

    # 3. 默认使用系统 PATH 中的 mpv
    return "mpv"


class CommandHandler:
    def _get_local_ip(self) -> str:
        """获取本机 IP（优先使用用户选定的网络接口）"""
        import socket
        iface = self.config.network_interface
        if iface:
            return iface
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    def __init__(self):
        self.config = Config()
        self.protocol = DLNAProtocol()
        # 把真实版本号注入 DLNA 协议层（UPnP SERVER / modelNumber 用）
        dlna.set_app_version(self.config.version)

        # MPV 管理器
        self.mpv_manager = MpvManager(self.config)
        mpv_info = self.mpv_manager.check()
        mpv_path = mpv_info.path or "mpv"
        logger.info(f"MPV status: {mpv_info.status.value}, path: {mpv_path}")
        self.renderer = MPVRenderer(path=mpv_path)

        self.media_parser = MediaParser()
        # 投本地文件走 MediaServer 的 HTTP 链接：必须用选定网卡 IP，
        # 否则多网卡/VPN 机器上电视拿到的是默认路由 IP，拉不到流
        self.media_server = MediaServer(
            lan_ip=self.config.network_interface or None)
        # 分享文本/任意页面 → 可播直链解析器（共享 BiliResolver 的 Cookie 缓存）
        self.share_resolver = ShareResolver(self.media_server)
        # 异步解析任务表 {task_id: {status: pending|done|error, result?, error?, ts}}
        self._resolve_tasks: Dict[str, dict] = {}
        self._resolve_lock = threading.Lock()
        self.cast_state = CastState()

        # 多设备投屏会话 {device_id: DeviceCastSession}
        self.device_sessions: Dict[str, DeviceCastSession] = {}

        # DLNA 描述服务器
        friendly_name = self.config.get_friendly_name()
        device_usn = f"uuid:{self.config.get_usn()}"
        self.dlna_server = DLNAServer(
            friendly_name=friendly_name,
            usn=device_usn,
            ip=self._get_local_ip(),
            port=8080,
            version=self.config.version
        )
        self.dlna_server.set_command_handler(self)

        # SSDP 服务 (端口会在 _start_services 中更新)
        self.ssdp = SSDPService(
            friendly_name=friendly_name,
            port=8080,
            usn=device_usn,
            version=self.config.version,
            preferred_ip=self.config.network_interface or None,
        )

        # 设备投屏确认 (Device Guard)
        self.device_guard = DeviceGuard(self.config)
        self.device_guard._on_confirm_request = self._on_guard_confirm_request

        # 注册命令
        self._commands: Dict[str, callable] = {
            # 设备管理
            "get_devices": self._get_devices,
            "refresh_devices": self._refresh_devices,
            "set_default_device": self._set_default_device,
            "rename_device": self._rename_device,
            "remove_device": self._remove_device,
            "hide_device": self._hide_device,
            "unhide_device": self._unhide_device,
            "get_hidden_devices": self._get_hidden_devices,

            # 投屏控制
            "start_cast": self._start_cast,
            "stop_cast": self._stop_cast,
            "get_cast_state": self._get_cast_state,
            "get_cast_url": self._get_cast_url,
            "set_volume": self._set_volume,
            "set_mute": self._set_mute,
            "pause_cast": self._pause_cast,
            "resume_cast": self._resume_cast,
            "seek_cast": self._seek_cast,

            # 媒体解析
            "parse_media": self._parse_media,
            "resolve_share": self._resolve_share,
            "get_resolve_result": self._get_resolve_result,

            # 设置
            "get_settings": self._get_settings,
            "save_settings": self._save_settings,
            "list_network_interfaces": self._list_network_interfaces,

            # 更新检查
            "check_update": self._check_update,
            "ignore_update_version": self._ignore_update_version,

            # 设备投屏确认
            "respond_cast_confirm": self._respond_cast_confirm,
            "get_guard_devices": self._get_guard_devices,
            "remove_guard_device": self._remove_guard_device,
            "set_guard_policy": self._set_guard_policy,
            "get_guard_settings": self._get_guard_settings,
            "save_guard_settings": self._save_guard_settings,

            # MPV 管理
            "check_mpv": self._check_mpv,
            "set_mpv_path": self._set_mpv_path,

            # 屏幕投屏
            "list_screen_sources": self._list_screen_sources,
            "start_screen_cast": self._start_screen_cast,
            "stop_screen_cast": self._stop_screen_cast,
            "screen_cast_status": self._screen_cast_status,
            "set_ffmpeg_path": self._set_ffmpeg_path,
        }

        # 设置回调
        self.ssdp.set_callbacks(
            on_device_found=self._on_device_found,
            should_ignore_device=self._should_ignore_device
        )

        # 启动后台服务
        self._start_services()

    def _start_services(self):
        """启动 SSDP 和媒体服务器"""
        # 注册 DLNA 状态变化回调 → 推送事件到前端
        self.protocol.set_on_state_change(self._on_protocol_state_change)

        # 启动 DLNA 描述服务器
        self.dlna_server.start()

        # 更新 SSDP 的端口 (DLNA 服务器可能使用了不同的端口)
        self.ssdp._port = self.dlna_server.get_port()

        # 启动 SSDP 服务 (内部会调用 _register)
        self.ssdp.start()

        # 启动媒体服务器
        self.media_server.start(port=self.config.media_port)

        # 连接 Protocol ↔ Renderer
        # Protocol 需要调用 Renderer 播放媒体
        # Renderer 需要回调 Protocol 更新状态
        self.protocol.set_renderer(self.renderer)
        self.renderer.set_protocol(self.protocol)

        # 注入 Device Guard → Protocol（拦截入站投屏）
        self.protocol.set_device_guard(self.device_guard)

        # 启动 DLNA 协议事件线程 (发送状态变化通知给订阅的客户端)
        self.protocol.start()

        # 启动 MPV 渲染器
        self.renderer.start()
        logger.info("MPV renderer started")

        # 屏幕投屏管理器（复用 config / ssdp / session）
        self.screencast = ScreenCastManager(self)

    def execute(self, cmd: str, params: Dict[str, Any]) -> Any:
        """执行命令"""
        if cmd not in self._commands:
            raise ValueError(f"Unknown command: {cmd}")
        return self._commands[cmd](params)

    def cleanup(self):
        """清理资源（退出路径，幂等）"""
        # 停止屏幕投屏
        self.screencast.cleanup()
        # 停止所有投屏会话
        for session in self.device_sessions.values():
            session.stop()
        self.device_sessions.clear()
        self.ssdp.stop()
        self.dlna_server.stop()
        self.media_server.stop()
        # shutdown() 用于退出路径，幂等可重复调用；stop() 用于运行时暂停
        self.renderer.shutdown()
        self.protocol.stop_event_thread()

    def _on_device_found(self, device: Device):
        """设备发现回调"""
        # 更新 DeviceGuard 的 SSDP 缓存（用于快速识别投屏设备）
        self.device_guard.update_ssdp_cache(device.ip, {
            "udn": device.id,
            "friendly_name": device.name,
            "model_name": device.model_name,
        })

        # 发送事件到前端
        protocol_io.emit_event("device_found", device.to_dict())

    def _should_ignore_device(self, device_udn: str) -> bool:
        """检查设备是否应该被忽略（隐藏）"""
        return self.config.is_device_hidden(device_udn)

    def _emit_event(self, event_name: str, data: dict):
        """推送事件到前端"""
        protocol_io.emit_event(event_name, data)

    def _on_protocol_state_change(self, name: str, value) -> None:
        """DLNA 状态变化回调 → 推送事件到前端"""
        # 只关心 TransportState 变化
        if name == "TransportState":
            # 映射 DLNA 状态到前端状态
            status_map = {
                "PLAYING": "playing",
                "PAUSED_PLAYBACK": "paused",
                "STOPPED": "stopped",
                "NO_MEDIA_PRESENT": "idle",
                "TRANSITIONING": "connecting",
            }
            status = status_map.get(value, value)

            # 更新 cast_state
            self.cast_state.status = status

            # 推送事件到前端
            event_data = {
                "status": status,
                "device_id": self.cast_state.device_id,
                "position": self.cast_state.position,
                "volume": self.cast_state.volume,
                "is_muted": self.cast_state.is_muted,
            }
            if self.cast_state.media:
                event_data["media"] = self.cast_state.media.to_dict()

            protocol_io.emit_event("cast_state_changed", event_data)

    # ── 设备管理 ──

    def _get_devices(self, params: dict) -> list:
        devices = self.ssdp.get_devices()
        # 过滤掉隐藏的设备
        hidden = self.config.hidden_devices
        return [d.to_dict() for d in devices if d.id not in hidden]

    def _refresh_devices(self, params: dict) -> list:
        self.ssdp.scan()
        return self._get_devices(params)

    def _set_default_device(self, params: dict) -> None:
        device_id = params["id"]
        self.config.set_default_device(device_id)
        self.config.save()

    def _rename_device(self, params: dict) -> None:
        device_id = params["id"]
        name = params["name"]
        self.ssdp.rename_device(device_id, name)

    def _remove_device(self, params: dict) -> None:
        device_id = params["id"]
        self.ssdp.remove_device(device_id)

    def _hide_device(self, params: dict) -> None:
        """隐藏设备 - 加入隐藏列表，SSDP 仍能发现但不推送给前端"""
        device_id = params["id"]
        self.config.hide_device(device_id)
        self.config.save()

    def _unhide_device(self, params: dict) -> None:
        """取消隐藏设备 - 从隐藏列表移除"""
        device_id = params["id"]
        self.config.unhide_device(device_id)
        self.config.save()

    def _get_hidden_devices(self, params: dict) -> list:
        """获取隐藏设备列表"""
        return self.config.hidden_devices

    # ── 投屏控制 ──

    def _get_or_create_session(self, device_id: str) -> DeviceCastSession:
        """获取或创建设备投屏会话"""
        if device_id in self.device_sessions:
            return self.device_sessions[device_id]

        # 创建新会话
        device = self.ssdp.get_device(device_id)
        if not device:
            raise ValueError(f"Device not found: {device_id}")

        session = DeviceCastSession(device, self.media_server)
        session.set_poll_callback(self._on_session_event)
        self.device_sessions[device_id] = session
        logger.info(f"Created cast session for device: {device.display_name}")
        return session

    def _on_session_event(self, device_id: str, event_type: str) -> None:
        """Session 事件回调 → 推送到前端"""
        session = self.device_sessions.get(device_id)
        if not session:
            return

        state = session.cast_state
        event_data = {
            "status": state.status,
            "device_id": device_id,
            "position": state.position,
            "duration": state.duration,
            "volume": state.volume,
            "is_muted": state.is_muted,
        }
        if state.media:
            event_data["media"] = state.media.to_dict()

        protocol_io.emit_event("cast_state_changed", event_data)

    def _start_cast(self, params: dict) -> None:
        device_id = params["device_id"]
        media_uri = params["media_uri"]
        mime_type = params.get("mime_type", "")  # 可选，前端传入

        # 如果是本地文件，通过媒体服务器提供 HTTP 访问
        if not media_uri.startswith("http"):
            media_uri = self.media_server.serve_file(media_uri)

        # 获取或创建设备会话（每个设备独立的 DLNAProtocol 实例）
        session = self._get_or_create_session(device_id)
        session.play(media_uri, mime_type)

        # 立即推送初始 cast_state_changed 事件到前端
        # 前端依赖此事件识别活跃投屏会话；若仅靠 polling 回调，
        # 首次 poll 可能返回 00:00:00（设备未就绪），前端无法识别会话
        state = session.cast_state
        event_data = {
            "status": state.status,
            "device_id": device_id,
            "position": state.position,
            "duration": state.duration,
            "volume": state.volume,
            "is_muted": state.is_muted,
        }
        if state.media:
            event_data["media"] = state.media.to_dict()

        protocol_io.emit_event("cast_state_changed", event_data)

        # 同时更新全局 cast_state（兼容旧前端）
        self.cast_state.status = "playing"
        self.cast_state.device_id = device_id

    def _stop_cast(self, params: dict) -> None:
        device_id = params.get("device_id")
        if device_id and device_id in self.device_sessions:
            # 停止指定设备
            self.device_sessions[device_id].stop()
            del self.device_sessions[device_id]
        else:
            # 停止所有设备
            for session in self.device_sessions.values():
                session.stop()
            self.device_sessions.clear()
            self.cast_state.status = "idle"
            self.cast_state.device_id = None

    def _pause_cast(self, params: dict) -> None:
        device_id = params.get("device_id")
        if device_id and device_id in self.device_sessions:
            self.device_sessions[device_id].pause()
        else:
            self.protocol.pause()
            self.cast_state.status = "paused"

    def _resume_cast(self, params: dict) -> None:
        device_id = params.get("device_id")
        if device_id and device_id in self.device_sessions:
            self.device_sessions[device_id].resume()
        else:
            self.protocol.play()
            self.cast_state.status = "playing"

    def _seek_cast(self, params: dict) -> None:
        position = params["position"]  # HH:MM:SS 格式
        device_id = params.get("device_id")
        if device_id and device_id in self.device_sessions:
            self.device_sessions[device_id].seek(position)
        else:
            self.protocol.seek(position)

    def _get_cast_state(self, params: dict) -> dict:
        device_id = params.get("device_id")
        if device_id and device_id in self.device_sessions:
            return self.device_sessions[device_id].get_state()
        # 返回所有设备的状态
        return {
            "sessions": {did: s.get_state() for did, s in self.device_sessions.items()},
            "current": self.cast_state.to_dict(),
        }

    def _get_cast_url(self, params: dict) -> dict:
        """获取当前投屏元素的 URL 信息"""
        device_id = params.get("device_id")
        if device_id and device_id in self.device_sessions:
            return self.device_sessions[device_id].get_cast_info()
        url = self.protocol.get_state_url()
        title = self.protocol.get_state_title()
        duration = self.protocol.get_state_duration()
        position = self.protocol.get_state_position()
        transport = self.protocol.get_state_transport_state()
        return {
            "url": url or "",
            "title": title or "",
            "duration": duration or "00:00:00",
            "position": position or "00:00:00",
            "status": transport or "STOPPED",
        }

    def _set_volume(self, params: dict) -> int:
        volume = params["volume"]
        device_id = params.get("device_id")
        if device_id and device_id in self.device_sessions:
            return self.device_sessions[device_id].set_volume(volume)
        self.protocol.set_volume(volume)
        self.cast_state.volume = volume
        return volume

    def _set_mute(self, params: dict) -> None:
        muted = params["muted"]
        device_id = params.get("device_id")
        if device_id and device_id in self.device_sessions:
            self.device_sessions[device_id].set_mute(muted)
        else:
            self.protocol.set_mute(muted)
            self.cast_state.is_muted = muted

    # ── 媒体解析 ──

    def _parse_media(self, params: dict) -> dict:
        media_type = params["type"]
        if media_type == "file":
            info = self.media_parser.parse_file(params["path"])
        else:
            info = self.media_parser.parse_url(params["url"])
        return info.to_dict()

    def _resolve_share(self, params: dict) -> dict:
        """从任意粘贴文本（分享口令/短链/网页/直链）解析可播放媒体。

        解析含多次外网请求（最长 25s），放后台线程执行：命令循环立即返回
        task_id，前端轮询 get_resolve_result。否则会卡住投屏状态轮询等命令。
        """
        text = params.get("text") or params.get("url") or ""
        if not text.strip():
            raise ValueError("粘贴内容为空")

        with self._resolve_lock:
            # 清理过期完成任务（10 分钟）
            now = time.time()
            for tid in [t for t, e in self._resolve_tasks.items()
                        if e["status"] != "pending" and now - e.get("ts", now) > 600]:
                del self._resolve_tasks[tid]
            pending = sum(1 for e in self._resolve_tasks.values()
                          if e["status"] == "pending")
            if pending >= 3:
                raise ValueError("已有 3 个解析任务进行中，请稍后再试")

            task_id = uuid.uuid4().hex[:12]
            self._resolve_tasks[task_id] = {"status": "pending", "ts": time.time()}

        def _run():
            try:
                result = self.share_resolver.resolve(text)
                done = {"status": "done", "result": result, "ts": time.time()}
            except Exception as e:
                logger.info(f"分享解析失败: {e}")
                done = {"status": "error", "error": str(e), "ts": time.time()}
            with self._resolve_lock:
                # 任务可能已被清理（超期/重启），写入前先确认还在
                if task_id in self._resolve_tasks:
                    self._resolve_tasks[task_id] = done

        threading.Thread(target=_run, name=f"RESOLVE_{task_id}", daemon=True).start()
        return {"task_id": task_id, "status": "pending"}

    def _get_resolve_result(self, params: dict) -> dict:
        task_id = params.get("task_id") or ""
        with self._resolve_lock:
            entry = self._resolve_tasks.get(task_id)
            if entry is None:
                raise ValueError("解析任务不存在或已过期")
            return dict(entry)

    # ── 设置 ──

    def _get_settings(self, params: dict) -> dict:
        return self.config.to_dict()

    def _list_network_interfaces(self, params: dict) -> dict:
        """枚举可选网络接口（供设置页选择投屏/设备发现使用的网卡）"""
        return {
            "interfaces": list_network_interfaces(),
            "selected": self.config.network_interface,
        }

    def _save_settings(self, params: dict) -> None:
        settings = params["settings"]

        # 热更新设备名称：拒绝空字符串写入名称（防止用户误删或脏数据），
        # 但空名称不应阻断其他设置项保存——此前会直接 return，
        # 导致音量等其余设置一并丢失
        new_name = settings.get("friendly_name")
        if new_name == "":
            settings = {**settings, "friendly_name": self.config.get_friendly_name()}
            new_name = None

        if new_name is not None and new_name != self.config.get_friendly_name():
            self.dlna_server.friendly_name = new_name
            DLNAHandler.friendly_name = new_name
            self.config.friendly_name = new_name
            logger.info(f"设备名称已更新: {new_name}")

        old_iface = self.config.network_interface
        self.config.update(settings)
        self.config.save()

        # 网络接口变更：SSDP 需要重新绑定网卡（后台线程执行，避免阻塞
        # 命令响应——restart 含线程 join 与监听重建，约 1-2 秒）
        new_iface = self.config.network_interface
        if new_iface != old_iface:
            self.ssdp.preferred_ip = new_iface or None
            # 媒体服务器 LAN 链接同步切到选定网卡
            self.media_server._lan_ip = (
                new_iface or MediaServer._get_local_ip())
            threading.Thread(
                target=self._restart_ssdp, name="SSDP_RESTART",
                daemon=True).start()

    def _restart_ssdp(self):
        try:
            self.ssdp.restart()
            logger.info(
                f"SSDP 已按选定接口重启: {self.config.network_interface or '自动'}")
        except Exception as e:
            logger.error(f"SSDP 重启失败: {e}")

    # ── 更新检查 ──

    @staticmethod
    def _parse_version(v: str) -> tuple:
        """将版本号解析为可比较的元组，符合语义化版本（SemVer）排序

        返回 `(major, minor, patch, prerelease_key)`：

        * 正式版（无 `-` 后缀）的 prerelease_key 为 `(1, ())`，预发布版为
          `(0, ids)`。首位 1 > 0，所以 "2.1.0" > "2.1.0-beta.1"。
        * 预发布版之间按标识符逐段比较，所以 "2.1.0-rc.2" > "2.1.0-rc.1"。
        * `+` 之后的构建元数据按规范忽略，所以 "2.1.0+build.5" == "2.1.0"。

        此前实现是 `re.findall(r'\\d+', v)`，会把 "2.1.0-beta.1" 解析成
        (2, 1, 0, 1)，比正式版 "2.1.0" 的 (2, 1, 0) 还大——于是用户装了
        正式版 2.1.0，却被提示"有新版本 2.1.0-beta.1"，被推向 beta。
        """
        import re
        core = v.strip().lstrip('vV')
        # 忽略 + 之后的构建元数据（SemVer 规范：不参与优先级比较）
        core = core.split('+', 1)[0]

        m = re.match(r'^\s*(\d+(?:\.\d+)*)\s*(?:-([0-9A-Za-z.\-]*))?\s*$', core)
        if not m:
            # 兜底：抓出所有数字，按正式版处理
            nums = tuple(int(n) for n in re.findall(r'\d+', core)) or (0,)
            nums = (nums + (0, 0, 0))[:3]
            return nums + (1, ())

        nums = tuple(int(n) for n in m.group(1).split('.'))
        nums = (nums + (0, 0, 0))[:3]

        pre = m.group(2)
        if not pre:
            # 正式版：用 (1, ()) 表示，排在所有预发布版之上
            return nums + (1, ())

        # 预发布标识符逐段比较：数字按数值比，其余按字典序；
        # 数字标识符优先级低于非数字标识符（SemVer 规则 11.4）。
        ids = []
        for part in pre.split('.'):
            if part == '':
                continue
            if part.isdigit():
                ids.append((0, int(part), ''))
            else:
                ids.append((1, 0, part))
        # 预发布版：用 (0, ids) 表示。正式版首元素 1 > 预发布版 0，
        # 所以 2.1.0 > 2.1.0-beta.1；同为预发布版时再比 ids。
        return nums + (0, tuple(ids))

    def _check_update(self, params: dict) -> dict:
        """
        检查是否有新版本。
        params:
            current_version: str  当前版本号 (来自 tauri.conf.json)
            sources: list[str]    检查源，可选 "github" / "gitee"，默认 ["github"]
            force: bool           是否忽略用户设置的"此版本不提示"
        返回:
            { has_update, current, latest, url, body, source }
        """
        current_version = params.get("current_version", "0.0.0")
        sources = params.get("sources", ["github"])
        force = params.get("force", False)
        ignored_version = self.config.ignored_update_version

        # 更新源配置: (api_url, timeout)
        source_configs = {
            "github": (
                "https://api.github.com/repos/pingod/HanCast/releases/latest",
                1,   # GitHub 超时 1 秒
            ),
            "gitee": (
                "https://gitee.com/api/v5/repos/buxiangqumingzi/han-cast/releases/latest",
                3,   # Gitee 超时 3 秒
            ),
        }

        for source in sources:
            if source not in source_configs:
                continue
            api_url, timeout = source_configs[source]
            try:
                result = self._fetch_github_release(api_url, current_version, timeout=timeout)
                if result:
                    # 检查是否被用户忽略（除非 force=True）
                    if not force and ignored_version and result["latest"] == ignored_version:
                        logger.info(f"Update {result['latest']} ignored by user")
                        return {
                            "has_update": False,
                            "current": current_version,
                            "latest": result["latest"],
                            "url": "",
                            "body": "",
                            "source": source,
                        }
                    result["source"] = source
                    return result
            except Exception as e:
                logger.warning(f"Update check failed ({source}): {e}")
                continue

        # 所有源都失败或无更新
        return {
            "has_update": False,
            "current": current_version,
            "latest": current_version,
            "url": "",
            "body": "",
            "source": "",
            "error": "无法连接更新服务器",
        }

    def _fetch_github_release(self, api_url: str, current_version: str, timeout: int = 2) -> Optional[dict]:
        """从 GitHub/Gitee releases API 获取最新版本"""
        headers = {"Accept": "application/vnd.github.v3+json"}
        resp = requests.get(api_url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

        tag = data.get("tag_name", "")
        latest_version = tag.lstrip("vV")
        html_url = data.get("html_url", "")
        body = data.get("body", "") or ""

        # 比较版本
        current_tuple = self._parse_version(current_version)
        latest_tuple = self._parse_version(latest_version)

        if latest_tuple > current_tuple:
            return {
                "has_update": True,
                "current": current_version,
                "latest": latest_version,
                "url": html_url,
                "body": body[:500],  # 截断过长的 changelog
            }

        return None

    def _ignore_update_version(self, params: dict) -> bool:
        """用户选择'此版本不再提示'，记录到配置"""
        version = params.get("version", "")
        if not version:
            return False
        self.config.ignore_update_version(version)
        return True

    # ── 设备投屏确认 (Device Guard) ──

    def _on_guard_confirm_request(self, pending) -> None:
        """Guard 确认请求回调 → 推送事件到前端"""
        event_data = {
            "request_id": pending.request_id,
            "device": pending.device_info,
            "timeout": self.device_guard.confirm_timeout,
        }
        protocol_io.emit_event("cast_confirm_request", event_data)

    def _respond_cast_confirm(self, params: dict) -> bool:
        """用户响应投屏确认"""
        request_id = params["request_id"]
        approved = params["approved"]
        policy = params.get("policy", "once")  # "once" | "always" | "blacklist"
        return self.device_guard.confirm(request_id, approved, policy)

    def _get_guard_devices(self, params: dict) -> dict:
        """获取设备确认列表"""
        return {
            "trusted": self.device_guard.get_trusted_devices(),
            "blacklisted": self.device_guard.get_blacklisted_devices(),
        }

    def _remove_guard_device(self, params: dict) -> bool:
        """移除设备（从信任/黑名单中删除）"""
        device_key = params["device_key"]
        return self.device_guard.remove_device(device_key)

    def _set_guard_policy(self, params: dict) -> bool:
        """修改设备策略"""
        device_key = params["device_key"]
        policy = params["policy"]  # "trusted" | "blacklisted"
        return self.device_guard.set_policy(device_key, policy)

    def _get_guard_settings(self, params: dict) -> dict:
        """获取设备确认设置"""
        return {
            "enabled": self.device_guard.enabled,
            "confirm_timeout": self.device_guard.confirm_timeout,
        }

    def _save_guard_settings(self, params: dict) -> None:
        """保存设备确认设置"""
        if "enabled" in params:
            self.device_guard.enabled = params["enabled"]
        if "confirm_timeout" in params:
            self.device_guard.confirm_timeout = params["confirm_timeout"]

    # ── MPV 管理 ──

    def _check_mpv(self, params: dict) -> dict:
        """检查 MPV 可用状态"""
        info = self.mpv_manager.check()
        return info.to_dict()

    def _set_mpv_path(self, params: dict) -> dict:
        """手动指定 MPV 路径"""
        path = params["path"]
        info = self.mpv_manager.set_path(path)

        # 设置成功后重新加载渲染器
        # 注意：新 renderer 必须完整接入 Protocol（回调 + 反向引用）并启动，
        # 否则旧 renderer 停掉后投屏就完全失效。
        if info.status.value == "ready" and info.path:
            logger.info(f"Reloading MPV renderer with path: {info.path}")
            old_renderer = self.renderer
            new_renderer = MPVRenderer(path=info.path)
            # 1. 让 Protocol 指向新 renderer（内部会把 protocol 注入新 renderer）
            self.protocol.set_renderer(new_renderer)
            # 2. 启动新 renderer（启动 MPV 进程 + IPC 线程）
            new_renderer.start()
            self.renderer = new_renderer
            # 3. 停掉旧 renderer（幂等：未启动时静默返回）
            try:
                old_renderer.shutdown()
            except Exception as e:
                logger.warning(f"Failed to shutdown old renderer: {e}")
            logger.info("MPV renderer reloaded successfully")

        return info.to_dict()

    # ── 屏幕投屏（电脑投屏到电视 / 浏览器）──

    def _list_screen_sources(self, params: dict) -> dict:
        """枚举屏幕/窗口/音频设备 + ffmpeg 状态"""
        return self.screencast.list_sources()

    def _start_screen_cast(self, params: dict) -> dict:
        """开始屏幕投屏（target: dlna / webrtc）"""
        return self.screencast.start(params)

    def _stop_screen_cast(self, params: dict) -> dict:
        """停止屏幕投屏"""
        return self.screencast.stop()

    def _screen_cast_status(self, params: dict) -> dict:
        """屏幕投屏状态"""
        return self.screencast.status()

    def _set_ffmpeg_path(self, params: dict) -> dict:
        """手动指定 FFmpeg 路径"""
        return self.screencast.set_ffmpeg_path(params)
