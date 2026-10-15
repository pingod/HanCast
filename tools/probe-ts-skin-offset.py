#!/usr/bin/env python3
"""定位「PNG 皮 + TS」的精确剥离偏移，并验证剥完能否被 ffmpeg 解封装。

用法: tools/probe-ts-skin-offset.py <m3u8_url> [--index N]
"""
import argparse
import collections
import subprocess
import sys
import urllib.parse as up
from pathlib import Path

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def get(url, headers, want=4000000):
    r = requests.get(url, headers=headers, timeout=30, stream=True)
    body = b""
    for c in r.iter_content(chunk_size=65536):
        body += c
        if len(body) >= want:
            break
    r.close()
    return r, body


def alignment(body, off, span):
    return sum(1 for i in range(span) if body[off + 188 * i] == 0x47)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("playlist")
    ap.add_argument("--index", type=int, default=0)
    args = ap.parse_args()

    s = requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(args.playlist, timeout=15)
    lines = [ln.strip() for ln in r.text.splitlines() if ln.strip() and not ln.startswith("#")]
    seg = up.urljoin(r.url, lines[args.index])
    print(f"segment: {seg[:120]}")

    variants = {
        "UA 无 Range": {"User-Agent": UA},
        "UA + bytes=0-": {"User-Agent": UA, "Range": "bytes=0-"},
        "UA + bytes=0-4294967295": {"User-Agent": UA, "Range": "bytes=0-4294967295"},
        "无 UA 无 Range": {"User-Agent": ""},
    }
    for label, h in variants.items():
        rr, body = get(seg, h)
        clen = rr.headers.get("Content-Length")
        crange = rr.headers.get("Content-Range")
        best = max(range(0, min(4096, len(body) - 188 * 40)),
                   key=lambda o: alignment(body, o, 40))
        print(f"\n{label}: status={rr.status_code} ctype={rr.headers.get('Content-Type')!r} "
              f"clen={clen} content-range={crange} 采样={len(body)}B")
        print(f"  最佳对齐偏移={best} (40 包中 {alignment(body, best, 40)} 个 0x47)，"
              f"偏移 0 对齐={alignment(body, 0, 40)}")
        # 剥皮前后各交给 ffprobe 一次，验证「剥皮才解得开」
        # 落 .tmp-win（仓库内本地临时区），别写 /tmp——那里系统只清 >3 天的普通文件，实测等于不回收
        scratch = Path(__file__).resolve().parent.parent / ".tmp-win"
        scratch.mkdir(exist_ok=True)
        for tag, data in (("原样", body), ("剥皮", body[best:])):
            path = scratch / f"hancast-skin-{tag}.ts"
            with open(path, "wb") as f:
                f.write(data)
            p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                "format=format_name,duration:stream=codec_type",
                                "-of", "default=noprint_wrappers=1", path],
                               capture_output=True)
            print(f"  ffprobe {tag}({len(data)}B): rc={p.returncode} "
                  f"{p.stdout.decode().strip()[:120]!r} {p.stderr.decode().strip()[:100]!r}")


if __name__ == "__main__":
    main()
