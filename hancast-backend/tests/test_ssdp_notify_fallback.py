"""NOTIFY 发送失败路径：errno 告警去重 + 组播被阻断时的子网广播兜底。"""

import logging
import socket

import pytest

from hancast_sidecar import ssdp as ssdp_mod
from hancast_sidecar.ssdp import Sock, SSDPService


class _FakeUdp:
    def __init__(self, fail_with=None, log=None):
        self.fail_with = fail_with
        self.sent = []
        self.log = log

    def setsockopt(self, *args, **kwargs):
        pass

    def sendto(self, data, addr):
        if self.fail_with is not None:
            raise OSError(*self.fail_with)
        self.sent.append((data, addr))

    def close(self):
        pass


@pytest.fixture
def service():
    return SSDPService(friendly_name="T", port=8080, preferred_ip="192.168.1.5")


def _make_sock(fail_with=None):
    raw = _FakeUdp(fail_with=fail_with)
    s = Sock.__new__(Sock)
    s.ip = "192.168.1.5"
    s.sock = raw
    s.send_failed = False
    return s, raw


def test_send_it_returns_true_on_success():
    s, raw = _make_sock()
    assert s.send_it("http://{}:8080/description.xml", ("239.255.255.250", 1900))
    assert raw.sent[0][0] == b"http://192.168.1.5:8080/description.xml"


def test_send_it_warns_once_with_errno(caplog):
    s, _ = _make_sock(fail_with=(65, "No route to host"))
    with caplog.at_level(logging.WARNING):
        assert s.send_it("x{}", ("239.255.255.250", 1900)) is False
        assert s.send_it("x{}", ("239.255.255.250", 1900)) is False
        assert s.send_it("x{}", ("239.255.255.250", 1900)) is False
    msgs = [r.getMessage() for r in caplog.records if "failure sending out data" in r.getMessage()]
    assert len(msgs) == 1
    assert "No route to host" in msgs[0]


def test_notify_falls_back_to_broadcast(service, monkeypatch):
    created = []

    def fake_socket(*args, **kwargs):
        f = _FakeUdp()
        created.append(f)
        return f

    monkeypatch.setattr(ssdp_mod.socket, "socket", fake_socket)

    dead, _ = _make_sock(fail_with=(65, "No route to host"))
    service.ip_list = [("192.168.1.5", "255.255.255.0")]
    service.sock_list = [dead]
    service._known = {
        "uuid:x::upnp:rootdevice": {
            "USN": "uuid:x::upnp:rootdevice",
            "LOCATION": "http://{}:8080/description.xml",
            "ST": "upnp:rootdevice",
            "EXT": "",
            "SERVER": "fake",
            "CACHE-CONTROL": "max-age=66",
        }
    }
    service._notify_bcast_logged = False

    service._do_notify()

    bcast_socks = [f for f in created if f.sent]
    assert bcast_socks, "组播全失败时应走子网广播兜底"
    data, addr = bcast_socks[0].sent[0]
    assert addr == ("192.168.1.255", 1900)
    assert b"http://192.168.1.5:8080/description.xml" in data


def test_broadcast_keeps_sending_but_logs_once(service, monkeypatch, caplog):
    created = []
    monkeypatch.setattr(
        ssdp_mod.socket, "socket",
        lambda *a, **k: created.append(_FakeUdp()) or created[-1])
    service.ip_list = [("192.168.1.5", "255.255.255.0")]

    with caplog.at_level(logging.INFO):
        service._notify_broadcast("http://{}:8080/d")
        service._notify_broadcast("http://{}:8080/d")

    assert len([f for f in created if f.sent]) == 2, "每轮都要真发，否则对端会超时剔除"
    logs = [r.getMessage() for r in caplog.records if "已通过子网广播发送 NOTIFY" in r.getMessage()]
    assert len(logs) == 1


def test_broadcast_dead_stops_retrying(service, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise OSError(49, "Can't assign requested address")

    monkeypatch.setattr(ssdp_mod.socket, "socket", boom)
    service.ip_list = [("192.168.1.5", "255.255.255.0")]

    with caplog.at_level(logging.WARNING):
        service._notify_broadcast("http://{}:8080/d")
        service._notify_broadcast("http://{}:8080/d")

    assert service._notify_bcast_dead is True
    assert len([r for r in caplog.records if "子网广播 NOTIFY 不可用" in r.getMessage()]) == 1


def test_no_broadcast_when_multicast_ok(service, monkeypatch):
    live, _ = _make_sock()
    called = []
    monkeypatch.setattr(
        service, "_notify_broadcast",
        lambda *a, **k: called.append(a))
    service.sock_list = [live]
    service._known = {
        "uuid:x": {"USN": "uuid:x", "LOCATION": "http://{}:8080/d",
                   "ST": "x", "EXT": "", "SERVER": "s", "CACHE-CONTROL": "max-age=66"}
    }
    service._do_notify()
    assert called == []


def test_get_broadcast_ip_edge_cases(service):
    assert service._get_broadcast_ip("192.168.1.5", "255.255.255.0") == "192.168.1.255"
    assert service._get_broadcast_ip("10.0.0.5", "255.255.0.0") == "10.0.255.255"
    assert service._get_broadcast_ip("100.64.1.1", "255.255.255.255") is None
    assert service._get_broadcast_ip("bad", "255.255.255.0") is None
    assert socket.inet_aton(service._get_broadcast_ip("192.168.1.5", "255.255.255.0"))
