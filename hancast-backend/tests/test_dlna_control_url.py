"""DLNA 控制地址解析：设备描述未必在 /description.xml

现场证据（TCL 85T8G Max）：描述只在根路径 "/" 提供，硬编码
/description.xml 拿到 404 HTML，控制地址解析失败后每条 SOAP 都被跳过，
App 显示「推送成功」而电视一次都没收到命令。
"""
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hancast_sidecar.models.device import Device
from hancast_sidecar.protocol import dlna as dlna_mod

AV_DESC = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>TCL 85T8G Max</friendlyName>
    <UDN>uuid:6e3eb339</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <controlURL>AVTransport/6e3eb339/control</controlURL>
      </service>
    </serviceList>
  </device>
</root>
"""

ABSOLUTE_DESC = AV_DESC.replace(
    "<controlURL>AVTransport/6e3eb339/control</controlURL>",
    "<controlURL>/AVT/control</controlURL>")


class FakeResp:
    def __init__(self, body, status=200):
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = status

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise dlna_mod.requests.HTTPError(f"HTTP {self.status_code}")


HTML_404 = "<html><body><h1>Not Found</h1></body></html>"


def make_get(desc_url, body):
    """只在 desc_url 上返回设备描述，其余路径给 404 HTML（电视的真实行为）"""
    def _get(url, timeout=None, **kw):
        if url == desc_url:
            return FakeResp(body)
        return FakeResp(HTML_404, status=404)
    return _get


def device(**kw):
    base = dict(id="uuid:6e3eb339", name="TCL 85T8G Max", device_type="tv",
                ip="192.168.1.13", port=16514, status="online",
                udn="uuid:6e3eb339")
    base.update(kw)
    return Device(**base)


@pytest.fixture
def protocol(monkeypatch):
    p = dlna_mod.DLNAProtocol()
    monkeypatch.setattr(dlna_mod.requests, "get", lambda *a, **k: FakeResp(HTML_404, 404))
    return p


def test_location_with_root_path_desc(protocol, monkeypatch):
    """SSDP LOCATION 指向根路径时，相对 controlURL 按描述地址解析"""
    monkeypatch.setattr(dlna_mod.requests, "get",
                        make_get("http://192.168.1.13:16514/", AV_DESC))
    protocol.set_device(device(location="http://192.168.1.13:16514/"))
    assert protocol._control_url == (
        "http://192.168.1.13:16514/AVTransport/6e3eb339/control")


def test_fallback_to_root_when_no_location(protocol, monkeypatch):
    """老设备记录没有 LOCATION：回退穷举，根路径命中同样要能用"""
    monkeypatch.setattr(dlna_mod.requests, "get",
                        make_get("http://192.168.1.13:16514/", AV_DESC))
    protocol.set_device(device())
    assert protocol._control_url is not None


def test_absolute_control_url_kept(protocol, monkeypatch):
    monkeypatch.setattr(dlna_mod.requests, "get",
                        make_get("http://192.168.1.13:16514/", ABSOLUTE_DESC))
    protocol.set_device(device(location="http://192.168.1.13:16514/"))
    assert protocol._control_url == "http://192.168.1.13:16514/AVT/control"


def test_classical_description_xml_still_works(protocol, monkeypatch):
    monkeypatch.setattr(dlna_mod.requests, "get", make_get(
        "http://192.168.1.5:8080/description.xml", AV_DESC))
    protocol.set_device(device(ip="192.168.1.5", port=8080))
    assert protocol._control_url == (
        "http://192.168.1.5:8080/AVTransport/6e3eb339/control")


def test_play_fails_loudly_without_control_url(protocol, monkeypatch):
    """解析不出控制地址必须报错，不能再报「推送成功」"""
    calls = []
    monkeypatch.setattr(dlna_mod.requests, "get",
                        lambda *a, **k: calls.append(a[0]) or FakeResp(HTML_404, 404))
    protocol.set_device(device())
    assert protocol._control_url is None
    with pytest.raises(dlna_mod.ControlUrlUnavailable):
        protocol.play("http://192.168.1.13:8765/hls/abc/live.m3u8")
    assert calls  # 确实尝试过解析，而不是直接跳过


def test_send_action_retries_resolution(protocol, monkeypatch):
    """控制地址缺失时低频重试：设备描述服务晚就绪也能恢复"""
    state = {"urls": []}

    def _get(url, timeout=None, **kw):
        state["urls"].append(url)
        if url.endswith("/"):
            return FakeResp(AV_DESC)
        return FakeResp(HTML_404, 404)

    monkeypatch.setattr(dlna_mod.requests, "get", _get)
    protocol._device = device()
    protocol._control_url = None
    sent = []
    monkeypatch.setattr(
        dlna_mod.requests, "post",
        lambda url, **kw: sent.append(url) or FakeResp("<s/>"))
    assert protocol._send_action(dlna_mod.NS_AVTRANSPORT, "Play",
                                 {"InstanceID": 0}) is not None
    assert sent and sent[0].endswith("/AVTransport/6e3eb339/control")


def test_5xx_is_reported_as_dead_renderer(protocol, monkeypatch):
    """电视端 DLNA 服务退出现场表现：所有控制动作 HTTP 500 空正文"""
    monkeypatch.setattr(dlna_mod.requests, "get",
                        make_get("http://192.168.1.13:16514/", AV_DESC))
    protocol.set_device(device(location="http://192.168.1.13:16514/"))

    err = requests.HTTPError("500 Server Error")
    err.response = FakeResp("", status=500)

    def _post(url, **kw):
        raise err

    monkeypatch.setattr(dlna_mod.requests, "post", _post)
    with pytest.raises(dlna_mod.RendererUnresponsive) as ei:
        protocol.play("http://192.168.1.13:8765/hls/abc/live.m3u8")
    assert "HTTP 500" in str(ei.value)


def test_lan_http_bypasses_system_proxy(protocol, monkeypatch):
    """链路内 HTTP 不能走系统代理

    macOS 的代理例外列表写成 CIDR（192.168.0.0/16）时 urllib 匹配不上，
    requests 会把发往电视/对端电脑的描述抓取与 SOAP 命令送进
    127.0.0.1:7890 这类本地代理；代理一掉线，现象就是「发现不了设备」
    「推送成功但电视黑屏」，日志里只剩一句 read timeout。
    """
    seen = {}

    def _get(url, timeout=None, **kw):
        seen["get"] = kw.get("proxies")
        if url.endswith("/"):
            return FakeResp(AV_DESC)
        return FakeResp(HTML_404, 404)

    def _post(url, data=None, headers=None, timeout=None, **kw):
        seen["post"] = kw.get("proxies")
        return FakeResp('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/'
                        'envelope/"><s:Body/></s:Envelope>')

    monkeypatch.setattr(dlna_mod.requests, "get", _get)
    monkeypatch.setattr(dlna_mod.requests, "post", _post)
    protocol.set_device(device(location="http://192.168.1.13:16514/"))
    protocol.play("http://192.168.1.5:8765/hls/abc/live.m3u8")

    assert seen["get"] == {"http": "", "https": ""}
    assert seen["post"] == {"http": "", "https": ""}
