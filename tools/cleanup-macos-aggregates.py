"""清掉残留在系统里的 HanCast CoreAudio 聚合设备（默认为只打印计划）

为什么会有残留：AudioHardwareCreateAggregateDevice 建出来的设备是系统级的，
建它的进程退出后设备还留在「音频 MIDI 设置」里，而它引用的环回（process tap）
已经随进程死了——留下一块永远静音的假设备，还会占住 UID 让下一次同名创建报
'nope'。开发探针（tools/probe-*.py）每次跑都可能留下一台，所以需要一个清理入口。

环回对象本身不用管：process tap 的生命周期跟着创建它的进程，进程一退就没了。

用法：
    hancast-backend/.venv/bin/python tools/cleanup-macos-aggregates.py
    ... --apply              真的删除
    ... --apply --product    连产品设备「HanCast 系统声音」一起删（正在跑的
                             sidecar 会拒绝，避免把用户的投屏采集清掉）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "hancast-backend"))

from hancast_sidecar.screencast import macos_tap as T  # noqa: E402

OWN_PREFIX = "com.hancast."


def destroy(dev: int) -> str:
    """删除一台聚合设备，返回 OSStatus 文本。

    不用 macos_tap._destroy_object：它把失败记到 logger.debug，清理工具里
    「没删掉」和「删掉了」看起来一模一样，必须拿到状态码。
    """
    import ctypes
    fn = T._CA.AudioHardwareDestroyAggregateDevice
    fn.restype = ctypes.c_int32
    fn.argtypes = [ctypes.c_uint32]
    return f"{fn(ctypes.c_uint32(dev)):#x}"


def prop_ids(obj: int, fourcc: str) -> list:
    try:
        return T._prop_array(obj, T._fourcc(fourcc))
    except Exception:  # noqa: BLE001 - 设备正在消失
        return []


def describe(dev: int) -> tuple:
    """(UID, 子环回对象, 输入流数) —— 三个都指向同一件事：这台设备还有没有人连着"""
    uid = T._device_uid(dev)
    taps = prop_ids(dev, "atap")
    return uid, taps, T._input_streams(dev)


def wait_until_gone(devs: list, seconds: float = 6.0) -> list:
    """等系统真的把设备摘掉。

    AudioHardwareDestroyAggregateDevice 返回 noErr 只代表请求受理：设备是异步
    从 dev# 列表里消失的，删完立刻读会看到它还在（实测会被误判成「删除失败」）。
    """
    import time
    deadline = time.monotonic() + seconds
    target = set(devs)
    while time.monotonic() < deadline:
        left = [d for d in T._devices() if d in target]
        if not left:
            return []
        time.sleep(0.3)
    return [d for d in T._devices() if d in target]


def sidecar_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", "hancast-sidecar"],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:  # noqa: BLE001 - 探测失败按「在跑」处理，宁可少删
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="真的删除（默认只打印）")
    ap.add_argument("--product", action="store_true",
                    help=f"连产品设备（UID={T.TAP_DEVICE_UID}）一起删")
    args = ap.parse_args()

    if not T._objc() or not T._CA:
        print("CoreAudio 桥起不来，无法枚举")
        return 2

    ours, kept = [], []
    for dev in T._devices():
        uid, taps, streams = describe(dev)
        if not uid.startswith(OWN_PREFIX):
            continue
        reason = ""
        if uid == T.TAP_DEVICE_UID and not args.product:
            reason = f"产品设备，未加 --product"
        elif uid == T.TAP_DEVICE_UID and sidecar_running():
            reason = "sidecar 正在运行，不能清走它在用的采集设备"
        if reason:
            kept.append((dev, uid, taps, streams, reason))
        else:
            ours.append((dev, uid, taps, streams))

    print(f"匹配前缀 {OWN_PREFIX}* 的聚合设备：")
    for dev, uid, taps, streams in ours:
        print(f"  dev={dev} uid={uid} 子环回={taps} 输入流={streams}")
    for dev, uid, taps, streams, reason in kept:
        print(f"  dev={dev} uid={uid} 子环回={taps} 输入流={streams} → 保留：{reason}")

    if not ours:
        print("SUMMARY: 无残留")
        return 0
    if not args.apply:
        print(f"SUMMARY: DRY-RUN（{len(ours)} 台待清，加 --apply 才删）")
        return 0

    for dev, uid, _taps, _streams in ours:
        print(f"  删除 dev={dev} uid={uid} → OSStatus {destroy(dev)}")

    left = wait_until_gone([dev for dev, _u, _t, _s in ours])
    print("剩余:", ", ".join(f"{d}({T._device_uid(d)})" for d in left) or "无")
    print("SUMMARY:", "FAIL: 删除后仍在" if left else "PASS")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())
