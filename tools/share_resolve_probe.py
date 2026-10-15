#!/usr/bin/env python3
"""粘贴投屏解析现场复现器：把一段分享文案喂给真实的解析管线并验证播放器能不能解出画面。

用法:
    python tools/share_resolve_probe.py "<粘贴的链接或口令文案>"
    python tools/share_resolve_probe.py --play "<url>"      # 解析完再用 ffmpeg 实解一段
    python tools/share_resolve_probe.py --mpv "<url>"       # 解析完再用打包版 mpv 实解
    python tools/share_resolve_probe.py --serve "<url>"     # 起真实媒体代理（默认用替身）
    python tools/share_resolve_probe.py --log "<url>"       # 只看决策日志
    python tools/share_resolve_probe.py --rules-dir /tmp/r "<url>"
    python tools/share_resolve_probe.py --mpv --segment "<分片/流地址>" --referer "<页面地址>"
                                                            # 跳过解析，只验代理+播放器

默认替身代理不占端口、不起服务；--serve/--play 会真的起 MediaServer，
于是「伪装分片走本机代理」这类结论可以直接看到播放器视角的结果。
"""
import argparse
import atexit
import json
import logging
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hancast-backend"))

from hancast_sidecar.media.share_resolver import (  # noqa: E402
    ShareResolver, extract_candidates)


class StubMediaServer:
    """MediaServer 替身：记录代理注册，返回可辨识地址，不监听端口"""

    def __init__(self):
        self.proxies = []

    def register_proxy(self, remote_url, headers=None, suffix=""):
        self.proxies.append((remote_url, headers or {}))
        return f"http://127.0.0.1:0/proxy/{len(self.proxies)}{suffix}"


def probe_play(uri: str, seconds: int = 12) -> int:
    """用 ffmpeg 站在播放器视角实解一段：解码不出错才算能播

    选 ffmpeg 而不是 ffprobe：hls.c 的「分片后缀白名单」只在真正拉分片时触发，
    而它正是这类站点最容易踩的坑。
    """
    exe = shutil.which("ffmpeg") or shutil.which("ffprobe")
    if not exe:
        print("系统里没有 ffmpeg/ffprobe，跳过实解验证", file=sys.stderr)
        return 2
    probe_only = os.path.basename(exe) == "ffprobe"
    cmd = [exe, "-hide_banner", "-loglevel", "warning", "-user_agent",
           "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
           "-i", uri]
    cmd += (["-show_entries", "format=duration", "-of", "default=nw=1"] if probe_only
            else ["-t", str(seconds), "-f", "null", "-"])
    err = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if err.returncode == 0:
        print(f"实解通过：{'ffprobe 探测' if probe_only else f'ffmpeg 解码 {seconds}s'} 无错误",
              file=sys.stderr)
        return 0
    print(f"实解失败 rc={err.returncode}", file=sys.stderr)
    for line in (err.stderr or "").strip().splitlines()[-6:]:
        print("   ", line, file=sys.stderr)
    return 1


MPV_BUNDLED = ("/Applications/HanCast.app/Contents/Resources/mpv/"
               "mpv.app/Contents/MacOS/mpv")
_PLAY_FAIL_HINTS = ("blocked for security reasons", "Failed to open segment",
                    "not in allowed_segment_extensions", "Failed to open",
                    "Error opening", "Invalid data found")


def probe_mpv(uri: str, seconds: int = 8, timeout: int = 90) -> int:
    """用打包版 mpv（真正干渲染的那个）实解一段

    打包 mpv 里的 ffmpeg 比系统版更严：hls.c 的 allowed_extensions 会把无后缀
    或 .png 分片直接判死，代理路径的后缀就是为它准备的。
    """
    exe = MPV_BUNDLED if os.path.exists(MPV_BUNDLED) else shutil.which("mpv")
    if not exe:
        print("找不到 mpv（打包版与 PATH 都没有），跳过实解", file=sys.stderr)
        return 2
    cmd = [exe, "--no-config", "--ao=null", "--vo=null", "--msg-level=demux=v",
           f"--length={seconds}", uri]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        code = r.returncode
        hung = False
    except subprocess.TimeoutExpired as e:
        out = _text(e.stdout) + _text(e.stderr)
        code = 0
        hung = True
    bad = [h for h in _PLAY_FAIL_HINTS if h in out]
    # 掐断超时只有在「确实认出了封装格式」时才算通过，否则可能是卡在打开阶段
    opened = "Detected file format" in out or "Trying demuxers for level=1" in out
    if code == 0 and not bad and not hung:
        print(f"实解通过：{os.path.basename(exe)}（{exe}）播放 {seconds}s 无报错",
              file=sys.stderr)
        return 0
    if hung and not bad and opened:
        print(f"实解通过：{os.path.basename(exe)} 播放 {seconds}s 无报错"
              f"（{timeout}s 未自行退出，已认出封装格式）", file=sys.stderr)
        return 0
    print(f"实解失败 rc={code}{'/超时' if hung else ''} 命中: "
          f"{bad or ('无错误串但没解出画面' if hung else '无错误串但退出码非 0')}",
          file=sys.stderr)
    for line in [l for l in out.splitlines() if l.strip()][-6:]:
        print("   ", line[:200], file=sys.stderr)
    return 1


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""



