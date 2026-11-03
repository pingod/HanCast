"""手机遥控器页的设备列表与手动刷新（v2.4.11）。

用户报的两件事：
1. 「手机控制页面无法准确列出局域网设备」——页面下拉只吃 `sessions`（正在投的那几台），
   没在投的电视在手机上看不见、选不了，而电脑端明明列着它们。
2. 「改成手动刷新方式」——页面每秒轮询，设备列表一跳一跳、选中项自己变，看起来像列表不准。

这里钉住后端一侧的契约：状态快照里的 `devices` 是**全部已知设备**、聚焦可以是没在投的设备、
`refresh_devices` 真的发起重扫；页面那一侧则确认没有轮询、刷新按钮真的走 `refresh_devices`。
"""
import pytest

import hancast_sidecar.commands as commands_mod
import hancast_sidecar.remote.server as remote_server
from hancast_sidecar.commands import CommandHandler


class _StubDevice:
    def __init__(self, did, name, ip="192.168.1.50"):
        self.id = did
        self.display_name = name
        self.ip = ip


@pytest.fixture()
def handler(monkeypatch):
    h = CommandHandler()
    h._events = []
    monkeypatch.setattr(commands_mod.protocol_io, "emit_event",
                        lambda name, data: h._events.append((name, data)))
    # SSDP 结果是外部状态：这里直接给一份"局域网里有什么"
    h._ssdp_devices = [
        _StubDevice("tv-1", "客厅电视", "192.168.1.61"),
        _StubDevice("tv-2", "卧室电视", "192.168.1.62"),
    ]
    monkeypatch.setattr(h.ssdp, "get_devices", lambda: list(h._ssdp_devices))
    h._scans = []
    monkeypatch.setattr(h.ssdp, "scan", lambda: h._scans.append(1))
    yield h
    try:
        h.cleanup()
    except Exception:
        pass


def _fake_session(did, name, status="playing", title="片名"):
    class _St:
        pass
    st = _St()
    st.status = status
    st.position = "00:00:10"
    st.duration = "00:10:00"
    st.volume = 50
    st.is_muted = False
    st.media = _St()
    st.media.title = title
    st.media.thumbnail = None
    sess = _St()
    sess.cast_state = st
    sess.device = _St()
    sess.device.display_name = name
    return sess


def test_设备列表含没有会话的设备(handler):
    """核心回归：正在投的那台之外，局域网里其它设备也必须出现在列表里。"""
    handler.device_sessions = {"tv-1": _fake_session("tv-1", "客厅电视")}
    handler.cast_state.device_id = "tv-1"
    state = handler._remote_sessions_state()
    ids = [d["device_id"] for d in state["devices"]]
    assert ids == ["tv-1", "tv-2"], ids
    tv2 = next(d for d in state["devices"] if d["device_id"] == "tv-2")
    assert tv2["online"] is True
    assert tv2["session_status"] == ""      # 没有会话
    assert tv2["ip"] == "192.168.1.62"


def test_隐藏设备不进列表(handler):
    handler._ssdp_devices = [_StubDevice("tv-1", "客厅电视")]
    handler.config.set("hidden_devices", ["tv-2"])
    try:
        ids = [d["device_id"] for d in handler._remote_sessions_state()["devices"]]
        assert ids == ["tv-1"], ids
    finally:
        handler.config.set("hidden_devices", [])


def test_会话还在但设备掉线时标离线(handler):
    handler.device_sessions = {"tv-9": _fake_session("tv-9", "老电视")}
    state = handler._remote_sessions_state()
    gone = next(d for d in state["devices"] if d["device_id"] == "tv-9")
    assert gone["online"] is False
    assert gone["session_status"] == "playing"


def test_聚焦可以是没在投的设备(handler):
    """电视在列表里就能选——以前只认会话，没在投的永远选不上。"""
    handler.device_sessions = {}
    handler.cast_state.device_id = None
    r = handler._remote_action("focus_device", "tv-2")
    assert r["device_id"] == "tv-2"
    assert handler.cast_state.device_id == "tv-2"


def test_聚焦未知设备不改状态(handler):
    handler.cast_state.device_id = "tv-1"
    handler._remote_action("focus_device", "not-a-device")
    assert handler.cast_state.device_id == "tv-1"


def test_聚焦为空时回落到第一台(handler):
    handler.device_sessions = {}
    handler.cast_state.device_id = "not-a-device"
    assert handler._remote_sessions_state()["focus"] == "tv-1"


def test_手动刷新会发起局域网重扫(handler):
    r = handler._remote_action("refresh_devices", None)
    assert r["scanning"] is True
    assert handler._scans == [1]


def test_页面不轮询且刷新走refresh_devices():
    """页面侧契约：去掉每秒轮询，刷新按钮先重扫再拉状态。"""
    page = remote_server._PAGE
    assert "setInterval(poll" not in page, "页面不应再自动轮询状态"
    assert "async function refresh()" in page
    assert "'refresh_devices'" in page
    assert "state.devices" in page, "下拉必须吃 state.devices（全量设备）而不是 state.sessions"


def test_前端报错落进同一份日志(caplog):
    """`frontend_log` 命令：WebView 里的报错要能在后台日志面板里查到。

    这是「媒体输入区自己消失了却查不到任何线索」那次的直接补丁——前端把
    error / unhandledrejection / 组件错误都走这条命令落盘。
    """
    h = CommandHandler()
    try:
        with caplog.at_level("ERROR", logger="hancast.commands"):
            r = h._frontend_log({"level": "error", "message": "区块炸了",
                                 "stack": "Error: x\n  at setup", "source": "section"})
        assert r == {"ok": True}
        text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "[webview/section] 区块炸了" in text
        assert "at setup" in text
    finally:
        try:
            h.cleanup()
        except Exception:
            pass


def test_前端报错长度被截断(caplog):
    """超长正文要截断，不许把整段落盘（DIDL/堆栈可能很长）。"""
    h = CommandHandler()
    try:
        with caplog.at_level("ERROR", logger="hancast.commands"):
            h._frontend_log({"level": "error", "message": "x" * 5000, "stack": "y" * 9000})
        text = "\n".join(rec.getMessage() for rec in caplog.records)
        assert len(text) < 7000, len(text)
    finally:
        try:
            h.cleanup()
        except Exception:
            pass
