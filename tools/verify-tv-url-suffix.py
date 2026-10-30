#!/usr/bin/env python3
"""电视直链后缀门：判据矩阵（v2.4.2 的真机结论入库）

现场（2026-10-28）：抖音分享解析后投 TCL 85T8G Max 一点动静都没有，同一份内容
投另一台跑 HanCast 的 Linux 机器却正常。用裸 SOAP 手工灌各种地址才看清：

    地址形态                                          电视是否发起 GET
    http://192.168.1.5:18080/ext/test.mp4             是
    http://192.168.1.5:18080/proxy/a1b2c3.ts          是
    http://192.168.1.5:18080/noext/stream?vid=1       否（服务端零连接）
    抖音 …/video/tos/cn/tos-cn-ve-15/<hash>/           否

把同一份 MP4 字节换个路径就换一个结果，**DIDL 里的 protocolInfo 被无视**，
电视只按 URL 末段后缀决定拉不拉流。抖音直链的路径恰好无后缀 ⇒ 永远不碰。
所以 v2.4.2 在 `_publish()` 加了一道门：交付给电视的直链后缀不认账时，
改走本机代理（`/proxy/<hash><suffix>`）再给电视。

本工具只跑纯函数，不连网、不出广播、不读写用户配置——判据是那套真机矩阵
在 `player_ext_ok()` / `player_safe_suffix()` 上的投影。真机复核仍以电视实际
发起拉流为准（`docs/故障排查.md`）。

用法：
    python3 tools/verify-tv-url-suffix.py
    python3 tools/verify-tv-url-suffix.py -v      # 逐条打印判据
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hancast-backend"))

from hancast_sidecar.media.server import (  # noqa: E402
    player_ext_ok,
    player_safe_suffix,
)

VERBOSE = False
FAILS = []


def chk(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{name}: got {got!r} want {want!r}")
        print(f"  FAIL  {name} — got {got!r} want {want!r}")
    elif VERBOSE:
        print(f"  PASS  {name}  ({got!r})")
    return ok


# ── 后缀白名单：真机认账的形态 ──
OK_URLS = [
    ("普通 mp4 直链", "http://192.168.1.5:18080/media/abc.mp4"),
    ("代理 ts", "http://192.168.1.5:18080/proxy/a1b2c3.ts"),
    ("HLS 播放列表", "http://192.168.1.5:18080/hls/9cddc7/live.m3u8"),
    ("带查询串", "http://cdn.example.com/v.mp4?auth=1&expires=2"),
    ("大写后缀", "http://cdn.example.com/V.MP4"),
    ("分片伪装成图片也要认出来", "http://cdn.example.com/seg.png.ts"),
]

# ── 电视不会碰的形态（必须判不合格，否则门形同不存在）──
BAD_URLS = [
    ("抖音直链：无后缀目录式", "https://v3-web.douyinvod.com/abc/video/tos/cn/tos-cn-ve-15/hash123/"),
    ("无后缀带查询串", "http://192.168.1.5:18080/noext/stream?vid=1"),
    ("尾部斜杠", "http://cdn.example.com/media/"),
    ("后缀不在白名单", "http://cdn.example.com/v.php"),
    ("没有路径", "http://cdn.example.com"),
]


def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    print("后缀白名单（电视认账的形态）：")
    for name, url in OK_URLS:
        chk(f"player_ext_ok({name})", player_ext_ok(url), True)

    print("电视不拉流的形态（必须判不合格）：")
    for name, url in BAD_URLS:
        chk(f"player_ext_ok({name})", player_ext_ok(url), False)

    print("代理后缀回填（改走代理时给电视一个合规后缀）：")
    cases = [
        ("mp4 原样保留", "http://cdn.example.com/a/b.mp4", "video/mp4", ".mp4"),
        ("无后缀按 MIME 兜底", "https://v3.douyinvod.com/x/video/tos/cn/hash/", "video/mp4", ".mp4"),
        ("无 MIME 兜到底", "https://cdn.example.com/noext/", "", ".ts"),
        ("播放列表优先 .m3u8", "http://cdn.example.com/live/master", "application/vnd.apple.mpegurl", ".m3u8"),
        ("非白名单后缀被换掉", "http://cdn.example.com/seg.php", "video/mp2t", ".ts"),
    ]
    for name, url, mime, want in cases:
        chk(f"player_safe_suffix({name})", player_safe_suffix(url, mime), want)

    print("回填出来的后缀必须自己也在白名单里（否则门会把自己关进去）：")
    for name, url, mime, _ in cases:
        suffix = player_safe_suffix(url, mime)
        chk(f"回填后缀合规({name})", player_ext_ok("http://h/proxy/deadbeef" + suffix), True)

    print()
    if FAILS:
        print(f"RESULT: FAIL（{len(FAILS)} 条不符）")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
