"""入站 SOAP 信封的容错（docs/故障排查.md §5.4）

现场证据（2026-10-25，同一份日志里 6 次）：某个控制点把
`<?xml version="1.0"?>` 前面垫了 16 个空格再 POST 过来。libxml 严格按 XML 规范
判定声明必须在文档最开头，报
`XML declaration allowed only at the start of the document, line 1, column 22`
——列号 22 = 声明真实起始列 17 + 5，正好对上 16 个前导字符。

旧行为：异常从 `DLNAProtocol.call()` 的 `etree.fromstring` 冒到 server.py 的兜底
catch，回一个裸 HTTP 500。用户侧是「投不上」，日志侧只有一句解析器原文：动作分发
没走到、Device Guard 确认弹窗也不会出现、连是谁发的都没记下来。

新行为：前导空白/BOM 剔掉继续处理；剔完仍解析不了的，回带 errorCode 的 SOAP Fault。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hancast_sidecar.protocol import dlna as dlna_mod
from hancast_sidecar.protocol.dlna import DLNAProtocol, Service

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
AV_NS = "urn:schemas-upnp-org:service:AVTransport:1"

ENVELOPE = (
    f'<s:Envelope xmlns:s="{SOAP_NS}" '
    f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    f'<s:Body><u:SetAVTransportURI xmlns:u="{AV_NS}">'
    '<InstanceID>0</InstanceID>'
    '<CurrentURI>http://192.168.1.5:61482/sample.mp4</CurrentURI>'
    '<CurrentURIMetaData></CurrentURIMetaData>'
    '</u:SetAVTransportURI></s:Body></s:Envelope>'
)
DECL = '<?xml version="1.0"?>'


@pytest.fixture
def protocol(monkeypatch):
    """注册一个只记录参数的假 action，隔离掉渲染器与 Device Guard"""
    calls = []

    def fake_action(self, data):
        calls.append(data)
        return {}

    monkeypatch.setattr(DLNAProtocol, "AVTransport_SetAVTransportURI", fake_action)
    Service.build(
        "AVTransport", AV_NS,
        {"SetAVTransportURI": type("A", (), {
            "input": [type("Arg", (), {"name": "InstanceID", "state": "InstanceID"}),
                      type("Arg", (), {"name": "CurrentURI", "state": "CurrentURI"}),
                      type("Arg", (), {"name": "CurrentURIMetaData",
                                       "state": "CurrentTrackMetaData"})],
            "output": []})},
    )
    p = DLNAProtocol()
    p._device_guard = None
    p._renderer = None
    return p, calls


class TestLeadingJunk:
    def test_现场复现_16个前导空格(self, protocol):
        p, calls = protocol
        raw = (" " * 16 + DECL + ENVELOPE).encode("utf-8")
        res = p.call(raw, caller_ip="192.168.1.68")
        # 成功路径回的是 SetAVTransportURIResponse 空信封，不是 Fault
        assert b"Fault" not in res
        assert b"SetAVTransportURIResponse" in res
        assert len(calls) == 1, "动作分发没走到，等于又变成一次静默失败"
        assert calls[0]["CurrentURI"].value == "http://192.168.1.5:61482/sample.mp4"

    def test_BOM与空白交替(self, protocol):
        p, calls = protocol
        raw = ("\ufeff" + "  " + "\ufeff\t" + DECL + ENVELOPE).encode("utf-8")
        p.call(raw, caller_ip="192.168.1.68")
        assert len(calls) == 1

    def test_干净报文原样通过(self, protocol):
        p, calls = protocol
        p.call((DECL + ENVELOPE).encode("utf-8"), caller_ip="192.168.1.68")
        assert len(calls) == 1


class TestStripHelper:
    @pytest.mark.parametrize("raw", [
        b"   \t\n <?xml",
        "\ufeff  <x/>",
        "  <x/>",
    ])
    def test_剔掉的字节数非零(self, raw):
        cleaned, n = dlna_mod.strip_soap_leading_junk(raw)
        assert n > 0
        assert not cleaned.startswith((b" ", b"\t", b"\n", b"\xef"))

    def test_正文一个字节都不动(self):
        cleaned, n = dlna_mod.strip_soap_leading_junk("  " + DECL + ENVELOPE)
        assert cleaned.decode() == DECL + ENVELOPE
        assert n == 2

    def test_无垃圾时零开销(self):
        cleaned, n = dlna_mod.strip_soap_leading_junk(ENVELOPE.encode())
        assert n == 0
        assert cleaned == ENVELOPE.encode()

    def test_空与None不炸(self):
        assert dlna_mod.strip_soap_leading_junk(b"") == (b"", 0)
        assert dlna_mod.strip_soap_leading_junk(None)[1] == 0


class TestUnparseableBecomesFault:
    def test_彻底不是XML_回SOAP_Fault而非异常(self, protocol):
        p, _ = protocol
        # 剔完前导垃圾仍是废文：这里没有声明也没有根元素
        res = p.call(b"not xml at all", caller_ip="192.168.1.68")
        assert b"Fault" in res, "必须回 Fault，让控制端读到原因"
        assert b"Unparseable SOAP envelope" in res
        text = res.decode()
        assert "errorCode" in text and "402" in text

    def test_空body不再冒异常(self, protocol):
        p, _ = protocol
        res = p.call(b"", caller_ip="192.168.1.68")
        assert b"Fault" in res

    def test_声明前塞非空白文本_仍回Fault(self, protocol):
        p, _ = protocol
        res = p.call(("junk" + DECL + ENVELOPE).encode(), caller_ip="10.0.0.1")
        assert b"Fault" in res
