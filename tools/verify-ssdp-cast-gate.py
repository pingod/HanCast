#!/usr/bin/env python3
"""投屏目标准入：路由器不能混进设备列表（v2.3.13 的真机验收）

缺陷现场（2026-10-15 15:35:07，Mac 与 Windows 测试机同一时刻的日志）：

    Found device via NOTIFY: 荣耀路由X1 增强版 (192.168.1.250:37215)

路由器是一台合格的 UPnP 设备，老的解析「只要有 UDN 就登记」，于是它成了投屏目标。
v2.3.13 起准入看**服务**：描述里必须有 AVTransport，否则记一行理由、不进列表。

本工具一次起两台替身，把「该收的收进来」和「该拒的拒掉」同时钉死：

1. 假电视（MediaRenderer + AVTransport）→ 日志出现 `Found device via NOTIFY: <它>`
2. 假路由器（MediaServer + ContentDirectory，无 AVTransport）→ 日志出现
   `不列入投屏目标：<它> 不提供 AVTransport 投屏服务`，且**永远不出现**它的 Found 行

LOCATION 一律写 127.0.0.1：通告包源地址是真实网卡，但描述地址不属于本机网卡，
因此不会被 `_is_own_location` 的自通告回环过滤挡掉（与 verify-ssdp-offline-eviction.py 同法）。

只读日志、不写任何用户配置；退出前给两台替身补发 byebye。

用法：
    python3 tools/verify-ssdp-cast-gate.py
    python3 tools/verify-ssdp-cast-gate.py --log /path/to/hancast.log
    python3 tools/verify-ssdp-cast-gate.py --serve tv        # 只当替身（内部用）
    python3 tools/verify-ssdp-cast-gate.py --serve router --port 49312
"""
import argparse
import http.server
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
DEFAULT_LOG = os.path.expanduser("~/Library/Application Support/HanCast/logs/hancast.log")

TV_TMPL = """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>{name}</friendlyName>
    <manufacturer>HanCast Tools</manufacturer>
    <modelName>Cast Gate Probe TV</modelName>
    <UDN>uuid:{udn}</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <SCPDURL>/AVTransport.xml</SCPDURL>
        <controlURL>/upnp/control/avtransport</controlURL>
        <eventSubURL>/upnp/event/avtransport</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>
"""

# 荣耀路由X1 那类：描述完全合法、有服务，但没有一个服务能用来投屏
ROUTER_TMPL = """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>{name}</friendlyName>
    <manufacturer>HanCast Tools</manufacturer>
    <modelName>Cast Gate Probe Router</modelName>
    <UDN>uuid:{udn}</UDN>
    <!-- 注释节点：lxml 遍历会碰到它，tag 不是字符串，描述不能因此被判坏 -->
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <SCPDURL>/CDS.xml</SCPDURL>
        <controlURL>/upnp/control/cds</controlURL>
        <eventSubURL>/upnp/event/cds</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>
"""

KINDS = {
    # kind: (设备名后缀, deviceType NT, 描述模板)
    "tv": ("CastGateTV", "urn:schemas-upnp-org:device:MediaRenderer:1", TV_TMPL),
    "router": ("CastGateRouter", "urn:schemas-upnp-org:device:MediaServer:1",
               ROUTER_TMPL),
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/description.xml":
            body = self.server.tmpl.format(name=self.server.dev_name,
                                           udn=self.server.udn).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", 'text/xml; charset="utf-8"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, *a):
        pass


def make_socks(src_ip):
    socks = []
    for ip in {src_ip, "0.0.0.0"}:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        try:
            s.bind((ip, 0))
            if ip != "0.0.0.0":
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                             socket.inet_aton(ip))
        except OSError:
            s.close()
            continue
        socks.append(s)
    return socks


def announce(socks, kind, udn, location, nts="ssdp:alive"):
    _, device_nt, _ = KINDS[kind]
    targets = [("upnp:rootdevice", f"uuid:{udn}::upnp:rootdevice"),
               (f"uuid:{udn}", f"uuid:{udn}"),
               (device_nt, f"uuid:{udn}::{device_nt}")]
    for nt, usn in targets:
        msg = (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            f"CACHE-CONTROL: max-age=66\r\n"
            f"LOCATION: {location}\r\n"
            f"NT: {nt}\r\n"
            f"NTS: {nts}\r\n"
            f"USN: {usn}\r\n"
            "SERVER: Linux/5.15 UPnP/1.0 HanCastProbe/1.0\r\n\r\n"
        ).encode()
        for sk in socks:
            try:
                sk.sendto(msg, (SSDP_ADDR, SSDP_PORT))
            except OSError:
                pass


