"""
设备投屏会话 - 管理单个设备的投屏状态和控制
"""
import threading
import logging
from typing import Optional, Callable, Any
from .cast import CastState
from ..protocol.dlna import DLNAProtocol, build_didl_item
from .device import Device

logger = logging.getLogger("hancast.session")


class DeviceCastSession:
    """
    单个设备的投屏会话

    每个设备维护独立的：
    - DLNAProtocol 实例（协议控制）
    - CastState 状态（播放状态）
    - 进度轮询线程
    """

    # 连续轮询失败上限（每秒 1 次 ≈ 10 秒），超过即判定设备失联
    POLL_FAIL_LIMIT = 10

    def __init__(self, device: Device, media_server: Any):
        self.device = device
        self.device_id = device.id
        self.protocol = DLNAProtocol()
        self.protocol.set_device(device)
        self.cast_state = CastState(device_id=self.device_id)
        self.media_server = media_server
        # 同一个电视可能被「媒体投屏」和「屏幕投屏」两条通道争用；归属写在
        # 会话实例上，停止通道时只认自己那一份，不再按 device_id 误停对方。
        self.owner = "media"

        # 每轮独立的取消令牌同时作为代次标识，旧线程不能借新一轮的 running 复活。
        # 锁序：handler registry 锁 -> 本锁；网络调用与上层回调都不能持有本锁。
        self._poll_lock = threading.Lock()
        self._poll_generation: Optional[threading.Event] = None
        self._poll_running = False
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_callback: Optional[
            Callable[["DeviceCastSession", str, threading.Event], None]
        ] = None
        # 最近一次 stop() 未送达的原因（None = 已送达或尚未停止）
        self.stop_error: Optional[str] = None

        logger.info(f"DeviceCastSession created for: {device.display_name} ({device.ip})")

    def refresh_device(self, device: Device) -> None:
        """换用最新的设备记录并重解析控制地址

        老会话里的 Device 可能是修复前发现的（没有描述地址），控制地址解析
        不出来就只能一直「推送成功但电视黑屏」。
        """
        self.device = device
        self.protocol.refresh_device(device)

    def play(self, media_uri: str, mime_type: str = "",
             metadata: str = "") -> None:
        """播放媒体；调用方在 handler registry 锁下串行化播放替换。"""
        # 必须先失效旧轮询，包括视频切图片，以及 serve_file/play 失败的路径。
        self.stop_poll()
        # 如果是本地文件，通过媒体服务器提供 HTTP 访问
        if not media_uri.startswith("http"):
            media_uri = self.media_server.serve_file(media_uri)

        # `mime_type` 传进来就断了是 2026-10-28 查出的口子：`_start_cast` 收了前端
        # 的 mimeType，这里却只把 metadata 透给协议层，而媒体投屏这条路径没人构造
        # metadata ⇒ 电视收到的 CurrentURIMetaData 恒为空串，只能靠自己猜类型。
        # 图片保持原样（build_didl_item 写死 object.item.videoItem，别给图片下发错类）。
        if not metadata and mime_type and not mime_type.startswith("image/"):
            media = getattr(self.cast_state, "media", None)
            metadata = build_didl_item(
                media_uri, mime_type, getattr(media, "title", "") or "HanCast")

        self.protocol.play(media_uri, metadata)
        position = self.protocol.get_state_position() or "00:00:00"
        duration = self.protocol.get_state_duration() or "00:00:00"
        with self._poll_lock:
            self.cast_state.status = "playing"
            self.cast_state.position = position
            self.cast_state.duration = duration

        # 图片类媒体不需要轮询进度
        if mime_type.startswith("image/"):
            logger.info(f"Image cast, skip progress polling: {mime_type}")
        else:
            self.start_poll()

    def stop(self) -> bool:
        """停止播放。

        返回 False 表示远端没有确认收到 Stop（设备失联、控制服务已退出、或
        本机根本没解析出它的控制地址），但本地会话一律已经释放——调用方必须
        照常丢弃该会话，否则残留会话会让用户「怎么点都停不掉投屏」。
        失败原因记在 `stop_error`，供前端把「界面停了但电视还在播」讲清楚。
        """
        self.stop_poll()
        self.stop_error = None
        ok = True
        try:
            if not self.protocol.stop():
                ok = False
                self.stop_error = (
                    f"{self.device.display_name} 的播放控制接口不可达，"
                    f"停止指令没有送达")
                logger.warning(f"Stop action skipped for "
                               f"{self.device.display_name}: no control URL")
        except Exception as e:
            ok = False
            self.stop_error = str(e) or e.__class__.__name__
            logger.warning(
                f"Stop action failed for {self.device.display_name}: {e}")
        self.cast_state.status = "idle"
        return ok

    def pause(self) -> None:
        """暂停播放"""
        self.protocol.pause()
        self.cast_state.status = "paused"

    def resume(self) -> None:
        """恢复播放"""
        self.protocol.play()
        self.cast_state.status = "playing"

    def seek(self, position: str) -> None:
        """跳转进度"""
        self.protocol.seek(position)

    def set_volume(self, volume: int) -> int:
        """设置音量"""
        self.protocol.set_volume(volume)
        self.cast_state.volume = volume
        return volume

    def set_mute(self, muted: bool) -> None:
        """设置静音"""
        self.protocol.set_mute(muted)
        self.cast_state.is_muted = muted

    def get_state(self) -> dict:
        """获取投屏状态"""
        return self.cast_state.to_dict()

    def get_cast_info(self) -> dict:
        """获取详细的投屏信息"""
        url = self.protocol.get_state_url()
        title = self.protocol.get_state_title()
        # position/duration 由 _poll_loop 轮询远端设备后写入 cast_state，
        # protocol 内部 state 未同步这些值，因此必须从 cast_state 读取
        return {
            "device_id": self.device_id,
            "device_name": self.device.display_name,
            "url": url or "",
            "title": title or "",
            "duration": self.cast_state.duration or "00:00:00",
            "position": self.cast_state.position or "00:00:00",
            "status": self.cast_state.status or "idle",
            "cast_state": self.cast_state.to_dict(),
        }

    def set_poll_callback(
        self, callback: Callable[["DeviceCastSession", str, threading.Event], None]
    ) -> None:
        """设置 callback(session, event_type, generation)，在 session 锁外调用。"""
        with self._poll_lock:
            self._poll_callback = callback

    def is_current_poll(self, generation: threading.Event) -> bool:
        """代次是否仍有效（不等于线程仍在运行）。

        STOPPED/连续失败自行结束时保留代次，以便递送最后一个事件；显式
        stop_poll 或下一次启动才失效。handler 必须在 registry 锁下同时校验
        映射对象 is session 和此方法，因为锁外回调可能跨越播放替换。
        """
        with self._poll_lock:
            return (generation is not None
                    and self._poll_generation is generation
                    and not generation.is_set())

    def start_poll(self) -> None:
        """启动新一代轮询；正在运行时保持幂等。"""
        with self._poll_lock:
            if self._poll_running:
                return
            if self._poll_generation is not None:
                self._poll_generation.set()
            generation = threading.Event()
            self._poll_generation = generation
            self._poll_running = True
            thread = threading.Thread(
                target=self._poll_loop, args=(generation,), daemon=True)
            self._poll_thread = thread
        thread.start()
        logger.debug(f"Progress polling started for device: {self.device.display_name}")

    def stop_poll(self) -> None:
        """失效当前代次并唤醒等待；不 join 尚在网络调用或等待 handler 锁的线程。"""
        with self._poll_lock:
            if self._poll_generation is not None:
                self._poll_generation.set()
            self._poll_generation = None
            self._poll_running = False
        logger.debug(f"Progress polling stopped for device: {self.device.display_name}")

    def _emit_poll_event(self, generation: threading.Event, event_type: str) -> None:
        with self._poll_lock:
            if self._poll_generation is not generation or generation.is_set():
                return
            callback = self._poll_callback
        # 不可持 session 锁回调：handler 持 registry 锁后会再次校验代次。
        if callback:
            try:
                callback(self, event_type, generation)
            except Exception:
                # 上层回调异常不是设备失联，不计入网络连续失败次数。
                logger.exception("Poll callback failed: %s", event_type)

    def _poll_loop(self, generation: threading.Event) -> None:
        """轮询远程设备；网络返回后校验代次与写状态必须在同一临界区。"""
        transport_check_counter = 0
        consecutive_failures = 0
        with self._poll_lock:
            if self._poll_generation is not generation or generation.is_set():
                return
            last_status = self.cast_state.status
        # 部分设备需要 2-3 秒加载媒体元数据；取消必须能立即唤醒这段等待。
        if generation.wait(2.0):
            return

        while self.is_current_poll(generation):
            try:
                pos_info = self.protocol.get_position_info()
                duration = pos_info.get('TrackDuration', '00:00:00')
                position = pos_info.get('RelTime', '00:00:00')
                if not self.is_current_poll(generation):
                    return

                # 传输状态只每 3 秒查一次（变化不频繁，减少网络开销）。
                transport_check_counter += 1
                check_transport = transport_check_counter >= 3
                state = ''
                if check_transport:
                    transport_check_counter = 0
                    transport_info = self.protocol.get_transport_info()
                    state = transport_info.get('CurrentTransportState', '')
            except Exception as e:
                with self._poll_lock:
                    if self._poll_generation is not generation or generation.is_set():
                        return
                    consecutive_failures += 1
                    terminal = consecutive_failures >= self.POLL_FAIL_LIMIT
                    if terminal:
                        self.cast_state.status = "idle"
                        # 自行终止不取消代次，最后一个事件仍可通过 handler 校验。
                        self._poll_running = False
                logger.warning(
                    f"Polling failed for device {self.device.display_name}"
                    f" ({consecutive_failures}/{self.POLL_FAIL_LIMIT}): {e}")
                if terminal:
                    logger.error(
                        f"Device {self.device.display_name} unreachable, "
                        f"release session")
                    self._emit_poll_event(generation, "device_unreachable")
                    return
            else:
                events = []
                with self._poll_lock:
                    if self._poll_generation is not generation or generation.is_set():
                        return
                    terminal = state == 'STOPPED'
                    if terminal:
                        self.cast_state.status = "idle"
                        self._poll_running = False
                        events.append("playback_stopped")
                    else:
                        if state == 'PLAYING':
                            self.cast_state.status = "playing"
                        elif state == 'PAUSED_PLAYBACK':
                            self.cast_state.status = "paused"
                        if check_transport and self.cast_state.status != last_status:
                            last_status = self.cast_state.status
                            events.append("status_changed")

                        # 避免设备的空值覆盖已有进度。
                        if duration and duration != '00:00:00':
                            self.cast_state.duration = duration
                        if position and position != '00:00:00':
                            self.cast_state.position = position
                        consecutive_failures = 0
                        events.append("progress_update")
                if terminal:
                    logger.info(f"Playback stopped on device: {self.device.display_name}")
                elif "status_changed" in events:
                    logger.info(
                        f"Device {self.device.display_name} status changed: {last_status}")
                for event_type in events:
                    self._emit_poll_event(generation, event_type)
                if terminal:
                    return

            if generation.wait(1.0):
                return
