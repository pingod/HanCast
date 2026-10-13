"""「点了停止、电视还在播」这条链路的契约测试。

缺陷 #2 的形态是：本机把会话和 HLS 通道都拆了，但远端从没收到 AVTransport
Stop，UI 却把这次停止当成成功。所以这里锁三件事：
1. `_send_action` 解析不出控制地址时返回 None，`stop()` 必须把「没送达」带出去；
2. 会话把「没送达」记成 `stop_error`，同时**照样**释放本地会话；
3. `_stop_cast` / 屏幕投屏 stop 把 control_failed 与原因回传，前端才有话可说。
"""
from unittest.mock import MagicMock

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
    """只装配 _stop_cast 用到的字段，绕开 CommandHandler 的重量级初始化"""
    handler = CommandHandler.__new__(CommandHandler)
    handler.device_sessions = dict(sessions)
    handler.cast_state = MagicMock()
    handler.cast_state.device_id = "tv1"
    handler.cast_state.status = "playing"
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
    mgr._emit = MagicMock()
    return mgr


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
