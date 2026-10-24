"""「点了停止、电视还在播」这条链路的契约测试。

缺陷 #2 的形态是：本机把会话和 HLS 通道都拆了，但远端从没收到 AVTransport
Stop，UI 却把这次停止当成成功。所以这里锁三件事：
1. `_send_action` 解析不出控制地址时返回 None，`stop()` 必须把「没送达」带出去；
2. 会话把「没送达」记成 `stop_error`，同时**照样**释放本地会话；
3. `_stop_cast` / 屏幕投屏 stop 把 control_failed 与原因回传，前端才有话可说。
"""
from unittest.mock import MagicMock
import threading

import pytest

from hancast_sidecar.commands import CommandHandler
from hancast_sidecar.models.device import Device
from hancast_sidecar.models.session import DeviceCastSession
from hancast_sidecar.protocol import dlna as dlna_mod
from hancast_sidecar.screencast.manager import ScreenCastManager


def _device(device_id="tv1"):
    return Device(id=device_id, name="客厅电视", device_type="tv",
                  ip="192.168.1.13", port=16514, status="online",
                  location="http://192.168.1.13:16514/description.xml")


@pytest.fixture(autouse=True)
def no_description_fetch(monkeypatch):
    """set_device 会去抓设备描述解析控制地址——这里只测停止语义，不碰网络"""
    monkeypatch.setattr(dlna_mod.DLNAProtocol, "_resolve_control_url",
                        lambda self: None)


def test_stop_reports_not_delivered_without_control_url(monkeypatch):
    """控制地址解析不出来 = 一条指令都没发出去，不能算「已停止」"""
    proto = dlna_mod.DLNAProtocol()
    proto.set_device(_device())
    posted = []
    monkeypatch.setattr(dlna_mod.requests, "post",
                        lambda *a, **kw: posted.append(a))
    assert proto.stop() is False
    assert posted == []


def test_stop_reports_delivered_when_action_acked(monkeypatch):
    proto = dlna_mod.DLNAProtocol()
    proto.set_device(_device())
    proto._control_url = ("http://192.168.1.13:16514/MediaRenderer/"
                          "AVTransport/Control")
    resp = MagicMock(
        status_code=200,
        content=b'<?xml version="1.0"?>'
                b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                b'<s:Body/></s:Envelope>',
        text="")
    monkeypatch.setattr(dlna_mod.requests, "post", lambda *a, **kw: resp)
    assert proto.stop() is True


def test_session_stop_keeps_reason_and_releases_local_state():
    session = DeviceCastSession(_device(), MagicMock())
    session.protocol = MagicMock()
    session.protocol.stop.return_value = False
    session.cast_state.status = "playing"

    assert session.stop() is False
    assert "客厅电视" in session.stop_error
    # 本地状态必须已经落地，否则会话残留 = 界面永远「正在投屏」
    assert session.cast_state.status == "idle"


def test_session_stop_swallows_exception_but_reports_failure():
    session = DeviceCastSession(_device(), MagicMock())
    session.protocol = MagicMock()
    session.protocol.stop.side_effect = dlna_mod.RendererUnresponsive(
        "客厅电视 的 DLNA 控制服务返回 HTTP 500")

    assert session.stop() is False
    assert "HTTP 500" in session.stop_error


def _handler_with_sessions(**sessions):
    """只装配投屏用到的字段，绕开 CommandHandler 的重量级初始化"""
    handler = CommandHandler.__new__(CommandHandler)
    handler._cast_lock = threading.RLock()
    handler.device_sessions = dict(sessions)
    handler.cast_state = MagicMock()
    handler.cast_state.device_id = "tv1"
    handler.cast_state.status = "playing"
    # 生产里 screencast 在 _start_services 末尾建好；这里给替身，
    # 让「释放会话时同步通知屏幕通道」这条契约能被断言
    handler.screencast = MagicMock()
    return handler


def test_stop_cast_returns_failed_devices_with_reasons(monkeypatch):
    monkeypatch.setattr("hancast_sidecar.commands.protocol_io.emit_event",
                        lambda *a, **kw: None)
    session = MagicMock()
    session.stop.return_value = False
    session.stop_error = "控制接口不可达"
    session.cast_state = MagicMock(status="idle", position="00:00:00",
                                   duration="00:00:00", volume=80,
                                   is_muted=True, media=None)
    handler = _handler_with_sessions(tv1=session)

    res = handler._stop_cast({"device_id": "tv1"})

    assert res["control_failed"] == ["tv1"]
    assert res["reasons"] == {"tv1": "控制接口不可达"}
    # 会话已摘、状态已置闲——停止在本地这一侧确实完成了
    assert handler.device_sessions == {}
    assert handler.cast_state.status == "idle"


