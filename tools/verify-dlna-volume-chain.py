#!/usr/bin/env python3
"""离线验证 DLNA 音量键链路（手机作为控制端 → RenderingControl → MPV）。

不需要真机：直接给 DLNAProtocol.call() 喂 SOAP 信封，用假 renderer / 截获的
IPC 命令做判据。覆盖三类曾被证实的 bug：
1. 入站值不归一化（'45.5' → int() 抛 ValueError → 冒成 HTTP 500；字符串写进
   state 让每次控制都被判成「变化」）
2. SetMute 只认 '1'，安卓发 'true' 时静音失效
3. mpv IPC 重连时 set_observe 硬编码把音量抹成 100

判据全部 PASS 才退出 0。用法：
hancast-backend/.venv/bin/python tools/verify-dlna-volume-chain.py
（系统 python3 没有 requests/lxml，必须用后端 venv 的解释器）"""
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hancast-backend"))
logging.disable(logging.CRITICAL)

from hancast_sidecar.protocol.dlna import DLNAProtocol  # noqa: E402
from hancast_sidecar.renderer.mpv import MPVRenderer  # noqa: E402

NS = "urn:schemas-upnp-org:service:{service}:1"
results = []


class FakeRenderer:
    def __init__(self):
        self.calls = []

    def set_protocol(self, protocol):
        pass

    def set_media_volume(self, value):
        self.calls.append(("volume", value))

    def set_media_mute(self, value):
        self.calls.append(("mute", value))


def envelope(service, action, args_xml):
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        f'<s:Body><u:{action} xmlns:u="{NS.format(service=service)}">'
        f'{args_xml}</u:{action}></s:Body></s:Envelope>'
    ).encode()


def check(label, cond, detail=""):
    results.append(cond)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not cond else ""))


def volume_case(p, r, raw):
    args = "<InstanceID>0</InstanceID><Channel>Master</Channel>"
    args += f"<DesiredVolume>{raw}</DesiredVolume>" if raw is not None else ""
    return p.call(envelope("RenderingControl", "SetVolume", args))


def main():
    print("=== SetVolume 入站归一化 ===")
    p = DLNAProtocol()
    r = FakeRenderer()
    p.set_renderer(r)

    for raw, want in [("50", 50), ("45.5", 46), ("200", 100), ("-5", 0)]:
        r.calls.clear()
        try:
            resp = volume_case(p, r, raw)
            err = None
        except Exception as e:  # noqa: BLE001
            resp, err = b"", e
        sent = r.calls[0][1] if r.calls else None
        state = p.state_list["Volume"].value
        check(f"DesiredVolume={raw!r} → 播放器收 {want}、状态为 int",
              err is None and sent == want and state == want and isinstance(state, int),
              f"err={err} sent={sent!r} state={state!r}")

    for raw in ["", "abc", None]:
        resp = volume_case(p, r, raw)
        text = resp.decode("utf-8", "replace")
        check(f"DesiredVolume={raw!r} → 回 SOAP Fault 402 而不是抛异常",
              b"Fault" in resp and "402" in text, text[:120])

    # 控制器重复下发同一个值：状态必须仍然是数值，不能退化成字符串
    volume_case(p, r, "30")
    volume_case(p, r, "30")
    state = p.state_list["Volume"].value
    check("重复下发同一音量后状态仍是 int（否则每次控制都重发 NOTIFY）",
          state == 30 and isinstance(state, int), f"state={state!r}")

    print("=== GetVolume 回给控制端 ===")
    resp = p.call(envelope("RenderingControl", "GetVolume",
                           "<InstanceID>0</InstanceID><Channel>Master</Channel>")).decode()
    m = re.search(r"<CurrentVolume>([^<]*)</CurrentVolume>", resp)
    check("GetVolume 回数值 30", bool(m) and m.group(1) == "30", resp[:200])

    print("=== SetMute 布尔写法 ===")
    for raw, want in [("1", True), ("0", False), ("true", True), ("false", False),
                      ("yes", True), ("no", False)]:
        r.calls.clear()
        resp = p.call(envelope("RenderingControl", "SetMute",
                               f"<InstanceID>0</InstanceID><Channel>Master</Channel>"
                               f"<DesiredMute>{raw}</DesiredMute>"))
        sent = r.calls[0][1] if r.calls else None
        state = p.state_list["Mute"].value
        check(f"DesiredMute={raw!r} → {want}",
              sent is want and state is want and b"Fault" not in resp,
              f"sent={sent!r} state={state!r} resp={resp[:100]}")

    print("=== 未知 action 仍回 Fault 401 ===")
    resp = p.call(envelope("RenderingControl", "NoSuchAction", "<InstanceID>0</InstanceID>"))
    check("未知 action → 401", b"Fault" in resp and "401" in resp.decode(), resp[:150])

    print("=== mpv IPC 重连不复位的音量 ===")
    ipc = []
    MPVRenderer.send_command = lambda self, cmd: ipc.append(cmd)  # noqa: ARG005
    renderer = MPVRenderer()
    renderer.set_observe()
    first = [c for c in ipc if c[:2] == ["set_property", "volume"]]
    check("首次连接仍按默认 100 下发（与旧行为一致，用来对齐 DLNA 状态）",
          first and first[0][2] == 100, str(first))

    ipc.clear()
    renderer.set_media_volume(30)
    renderer.set_observe()  # 模拟 IPC 掉线重连
    again = [c for c in ipc if c[:2] == ["set_property", "volume"]]
    check("重连后按最近音量 30 复位，不再抹成 100",
          again and again[0][2] == 30, str(again))

    print("=== 播放器回报的越界音量 ===")
    p2 = DLNAProtocol()
    p2.set_state_volume(130)
    check("mpv volume>100 回报时钳制到 100", p2.state_list["Volume"].value == 100,
          repr(p2.state_list["Volume"].value))

    failed = results.count(False)
    print(f"\nRESULT: {'ALL PASS' if not failed else f'{failed} FAIL'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
