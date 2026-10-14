#!/usr/bin/env python3
"""静默下线的设备必须被探活链路摘掉（对应「局域网设备下线了还是会显示」）

byebye 那条路好验——对端正常退出一定会发告别包，真凶是**断电、拔网线、
被 kill -9** 这一类来不及告别的设备：它们只会安静地赖在列表里。
本工具造一个可控替身：

1. 起一台假的 MediaRenderer（127.0.0.1 上的 description.xml + 周期性
   NOTIFY ssdp:alive），确认 HanCast 把它发现进列表；
2. `SIGKILL` 掉替身——它一条 byebye 都发不出来；
3. 等 sidecar 的静默扫描（`discovery.device_stale_s`，默认 90s）连探两次
   无应答后，日志里出现「设备已下线（连续 2 次探活无应答）」。

LOCATION 故意写 127.0.0.1：通告包源地址是真实网卡，但描述地址不是本机
网卡，因此不会被 `_is_own_location` 的自通告回环过滤挡掉。

用法：
    python3 tools/verify-ssdp-offline-eviction.py            # 用打包版日志
    python3 tools/verify-ssdp-offline-eviction.py --log /path/to/hancast.log
    python3 tools/verify-ssdp-offline-eviction.py --serve --port 49217  # 只当替身（内部用）
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
STALE_S_DEFAULT = 90.0

DESC_TMPL = """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
  <deviceList>
    <device>
      <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
      <friendlyName>{name}</friendlyName>
      <manufacturer>HanCast Tools</manufacturer>
      <modelName>Offline Eviction Probe</modelName>
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
  </deviceList>
