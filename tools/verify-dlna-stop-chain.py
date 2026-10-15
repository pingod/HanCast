#!/usr/bin/env python3
"""真机验证 DLNA 停止链路：用 CI 打包进 .app 的 sidecar 把测试视频推给
Windows 上的 HanCast renderer，再从 renderer 自己那边确认 TransportState
真的从 PLAYING 变回 STOPPED（只看本机状态会骗人）。

前置条件：
1. 先整体退出 HanCast（`osascript -e 'tell application "HanCast" to quit'`）。
   sidecar 空载时要占 8080 做媒体/DLNA 服务器，GUI 还开着就是两个实例抢端口。
   验证完记得把应用开回来。
2. 测试媒体默认在 /tmp/hc_media/test.mp4，用本机检测到的那份 ffmpeg 生成（v2.3.11 起不再随包；
   依赖面板下载的那份在 `<配置目录>/bin/ffmpeg`，macOS 即
   `~/Library/Application Support/HanCast/bin/ffmpeg`；没有就用 PATH 上的系统 ffmpeg）：
   ffmpeg -f lavfi -i testsrc=size=640x360:rate=25:duration=30 -c:v libx264 -pix_fmt yuv420p /tmp/hc_media/test.mp4
3. 局域网里要有一台在跑的 Windows renderer（默认认 display_name 含 AMD-YES 或 IP 192.168.1.68）。

判据：start_cast 后 renderer 自报 PLAYING，stop_cast 返回且 control_failed 为空，
停后 renderer 自报 STOPPED。三项齐了才打印 RESULT: ALL PASS（退出码 0）。"""
import json, os, subprocess, sys, threading, time, urllib.request

APP = "/Applications/HanCast.app/Contents/MacOS"
BIN = os.path.join(APP, "hancast-sidecar")
MEDIA = "/tmp/hc_media/test.mp4"

responses = {}
lock = threading.Lock()

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
            responses[d.get("id")] = d

def call(proc, cid, cmd, params=None, timeout=25):
    with lock:
        responses.pop(cid, None)
    proc.stdin.write(json.dumps({"id": cid, "cmd": cmd, "params": params or {}}) + "\n")
    proc.stdin.flush()
    end = time.time() + timeout
    while time.time() < end:
        with lock:
            r = responses.get(cid)
        if r:
            return r
        time.sleep(0.2)
    return {"id": cid, "timeout": True}

def soap_get_state(control_url):
    """直连 renderer 问它自己的 TransportState，--noproxy 等价：显式不走系统代理"""
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body><u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID></u:GetTransportInfo></s:Body></s:Envelope>')
    req = urllib.request.Request(control_url, data=body.encode(), method="POST",
                                 headers={"Content-Type": 'text/xml; charset="utf-8"',
                                          "SOAPAction": '"urn:schemas-upnp-org:service:AVTransport:1#GetTransportInfo"'})
    ph = urllib.request.ProxyHandler({})
    with urllib.request.build_opener(ph).open(req, timeout=8) as r:
        txt = r.read().decode("utf-8", "replace")
    key = "<CurrentTransportState>"
    if key in txt:
        return txt.split(key, 1)[1].split("<", 1)[0]
    return f"HTTP{r.status}:no-state"

def main():
    proc = subprocess.Popen([BIN], cwd=APP, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    threading.Thread(target=reader, args=(proc,), daemon=True).start()
    ok = True
    try:
        r = call(proc, 1, "refresh_devices", timeout=30)
        print("refresh_devices:", json.dumps(r, ensure_ascii=False)[:160])
        time.sleep(8)
        r = call(proc, 2, "get_devices", timeout=30)
        devs = (r.get("data") or {}).get("devices") if isinstance(r.get("data"), dict) else r.get("data")
        if not devs:
            print("FAIL 没有设备:", json.dumps(r, ensure_ascii=False)[:400]); return 1
        print("=== 设备 ===")
        for d in devs:
            print(f"  {d.get('id')} | {d.get('display_name')} | {d.get('ip')} | control={d.get('control_url') or d.get('av_control_url')}")
        target = None
        for d in devs:
            if "AMD-YES" in (d.get("display_name") or "") or str(d.get("ip")) == "192.168.1.68":
                target = d; break
        if not target:
            print("FAIL 未发现 Windows renderer"); return 1
        did = target["id"]
        ctrl = target.get("control_url") or target.get("av_control_url")
        print(f"\n目标: {target.get('display_name')} id={did}")
        if ctrl:
            print("投前 renderer 自报状态:", soap_get_state(ctrl))

        r = call(proc, 3, "start_cast", {"device_id": did, "media_uri": MEDIA,
                                          "mime_type": "video/mp4"}, timeout=40)
        print("start_cast:", json.dumps(r, ensure_ascii=False)[:300])
        if not r.get("success"):
            ok = False
        time.sleep(6)
        if ctrl:
            state = soap_get_state(ctrl)
            print("投后 renderer 自报状态:", state)
            if state != "PLAYING":
                ok = False

        r = call(proc, 4, "stop_cast", {"device_id": did}, timeout=40)
        print("stop_cast:", json.dumps(r, ensure_ascii=False)[:400])
        data = r.get("data") or {}
        if not r.get("success") or data.get("control_failed"):
            print("  -> 本机判定：停止指令未全部送达")
            ok = False
        time.sleep(4)
        if ctrl:
            state = soap_get_state(ctrl)
            print("停后 renderer 自报状态:", state)
            if state != "STOPPED":
                print("  -> 真机上电视/渲染端仍在播，停止链路有问题")
                ok = False
        r = call(proc, 5, "get_cast_state", timeout=20)
        print("get_cast_state:", json.dumps(r.get("data"), ensure_ascii=False)[:200])
    finally:
        call(proc, 99, "exit", timeout=8)
        try:
            proc.wait(timeout=6)
        except Exception:
            proc.kill()
    print("RESULT:", "ALL PASS" if ok else "FAIL")
    return 0 if ok else 1

sys.exit(main())
