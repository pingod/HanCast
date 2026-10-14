#!/usr/bin/env python3
"""把「已安装的规则包」升到官方索引版本——走程序自己的拉取链路

为什么需要它：规则包发布后（`rule-packs/dist/` 出新版本），要确认**打包版**
能真的把新版拉下来并启用。截图上点「检查更新」只能证明这一次，而这条链路
每次发新包都要重验；手改 `share_rules/plugins/` 里的文件则是拿补丁冒充验收，
产物里根本没跑过 `plugin_pull`。

做法：先干净退出 GUI（否则会同时跑两个 sidecar，1900 端口与媒体服务互相干扰，
验收日志会被上一轮的假象污染），再按 stdin/stdout JSON 协议驱动打包版
sidecar 执行 `plugin_check_updates` → `plugin_pull`，最后把 GUI 拉回来。

用法：
    python3 tools/pull-rule-packs-installed.py                 # 升全部有更新的包
    python3 tools/pull-rule-packs-installed.py --only hancast-douyin
    python3 tools/pull-rule-packs-installed.py --no-relaunch   # 只拉不重启 GUI
    # 指向别的 sidecar（例如远程机器上拷下来的产物）：
    python3 tools/pull-rule-packs-installed.py --app /Applications/HanCast.app

需要访问 raw.githubusercontent.com，本机走系统代理时记得带 HTTPS_PROXY。
"""
import argparse
import json
import os
import subprocess
import sys
import time

APP = "/Applications/HanCast.app"
SIDECAR_REL = "Contents/MacOS/hancast-sidecar"
RES_REL = "Contents/Resources"
GUI_NAME = "HanCast"


def sidecar_paths(app: str) -> tuple[str, str]:
    return (os.path.join(app, SIDECAR_REL), os.path.join(app, RES_REL))


def gui_pid() -> int | None:
    out = subprocess.run(["pgrep", "-f", f"{GUI_NAME}.app/Contents/MacOS/{GUI_NAME}"],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def sidecar_pid() -> int | None:
    out = subprocess.run(["pgrep", "-f", "hancast-sidecar"],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def quit_gui(wait_s: float = 25.0) -> bool:
    """osascript 优雅退出，并确认 sidecar 真的走了（它会收到 stdin EOF 后自己收摊）"""
    subprocess.run(["osascript", "-e", f'quit app "{GUI_NAME}"'],
                   capture_output=True, text=True)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if gui_pid() is None and sidecar_pid() is None:
            return True
        time.sleep(0.5)
    return gui_pid() is None and sidecar_pid() is None


def ask(sidecar: str, cwd: str, requests: list[dict], timeout: float) -> list[dict]:
    """把命令逐行喂给 sidecar，按 id 取回响应"""
    payload = "".join(json.dumps(r) + "\n" for r in requests) + json.dumps(
        {"id": "bye", "cmd": "exit", "params": {}}) + "\n"
    proc = subprocess.run([sidecar], input=payload, cwd=cwd,
                          capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    by_id = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in msg:
            by_id[msg["id"]] = msg
    if proc.returncode not in (0, None):
        sys.stderr.write(proc.stderr[-2000:])
    return [by_id.get(r["id"], {"success": False, "error": "无响应"}) for r in requests]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--app", default=APP, help="应用包路径")
    ap.add_argument("--only", action="append", default=[],
                    help="只拉指定包名（可重复）")
    ap.add_argument("--keep-open", action="store_true",
                    help="GUI 本来就没开：不要先退出它（自行确保没有第二个 sidecar 抢端口）")
    ap.add_argument("--no-relaunch", action="store_true", help="拉完不要重新打开 GUI")
    ap.add_argument("--timeout", type=float, default=180, help="sidecar 一轮的上限秒")
    args = ap.parse_args()

    sidecar, cwd = sidecar_paths(args.app)
    if not os.path.isfile(sidecar):
        print(f"[FAIL] 找不到 sidecar：{sidecar}")
        return 1

    if not args.keep_open and (gui_pid() or sidecar_pid()):
        print(f"[1/4] 退出 GUI（gui={gui_pid()} sidecar={sidecar_pid()}）…")
        if not quit_gui():
            print(f"[FAIL] GUI 或 sidecar 还在（gui={gui_pid()} sidecar={sidecar_pid()}）"
                  "——两个实例并跑会让端口与日志互相干扰，先手工退出再跑")
            return 1
    else:
        print("[1/4] GUI 未运行，直接驱动 sidecar")

    print("[2/4] plugin_check_updates（官方索引）…")
    check = ask(sidecar, cwd, [{"id": "chk", "cmd": "plugin_check_updates",
                                "params": {}}], args.timeout)[0]
    if not check.get("success"):
        print(f"[FAIL] 检查更新失败：{check.get('error')}")
        return 1
    data = check.get("data") or {}
    updates = data.get("updates") or []
    available = data.get("available") or []
    print(f"     索引 {data.get('index_url')}")
    print(f"     本机已装 {data.get('installed')} 个；可更新 {len(updates)} 个，"
          f"索引里未安装 {len(available)} 个")
    for u in updates:
        print(f"     - {u.get('name')}: {u.get('current')} → {u.get('version')}")
    for a in available:
        print(f"     · 未安装 {a.get('name')} {a.get('version')}")

    names = args.only or [u.get("name") for u in updates]
    names = [n for n in names if n]
    if not names:
        print("[3/4] 没有要拉的包（已经是索引版本）")
    else:
        # 一次 sidecar 启动里批量拉完：每条命令都新起一个实例的话，
        # 每次都要重跑 SSDP/媒体服务初始化，慢且容易撞上一轮还没释放的端口
        print(f"[3/4] plugin_pull: {', '.join(names)}")
        reqs = [{"id": f"pull:{n}", "cmd": "plugin_pull", "params": {"name": n}}
                for n in names]
        results = ask(sidecar, cwd, reqs, args.timeout + 60 * len(names))
        bad = 0
        for name, res in zip(names, results):
            if not res.get("success"):
                print(f"     [FAIL] {name}: {res.get('error')}")
                bad += 1
                continue
            plugins = (res.get("data") or {}).get("plugins") or []
            row = next((p for p in plugins if p.get("name") == name), {})
            rules = row.get("rules") or []
            on = sum(1 for r in rules if r.get("enabled"))
            err = f" 错误：{row.get('error')}" if row.get("error") else ""
            print(f"     {name} → v{row.get('version', '?')}，规则 {on}/{len(rules)} 启用{err}")
        if bad:
            return 1

    if not args.no_relaunch:
        print("[4/4] 重新打开 GUI…")
        subprocess.run(["open", "-a", GUI_NAME], check=False)
        time.sleep(6)
        print(f"     gui={gui_pid()} sidecar={sidecar_pid()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
