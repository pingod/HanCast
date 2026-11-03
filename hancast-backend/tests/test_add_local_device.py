"""「添加本机」用于自己投自己（v2.4.7）。

背景：HanCast 同时是控制端和 DLNA 渲染端，但设备列表只装 SSDP 发现的**别的**机器；
本机把自己广播出去的那台常常被隐藏/忽略，于是「本机 → 本机」没有入口。
`add_local_device` 直接按「本机 LAN IP + 自己的 DLNA 端口」把自己登记进列表，
并顺手写进 Device Guard 信任列表——自己投自己再弹「未知设备请求投屏」纯属自己吓自己。

这里钉死三件事：
1. 地址由 `_get_local_ip()` + DLNA 实际端口拼出来，不吃用户输入；
2. 登记后设备在 `device_guard` 的信任列表里（不是 pending）；
3. 不写 `manual_devices`（那是给「手填 IP 填错要能手删」用的持久化兜底）。
"""

from hancast_sidecar.commands import CommandHandler
from hancast_sidecar.security.guard import DeviceGuard


class _StubConfig:
    def __init__(self):
        self.manual_devices = []
        self.network_interface = ""

    def get_device_guard_config(self):
        return {}

    def add_manual_device(self, _addr):
        self.manual_devices.append(_addr)

    def save(self):
        pass


class _StubDevice:
    udn = "uuid:self-0000"

    def to_dict(self):
        return {
            "id": self.udn,
            "name": "HanCast(SelfMac.local)",
            "ip": "192.168.1.5",
            "port": 8080,
            "device_type": "tv",
            "udn": self.udn,
        }


class _StubSSDP:
    def __init__(self):
        self.calls = []

    def add_device_by_target(self, target):
        self.calls.append(target)
        return _StubDevice()


class _StubDLNA:
    def get_port(self):
        return 8080


def _handler(monkeypatch):
    h = CommandHandler.__new__(CommandHandler)  # 绕开 __init__（会起真服务）
    h.config = _StubConfig()
    h.ssdp = _StubSSDP()
    h.dlna_server = _StubDLNA()
    h.device_guard = DeviceGuard(h.config)
    monkeypatch.setattr(h, "_get_local_ip", lambda: "192.168.1.5")
    monkeypatch.setattr(h.device_guard, "_save", lambda: None)
    return h


def test_registers_with_local_ip_and_dlna_port(monkeypatch):
    h = _handler(monkeypatch)
    d = h._add_local_device({})
    assert h.ssdp.calls == ["192.168.1.5:8080"]  # 算出来的地址，不是用户输入
    assert d["name"] == "HanCast(SelfMac.local)"
    assert d["ip"] == "192.168.1.5"


def test_self_is_trusted_so_no_confirm_dialog(monkeypatch):
    h = _handler(monkeypatch)
    h._add_local_device({})
    assert h.device_guard.check("192.168.1.5") == "trusted"
    trusted = [e["friendly_name"] for e in h.device_guard.get_trusted_devices()]
    assert "HanCast(SelfMac.local)" in trusted


def test_does_not_persist_into_manual_devices(monkeypatch):
    # 本机地址每次启动按当前网卡重算，存成手动地址会随 IP 变化变成死记录
    h = _handler(monkeypatch)
    h._add_local_device({})
    assert h.config.manual_devices == []


def test_ip_change_leaves_no_stale_trust(monkeypatch):
    """换网段后再加一次：新 IP 被信任，旧 IP 那条不该影响判断（旧 IP 仍是 trusted
    但只是历史条目，不会把新 IP 顶成 pending）——这里确认新 IP 直接 trusted。"""
    h = _handler(monkeypatch)
    h._add_local_device({})
    monkeypatch.setattr(h, "_get_local_ip", lambda: "10.0.0.9")
    h.dlna_server.get_port = lambda: 61234
    h._add_local_device({})
    assert h.device_guard.check("10.0.0.9") == "trusted"
    assert h.ssdp.calls[-1] == "10.0.0.9:61234"