def serve(kind, src_ip, port, interval, duration, udn):
    name = f"HanCast{KINDS[kind][0]}-{port}"
    location = f"http://127.0.0.1:{port}/description.xml"
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.dev_name, httpd.udn, httpd.tmpl = name, udn, KINDS[kind][2]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    socks = make_socks(src_ip)
    if not socks:
        print("[serve] 无法建立通告 socket", flush=True)
        raise SystemExit(1)
    print(f"[serve] {kind} name={name} location={location}", flush=True)
    deadline = time.time() + duration
    while time.time() < deadline:
        announce(socks, kind, udn, location)
        time.sleep(interval)


def tail_from(path, offset):
    try:
        with open(path, "r", errors="replace") as f:
            f.seek(offset)
            return f.read(), f.tell()
    except OSError:
        return "", offset


def run(log_path, src_ip, ports, timeout, interval):
    if not os.path.exists(log_path):
        print(f"[FAIL] 找不到 sidecar 日志：{log_path}（HanCast 是否在跑？）")
        return 1

    plan = [("tv", ports[0]), ("router", ports[1])]
    devices = []
    for kind, port in plan:
        udn = str(uuid.uuid4())
        devices.append({
            "kind": kind, "port": port, "udn": udn,
            "name": f"HanCast{KINDS[kind][0]}-{port}",
            "location": f"http://127.0.0.1:{port}/description.xml",
        })

    offset = os.path.getsize(log_path)
    kids = [subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--serve", d["kind"],
         "--src-ip", src_ip, "--port", str(d["port"]), "--udn", d["udn"],
         "--interval", str(interval)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT) for d in devices]

    accepted = rejected = listed_router = ""
    deadline = time.time() + timeout
    try:
        print(f"[1/2] 两台替身已启动（{', '.join(d['name'] for d in devices)}），"
              f"等发现链路表态，最多 {int(timeout)}s")
        while time.time() < deadline and not (accepted and rejected):
            chunk, offset = tail_from(log_path, offset)
            for line in chunk.splitlines():
                for d in devices:
                    if d["name"] not in line:
                        continue
                    if "Found device" in line:
                        if d["kind"] == "tv":
                            accepted = line.strip()
                        else:
                            listed_router = line.strip()
                    elif "不列入投屏目标" in line and d["kind"] == "router":
                        rejected = line.strip()
                if listed_router:
                    break
            for p in kids:
                if p.poll() is not None:
                    print(f"[FAIL] 替身进程提前退出（code={p.returncode}）")
                    return 1
            time.sleep(1.0)
    finally:
        for p in kids:
            if p.poll() is None:
                p.kill()
                p.wait()
        # 收尾补发告别，免得替身赖在谁的列表里等超时摘除
        socks = make_socks(src_ip)
        for d in devices:
            announce(socks, d["kind"], d["udn"], d["location"], nts="ssdp:byebye")
        for s in socks:
            s.close()

    if listed_router:
        print("[FAIL] 没有 AVTransport 的替身仍被登记为投屏目标：")
        print(f"       {listed_router}")
        return 1
    if not accepted:
        print("[FAIL] 假电视没被发现——先查发现链路（组播是否被拦、HanCast 是否在跑），"
              "准入过滤无从验证")
        return 1
    if not rejected:
        print(f"[FAIL] {int(timeout)}s 内没看到假路由器被点名拒绝——过滤没生效")
        return 1
    print("[2/2] PASS：渲染端收进列表、路由器挡在门外")
    print(f"       收：{accepted}")
    print(f"       拒：{rejected}")
    return 0


