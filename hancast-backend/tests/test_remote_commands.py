"""手机遥控器页的命令层（v2.4.8）—— 队列类指令回推事件、上传/聚焦投屏、UI 快照同步。

这些方法本身不直接播，而是要么回推 `remote_command` 事件交前端执行，要么复用
`_start_cast`。测试用 stub 隔离掉真实投屏与协议层，只钉「意图对不对」。
"""
import json
import os
import tempfile

import pytest

import hancast_sidecar.commands as commands_mod
from hancast_sidecar.commands import CommandHandler


@pytest.fixture()
def handler(monkeypatch):
    h = CommandHandler()
    h._events = []
    # 拦掉真正出通道的事件与投屏，只记录意图
    monkeypatch.setattr(commands_mod.protocol_io, "emit_event",
                        lambda name, data: h._events.append((name, data)))
    monkeypatch.setattr(h, "_start_cast", lambda params: h._events.append(("CAST", params)))
    yield h
    try:
        h.cleanup()
    except Exception:
        pass


def test_切集指令回推事件不自己播(handler):
    handler.cast_state.device_id = "d1"
    r = handler._remote_action("next", None)
    assert r["delegated"] is True
    assert ("remote_command", {"action": "next", "value": None, "device_id": "d1"}) in handler._events


def test_改播放模式回推事件(handler):
    handler.cast_state.device_id = "d1"
    handler._remote_action("mode", "shuffle")
    ev = [e for e in handler._events if e[0] == "remote_command"]
    assert ev and ev[-1][1]["action"] == "mode" and ev[-1][1]["value"] == "shuffle"


def test_播完即停回推事件(handler):
    handler.cast_state.device_id = "d1"
    handler._remote_action("stop_after", True)
    ev = [e for e in handler._events if e[0] == "remote_command"]
    assert ev and ev[-1][1]["action"] == "stop_after" and ev[-1][1]["value"] is True


def test_未知指令抛错(handler):
    with pytest.raises(ValueError):
        handler._remote_action("frobnicate", None)


def test_上传把临时文件投给聚焦设备(handler):
    handler.cast_state.device_id = "d1"
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.write(b"x")
    tmp.close()
    try:
        r = handler._remote_upload(tmp.name, "我的视频.mp4")
        assert r["device_id"] == "d1"
        assert r["title"] == "我的视频"
        casts = [e for e in handler._events if e[0] == "CAST"]
        assert casts and casts[-1][1]["media_uri"] == tmp.name
        assert casts[-1][1]["title"] == "我的视频"
    finally:
        os.unlink(tmp.name)


def test_上传无聚焦设备报错(handler, monkeypatch):
    """「没有聚焦设备」这个前提必须自己钉住

    这条用例原来只清了 `cast_state.device_id`，而 `_remote_upload` 还会退回去读
    遥控器页的 `focus`。本机局域网里只要有设备在（SSDP 真发现了一台），`focus`
    就非空，用例于是「DID NOT RAISE」——判据依赖环境，红的其实是测试自己。
    这里把 sessions 状态一并桩成空，测的才是它想测的那条分支。
    """
    handler.cast_state.device_id = ""
    monkeypatch.setattr(handler, "_remote_sessions_state", lambda: {"focus": "", "sessions": {}})
    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()
    try:
        with pytest.raises(ValueError):
            handler._remote_upload(tmp.name, "a.mp4")
    finally:
        os.unlink(tmp.name)


def test_ui快照写入镜像并可读回(handler):
    handler.execute("remote_update_ui", {
        "queue": [{"title": "第一集", "playing": True}],
        "history": [{"title": "昨儿", "device_name": "客厅", "kind": "url"}],
        "play_mode": "shuffle",
        "stop_after_current": True,
    })
    state = handler._remote_sessions_state()
    assert state["queue"][0]["title"] == "第一集"
    assert state["history"][0]["title"] == "昨儿"
    assert state["play_mode"] == "shuffle"
    assert state["stop_after_current"] is True


def test_start_cast接受title覆盖(handler, monkeypatch):
    """_start_cast 收到 title 时要把标题塞进 cast_state.media（供 DIDL 用）。"""
    # handler fixture 把 _start_cast 换成了记录 lambda，这里装回真方法再测
    real_start_cast = CommandHandler._start_cast.__get__(handler)
    monkeypatch.setattr(handler, "_start_cast", real_start_cast)
    captured = {}

    class FakeState:
        def __init__(self):
            self.media = None
            self.status = "playing"
            self.position = "00:00:00"
            self.duration = "00:00:00"
            self.volume = 50
            self.is_muted = False
    class FakeSession:
        def __init__(self):
            self.cast_state = FakeState()
        def play(self, uri, mime=""):
            captured["played"] = True
    fake = FakeSession()
    monkeypatch.setattr(handler, "_get_or_create_session", lambda did: fake)
    monkeypatch.setattr(handler.screencast, "stop_for_device", lambda did: None)
    handler._start_cast({"device_id": "d1", "media_uri": "http://x/a.mp4",
                         "mime_type": "video/mp4", "title": "漂亮标题"})
    assert fake.cast_state.media is not None
    assert fake.cast_state.media.title == "漂亮标题"
    assert captured.get("played") is True
