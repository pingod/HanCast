"""投屏目标的准入：描述里必须声明 AVTransport 服务（v2.3.13）

现场证据（2026-10-15 15:35:07，Mac 与 .68 两份日志同一时刻）：

    Found device via NOTIFY: 荣耀路由X1 增强版 (192.168.1.250:37215)

荣耀路由器是一台**合格的 UPnP 设备**（MediaServer / InternetGatewayDevice），
老的解析逻辑「只要有 UDN 就登记」，于是它被当成投屏目标列进了设备表；用户点了它
只会得到一次注定失败的投屏。设备列表的准入条件应该是**服务**而不是设备类型标签：
控制端能做的所有事（SetAVTransportURI / Play / Pause / Stop / 进度）全走 AVTransport。

本文件全部离线跑 XML 字符串，不碰网络、不写用户配置。
"""

import logging

import pytest

from hancast_sidecar.ssdp import SSDPService

LOC_ROUTER = "http://192.168.1.250:37215/rootDesc.xml"
LOC_TV = "http://192.168.1.13:16514/description.xml"

# 真实形态的电视描述（TCL 85T8G Max 的精简版）
TV_DESC = """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>TCL 85T8G Max</friendlyName>
    <UDN>uuid:6e3eb339-0000-1111-2222-333344445555</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <controlURL>AVTransport/6e3eb339/control</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <controlURL>RC/6e3eb339/control</controlURL>
      </service>
    </serviceList>
  </device>
</root>
"""

# 路由器：有 deviceType、有 UDN、有服务，但没有一个服务能用来投屏
ROUTER_DESC = """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>荣耀路由X1 增强版</friendlyName>
    <UDN>uuid:c8-94-bb-11-22-33</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <controlURL>ContentDirectory/control</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <controlURL>CM/control</controlURL>
      </service>
    </serviceList>
  </device>
</root>
"""

# WAN 口设备（TR-098 那一类）：连 deviceType 都不是 DLNA 家族
IGW_DESC = TV_DESC.replace(
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1").replace(
    "urn:schemas-upnp-org:service:AVTransport:1",
    "urn:schemas-upnp-org:service:WANIPConnection:1")


@pytest.fixture
def service():
    return SSDPService(friendly_name="T", port=8080, preferred_ip="192.168.1.5")


def parse(service, xml, ip, location):
    return service._parse_device_description(xml, ip, port=8080, location=location)


# ── 准入看服务 ──

def test_renderer_with_avtransport_is_accepted(service):
    dev = parse(service, TV_DESC, "192.168.1.13", LOC_TV)
    assert dev is not None
    assert dev.name == "TCL 85T8G Max"
    assert dev.device_type == "tv"


def test_media_server_without_avtransport_is_dropped(service):
    """路由器/NAS：描述完全合法，但没有任何可用的播控服务"""
    assert parse(service, ROUTER_DESC, "192.168.1.250", LOC_ROUTER) is None


def test_internet_gateway_device_is_dropped(service):
    assert parse(service, IGW_DESC, "192.168.1.250", LOC_ROUTER) is None


def test_avtransport_on_embedded_device_counts(service):
    """根设备挂 AVTransport 的写法不统一：有的机型把播控服务放在内嵌设备里。
    服务遍历整份描述（root.iter），不能因为标签落在子设备就漏掉。"""
    embedded = """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>Combo Box</friendlyName>
    <UDN>uuid:combo-0001</UDN>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
        <friendlyName>Combo Box Renderer</friendlyName>
        <UDN>uuid:combo-0001-renderer</UDN>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
            <controlURL>AVT/control</controlURL>
          </service>
        </serviceList>
      </device>
    </deviceList>
  </device>
</root>
"""
    assert parse(service, embedded, "192.168.1.13", LOC_TV) is not None


def test_namespace_less_description_still_parsed(service):
    """部分山寨机不带 xmlns：local-name 遍历要照样认出 serviceType"""
    bare = TV_DESC.replace(' xmlns="urn:schemas-upnp-org:device-1-0"', "")
    assert parse(service, bare, "192.168.1.13", LOC_TV) is not None