</root>
"""


def lan_ip():
    """挑一块真实网卡地址作为通告源（让设备表里那台假设备显示成正常内网 IP）"""
    out = subprocess.run(["route", "-n", "get", "default"],
                         capture_output=True, text=True).stdout
    iface = None
    for line in out.splitlines():
        if "interface" in line:
            iface = line.split(":")[-1].strip()
            break
    if iface:
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


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/description.xml":
            body = DESC_TMPL.format(name=self.server.dev_name,
                                    udn=self.server.udn).encode()
            self.send_response(200)
            self.send_header("Content-Type", 'text/xml; charset="utf-8"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, *a):  # 安静：进度只写我们自己的 stdout
        pass


def announce(socks, name, udn, location, nts="ssdp:alive", ttl_cachectl="max-age=66"):
    usn_root = f"uuid:{udn}::urn:schemas-upnp-org:device:MediaRenderer:1"
    targets = [("upnp:rootdevice", f"uuid:{udn}::upnp:rootdevice"),
               (f"uuid:{udn}", f"uuid:{udn}"),
               ("urn:schemas-upnp-org:device:MediaRenderer:1", usn_root)]
    for nt, usn in targets:
        msg = (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            f"CACHE-CONTROL: {ttl_cachectl}\r\n"
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


def serve(src_ip: str, port: int, interval: float, duration: float,
          udn: str) -> None:
    """当替身渲染器：发通告 + 供描述，直到被 SIGKILL"""
    name = f"HanCastOfflineProbe-{port}"
    location = f"http://127.0.0.1:{port}/description.xml"

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.dev_name = name
    httpd.udn = udn
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

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
    if not socks:
        print("[serve] 无法建立通告 socket", flush=True)
        raise SystemExit(1)

    print(f"[serve] name={name} udn={udn} location={location} src={src_ip}",
          flush=True)
    deadline = time.time() + duration
    while time.time() < deadline:
        announce(socks, name, udn, location)
        time.sleep(interval)


def tail_from(path: str, offset: int) -> tuple[str, int]:
    try:
        with open(path, "r", errors="replace") as f:
            f.seek(offset)
            data = f.read()
            return data, f.tell()
    except OSError:
        return "", offset


def run(log_path: str, src_ip: str, port: int, stale_s: float,
        timeout: float, no_cleanup: bool) -> int:
    name = f"HanCastOfflineProbe-{port}"
    udn = str(uuid.uuid4())
    location = f"http://127.0.0.1:{port}/description.xml"
    if not os.path.exists(log_path):
        print(f"[FAIL] 找不到 sidecar 日志：{log_path}（HanCast 是否在跑？）")
        return 1

    offset = os.path.getsize(log_path)
    child = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--serve",
         "--src-ip", src_ip, "--port", str(port), "--udn", udn],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    found = evicted = ""
    deadline = time.time() + timeout
    killed_at = None
    try:
        # ── 阶段 1：等发现 ──
        print(f"[1/3] 替身设备已启动（pid={child.pid}），等它被发现…")
        while time.time() < deadline and not found:
            chunk, offset = tail_from(log_path, offset)
            for line in chunk.splitlines():
                if name in line and "Found device" in line:
                    found = line.strip()
            if child.poll() is not None:
                print(f"[FAIL] 替身进程提前退出（code={child.returncode}）")
                return 1
            time.sleep(1.0)
        if not found:
            print(f"[FAIL] {int(deadline - time.time())}s 内没发现这台替身设备"
                  "——先查发现链路，下线判定无从谈起")
            return 1
        print(f"     {found}")

        # ── 阶段 2：SIGKILL，制造静默死亡 ──
        child.kill()
        child.wait()
        killed_at = time.time()
        expect = stale_s + 2 * 30 + 20
        print(f"[2/3] 已 SIGKILL 替身（无 byebye）。按 stale_s={stale_s:.0f}s "
              f"推算，约 {expect:.0f}s 内应被摘除，最多等 {int(timeout)}s")

        # ── 阶段 3：等探活摘除 ──
        while time.time() < deadline:
            chunk, offset = tail_from(log_path, offset)
            for line in chunk.splitlines():
                if name not in line:
                    continue
                if "探活无应答" in line or "设备已下线" in line:
                    print(f"     {line.strip()}")
                if "下线" in line and "探活" in line:
                    evicted = line.strip()
            if evicted:
                break
            time.sleep(1.0)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        if not no_cleanup and not evicted:
            # 判定失败时兜底补发告别，免得测试设备赖在谁的列表里
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
                s.bind((src_ip, 0))
                announce([s], name, udn, location, nts="ssdp:byebye")
                s.close()
            except OSError:
                pass

    if not evicted:
        print(f"[FAIL] {int(timeout)}s 内未看到「连续 2 次探活无应答」下线日志"
              "——静默扫描链路没跑通")
        return 1
    took = time.time() - killed_at
    print(f"[3/3] PASS：静默死亡的设备在 {took:.0f}s 后被摘除")
    print(f"     {evicted}")
    print(f"     证据：{found}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", default=DEFAULT_LOG, help="sidecar 日志路径")
    ap.add_argument("--src-ip", default=None, help="通告源网卡地址（默认自动选默认路由网卡）")
    ap.add_argument("--port", type=int, default=49217, help="替身描述服务端口")
    ap.add_argument("--stale-s", type=float, default=None,
                    help=f"discovery.device_stale_s 的值（默认 {STALE_S_DEFAULT:.0f}；"
                         "改过后台参数时请传实际值）")
    ap.add_argument("--timeout", type=float, default=420, help="总等待上限秒数")
    ap.add_argument("--no-cleanup", action="store_true", help="保留替身进程（调试用）")
    ap.add_argument("--interval", type=float, default=5.0, help="通告间隔秒（--serve 用）")
    ap.add_argument("--duration", type=float, default=3600.0, help="通告总时长秒（--serve 用）")
    ap.add_argument("--serve", action="store_true", help="只当替身渲染器（主流程内部调用）")
    ap.add_argument("--udn", default=None, help="替身设备的 UDN（--serve 用，主流程生成）")
    args = ap.parse_args()

    src_ip = args.src_ip or lan_ip()
    if args.serve:
        serve(src_ip, args.port, args.interval, args.duration,
              args.udn or str(uuid.uuid4()))
        return 0
    return run(args.log, src_ip, args.port,
               args.stale_s if args.stale_s is not None else STALE_S_DEFAULT,
               args.timeout, args.no_cleanup)


if __name__ == "__main__":
    raise SystemExit(main())
