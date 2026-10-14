#!/usr/bin/env python3
"""量「装进这台机器的那个产物」对外握手长什么样，而不是源码 venv 的。

为什么还要再写一个：tools/probe-tls-ja3.py 的 --client hancast 走的是
sys.path 里的 hancast_sidecar 源码，它只能证明接线对；v2.3.7 那次翻车恰恰是
接线对、`bundle.resources` 白名单漏了 `curl_cffi/`，产物里 import 失败后按设计
静默降级回 requests——**只有装好的包才暴露得出来**。所以这里起的是
.app 里那个冻结二进制，喂它一条真实命令，看它自己发出去的 ClientHello。

用法：
    python3 tools/probe-installed-ja3.py                       # 默认 /Applications/HanCast.app
    python3 tools/probe-installed-ja3.py --binary <sidecar 路径>
    python3 tools/probe-installed-ja3.py --timeout 60

判据（同一次运行内自对照，不依赖任何第三方指纹站）：
  - 产物握手的 ClientHello 里出现 GREASE（0x?a?a）→ curl_cffi 伪装生效。
    OpenSSL/SecureTransport 发不出 GREASE，Chrome 系伪装必发，所以这一项单独就够判。
  - 零 GREASE → 产物没带 curl_cffi，静默降级回了 requests（v2.3.7 就是这个形状）。
    注意别拿「JA3 与基线不同」当生效证据：urllib3 v2 会自己加 ALPN http/1.1，
    裸 ssl 基线与 requests 的 JA3 本来就不逐字相同。

副作用：解析失败会进「解析失败台账」（落在用户的 hancast 配置目录）。地址带
一次性标记，收尾用 failure_delete{url} 只删自己那一条——绝不调 failure_clear，
那是用户的数据。
"""

import argparse
import hashlib
import importlib.util
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from itertools import count

HERE = os.path.dirname(os.path.abspath(__file__))

_spec = importlib.util.spec_from_file_location(
    "ja3probe", os.path.join(HERE, "probe-tls-ja3.py"))
jp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jp)

DEFAULT_BIN = "/Applications/HanCast.app/Contents/MacOS/hancast-sidecar"
MARK = "hancast-ja3-probe"