def test_service_urn_case_is_matched_leniently(service):
    """URN 规范写法是 AVTransport，但大小写混乱的描述宁滥勿缺"""
    lowered = TV_DESC.replace("service:AVTransport:1", "service:avtransport:1")
    assert parse(service, lowered, "192.168.1.13", LOC_TV) is not None


# ── NOTIFY 链路：路由器不再进设备表 ──

class _FakeResp:
    def __init__(self, body):
        self.text = body
        self.status_code = 200  # 手动添加路径会先校验状态码
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"


def test_notify_path_does_not_register_router(service, monkeypatch):
    seen = []
    service._on_device_found = seen.append
    monkeypatch.setattr("hancast_sidecar.ssdp.requests.get",
                        lambda *a, **k: _FakeResp(ROUTER_DESC))
    service._fetch_device_from_notify(LOC_ROUTER, "192.168.1.250")
    assert service.get_devices() == []
    assert seen == []


def test_notify_path_still_registers_renderer(service, monkeypatch):
    seen = []
    service._on_device_found = seen.append
    monkeypatch.setattr("hancast_sidecar.ssdp.requests.get",
                        lambda *a, **k: _FakeResp(TV_DESC))
    service._fetch_device_from_notify(LOC_TV, "192.168.1.13")
    assert [d.name for d in service.get_devices()] == ["TCL 85T8G Max"]
    assert [d.id for d in seen] == ["uuid:6e3eb339-0000-1111-2222-333344445555"]


# ── 手动添加：报错要点名准入条件 ──

def test_manual_add_rejects_router_with_precise_message(service, monkeypatch):
    monkeypatch.setattr("hancast_sidecar.ssdp.requests.get",
                        lambda *a, **k: _FakeResp(ROUTER_DESC))
    with pytest.raises(ValueError) as err:
        service.add_device_by_target(LOC_ROUTER)
    assert "AVTransport" in str(err.value)


# ── 日志：同一 location 只在首次露一行 ──

def test_rejection_logs_once_then_debugs(service, caplog):
    with caplog.at_level(logging.DEBUG, logger="hancast.ssdp"):
        parse(service, ROUTER_DESC, "192.168.1.250", LOC_ROUTER)
        parse(service, ROUTER_DESC, "192.168.1.250", LOC_ROUTER)
    info = [r for r in caplog.records
            if r.levelno == logging.INFO and "不列入投屏目标" in r.getMessage()]
    debug = [r for r in caplog.records
             if r.levelno == logging.DEBUG and "不列入投屏目标" in r.getMessage()]
    assert len(info) == 1, "路由器每 30s 重播一次 NOTIFY，第二次起必须降噪"
    assert len(debug) == 1
    assert "荣耀路由X1 增强版" in info[0].getMessage()
    assert "MediaServer" in info[0].getMessage()


def test_non_upnp_response_warns(service, caplog):
    with caplog.at_level(logging.WARNING, logger="hancast.ssdp"):
        parse(service, "<html><body>hi</body></html>", "192.168.1.9",
              "http://192.168.1.9/")
    warns = [r for r in caplog.records
             if r.levelno == logging.WARNING and "不列入投屏目标" in r.getMessage()]
    assert len(warns) == 1


def test_xml_comments_do_not_break_the_service_scan(service):
    """真实厂商的描述里遍地是 XML 注释。lxml 遍历会碰到注释节点，
    而注释节点的 .tag 是个可调用对象，不是字符串——早期版本在这里抛
    AttributeError，被宽 except 吞掉后**整份合格描述被判成坏数据**，
    表现是「电视从列表里消失」而不是「报错」。这条是那次踩坑的回归锁。"""
    commented = TV_DESC.replace(
        "<serviceList>",
        "<!-- generated by TCL firmware --><?xml-stylesheet href='/x.xsl'?><serviceList>")
    dev = parse(service, commented, "192.168.1.13", LOC_TV)
    assert dev is not None, "带注释的描述照样要收进来"


def test_success_clears_the_rejection_counter(service):
    """同一 location 先被拒后被接受（固件升级补上服务）时，计数器要能重来"""
    parse(service, ROUTER_DESC, "192.168.1.13", LOC_TV)
    assert service._rejected_targets.get(LOC_TV) == 1
    parse(service, TV_DESC, "192.168.1.13", LOC_TV)
    assert LOC_TV not in service._rejected_targets