def lan_ip():
    """默认路由网卡上的地址，作为通告包源地址"""
    out = subprocess.run(["route", "-n", "get", "default"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "interface" in line:
            iface = line.split(":")[-1].strip()
            ip = subprocess.run(["ipconfig", "getifaddr", iface],
                                capture_output=True, text=True).stdout.strip()
            if ip:
                return ip
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def run_source(src_ip, ports, timeout, interval):
    """就地用仓库源码跑一遍真实发现链路（打 tag 前的验收）

    与 --log 模式的分工：这条走真 socket + 真 HTTP 抓取，证明**源码**判得对；
    装好新版后再用默认模式验**产物**。两种模式共用同一对替身。
    需要 1900 空闲：先退出在跑的 HanCast（别 pkill sidecar，会留空壳 GUI）。
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo, "hancast-backend"))
    import logging

    from hancast_sidecar.ssdp import SSDPService

    plan = [("tv", ports[0]), ("router", ports[1])]
    devices = [dict(kind=k, port=p, udn=str(uuid.uuid4()),
                    name=f"HanCast{KINDS[k][0]}-{p}",
                    location=f"http://127.0.0.1:{p}/description.xml")
               for k, p in plan]
    tv, router = devices[0], devices[1]

    rejections = []

    class _Capture(logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            if "不列入投屏目标" in msg:
                rejections.append(msg)

    logging.getLogger("hancast.ssdp").addHandler(_Capture())
    logging.getLogger("hancast.ssdp").setLevel(logging.DEBUG)

    service = SSDPService(friendly_name="CastGateProbe", port=8099,
                          preferred_ip=src_ip)
    kids = [subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--serve", d["kind"],
         "--src-ip", src_ip, "--port", str(d["port"]), "--udn", d["udn"],
         "--interval", str(interval)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT) for d in devices]

    listed_names = set()
    try:
        service.start()
        print(f"[1/2] 源码侧 SSDPService 已启动（{src_ip}），替身："
              f"{tv['name']} / {router['name']}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            listed_names = {d.name for d in service.get_devices()}
            rejected = [m for m in rejections if router["name"] in m]
            if (tv["name"] in listed_names and rejected
                    and router["name"] not in listed_names):
                break
            for p in kids:
                if p.poll() is not None:
                    print(f"[FAIL] 替身进程提前退出（code={p.returncode}）")
                    return 1
            time.sleep(1.0)
    finally:
        service.stop()
        for p in kids:
            if p.poll() is None:
                p.kill()
                p.wait()
        socks = make_socks(src_ip)
        for d in devices:
            announce(socks, d["kind"], d["udn"], d["location"], nts="ssdp:byebye")
        for s in socks:
            s.close()

    names = sorted(listed_names)
    if router["name"] in names:
        print(f"[FAIL] 源码仍把没有 AVTransport 的 {router['name']} 登记成投屏目标")
        return 1
    if tv["name"] not in names:
        print(f"[FAIL] 假电视没被发现（当前列表：{names or '空'}）——"
              "发现链路本身没跑通，过滤判定无从谈起")
        return 1
    rejected = [m for m in rejections if router["name"] in m]
    if not rejected:
        print("[FAIL] 路由器替身被静默丢弃，日志里没有拒绝理由——判据不成立")
        return 1
    print("[2/2] PASS（源码）：渲染端进列表、路由器挡在门外并留理由")
    print(f"       替身列表：{', '.join(n for n in names if 'CastGate' in n)}")
    print(f"       拒：{rejected[0]}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", default=DEFAULT_LOG, help="sidecar 日志路径")
    ap.add_argument("--src-ip", default=None, help="通告源网卡（默认自动选）")
    ap.add_argument("--ports", type=int, nargs=2, default=[49311, 49312],
                    help="两台替身的描述服务端口（电视 路由器）")
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--source", action="store_true",
                    help="就地跑仓库源码的发现链路（打 tag 前用；需 1900 空闲）")
    ap.add_argument("--serve", choices=sorted(KINDS), help="只当替身（主流程内部调用）")
    ap.add_argument("--port", type=int, default=49311)
    ap.add_argument("--duration", type=float, default=3600.0)
    ap.add_argument("--udn", default=None)
    args = ap.parse_args()

    src_ip = args.src_ip or lan_ip()
    if args.serve:
        serve(args.serve, src_ip, args.port, args.interval, args.duration,
              args.udn or str(uuid.uuid4()))
        return 0
    if args.source:
        return run_source(src_ip, args.ports, args.timeout, args.interval)
    return run(args.log, src_ip, args.ports, args.timeout, args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
