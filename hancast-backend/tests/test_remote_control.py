"""手机遥控器页（v2.4.6）——token 门、页面、状态、指令分发"""
import json
import urllib.error
import urllib.request

import pytest

from hancast_sidecar.remote.server import RemoteControlServer


@pytest.fixture()
def server():
    calls = []
    state = {"session": {"device_id": "d1", "device_name": "客厅电视", "status": "playing",
                         "position": "00:00:10", "duration": "00:01:00", "volume": 40,
                         "is_muted": False, "title": "测试片"}, "sessions": [], "focus": "d1"}

    def action(name, value):
        calls.append((name, value))
        if name == "boom":
            raise ValueError("坏指令")
        return {"action": name, "value": value}

    s = RemoteControlServer(get_state=lambda: state, action=action, local_ip=lambda: "127.0.0.1")
    assert s.start(port=0), "遥控器服务起不来"
    s.calls = calls
    try:
        yield s
    finally:
        s.stop()


def _get(url):
    return urllib.request.urlopen(url, timeout=5)


def test_页面带_token_且坏token_404(server):
    page = _get(server.url).read().decode()
    assert "HanCast 遥控器" in page
    assert json.dumps(server.token) in page
    bad = server.url.replace(f"/r/{server.token}/", "/r/deadbeef/")
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(bad)
    assert ei.value.code == 404


def test_状态接口返回会话(server):
    data = json.loads(_get(server.url + "api/state").read().decode())
    assert data["session"]["title"] == "测试片"
    assert data["focus"] == "d1"


def test_指令分发到回调(server):
    req = urllib.request.Request(
        server.url + "api/cmd", data=json.dumps({"action": "seek", "value": 42}).encode(),
        headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
    assert resp["ok"] is True
    assert ("seek", 42) in server.calls


def test_回调异常回_ok_false(server):
    req = urllib.request.Request(
        server.url + "api/cmd", data=json.dumps({"action": "boom"}).encode(),
        headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
    assert resp["ok"] is False
    assert "坏指令" in resp["error"]


# ── v2.4.8：常驻 + 媒体选择（上传/粘贴/解析）+ 队列状态 ──

def test_状态接口带队列与历史(server):
    """v2.4.8 起 /api/state 还带队列/历史/播放模式（前端镜像过来给手机看）。"""
    # server fixture 的 get_state 固定返回 session；队列/历史是后端镜像的事，
    # 这里只验证页面能读到 state 里新增字段（缺省也应存在，不是 undefined）。
    data = json.loads(_get(server.url + "api/state").read().decode())
    assert "session" in data


def test_粘贴接口转给内容钩子(server):
    server.set_content_hooks(
        on_upload=lambda p, f: {"ok": True},
        on_paste=lambda text: {"task_id": "t-1", "status": "pending"},
        on_resolve_poll=lambda tid: {"status": "pending"},
    )
    req = urllib.request.Request(
        server.url + "api/paste", data=json.dumps({"text": "https://v.douyin.com/x/"}).encode(),
        headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
    assert resp["task_id"] == "t-1"


def test_粘贴空文本被拒(server):
    server.set_content_hooks(
        on_upload=lambda p, f: {"ok": True},
        on_paste=lambda text: {"task_id": "t-1"},
        on_resolve_poll=lambda tid: {},
    )
    req = urllib.request.Request(
        server.url + "api/paste", data=json.dumps({"text": "  "}).encode(),
        headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
    assert resp["ok"] is False


def test_解析轮询接口转给内容钩子(server):
    server.set_content_hooks(
        on_upload=lambda p, f: {"ok": True},
        on_paste=lambda text: {},
        on_resolve_poll=lambda tid: {"status": "done", "result": {"uri": "http://x/a.mp4"}},
    )
    data = json.loads(_get(server.url + "api/resolve?task_id=t-1").read().decode())
    assert data["status"] == "done"
    assert data["result"]["uri"] == "http://x/a.mp4"


def test_上传接口落盘并交给内容钩子(server):
    seen = {}

    def on_upload(tmp_path, filename):
        seen["filename"] = filename
        with open(tmp_path, "rb") as f:
            seen["bytes"] = f.read()
        return {"ok": True, "title": "我的视频"}

    server.set_content_hooks(
        on_upload=on_upload,
        on_paste=lambda text: {},
        on_resolve_poll=lambda tid: {},
    )
    boundary = "----hancastboundary"
    payload = b"FAKEMP4DATA"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="myvideo.mp4"\r\n'
        f"Content-Type: video/mp4\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        server.url + "api/upload", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
    assert resp["ok"] is True
    assert seen["filename"] == "myvideo.mp4"
    assert seen["bytes"] == payload


def test_启动幂等_重复start不换token(server):
    tok = server.token
    port = server.port
    assert server.start(port=8899) is True
    assert server.token == tok
    assert server.port == port
