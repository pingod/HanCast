#!/usr/bin/env python3
"""真机验证「浏览器通道分片时长可配置且不必重编译」。

用 CI 打进 .app 的 sidecar 起真实的屏幕投屏（target=browser），直接读它产出的
HLS 播放列表做判据，分两段：
1. 默认参数 → 播放列表必须是 1 秒分片（EXT-X-TARGETDURATION:1，实测 EXTINF ≈1s）
2. save_params 把 screencast.browser_segment_seconds 改成 4 → 同一份二进制、
   不重新编译，停一次再开 → 播放列表必须变成 4 秒分片
最后把参数改回默认（覆盖项应当被清掉，不是留一个 ==默认值 的脏键）。

前置条件：先整体退出 HanCast（GUI 与 sidecar 抢 8080），脚本结束会把应用开回来。
屏幕捕获依赖父进程的「录屏」权限，被拒时 start 会直接报错，判据会如实 FAIL。
用法：python3 tools/verify-browser-segment-live.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

APP = "/Applications/HanCast.app/Contents/MacOS"
BIN = os.path.join(APP, "hancast-sidecar")
SETTING = os.path.expanduser("~/Library/Application Support/HanCast/hancast_setting.json")
KEY = "screencast.browser_segment_seconds"

results = []
results_dict = {}
lock = threading.Lock()


def ok(cond, label, extra=""):
    results.append(bool(cond))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{extra}]" if extra else ""))
    return bool(cond)


def reader(proc):
    for line in proc.stdout:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        with lock:
            results_dict[d.get("id")] = d


results_dict = {}


def call(proc, cid, cmd, params=None, timeout=40):
    with lock:
        results_dict.pop(cid, None)
    proc.stdin.write(json.dumps({"id": cid, "cmd": cmd, "params": params or {}}) + "\n")
    proc.stdin.flush()
    end = time.time() + timeout
    while time.time() < end:
        with lock:
            r = results_dict.get(cid)
        if r:
            return r
        time.sleep(0.2)
    return {"id": cid, "timeout": True}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "HanCastProbe/1.0"})
    ph = urllib.request.ProxyHandler({})
    with urllib.request.build_opener(ph).open(req, timeout=8) as r:
        return r.read().decode("utf-8", "replace")


def wait_playlist(m3u8_url, min_segments=3, timeout=45):
    """等到播放列表里真的攒出分片，返回最后一次响应体"""
    end = time.time() + timeout
    last = ""
    while time.time() < end:
        try:
            last = fetch(m3u8_url)
        except Exception:
            time.sleep(1)
            continue
        extinf = re.findall(r"#EXTINF:([\d.]+)", last)
        if len(extinf) >= min_segments:
            return last, extinf
        time.sleep(1)
    return last, re.findall(r"#EXTINF:([\d.]+)", last)


def judge_segment(label, body, extinf, expect):
    td = re.search(r"#EXT-X-TARGETDURATION:(\d+)", body)
    ok(td is not None, f"{label}: 播放列表有 TARGETDURATION",
       td.group(1) if td else "缺失")
    if td:
        ok(int(td.group(1)) == expect, f"{label}: TARGETDURATION == {expect}",
           f"实际 {td.group(1)}")
    if extinf:
        vals = [float(x) for x in extinf]
        mean = sum(vals) / len(vals)
        ok(abs(mean - expect) <= max(0.6, expect * 0.35),
           f"{label}: 分片实测均值 ≈{expect}s",
           f"avg={mean:.2f} n={len(vals)} max={max(vals):.2f}")
    else:
        ok(False, f"{label}: 攒出分片", "超时无 EXTINF")


def app_running():
    try:
        out = subprocess.run(["pgrep", "-x", "HanCast"], capture_output=True, text=True)
        return out.returncode == 0
    except Exception:
        return False


def main():
    if not os.path.exists(BIN):
        print(f"FAIL 找不到打包 sidecar: {BIN}\n（先装 HanCast.app）")
        return 1

    backup = None
    if os.path.exists(SETTING):
        backup = "/tmp/hancast_setting.backup.json"
        shutil.copy2(SETTING, backup)
    was_running = app_running()
    if was_running:
        print("退出 GUI 实例（避免与探针 sidecar 抢端口）…")
        subprocess.run(["osascript", "-e", 'tell application "HanCast" to quit'],
                       capture_output=True)
        time.sleep(4)

    proc = None
    try:
        proc = subprocess.Popen([BIN], cwd=APP, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1)
        threading.Thread(target=reader, args=(proc,), daemon=True).start()

        print("\n=== 登记表 ===")
        r = call(proc, 1, "get_params")
        specs = ((r.get("data") or {}).get("specs")) or []
        ok(len(specs) >= 18, "get_params 返回 ≥18 项", f"{len(specs)} 项")
        by_key = {s["key"]: s for s in specs}
        base = by_key.get(KEY)
        ok(base is not None, f"登记表含 {KEY}")
        if base:
            ok(base["default"] == 1 and base["value"] == 1 and not base["overridden"],
               "默认与当前值都是 1 秒（与 v2.2.17 契约一致）",
               f"default={base['default']} value={base['value']}")

        print("\n=== 第一轮：默认 1 秒分片 ===")
        start_params = {"target": "browser", "source": {"type": "screen", "id": "desktop"},
                        "audio": {"mic": False, "system": False},
                        "video": {"width": 1280, "height": 720, "fps": 25,
                                  "bitrate": "2M"}}
        r = call(proc, 2, "start_screen_cast", start_params, timeout=60)
        data = r.get("data") or {}
        if not ok(data.get("ok") or r.get("success"), "浏览器通道投屏已启动",
                  json.dumps(data, ensure_ascii=False)[:200]):
            return 1
        st = (call(proc, 3, "screen_cast_status").get("data") or {})
        m3u8 = st.get("url")
        ok(bool(m3u8), "状态里拿到 m3u8 地址", str(m3u8))
        body, extinf = wait_playlist(m3u8)
        judge_segment("默认", body, extinf, 1)

        print("\n=== 改参数（不重新编译、不换二进制）===")
        r = call(proc, 4, "save_params", {"values": {KEY: 4}})
        d = r.get("data") or {}
        ok(d.get("ok") is True, "save_params 写 4 秒", json.dumps(d, ensure_ascii=False)[:200])
        r = call(proc, 5, "get_params")
        s2 = {s["key"]: s for s in ((r.get("data") or {}).get("specs")) or []}
        ok(s2.get(KEY, {}).get("value") == 4 and s2[KEY]["overridden"] is True,
           "读回来 value=4 且标记为已覆盖",
           f"value={s2.get(KEY, {}).get('value')} overridden={s2.get(KEY, {}).get('overridden')}")

        call(proc, 6, "stop_screen_cast", timeout=40)
        time.sleep(2)
        r = call(proc, 7, "start_screen_cast", start_params, timeout=60)
        if not ok((r.get("data") or {}).get("ok"), "第二轮投屏已启动",
                  json.dumps(r.get("data") or {}, ensure_ascii=False)[:200]):
            return 1
        st = (call(proc, 8, "screen_cast_status").get("data") or {})
        m3u8 = st.get("url")
        body, extinf = wait_playlist(m3u8)
        judge_segment("覆盖为 4 秒", body, extinf, 4)

        print("\n=== 恢复默认 ===")
        r = call(proc, 9, "save_params", {"values": {KEY: 1}})
        ok((r.get("data") or {}).get("ok") is True, "把参数写回 1")
        r = call(proc, 10, "get_params")
        s3 = {s["key"]: s for s in ((r.get("data") or {}).get("specs")) or []}
        ok(s3.get(KEY, {}).get("overridden") is False,
           "覆盖项被清掉（不留 ==默认值 的脏键）",
           f"overridden={s3.get(KEY, {}).get('overridden')}")
        call(proc, 11, "stop_screen_cast", timeout=40)
    finally:
        if proc:
            call(proc, 99, "exit", timeout=8)
            try:
                proc.wait(timeout=6)
            except Exception:
                proc.kill()
        if backup:
            shutil.copy2(backup, SETTING)
            print(f"\n已还原 {SETTING}")
        if was_running:
            subprocess.run(["open", "/Applications/HanCast.app"], capture_output=True)
            print("已把 HanCast.app 重新打开")

    print("\nRESULT:", "ALL PASS" if all(results) else f"FAIL（{results.count(False)}/{len(results)}）")
    return 0 if all(results) else 1


sys.exit(main())
