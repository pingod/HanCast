#!/usr/bin/env python3
"""用源码 sidecar 的真实解析管线跑规则包，打印每条链接是哪条规则命中的。

不点 GUI、不起整个应用：只把 ShareResolver 拎出来，配一个假的媒体服务器
（注册代理只记地址、不落流），因此这里证明的是「规则能不能取出可播地址」，
不含电视端真实播放。

用法:
  hancast-backend/.venv/bin/python tools/verify-rule-packs-live.py <链接或口令> …
  … --share-rules <dir>     用指定规则目录（默认临时目录 + 随包内置规则包）
  … --keep                  保留临时目录，方便看插件缓存/落盘文件

判据看输出里的 via：`<包名>/<规则 id>` = 走了 JS 规则包；
`bilibili`、`douyin` 这类不带斜杠的 = 回退到了内置 Python 规则（说明包没生效）。
"""

import argparse
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hancast-backend"))

from hancast_sidecar.media.rules import jsplugins              # noqa: E402
from hancast_sidecar.media.share_resolver import ShareResolver  # noqa: E402


class StubMediaServer:
    """register_proxy 只发一个可辨认的假地址，解析链路其余部分照原样跑"""

    def __init__(self):
        self.proxies = []

    def register_proxy(self, remote_url, headers=None, suffix=""):
        self.proxies.append((remote_url, headers or {}))
        return f"http://127.0.0.1:0/proxy/{len(self.proxies)}{suffix}"


def pull_check(remote: str, headers: dict):
    """按解析给出的头真拉一段字节：证明这条规则交出去的是「能播的地址」"""
    import requests
    hdrs = dict(headers or {})
    hdrs["Range"] = "bytes=0-262143"
    try:
        with requests.get(remote, headers=hdrs, timeout=20, stream=True) as r:
            got = 0
            for chunk in r.iter_content(64 * 1024):
                got += len(chunk)
                if got > 400 * 1024:
                    break
            return r.status_code, r.headers.get("Content-Type", ""), got
    except Exception as e:
        return "ERR", f"{type(e).__name__}: {e}"[:80], 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="+", help="链接或分享口令文本")
    ap.add_argument("--share-rules", default="",
                    help="规则目录（含 plugins/）；默认临时目录并播种随包内置包")
    ap.add_argument("--pull", action="store_true",
                    help="对回源地址真拉 256KB，验证地址可用")
    ap.add_argument("--keep", action="store_true", help="保留临时规则目录")
    ap.add_argument("--verbose", action="store_true",
                    help="打开 sidecar 日志，看规则让路/失败的具体原因")
    args = ap.parse_args()

    if args.verbose:
        import logging
        logging.basicConfig(level=logging.INFO,
                            format="%(levelname)s %(name)s: %(message)s")

    temp_dir = ""
    if args.share_rules:
        rules_dir = os.path.abspath(args.share_rules)
    else:
        temp_dir = tempfile.mkdtemp(prefix="hancast-rulepack-")
        rules_dir = temp_dir

    try:
        resolver = ShareResolver(StubMediaServer(), rules_dir=rules_dir)
        mgr = resolver.plugin_mgr
        mgr.refresh()
        packs = mgr.list_info()
        print(f"规则目录: {rules_dir}")
        print(f"随包内置目录: {mgr.bundled_dir}")
        print("已装规则包:")
        for info in packs:
            state = "内置" if info.get("builtin") else "导入"
            rules = "、".join(
                f"{r['name']}[{'开' if r['enabled'] else '关'}]" for r in info["rules"])
            err = f" 加载失败: {info['error']}" if info.get("error") else ""
            print(f"  - {info['name']} {info['version']}（{state}）{rules}{err}")

        failures = 0
        for target in args.targets:
            print(f"\n=== {target}")
            proxy_count = len(resolver.media_server.proxies)
            started = time.monotonic()
            try:
                info = resolver.resolve(target)
            except Exception as e:
                failures += 1
                print(f"  FAIL {type(e).__name__}: {e}")
                continue
            print(f"  via    : {info.get('via')}")
            print(f"  标题   : {(info.get('title') or '')[:60]}")
            print(f"  播放   : {(info.get('uri') or '')[:110]}")
            print(f"  mime   : {info.get('mime_type')}  "
                  f"耗时 {time.monotonic() - started:.1f}s")
            if info.get("thumbnail"):
                print(f"  封面   : {str(info['thumbnail'])[:100]}")
            for remote, headers in resolver.media_server.proxies[proxy_count:]:
                # 代理背后那条真地址：拿它单独验通不通，才算证到「能播」
                print(f"  回源   : {remote[:160]}")
                print(f"  回源头 : {sorted(headers) or '（无）'}")
                if args.pull:
                    code, ctype, got = pull_check(remote, headers)
                    verdict = "OK" if isinstance(code, int) and code in (200, 206) \
                        and got > 0 else "FAIL"
                    print(f"  实拉   : {verdict} HTTP {code} "
                          f"{ctype[:30]} {got // 1024}KB")
                    if verdict == "FAIL":
                        failures += 1
        return 1 if failures else 0
    finally:
        if temp_dir and not args.keep:
            shutil.rmtree(temp_dir, ignore_errors=True)
        elif temp_dir:
            print(f"\n临时规则目录保留: {temp_dir}")


if __name__ == "__main__":
    sys.exit(main())
