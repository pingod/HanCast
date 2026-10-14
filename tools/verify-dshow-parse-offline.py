#!/usr/bin/env python3
"""离线回放 Windows dshow 设备枚举，验 _list_audio_devices 的解析。

用法：tools/verify-dshow-parse-offline.py <ffmpeg -list_devices 抓到的原始输出文件>

为什么要在 Mac 上跑：.68 的麦克风名是中文、设备列表随插拔变化，UI 里点一遍既慢
又不可复现。这里把真机抓到的原始 stderr 喂给打包用的同一份解析代码，判据是
「认出的音频设备集合 == 原始文本里带 (audio) 后缀的行」，且 Alternative name
行一条都不能混进来（它同样带引号，早期只按分段头切段的 build 会把它当设备）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "hancast-backend"))

from hancast_sidecar.screencast import ffmpeg_manager  # noqa: E402

FAILS: list[str] = []


def say(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    FAILS.append(msg)
    say(f"FAIL {msg}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture", help="dshow -list_devices 的原始输出（UTF-8）")
    args = ap.parse_args()

    raw = open(args.capture, "rb").read().decode("utf-8", "replace")
    want = re.findall(r'"(.+)"\s*\(audio\)\s*$', raw, re.M)
    alt = len(re.findall(r"Alternative name", raw))
    say(f"原始输出：audio 设备 {len(want)} 个，Alternative name 行 {alt} 条，"
        f"分段头 {'有' if 'DirectShow audio devices' in raw else '无'}")
    if not want:
        fail("抓到的文本里一个 (audio) 设备都没有，样本无效")

    # 把解析代码接到真机样本上：os.name 冒充 nt、ffmpeg 调用换成回放
    mgr = ffmpeg_manager.FfmpegManager()
    mgr.get_path = lambda: "ffmpeg"          # type: ignore[method-assign]
    real_name = os.name
    ffmpeg_manager._run_utf8 = lambda cmd, timeout: subprocess.CompletedProcess(  # type: ignore
        cmd, 0, "", raw)
    ffmpeg_manager.os.name = "nt"
    try:
        got = mgr._list_audio_devices()
    finally:
        ffmpeg_manager.os.name = real_name

    names = [d["name"] for d in got]
    say(f"解析结果：{len(names)} 个 → " + " / ".join(
        f"{d['name']}[{d['kind']}]" for d in got))

    if names != want:
        fail(f"设备集合不符\n  期望: {want}\n  实际: {names}")
    leaked = [n for n in names if n.startswith("@device_")]
    if leaked:
        fail(f"Alternative name 混进了设备列表: {leaked}")
    kinds = {d["name"]: d["kind"] for d in got}
    # 「耳机式麦克风」这类是输入设备，不能按字面「耳机」判成喇叭；
    # 只有纯输出名（扬声器/Speakers）被判成 mic 才是分类 bug
    for name, kind in kinds.items():
        if kind != "mic":
            continue
        low = name.lower()
        if any(k in low for k in ("speaker", "扬声器", "耳机（输出", "headphones")):
            fail(f"输出设备被判成麦克风: {name} → {kind}")
    say("分类明细: " + " / ".join(f"{n}→{k}" for n, k in kinds.items()))

    say("--- " + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAIL") + " ---")
    return len(FAILS)


if __name__ == "__main__":
    sys.exit(main())
