"""投屏来源识别的描述抓取同属链路内 HTTP，必须绕开系统代理。

约定见 ssdp._NO_PROXY：macOS 代理例外列表用 CIDR 时非豁免地址会被送进
127.0.0.1:9565 这类本地代理，代理一掉线，来源就退化成「未知设备」。
"""

from hancast_sidecar.security.guard import DeviceGuard

DESC = (b'<?xml version="1.0"?>'
        b'<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
        b'<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>'
        b'<friendlyName>WIN-PC HanCast</friendlyName>'
        b'<UDN>uuid:9d2b1c00-1111-2222-3333-444455556666</UDN>'
        b'</device></root>')


class _StubConfig:
    def get_device_guard_config(self):
        return {}


class _Resp:
    status_code = 200
    content = DESC


def test_tcp_probe_bypasses_system_proxy(monkeypatch):
    guard = DeviceGuard(_StubConfig())
    monkeypatch.setattr(guard, "_save_port_stats", lambda: None)
    seen = []

    def fake_get(url, timeout=None, **kw):
        seen.append((url, kw))
        return _Resp()

    monkeypatch.setattr("hancast_sidecar.security.guard.requests.get", fake_get)

    info = guard._resolve_by_tcp_probe("192.168.1.68")

    assert info and info["ip"] == "192.168.1.68"
    assert seen, "TCP 探测层应真的发出描述抓取"
    assert all(kw.get("proxies") == {"http": "", "https": ""} for _, kw in seen)
