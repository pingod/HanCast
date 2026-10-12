"""发现链路取证：设备描述抓取失败必须重试并留告警；NOTIFY 心跳要报通道与条数。

对应缺陷「macOS 端发现不了 Windows 设备」的接收侧：原实现把抓取失败记成
DEBUG，且 location 已在去重表里 → 一次拦截就让设备缺席整个去重窗口。
"""

import logging
import time

import pytest
import requests

from hancast_sidecar.models.device import Device
from hancast_sidecar.ssdp import Sock, SSDPService

LOC = "http://192.168.1.68:8080/description.xml"

RENDERER_DESC = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <device>
  <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
  <friendlyName>WIN-PC HanCast</friendlyName>
  <UDN>uuid:9d2b1c00-1111-2222-3333-444455556666</UDN>
 </device>
</root>
"""


class _FakeUdp:
    def __init__(self, fail_with=None):
        self.fail_with = fail_with
        self.sent = []

    def setsockopt(self, *a, **k):
        pass

    def sendto(self, data, addr):
        if self.fail_with is not None:
            raise OSError(*self.fail_with)
        self.sent.append((data, addr))

    def close(self):
        pass


def _make_sock(fail_with=None, ip="192.168.1.68"):
    raw = _FakeUdp(fail_with)
    s = Sock.__new__(Sock)
    s.ip = ip
    s.sock = raw
    s.send_failed = False
    return s


@pytest.fixture
def service():
    return SSDPService(friendly_name="T", port=8080, preferred_ip="192.168.1.68")


def _known_entry():
    return {"USN": "uuid:x::upnp:rootdevice",
            "LOCATION": "http://{}:8080/description.xml",
            "ST": "upnp:rootdevice", "EXT": "", "SERVER": "fake",
            "CACHE-CONTROL": "max-age=66"}


class _Resp:
    text = RENDERER_DESC
    apparent_encoding = "utf-8"
    encoding = "utf-8"


def test_fetch_failure_releases_dedup_and_warns_once(service, monkeypatch, caplog):
    def boom(*a, **k):
        raise requests.ConnectionError(
            "HTTPConnectionPool(host='192.168.1.68', port=8080): "
            "[WinError 10061] No connection could be made")

    monkeypatch.setattr("hancast_sidecar.ssdp.requests.get", boom)

    with caplog.at_level(logging.DEBUG):
        service._fetch_device_from_notify(LOC, "192.168.1.68")
        service._fetch_device_from_notify(LOC, "192.168.1.68")

    assert service._fetch_failures[LOC] == 2
    # 去重表被放开：下一轮 alive 会真的重试，而不是继续沉默
    assert service._fetched_locations[LOC] == 0.0
    warns = [r.getMessage() for r in caplog.records
             if "抓取设备描述失败" in r.getMessage()]
    assert len(warns) == 1, "首次告警、之后降级，避免每 30s 刷屏"
    assert "防火墙" in warns[0] and "192.168.1.68:8080" in warns[0]


def test_fetch_success_clears_failure(service, monkeypatch):
    monkeypatch.setattr("hancast_sidecar.ssdp.requests.get",
                        lambda *a, **k: _Resp())
    service._fetch_failures[LOC] = 3
    found = []
    service._on_device_found = found.append

    service._fetch_device_from_notify(LOC, "192.168.1.68")

    assert LOC not in service._fetch_failures
    dev = service.get_device("uuid:9d2b1c00-1111-2222-3333-444455556666")
    assert dev is not None and dev.device_type == "tv"
    assert dev.location == LOC, "控制地址要以描述地址为基准解析"
    assert found and found[0] is dev


def test_unparseable_description_warns(service, monkeypatch, caplog):
    class _Html:
        text = "<html>404 Not Found</html>"
        apparent_encoding = "utf-8"
        encoding = "utf-8"

    monkeypatch.setattr("hancast_sidecar.ssdp.requests.get",
                        lambda *a, **k: _Html())
    with caplog.at_level(logging.WARNING):
        service._fetch_device_from_notify(LOC, "192.168.1.68")
    assert "设备描述无法解析" in caplog.text
    assert service.get_devices() == []


def test_notify_alive_retries_after_failure(service, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_fetch_device_from_notify",
                        lambda loc, ip: calls.append((loc, ip)))
    service._fetched_locations[LOC] = 0.0  # 失败后留下的状态
    headers = {"nts": "ssdp:alive", "location": LOC, "usn": "uuid:x"}

    service._handle_notify(headers, ("192.168.1.68", 1900))
    time.sleep(0.3)
    assert calls == [(LOC, "192.168.1.68")]

    # 成功抓取后（去重时间戳为当前时间）同窗口内的重复 alive 不再重抓
    service._fetched_locations[LOC] = time.time()
    service._handle_notify(headers, ("192.168.1.68", 1900))
    time.sleep(0.3)
    assert len(calls) == 1


def test_register_keeps_custom_name_and_notifies_once(service):
    seen = []
    service._on_device_found = seen.append
    first = Device(id="uuid:1", name="TCL TV", device_type="tv",
                   ip="192.168.1.13", port=16514, status="online")
    service._register_found_device(first, "NOTIFY")
    second = Device(id="uuid:1", name="TCL TV 改名前的名字", device_type="tv",
                    ip="192.168.1.13", port=16514, status="online")
    second.custom_name = "客厅电视"
    service._register_found_device(second, "M-SEARCH")

    assert len(seen) == 1
    assert service.get_device("uuid:1").custom_name == "客厅电视"


def test_notify_heartbeat_reports_channel_and_count(service, monkeypatch):
    service.ip_list = [("192.168.1.68", "255.255.255.0")]
    service.sock_list = [_make_sock(), _make_sock(ip="192.168.137.1")]
    service._known = {"uuid:x": _known_entry()}

    service._do_notify()
    assert service._last_notify_count == 2
    assert service._notify_channel == "multicast"

    service.sock_list = [_make_sock(fail_with=(65, "No route to host"))]
    monkeypatch.setattr(ssdp_socket_module(), "socket",
                        lambda *a, **k: _FakeUdp())
    service.ip_list = [("192.168.1.68", "255.255.255.0")]
    service._notify_bcast_logged = True
    service._do_notify()
    assert service._notify_channel == "broadcast"
    assert service._last_notify_count == 1, "广播兜底同样要计入心跳统计"


def ssdp_socket_module():
    from hancast_sidecar import ssdp as m
    return m.socket


def test_notify_loop_does_not_spin_on_error(service, monkeypatch):
    sleeps = []

    def fake_sleep(_):
        sleeps.append(1)
        service._running = False

    monkeypatch.setattr("hancast_sidecar.ssdp.time.sleep", fake_sleep)
    monkeypatch.setattr(service, "_do_notify",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    service._running = True
    service._notify_loop()

    assert len(sleeps) == 1, "异常后必须退避，否则 30s 睡眠被跳过 → 死循环刷日志"


def test_heartbeat_logs_first_and_every_twentieth_round(service, monkeypatch, caplog):
    state = {"round": 0}

    def fake_do_notify():
        state["round"] += 1

    def fake_sleep(_):
        if state["round"] >= 21:
            service._running = False

    monkeypatch.setattr(service, "_do_notify", fake_do_notify)
    monkeypatch.setattr("hancast_sidecar.ssdp.time.sleep", fake_sleep)
    service.ip_list = [("192.168.1.68", "255.255.255.0")]
    service._last_notify_count = 6
    service._running = True

    with caplog.at_level(logging.INFO):
        service._notify_loop()

    assert state["round"] == 21
    beats = [r.getMessage() for r in caplog.records
             if "NOTIFY alive 心跳" in r.getMessage()]
    assert len(beats) == 2, "首轮 + 每 20 轮（约 10 分钟）各一次，平时不刷屏"
    assert "192.168.1.68" in beats[0] and "multicast" in beats[0]


def test_description_fetch_bypasses_system_proxy(service, monkeypatch):
    seen = {}

    def fake_get(url, timeout=None, **kw):
        seen.update(kw)
        return _Resp()

    monkeypatch.setattr("hancast_sidecar.ssdp.requests.get", fake_get)
    service._fetch_device_from_notify(LOC, "192.168.1.68")
    assert seen.get("proxies") == {"http": "", "https": ""}


def test_own_ip_list_excludes_virtual_nic_style_addresses(service):
    # 本机地址算「自己」，异机地址必须照抓
    service.ip_list = [("192.168.1.5", "255.255.255.0"),
                       ("192.168.215.0", "255.255.255.0")]
    assert service._is_own_location("http://192.168.215.0:8080/description.xml")
    assert service._is_own_location("http://192.168.1.5:8080/description.xml")
    assert not service._is_own_location(LOC)
    assert not service._is_own_location("urn:schemas-upnp-org:service:AVTransport:1")
    assert not service._is_own_location("://bad url")


def test_self_announce_is_not_fetched_on_either_path(service, monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(service, "_fetch_device_from_notify",
                        lambda loc, ip: calls.append((loc, ip)))
    monkeypatch.setattr("hancast_sidecar.ssdp.requests.get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            requests.ConnectionError("[Errno 49] Can't assign requested address")))
    service.ip_list = [("192.168.1.5", "255.255.255.0"),
                       ("192.168.215.0", "255.255.255.0")]
    own = "http://192.168.215.0:8080/description.xml"

    with caplog.at_level(logging.WARNING):
        # 广播兜底把自家 NOTIFY 送回自己的 1900 口
        service._handle_notify({"nts": "ssdp:alive", "location": own,
                               "usn": "uuid:hc"}, ("192.168.1.5", 1900))
        # M-SEARCH 的 200 OK 里同样带着自己的 LOCATION
        service._parse_response(
            "HTTP/1.1 200 OK\r\nLOCATION: %s\r\n" % own, ("192.168.1.5", 1900))
        time.sleep(0.2)

    assert calls == []
    assert [r for r in caplog.records if "抓取设备描述失败" in r.getMessage()] == []


def test_foreign_announce_still_fetched(service, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_fetch_device_from_notify",
                        lambda loc, ip: calls.append((loc, ip)))
    service.ip_list = [("192.168.1.5", "255.255.255.0")]
    service._handle_notify({"nts": "ssdp:alive", "location": LOC,
                            "usn": "uuid:win"}, ("192.168.1.68", 1900))
    time.sleep(0.3)
    assert calls == [(LOC, "192.168.1.68")]
