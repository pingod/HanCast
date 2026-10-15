#!/usr/bin/env python3
"""生成 .68 屏幕投屏「产品链路」验收的请求行（配合 tools/verify-win-screencast-audio-live.cmd）。

子命令：
  phase1 -o req.txt              只枚举设备（第一轮跑这个，拿设备名）
  devices <out.txt>              列出 phase-1（只跑 list_screen_sources）响应里的音频输入设备
  req <out.txt> --mic I [--sys J] [--second mic|system]... [-o req.txt]
                                 按序号取设备名，拼出 ASCII 请求文件
                                 --second 可重复，用来把三路组合排在一次运行里

为什么全是 ASCII：这台机器的设备名是中文，而 cmd 读带 UTF-8 中文的批处理会按字节偏移
错位解析（2026-10-14 实测）。json.dumps(ensure_ascii) 把中文变成 \\uXXXX，这里再把
( ) & < > | % 也转成 \\u 转义——sidecar 的 json.loads 照样还原，批处理里则一个特殊字符都不剩。
"""
from __future__ import annotations

import argparse
import json
import re
import sys

# v2.3.11 起产物不再自带 ffmpeg，请求里也不再硬写随包路径（旧值
# C:\Users\Administrator\AppData\Local\HanCast\ffmpeg\ffmpeg.exe 已不存在）。
# 默认不发 set_ffmpeg_path，让 .68 上的产品按「%APPDATA%\HanCast\bin → PATH」自己检测；
# 要强行指定某一份时才用 --ffmpeg。

# echo 会把这些字符当批处理语法；它们在 JSON 里只出现在字符串内部，转义后仍是合法 JSON
_BATCH_UNSAFE = {"(": "\\u0028", ")": "\\u0029", "&": "\\u0026",
                 "<": "\\u003c", ">": "\\u003e", "|": "\\u007c",
                 "%": "\\u0025"}


def line(payload: dict) -> str:
    s = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    for ch, esc in _BATCH_UNSAFE.items():
        s = s.replace(ch, esc)
    return s


def prelude(ffmpeg: str) -> list:
    """请求序列的头两条：可选的 set_ffmpeg_path + 设备枚举。
    id 2 留给 list_screen_sources，后续从 3 开始编号（批处理按行序读，id 只需唯一）"""
    req = []
    if ffmpeg:
        req.append({"id": 1, "cmd": "set_ffmpeg_path", "params": {"path": ffmpeg}})
    req.append({"id": 2, "cmd": "list_screen_sources", "params": {}})
    return req