def test_stop_cast_is_clean_when_device_acks(monkeypatch):
    monkeypatch.setattr("hancast_sidecar.commands.protocol_io.emit_event",
                        lambda *a, **kw: None)
    session = MagicMock()
    session.stop.return_value = True
    session.stop_error = None
    session.cast_state = MagicMock(status="idle", position="00:00:00",
                                   duration="00:00:00", volume=80,
                                   is_muted=False, media=None)
    handler = _handler_with_sessions(tv1=session)

    assert handler._stop_cast({})["control_failed"] == []


def _manager():
    handler = MagicMock()
    handler._get_local_ip.return_value = "192.168.1.5"
    mgr = ScreenCastManager(handler)
    mgr.httpd = MagicMock()
    mgr._capture = None
    mgr._session_id = "abc"
    mgr._state.update({"running": True, "target": "dlna",
                       "device_id": "tv1"})
    # 屏幕通道只认自己那份会话实例（生产里由 _get_or_create_session 登记）
    mgr._dlna_session = _screen_session("tv1")
    mgr._emit = MagicMock()
    return mgr


def _screen_session(device_id="tv1"):
    session = MagicMock()
    session.device_id = device_id
    session.owner = "screen"
    return session


def test_screencast_stop_passes_undelivered_devices():
    """屏幕投屏停了本地通道，不等于电视回到待机——未确认的设备要回传"""
    mgr = _manager()
    mgr.handler._stop_cast.return_value = {
        "ok": True, "stopped": ["tv1"], "control_failed": ["tv1"],
        "reasons": {"tv1": "客厅电视 的 DLNA 控制服务返回 HTTP 500"}}

    res = mgr.stop()

    assert res["control_failed"] == ["tv1"]
    assert "HTTP 500" in res["reasons"]["tv1"]
    assert mgr._state["running"] is False


def test_screencast_stop_reports_when_command_blows_up():
    mgr = _manager()
    mgr.handler._stop_cast.side_effect = RuntimeError("sidecar 未响应")

    res = mgr.stop()

    assert res["control_failed"] == ["tv1"]
    assert res["reasons"]["tv1"] == "sidecar 未响应"


# ── 会话归属：媒体投屏与屏幕投屏不再互相误停 ──────────────────────────

def _cast_handler():
    """够跑会话登记/释放的最小 handler（不启动任何服务）"""
    handler = CommandHandler.__new__(CommandHandler)
    handler._cast_lock = threading.RLock()
    handler.device_sessions = {}
    handler.media_server = MagicMock()
    handler.ssdp = MagicMock()
    handler.ssdp.get_device.return_value = _device()
    handler.cast_state = MagicMock(device_id=None, status="idle")
    handler.screencast = MagicMock()
    return handler


def test_media_and_screen_get_separate_sessions_for_one_tv():
    handler = _cast_handler()
    media = handler._get_or_create_session("tv1")
    screen = handler._get_or_create_session("tv1", owner="screen")
    assert media is not screen and screen.owner == "screen"
    # 同一条通道重复取用仍是同一实例，不会把正在跑的那份换掉
    assert handler._get_or_create_session("tv1", owner="screen") is screen
    assert handler.device_sessions["tv1"] is screen


def test_stop_cast_notifies_the_screen_channel(monkeypatch):
    monkeypatch.setattr("hancast_sidecar.commands.protocol_io.emit_event",
                        lambda *a, **kw: None)
    session = MagicMock()
    session.stop.return_value = True
    session.stop_error = None
    session.cast_state = MagicMock(status="idle", position="00:00:00",
                                   duration="00:00:00", volume=80,
                                   is_muted=False, media=None)
    handler = _handler_with_sessions(tv1=session)

    handler._stop_cast({"device_id": "tv1"})

    handler.screencast.release_session.assert_called_once_with(session)


def test_stop_cast_with_a_stale_session_keeps_the_new_one():
    older, newer = MagicMock(), MagicMock()
    handler = _handler_with_sessions(tv1=newer)

    res = handler._stop_cast({"device_id": "tv1"}, expected_session=older)

    assert res["stopped"] == [] and res["control_failed"] == []
    assert handler.device_sessions == {"tv1": newer}
    older.stop.assert_not_called()