def probe_registered_segment(args) -> int:
    """把单条流/分片地址直接登记进本机代理再实解（伪装分片链路复现）

    站点被 WAF 挡住、解析走不通时，用上一层抓到的分片地址照样能验播放器视角：
    图片伪装头剥没剥、Content-Range 修正后打包 mpv 认不认。
    """
    from hancast_sidecar.media.server import MediaServer, player_safe_suffix
    if not args.referer:
        print("--segment 需要 --referer，否则代理按「无请求头的外部站点」拒转",
              file=sys.stderr)
        return 2
    server = MediaServer()
    server.start()
    try:
        uri = server.register_proxy(args.segment, {"Referer": args.referer},
                                    player_safe_suffix(args.segment))
        print(f"代理地址: {uri}", file=sys.stderr)
        return probe_mpv(uri) if args.mpv else probe_play(uri)
    finally:
        server.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", help="粘贴内容（链接或口令文案）")
    ap.add_argument("--rules-dir", help="外置声明式规则目录（默认读用户配置目录）")
    ap.add_argument("--budget", type=float, default=25.0, help="单次解析时长上限（秒）")
    ap.add_argument("--serve", action="store_true", help="起真实 MediaServer 做代理")
    ap.add_argument("--play", action="store_true", help="解析结果用 ffmpeg 实解一段")
    ap.add_argument("--mpv", action="store_true", help="解析结果用打包版 mpv 实解一段")
    ap.add_argument("--log", action="store_true", help="只打印决策日志，不打印结果 JSON")
    ap.add_argument("-q", "--quiet", action="store_true", help="只打印最终结果")
    ap.add_argument("--segment", help="跳过解析，直接把这条远程流/分片地址登记进本机代理再实解")
    ap.add_argument("--referer", help="--segment 要携带的防盗链 Referer（页面地址）")
    ap.add_argument("--keep-ledger", action="store_true",
                    help="让失败记录写进用户真实的解析失败台账（默认改写临时目录）")
    args = ap.parse_args()
    if not args.segment and not args.text:
        ap.error("需要粘贴内容，或 --segment <流地址> --referer <页面地址>")

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        stream=sys.stderr, format="%(levelname)-7s %(name)s: %(message)s")

    if args.segment:
        return probe_registered_segment(args)

    server = StubMediaServer()
    if args.serve or args.play or args.mpv:
        from hancast_sidecar.media.server import MediaServer
        server = MediaServer()
        server.start()
        print(f"媒体代理: http://{server._lan_ip}:{server.port}", file=sys.stderr)

    import hancast_sidecar.media.share_resolver as sr
    if args.budget != 25.0:
        sr._TOTAL_BUDGET = args.budget

    if not args.keep_ledger:
        # 解不出来时 resolve() 会往台账记一条，而那份台账是设置页给用户看的真实失败清单；
        # 探针调试的地址默认挪到临时目录，别混进去
        import tempfile
        from hancast_sidecar.media import failures
        tmp_ledger = tempfile.mkdtemp(prefix="hancast-probe-ledger-", dir="/tmp")
        failures.SETTING_DIR = tmp_ledger
        atexit.register(shutil.rmtree, tmp_ledger, ignore_errors=True)

    print(f"候选: {extract_candidates(args.text)}", file=sys.stderr)
    try:
        result = ShareResolver(server, rules_dir=args.rules_dir).resolve(args.text)
    except Exception as e:
        print(f"解析失败: {e}", file=sys.stderr)
        return 1
    if not args.log:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    rc = 0
    if args.play:
        rc = probe_play(result["uri"])
    if rc == 0 and args.mpv:
        rc = probe_mpv(result["uri"])
    if args.serve or args.play or args.mpv:
        server.stop()
    return rc


if __name__ == "__main__":
    sys.exit(main())
