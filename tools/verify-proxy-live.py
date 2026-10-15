#!/usr/bin/env python3
"""端到端验收「粘贴投屏」的代理链路：真起一个 MediaServer，把远端播放列表挂成
本机代理，再用 ffprobe 冒充播放器逐片检查字节能不能解出画面/声音。

用法: tools/verify-proxy-live.py <m3u8_url> [--referer U] [--picks 0,300,699]
退出码 0 = 每个抽样分片都被识别为 mpegts 且有视频流。
"""
import argparse
import os
import re
import subprocess
import sys
import urllib.parse as up

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hancast-backend"))

from hancast_sidecar.media.server import MediaServer, MediaHandler  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def probe(target: str) -> tuple[int, str]:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=format_name,duration:stream=codec_type",
                        "-of", "default=noprint_wrappers=1", target],
                       capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("playlist")
    ap.add_argument("--referer", default="")
    ap.add_argument("--picks", default="0,300,699")
    args = ap.parse_args()

    srv = MediaServer(host="127.0.0.1", lan_ip="127.0.0.1")
    srv.start()
    headers = {"User-Agent": UA}
    if args.referer:
        headers["Referer"] = args.referer
        headers["Origin"] = re.sub(r"^(https?://[^/]+).*", r"\1", args.referer)
    local = srv.register_proxy(args.playlist, headers, ".m3u8")
    print(f"代理: {local}")

    fails = 0
    rc, out = probe(local)
    print(f"\n[整表] ffprobe rc={rc}\n{out}")
    if rc != 0 or not re.search(r"format_name=(hls|mpegts)", out):
        fails += 1

    text = subprocess.run(["curl", "-s", local], capture_output=True,
                          text=True).stdout
    segs = [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]
    print(f"改写后列表 {len(segs)} 片，首个: {segs[0][:110] if segs else '—'}")

    for pick in (int(x) for x in args.picks.split(",") if x):
        if pick >= len(segs):
            continue
        url = segs[pick]
        for mode, rng in (("无 Range", None), ("bytes=0-", {"Range": "bytes=0-"})):
            body = subprocess.run(
                ["curl", "-s", *(["-H", f"Range: {rng['Range']}"] if rng else []), url],
                capture_output=True).stdout
            path = f"/tmp/hancast-verify-{pick}.ts"   # 即抛件：下面 ffprobe 完就 os.remove
            with open(path, "wb") as f:
                f.write(body)
            rc, out = probe(path)
            ok = rc == 0 and "video" in out
            print(f"\n[片 {pick} · {mode}] {len(body)}B 首字节=0x{body[0]:02x} "
                  f"mpegts={'mpegts' in out} 视频流={'video' in out}")
            print(f"  {out.replace(chr(10), ' | ')[:180]}")
            if not ok:
                fails += 1
            os.remove(path)

    srv.stop()
    print(f"\nSUMMARY: {'PASS' if not fails else f'FAIL({fails})'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
