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
