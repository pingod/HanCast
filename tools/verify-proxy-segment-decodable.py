#!/usr/bin/env python3
"""产品代理链路验收：给一条需要防盗链的 m3u8，看本机代理转出来的分片能不能真解码

为什么不用 curl 直接看上游：上游给的是「披着 PNG 皮的 TS」还是推广占位图，
只有走产品自己的 _handle_proxy（补头、判型、剥皮、改写列表）才算验收。
判据是播放器视角的三件事：

1. 转出来的首字节是 TS 同步字节 0x47，且逐包游走时绝大多数步长是 188（真实流里
   偶尔夹一个 192/196 字节的包，固定偏移判据会整体平移，因此按步长游走判比例）；
2. 转发时 Content-Type 不再是 image/*、text/html（播放器按它选解码器）；
3. 打包版 ffmpeg 用**播放器的方式**（不指定 -f）探这段字节：解封装器不许是
   png_pipe/image2 这类图片解码器，且要有真视频流。退出码不算判据 —— 带皮的
   字节 ffmpeg 自己会重同步，`-f mpegts -c copy` 照样 rc=0，拿它判等于没判。

用法:
  # 验工作树
  hancast-backend/.venv/bin/python3.12 tools/verify-proxy-segment-decodable.py \
      <m3u8_url> --referer <页面或源站地址> [--ffmpeg <路径>]
  # 验「已经装上的那份产物」（禁止拿工作树冒充验收）
  hancast-backend/.venv/bin/python3.12 tools/verify-proxy-segment-decodable.py \
      <m3u8_url> --referer <页面> \
      --root /Applications/HanCast.app/Contents/Resources \
      --ffmpeg /Applications/HanCast.app/Contents/MacOS/ffmpeg/ffmpeg
  # 反例自检：喂一段「皮没剥」的字节，工具必须判 FAIL（判据没力气就别谈验收）
  hancast-backend/.venv/bin/python3.12 tools/verify-proxy-segment-decodable.py \
      --probe-only /path/to/skinned.ts

只读上游、只在本机 127.0.0.1 起一个临时端口，不碰任何产品配置。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

_TS_PACKET = 188
FAILS: list[str] = []


def say(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    FAILS.append(msg)
    say(f"FAIL {msg}")


def get(url: str, headers: dict, want: int) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read(want)
        return r.status, dict(r.headers), body


def ts_stride_walk(body: bytes, max_packets: int) -> dict[int, int]:
    """从 0 开始逐包找下一个 0x47，统计包间距的分布

    优先认 188/192/196 三种合法包长；都对不上就有限度地往前找同步字节（记为
    跳变步长），和播放器自己重同步的行为一致。
    """
    steps: dict[int, int] = {}
    pos = 0
    for _ in range(max_packets):
        nxt = next((d for d in (188, 192, 196)
                    if pos + d < len(body) and body[pos + d] == 0x47), None)
        if nxt is None:
            found = body.find(b"\x47", pos + _TS_PACKET, pos + _TS_PACKET + 256)
            if found < 0:
                break
            nxt = found - pos
        steps[nxt] = steps.get(nxt, 0) + 1
        pos += nxt
    return steps


def check_segment(url: str, ua: str, packets: int) -> bytes:
    """拉一条分片的前若干字节并就地判型，返回已取到的字节"""
    status, hdrs, body = get(url, {"User-Agent": ua, "Range": "bytes=0-"}, 1 << 20)
    ctype = (hdrs.get("Content-Type") or "").lower()
    say(f"  分片 GET: status={status} ctype={hdrs.get('Content-Type')} "
        f"cl={hdrs.get('Content-Length')} 取了 {len(body)} 字节")
    if status not in (200, 206):
        fail(f"分片响应码 {status}")
    if not body:
        fail("分片零字节")
        return body
    if body[0] != 0x47:
        fail(f"首字节不是 TS 同步字节（是 {body[:8].hex()}）—— 剥皮没生效")
        return body
    # 逐包游走，而不是拿固定偏移 body[i*188] 去对：真实的 TS 里偶尔夹一个
    # 192/196 字节包（带时间戳扩展），只要出现一次，后面所有固定偏移就整体平移，
    # 固定的「连续 40 包全中」会把好分片判成坏的（dyrs 实测：1594 包步长 188、
    # 1 包 192、1 处跳变，ffmpeg 自同步后完整解码 6 秒）。
    steps = ts_stride_walk(body, packets)
    ts_like = sum(c for s, c in steps.items() if s in (188, 192, 196))
    total = sum(steps.values())
    ratio = ts_like / total if total else 0.0
    say(f"  逐包步长 {dict(sorted(steps.items()))}，TS 包占比 {ratio:.1%}")
    if ratio < 0.95:
        fail(f"仅 {ratio:.1%} 的包按 TS 步长排布（剥皮后残留了非 TS 前缀或长度不对）")
    if ctype.startswith("image/") or ctype.startswith("text/html"):
        fail(f"Content-Type 仍是 {ctype}（播放器会按它挑解码器）")
    return body


# 播放器按「探到的解封装器」决定成败，不看退出码：带皮的分片 ffmpeg 自己就重同步
# 了，`-c copy` 照样 rc=0，拿它当判据等于没判。真信号是 Input #0 那一行 ——
# 皮没剥干净时这里是 png_pipe，mpv 随即报 "no audio or video data played"。
_IMAGE_DEMUXERS = ("png_pipe", "image2", "image2pipe", "jpeg_pipe", "gif", "apng_pipe")


def player_probe(path: str, ffmpeg: str) -> None:
    """按播放器的方式（不指定 -f）探这段字节，判它会被解成视频还是图片"""
    r = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path, "-t", "2", "-f", "null", "-"],
        capture_output=True, timeout=180)
    err = (r.stderr or b"").decode("utf-8", "replace")
    m = re.search(r"^Input #0, (\w+),", err, re.M)
    if not m:
        fail(f"ffmpeg 没能探出解封装器（rc={r.returncode}）: {err.strip()[-200:]}")
        return
    demuxer = m.group(1)
    video = re.search(r"^  Stream #\d+:\d+.*Video: ([\w()-]+)", err, re.M)
    say(f"  探测结果: Input #0, {demuxer}，视频流 "
        f"{video.group(1) if video else '（没有）'}")
    if demuxer in _IMAGE_DEMUXERS:
        fail(f"播放器把它当 {demuxer} 图片解（皮没剥干净 = 「有列表、画面全哑」）")
    elif not video:
        fail(f"解封装成 {demuxer} 了，但没有视频流")
    elif r.returncode != 0:
        fail(f"ffmpeg 真解码失败（rc={r.returncode}）: {err.strip()[-200:]}")
    else:
        say(f"  PASS 按 {demuxer} 解出视频流并解码 2 秒（{os.path.getsize(path)} 字节输入）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("m3u8", nargs="?", default="", help="需要防盗链的播放列表地址")
    ap.add_argument("--probe-only", metavar="FILE", default="",
                    help="只对本地这份字节跑播放器判型（反例自检，不碰产品代码）")
    ap.add_argument("--root", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hancast-backend"),
        help="被验代码的根：工作树的 hancast-backend，或产物里的 "
             "/Applications/HanCast.app/Contents/Resources")
    ap.add_argument("--referer", default="")
    ap.add_argument("--ua", default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/124.0.0.0 Safari/537.36")
    ap.add_argument("--ffmpeg", default="/Applications/HanCast.app/Contents/MacOS/ffmpeg/ffmpeg")
    ap.add_argument("--segments", type=int, default=2, help="验前几条分片")
    ap.add_argument("--packets", type=int, default=200, help="每条分片游走多少个 TS 包判对齐")
    ap.add_argument("--force-peek", type=int, default=0, metavar="字节",
                    help="把判型窗口强行压到这个长度（只在本进程内改，不落产品文件）。"
                         "v2.2.20 的窗口等效 400，用它复现那一次翻车，证明判据抓得住")
    args = ap.parse_args()

    if args.probe_only:
        if not os.path.isfile(args.probe_only):
            fail(f"找不到这份字节: {args.probe_only}")
            say(f"--- {len(FAILS)} FAIL ---")
            return 1
        player_probe(args.probe_only, args.ffmpeg)
        say(f"--- {'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAIL'} ---")
        return 1 if FAILS else 0
    if not args.m3u8:
        ap.error("要么给 m3u8 地址，要么用 --probe-only <文件>")

    global MediaHandler, MediaServer, player_safe_suffix
    sidecar_pkg = os.path.join(args.root, "hancast_sidecar")
    if not os.path.isdir(sidecar_pkg):
        fail(f"--root 下没有 hancast_sidecar 包: {args.root}")
        return 1
    sys.path.insert(0, args.root)
    from hancast_sidecar.media.server import (
        MediaHandler, MediaServer, player_safe_suffix)
    say(f"被验代码: {sys.modules['hancast_sidecar.media.server'].__file__}")

    if args.force_peek:
        # 反控：只在本进程里把窗口压回 v2.2.20 那一档。判据抓不住这次的回归，
        # 上一轮的「ALL PASS」就是假的
        import hancast_sidecar.media.server as server_mod
        server_mod._disguise_peek_len = lambda ctype: args.force_peek
        say(f"（反控：判型窗口压到 {args.force_peek} 字节）")

    headers = {"User-Agent": args.ua}
    if args.referer:
        headers["Referer"] = args.referer

    srv = MediaServer(host="127.0.0.1", port=0, lan_ip="127.0.0.1")
    srv.start(0)
    say(f"本机代理已起: {MediaHandler.base_url}")
    tmp = tempfile.mkdtemp(prefix="hancast-proxy-")
    try:
        pl_url = srv.register_proxy(args.m3u8, headers, player_safe_suffix(args.m3u8))
        for level in range(1, 4):
            _, pl_hdrs, pl_body = get(pl_url, {"User-Agent": args.ua}, 1 << 20)
            text = pl_body.decode("utf-8", "replace")
            say(f"  列表(第 {level} 层) GET: {pl_url} ctype={pl_hdrs.get('Content-Type')} "
                f"{len(pl_body)} 字节，改写后的条目行 {len(re.findall(r'^.*?/proxy/', text, re.M))} 条")
            if not text.startswith("#EXTM3U"):
                fail("代理转出来的不是 m3u8")
                return 1
            # 主列表（master）的条目行本身还是 m3u8：把它们当分片判「首字节 0x47」
            # 必然假失败，播放器则会自己下钻一层，所以这里跟着下一层
            if "#EXT-X-STREAM-INF" not in text:
                break
            entries = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
            if not entries:
                fail("主列表里没有变体")
                return 1
            pl_url = entries[0] if entries[0].startswith("http") else f"{MediaHandler.base_url}{entries[0]}"
        segs = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
        if not segs:
            fail("改写后的列表里没有分片")
            return 1
        for seg in segs[:args.segments]:
            url = seg if seg.startswith("http") else f"{MediaHandler.base_url}{seg}"
            say(f"分片 {seg[:40]}…")
            body = check_segment(url, args.ua, packets=args.packets)
            if body[:1] == b"\x47":
                path = os.path.join(tmp, os.path.basename(seg.split("?")[0]) + ".ts")
                with open(path, "wb") as f:
                    f.write(body)
                if os.path.isfile(args.ffmpeg):
                    player_probe(path, args.ffmpeg)
                else:
                    say(f"  （跳过解码验证：找不到 ffmpeg {args.ffmpeg}）")
    finally:
        srv.stop()

    say(f"--- {'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAIL'} ---")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