def test_retired_poll_cannot_release_the_replacement_session(monkeypatch):
    emit = MagicMock()
    monkeypatch.setattr("hancast_sidecar.commands.protocol_io.emit_event", emit)
    handler = _cast_handler()
    old = handler._get_or_create_session("tv1")
    with old._poll_lock:
        generation = threading.Event()
        old._poll_generation = generation
        old._poll_running = True
    # 新会话已登记（旧线程的网络请求这时才失败返回）
    replacement = DeviceCastSession(_device(), MagicMock())
    handler.device_sessions["tv1"] = replacement

    handler._on_session_event(old, "device_unreachable", generation)

    assert handler.device_sessions["tv1"] is replacement
    emit.assert_not_called()


def test_current_poll_still_releases_its_own_session(monkeypatch):
    monkeypatch.setattr("hancast_sidecar.commands.protocol_io.emit_event",
                        lambda *a, **kw: None)
    handler = _cast_handler()
    session = handler._get_or_create_session("tv1")
    with session._poll_lock:
        generation = threading.Event()
        session._poll_generation = generation
        session._poll_running = True

    handler._on_session_event(session, "device_unreachable", generation)

    assert handler.device_sessions == {}
    handler.screencast.release_session.assert_called_once_with(session)


def test_media_cast_hands_the_tv_over_from_the_screen_channel(monkeypatch):
    monkeypatch.setattr("hancast_sidecar.commands.protocol_io.emit_event",
                        lambda *a, **kw: None)
    handler = _cast_handler()
    session = MagicMock()
    session.cast_state = MagicMock(status="playing", position="00:00:00",
                                   duration="00:00:00", volume=80,
                                   is_muted=False, media=None)
    handler._get_or_create_session = MagicMock(return_value=session)

    handler._start_cast({"device_id": "tv1",
                         "media_uri": "https://example.test/a.mp4",
                         "mime_type": "video/mp4"})

    handler.screencast.stop_for_device.assert_called_once_with("tv1")


# ── 屏幕通道：让位、误停防护、启动期间的采集错误 ──────────────────────

def test_screen_handover_stops_capture_without_stopping_the_tv():
    mgr = _manager()
    mgr._teardown = MagicMock()

    res = mgr.stop_for_device("tv1")

    assert res["released"] is True
    assert mgr._dlna_session is None
    mgr.handler._stop_cast.assert_not_called()
    mgr._teardown.assert_called_once()


def test_screen_handover_ignores_another_device():
    mgr = _manager()
    mgr._teardown = MagicMock()

    assert mgr.stop_for_device("other") == {"ok": True, "released": False}
    mgr._teardown.assert_not_called()


def test_release_of_another_channels_session_leaves_the_screen_alone():
    mgr = _manager()
    mgr._teardown = MagicMock()

    mgr.release_session(_screen_session("tv9"))

    mgr._teardown.assert_not_called()


def test_release_of_our_session_stops_the_capture():
    mgr = _manager()
    mgr._teardown = MagicMock()

    mgr.release_session(mgr._dlna_session)

    assert mgr._dlna_session is None
    mgr._teardown.assert_called_once()


def test_capture_error_during_start_is_recorded_not_dropped():
    mgr = _manager()
    mgr._state["running"] = False
    mgr._session_id = "sid"
    mgr._starting = True
    mgr._teardown = MagicMock()

    mgr._on_capture_error("屏幕捕获进程已退出（退出码 1）", "sid")

    assert mgr._pending_error == "屏幕捕获进程已退出（退出码 1）"
    mgr._teardown.assert_not_called()


def test_capture_error_from_a_retired_session_is_ignored():
    mgr = _manager()
    mgr._session_id = "new"
    mgr._teardown = MagicMock()

    mgr._on_capture_error("旧看门狗迟到的报错", "old")

    assert mgr._pending_error is None
    mgr._teardown.assert_not_called()


def test_start_reports_a_capture_error_that_landed_during_play():
    from unittest.mock import patch

    mgr = _manager()
    mgr._state.update({"running": False, "target": None, "device_id": None})
    mgr._start = MagicMock(return_value={"ok": True, "target": "browser"})
    mgr._teardown = MagicMock()
    mgr._emit = MagicMock()

    def fake_start(target, params, source, audio, video):
        # Play 之前采集就退出了：running 还是 False
        mgr._on_capture_error("屏幕捕获进程已退出（退出码 1）", mgr._session_id)
        return {"ok": True, "target": target}

    mgr._start.side_effect = fake_start
    with patch.object(mgr.ffmpeg, "check", return_value={"ok": True}), \
         patch.object(mgr.ffmpeg, "_list_audio_devices", return_value=[]):
        res = mgr.start({"target": "browser", "source": {"type": "screen"}})

    assert res == {"ok": False, "error": "屏幕捕获进程已退出（退出码 1）"}
    mgr._teardown.assert_called_once()
    assert mgr._state["running"] is False