class Sidecar:
    """自己起的 sidecar：stdio JSON 协议，只跟这一个子进程说话"""

    def __init__(self, binary: str):
        self.p = subprocess.Popen(
            [binary],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.responses: dict = {}
        self.events: list = []
        self._ids = count(1)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "event" in d:
                self.events.append(d["event"])
            elif "id" in d:
                self.responses[d["id"]] = d

    def wait_event(self, name: str, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if name in self.events:
                return True
            if self.p.poll() is not None:
                return False
            time.sleep(0.1)
        return False

    def call(self, cmd: str, params: dict = None, timeout: float = 10) -> dict:
        rid = next(self._ids)
        try:
            self.p.stdin.write(
                json.dumps({"id": rid, "cmd": cmd, "params": params or {}},
                           ensure_ascii=False) + "\n")
            self.p.stdin.flush()
        except (OSError, ValueError):
            return {}
        end = time.time() + timeout
        while time.time() < end:
            r = self.responses.get(rid)
            if r is not None:
                return r
            time.sleep(0.05)
        return {}

    def close(self):
        """只收尾自己起的这个 pid——绝不 pkill sidecar（会杀掉正在投屏的实例）"""
        try:
            self.call("exit", timeout=3)
        except Exception:
            pass
        try:
            self.p.wait(timeout=5)
        except Exception:
            self.p.terminate()


def baseline_handshake(port: int):
    """stdlib ssl 裸握手 = Python 原生 OpenSSL 指纹，当同机同网口的对照"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        raw = socket.create_connection(("127.0.0.1", port), timeout=8)
        s = ctx.wrap_socket(raw, server_hostname="localhost")
        s.do_handshake()
        s.close()
    except Exception:
        pass  # 服务端读完 ClientHello 就断，握手必然失败，要的是 hello 本身


def grease_of(hello: dict) -> int:
    return (hello["grease_ciphers"] + hello["grease_exts"]
            + hello["grease_curves"])


def describe(name: str, hello, base_str: str = None) -> str:
    if not isinstance(hello, dict):
        print(f"  {name:24} 没抓到 ClientHello（{hello}）")
        return None
    s = jp.ja3(hello)
    print(f"  {name:24} JA3={hashlib.md5(s.encode()).hexdigest()}")
    print(f"  {'':24}   套件 {len(hello['cipher_suites'])}"
          f" / 扩展 {len(hello['extensions'])} / ALPN {hello['alpn']}"
          f" / GREASE {grease_of(hello)} 项")
    if base_str is not None:
        print(f"  {'':24}   与 stdlib ssl 基线"
              f"{'逐字相同（= 没伪装）' if s == base_str else '不同'}")
    return s


def wait_task(sc: Sidecar, task_id: str, timeout: float) -> str:
    """等解析任务落地：台账记录是在 resolve() 收尾时才写的，不等它就删不掉"""
    end = time.time() + timeout
    while time.time() < end:
        d = sc.call("get_resolve_result", {"task_id": task_id}, timeout=5).get("data") or {}
        status = d.get("status")
        if status and status != "pending":
            return status
        time.sleep(0.5)
    return "pending(超时)"


def clean_ledger(sc: Sidecar) -> int:
    snap = sc.call("failure_list", timeout=10).get("data") or {}
    removed = 0
    for bucket in snap.get("hosts", []):
        for entry in bucket.get("entries", []):
            u = entry.get("url") or ""
            if MARK in u:
                d = sc.call("failure_delete", {"url": u}, timeout=10).get("data") or {}
                removed += int(d.get("removed") or 0)
    return removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", default=DEFAULT_BIN)
    ap.add_argument("--timeout", type=float, default=45,
                    help="等产物发出 ClientHello 的上限秒数")
    args = ap.parse_args()

    if not os.path.exists(args.binary):
        print(f"找不到 sidecar: {args.binary}", file=sys.stderr)
        return 2

    catcher = jp.HelloCatcher()
    catcher.start(2)
    url = f"https://127.0.0.1:{catcher.port}/{MARK}"

    sc = Sidecar(args.binary)
    print(f"产物: {args.binary}")
    if not sc.wait_event("ready", 20):
        print("FAIL：子进程没发 ready 事件（二进制起不来？检查 --binary）", file=sys.stderr)
        sc.close()
        catcher.stop()
        return 1

    settings = sc.call("get_settings", timeout=10)
    ver = (settings.get("data") or {}).get("version") or "未知"
    print(f"自报版本: {ver}")

    # 连接顺序即结果顺序：先基线，再产物
    baseline_handshake(catcher.port)
    resp = sc.call("resolve_share", {"text": url}, timeout=10)
    task_id = (resp.get("data") or {}).get("task_id") or ""
    print(f"已让产物解析一次性地址（task {task_id or '未拿到'}），等它的 TLS 握手…")

    results = catcher.wait(timeout=args.timeout)
    if len(results) < 2:
        print(f"FAIL：只抓到 {len(results)}/2 份 ClientHello", file=sys.stderr)
        sc.close()
        return 1

    status = wait_task(sc, task_id, args.timeout) if task_id else "无 task"
    removed = clean_ledger(sc)
    sc.close()

    base_str = describe("基线 stdlib ssl", results[0])
    mine = describe(f"产物 {ver}", results[1], base_str)
    print(f"\n解析任务收尾: {status}；台账里自己那一条已删 {removed} 项")

    if mine is None or base_str is None:
        print("结论：数据不足，判不了")
        return 1
    hello = results[1]
    if grease_of(hello) > 0:
        print(f"PASS：产物对外带 GREASE（{grease_of(hello)} 项）且 JA3 与 OpenSSL 基线不同"
              " → curl_cffi 指纹伪造在装好的包里生效")
        return 0
    # 别被「与基线不同」骗了：urllib3 v2 会给裸 ssl 加不上 ALPN http/1.1，
    # 于是 requests 的 JA3 也和自己不一样。判「有没有伪装」只看 GREASE——
    # OpenSSL/SecureTransport 发不出 GREASE，Chrome 系伪装必发（curl_cffi 亦然）。
    print("FAIL：产物 ClientHello 零 GREASE"
          " → 这一版产物没走 curl_cffi，对外仍是 Python TLS 的形状（v2.3.7 就是这样）。"
          "与基线的差异只是 urllib3 的 ALPN，不是伪装。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
