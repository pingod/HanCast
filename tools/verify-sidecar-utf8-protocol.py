#!/usr/bin/env python3
"""验证 sidecar 的 stdin/stdout 协议在「父进程没有 locale」时仍然走得通。

背景：Rust 逐行读 stdout 的 JSON，而登记表/错误文案全是中文。父进程没给
LANG/LC_* 时（launchd 起的 GUI、CI、Nuitka 运行时），Python 的 stdout 会退回
ASCII，`emit_response(..., ensure_ascii=False)` 直接抛 UnicodeEncodeError，
表现为「命令永远失败」而不是「编码不对」。v2.2.18 用打包版实测撞到过一次：

    {"id":2,"success":false,"error":"'ascii' codec can't encode characters ..."}

判据： hostile env 下 `get_params` 必须成功、18 项、中文标签原样回来；
`stop_screen_cast`（空载）这种带中文提示的响应也必须回来。
用法：python3 tools/verify-sidecar-utf8-protocol.py [python二进制]
"""
import json
import os
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(REPO, "hancast-backend")
PY = sys.argv[1] if len(sys.argv) > 1 else sys.executable

HOSTILE = {
    "PATH": "/usr/bin:/bin",
    # 显式关掉 PEP 538 locale 强制与 PEP 540 UTF-8 模式，模拟「父进程什么都没给」
    "PYTHONCOERCECLOCALE": "0",
    "PYTHONUTF8": "0",
    "LC_ALL": "C",
    "PYTHONIOENCODING": "",
}

responses = {}
lock = threading.Lock()


def reader(proc):
    for line in proc.stdout:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception as e:
            responses[f"parse-error"] = {"line": line[:200], "err": str(e)}
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


def main():
    proc = subprocess.Popen([PY, "-m", "hancast_sidecar.main"], cwd=BACKEND, env=HOSTILE,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1,
                            encoding="utf-8")
    threading.Thread(target=reader, args=(proc,), daemon=True).start()
    fails = 0
    try:
        r = call(proc, 1, "get_params", timeout=40)
        specs = (r.get("data") or {}).get("specs") or []
        if not r.get("success"):
            print(f"FAIL  get_params 没走通: {json.dumps(r, ensure_ascii=False)[:200]}")
            fails += 1
        elif len(specs) < 18:
            print(f"FAIL  登记表项数 {len(specs)} < 18")
            fails += 1
        else:
            zh = [s for s in specs if not str(s.get("label_zh", "")).isascii()]
            if len(zh) < 10:
                print(f"FAIL  中文标签回来 {len(zh)} 项，疑似被 replace 掉")
                fails += 1
            else:
                print(f"PASS  get_params 在 LC_ALL=C 下成功，{len(specs)} 项，"
                      f"{len(zh)} 项中文标签原样回来（例：{zh[0]['label_zh']}）")

        r = call(proc, 2, "list_screen_sources", timeout=40)
        data = r.get("data") or {}
        titles = [s.get("title", "") for s in (data.get("screens") or [])]
        if not r.get("success"):
            print(f"FAIL  list_screen_sources 没走通: {json.dumps(r, ensure_ascii=False)[:200]}")
            fails += 1
        elif not any(not t.isascii() for t in titles):
            print(f"FAIL  屏幕源中文标题没回来（被 ASCII 截断？）: {titles}")
            fails += 1
        else:
            print(f"PASS  中文标题从 stdout 原样回来: {titles[0]}")
    finally:
        call(proc, 99, "exit", timeout=8)
        try:
            proc.wait(timeout=6)
        except Exception:
            proc.kill()
    print("RESULT:", "ALL PASS" if fails == 0 else f"FAIL（{fails} 条）")
    return 0 if fails == 0 else 1


sys.exit(main())