def audio_inputs(out_path: str):
    """out.txt 混着 ready/事件行，只挑带 audio_inputs 的那条响应"""
    found = []
    with open(out_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw.startswith("{") or "audio_inputs" not in raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            data = obj.get("data") or {}
            if isinstance(data.get("audio_inputs"), list):
                found = data["audio_inputs"]
    return found


def pick(devices, idx: int, what: str) -> str:
    if idx < 0 or idx >= len(devices):
        raise SystemExit(f"{what} 序号 {idx} 越界（共 {len(devices)} 个设备，先跑 devices 子命令）")
    return devices[idx]["name"]


def pick_matching(devices, pattern: str, what: str,
                  only_loopback: bool = False) -> str:
    """按名字正则选设备：换一台机器/换一副声卡序号就变了，验收脚本不该硬背数字"""
    if pattern == "default" and only_loopback:
        # 「当前出声」那块：只有默认渲染端点的环回真的带着声音，
        # 按名字硬挑（如 Realtek）在装了虚拟音效设备的机器上会挑中静音镜像，
        # 测出「投屏没声音」的假信号
        for d in devices:
            if d.get("loopback") and d.get("default"):
                print(f"    {what} ← {d['name']}（默认输出端点）")
                return d["name"]
        raise SystemExit(f"{what}=default 但枚举里没有标 default 的环回"
                         f"（这版产品还没带「当前出声」标记？）")
    rx = re.compile(pattern)
    for d in devices:
        if only_loopback and not d.get("loopback"):
            continue
        if rx.search(d["name"]):
            print(f"    {what} ← {d['name']}")
            return d["name"]
    raise SystemExit(f"{what} 没有名字匹配 {pattern!r} 的设备（先跑 devices 子命令看清单）")


def cmd_devices(out_path: str) -> int:
    devices = audio_inputs(out_path)
    if not devices:
        print(f"{out_path} 里没有 audio_inputs："
              f"先用只含 list_screen_sources 的 req 跑一轮 phase-1")
        return 1
    for i, d in enumerate(devices):
        tag = "loopback" if d.get("loopback") else d.get("kind", "?")
        extra = ""
        if d.get("loopback"):
            extra = f" rate={d.get('rate')} ch={d.get('channels')}"
        print(f"[{i}] {d['name']}  ({tag}{extra})")
    return 0


def cmd_req(args) -> int:
    devices = audio_inputs(args.from_out)
    if not devices:
        raise SystemExit(f"{args.from_out} 里没有 audio_inputs")

    mic = (pick_matching(devices, args.mic_match, "--mic-match")
           if args.mic_match else
           (pick(devices, args.mic, "--mic") if args.mic >= 0 else None))
    sysdev = (pick_matching(devices, args.sys_match, "--sys-match", only_loopback=True)
              if args.sys_match else
              (pick(devices, args.sys, "--sys") if args.sys >= 0 else None))
    if not mic and not sysdev:
        raise SystemExit("至少要给 --mic 或 --sys")

    def audio(mic_name, sys_name):
        out = {"mic": bool(mic_name), "system": bool(sys_name)}
        if mic_name:
            out["mic_device"] = mic_name
        if sys_name:
            out["system_device"] = sys_name
        return out

    source = {"type": "screen", "id": "desktop"}
    video = {"width": 1280, "height": 720, "fps": 15, "bitrate": "2M"}
    rounds = [(mic, sysdev)]
    # --second 可以给两次：三路组合（both / 单独 mic / 单独 system）要在一次
    # sidecar 生命周期里全跑完，否则「哪一路把管线饿死」就分不清是组合还是单路
    for which in (args.second or []):
        if which == "system" and sysdev:
            rounds.append((None, sysdev))
        if which == "mic" and mic:
            rounds.append((mic, None))

    req = prelude(args.ffmpeg)
    rid = 3
    # 一次 start 只测一路组合：正在投屏时 manager.start 直接拒绝，必须 stop 再开
    for mic_name, sys_name in rounds:
        req += [
            {"id": rid, "cmd": "start_screen_cast", "params": {
                "target": "browser", "source": source,
                "audio": audio(mic_name, sys_name), "video": video}},
        ]
        rid += 1
        # 每两条请求之间批处理等 3s，所以 --dwell N 次状态 ≈ 3N 秒驻留：
        # 「进程活着」和「分片还在往前走」是两件事，会话拉长才分得开
        for _ in range(max(1, args.dwell)):
            req.append({"id": rid, "cmd": "screen_cast_status", "params": {}})
            rid += 1
        req.append({"id": rid, "cmd": "stop_screen_cast", "params": {}})
        rid += 1
    req += [{"id": rid, "cmd": "get_logs", "params": {"limit": 400}},
            {"id": rid + 1, "cmd": "exit", "params": {}}]

    text = "\n".join(line(p) for p in req) + "\n"
    write_req(text, args.output)
    print(f"    轮次 {[(m, s) for m, s in rounds]}，请求 {len(req)} 条")
    return 0


def write_req(text: str, output) -> None:
    """CRLF：这个文件只喂给 cmd 的 for /f，LF-only 行尾会让它多吐空行
    （sidecar 回 Invalid JSON，无害但干扰判读）"""
    if not output:
        sys.stdout.write(text)
        return
    with open(output, "w", encoding="ascii", newline="\r\n") as f:
        f.write(text)
    print(f"wrote {output}")


def cmd_phase1(output, ffmpeg: str = "") -> int:
    """只要设备名：先把这三轮跑出来的 out.txt 交给 devices / req"""
    req = prelude(ffmpeg) + [{"id": 3, "cmd": "exit", "params": {}}]
    text = "\n".join(line(p) for p in req) + "\n"
    write_req(text, output)
    return 0


def main(argv) -> int:
    sub = argv[1] if len(argv) > 1 else ""
    if sub == "devices":
        rest = argv[2:]
        ap = argparse.ArgumentParser()
        ap.add_argument("out")
        return cmd_devices(ap.parse_args(rest).out)
    if sub == "phase1":
        ap = argparse.ArgumentParser()
        ap.add_argument("-o", "--output")
        ap.add_argument("--ffmpeg", default="", help="强行指定 .68 上的 ffmpeg 路径；不给就用产品检测的那份")
        a = ap.parse_args(argv[2:])
        return cmd_phase1(a.output, a.ffmpeg)
    if sub == "req":
        ap = argparse.ArgumentParser()
        ap.add_argument("from_out", help="phase-1 的 out.txt")
        ap.add_argument("--mic", type=int, default=-1, help="麦克风序号")
        ap.add_argument("--sys", type=int, default=-1, help="系统声音（loopback）序号")
        ap.add_argument("--mic-match", help="按名字正则选麦克风，免得硬背序号")
        ap.add_argument("--sys-match",
                        help="按名字正则选 WASAPI 环回；写 default 则自动取「当前出声」那块")
        ap.add_argument("--second", choices=["mic", "system"], action="append",
                        help="第一轮（两路组合）之后再单独跑这一路，可重复给")
        ap.add_argument("--dwell", type=int, default=1,
                        help="每轮问几次状态（≈3 秒/次），用来把会话拉长到能观察分片增长")
        ap.add_argument("--ffmpeg", default="", help="强行指定 .68 上的 ffmpeg 路径；不给就用产品检测的那份")
        ap.add_argument("-o", "--output")
        return cmd_req(ap.parse_args(argv[2:]))
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
