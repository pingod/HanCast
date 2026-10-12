#!/usr/bin/env python3
"""SSDP 现场探针：设备「发现不到」时先看网络上到底有什么

沿 HanCast 的组播路径（239.255.255.250:1900）双向取证：
  * 收：谁在发 NOTIFY aliv / 谁在问 M-SEARCH（含来源接口与 LOCATION）
  * 发：本机 M-SEARCH，谁应答

每块网卡各开一个组播成员，所以能直接看出「某块网卡收不到组播」这类
单向发现问题（例：macOS 端看不到 Windows 渲染器，而反向正常）。

用法：
    python3 tools/ssdp_probe.py                 # 全部网卡，听 20 秒并主动搜一轮
    python3 tools/ssdp_probe.py --seconds 60    # 只听，等电视上线
    python3 tools/ssdp_probe.py --st MediaRenderer --search-only
"""
import argparse
import select
import socket
import struct
import time
from urllib.parse import urlparse

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900


def local_interfaces():
    """本机 IPv4 地址列表（回环除外）"""
    ips = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in infos:
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    ips.append("0.0.0.0")
    return ips


def open_listener(bind_ip, join_group=True):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((bind_ip, SSDP_PORT))
    if join_group:
        mreq = struct.pack("4sl", socket.inet_aton(SSDP_ADDR), socket.INADDR_ANY)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.setblocking(False)
    return s


def build_search(st):
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {st}\r\n\r\n"
    ).encode()


def parse(data):
    lines = data.decode("utf-8", "replace").splitlines()
    status = lines[0].strip() if lines else ""
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().upper()] = value.strip()
    return status, headers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=20, help="监听时长")
    ap.add_argument("--st", default="ssdp:all",
                    help="搜索目标，如 ssdp:all / urn:schemas-upnp-org:device:MediaRenderer:1")
    ap.add_argument("--search-only", action="store_true", help="不发 M-SEARCH，只听")
    args = ap.parse_args()

    socks = {}
    for ip in local_interfaces():
        try:
            socks[ip] = open_listener(ip)
            print(f"[listen] {ip}:1900")
        except OSError as e:
            print(f"[listen] {ip}:1900 失败：{e}")
    if not socks:
        raise SystemExit("没有任何可绑定的网卡（1900 是否被 HanCast 占用？）")

    if not args.search_only:
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        for src in socks:
            if src == "0.0.0.0":
                continue
            try:
                out.bind((src, 0))
            except OSError:
                pass
            try:
                out.sendto(build_search(args.st), (SSDP_ADDR, SSDP_PORT))
                print(f"[search] from {src} ST={args.st}")
            except OSError as e:
                print(f"[search] from {src} 失败：{e}")
        socks["(search)"] = out

    deadline = time.time() + args.seconds
    seen = set()
    n = 0
    while time.time() < deadline:
        rready, _, _ = select.select(list(socks.values()), [], [], 1.0)
        for sk in rready:
            src_ip = sk.getsockname()[0]
            while True:
                try:
                    data, addr = sk.recvfrom(65535)
                except (OSError, BlockingIOError):
                    break
                status, headers = parse(data)
                loc = headers.get("LOCATION", "")
                key = (addr[0], status.split(" ")[0], headers.get("NTS", ""),
                       headers.get("NT", ""), headers.get("ST", ""), loc,
                       headers.get("USN", ""))
                if key in seen:
                    continue
                seen.add(key)
                n += 1
                host = urlparse(loc).netloc if loc else ""
                print(f"[{time.strftime('%H:%M:%S')}] via {src_ip:<12} "
                      f"from {addr[0]:<15} {status:<20} "
                      f"NTS={headers.get('NTS', '-')} "
                      f"NT={headers.get('NT', headers.get('ST', '-'))} "
                      f"USN={headers.get('USN', '-')} "
                      f"LOC={loc or '-'}"
                      + (f"  <== 与控制端口不同主机:{host}" if host and host.split(':')[0] != addr[0] else ""))
    print(f"\n共 {n} 条不同的 SSDP 报文（{args.seconds}s，"
          f"{len(socks) - 1} 块网卡）")
    for sk in socks.values():
        sk.close()


if __name__ == "__main__":
    main()
