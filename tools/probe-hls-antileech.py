#!/usr/bin/env python3
"""诊断 HLS 分片的防盗链/伪装行为：同一分片用不同请求头形状各拉一次首块，
打印魔数、PNG 头长度、TS 同步偏移，用来决定代理该走哪套头、要不要剥皮。

用法: tools/probe-hls-antileech.py <m3u8_url> [--index N] [--cookie "k=v; k2=v2"]
"""
import argparse
import re
import sys
import zlib

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def ts_offsets(head: bytes, span: int = 6, limit: int = 8192):
    """返回所有「从 off 起连续 span 个 188 字节包都以 0x47 开头」的偏移"""
    hits = []
    for off in range(0, min(limit, len(head) - 188 * span) + 1):
        if all(head[off + 188 * i] == 0x47 for i in range(span)):
            hits.append(off)
    return hits


def png_info(head: bytes):
    """PNG 结构的 IHDR/IDAT/IEND 位置；纯占位图和假皮真流都从这里区分"""
    out = {}
    for name in (b"IHDR", b"IDAT", b"IEND"):
        out[name.decode()] = head.find(name)
    out["crc_ok"] = None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) > 33:
        ln = int.from_bytes(head[8:12], "big")
        chunk = head[12:12 + ln]
        crc = int.from_bytes(head[12 + ln:16 + ln], "big")
        out["crc_ok"] = zlib.crc32(head[12:12 + ln + 4]) & 0xFFFFFFFF == crc
        out["first_chunk_len"] = ln
    return out


def report(label, r, body):
    head = body[:20000]
    tso = ts_offsets(head)
    print(f"\n=== {label}")
    print(f"  status={r.status_code} ctype={r.headers.get('Content-Type')!r} "
          f"clen={r.headers.get('Content-Length')} "
          f"content-range={r.headers.get('Content-Range')!r}")
    print(f"  head16={head[:16].hex()}")
    print(f"  png={png_info(head)}")
    print(f"  前 20KB 内 TS 同步偏移: {tso[:6]}{' …' if len(tso) > 6 else ''}")
    print(f"  拿到 {len(body)} 字节；html 标记={body[:200].lstrip()[:60]!r}")
    return tso


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("playlist")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--cookie", default="")
    args = ap.parse_args()

    s = requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(args.playlist, timeout=15)
    print(f"playlist: {r.status_code} {r.headers.get('Content-Type')} {len(r.text)}B")
    lines = [ln.strip() for ln in r.text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        sys.exit("播放列表里没有分片")
    import urllib.parse as up
    seg = up.urljoin(r.url, lines[args.index])
    print(f"segment[{args.index}]: {seg[:140]}")

    ref = up.urljoin(args.playlist, "/")
    shapes = {
        "A 页面头(UA+Referer+Origin)": {"User-Agent": UA, "Referer": ref,
                                        "Origin": ref.rstrip("/")},
        "B 页面头 + Cookie": {"User-Agent": UA, "Referer": ref, "Origin": ref.rstrip("/"),
                              "Cookie": args.cookie} if args.cookie else None,
        "C 裸 UA": {"User-Agent": UA},
        "D 自 Referer(同域 api/m3u8)": {"User-Agent": UA, "Referer": args.playlist},
        "E 无 UA": {"User-Agent": "", "Referer": ref},
        "F 带 Range 的页面头": {"User-Agent": UA, "Referer": ref, "Range": "bytes=0-1023"},
    }
    for label, headers in shapes.items():
        if not headers:
            continue
        try:
            rr = s.get(seg, headers=headers, timeout=15, stream=True)
            body = b""
            for chunk in rr.iter_content(chunk_size=8192):
                body += chunk
                if len(body) >= 20000:
                    break
            rr.close()
            report(label, rr, body)
        except Exception as e:
            print(f"\n=== {label}\n  失败: {e}")


if __name__ == "__main__":
    main()
