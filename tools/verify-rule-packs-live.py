#!/usr/bin/env python3
"""用源码 sidecar 的真实解析管线跑规则包，打印每条链接是哪条规则命中的。

不点 GUI、不起整个应用：只把 ShareResolver 拎出来，配一个假的媒体服务器
（注册代理只记地址、不落流），因此这里证明的是「规则能不能取出可播地址」，
不含电视端真实播放。

用法:
  hancast-backend/.venv/bin/python tools/verify-rule-packs-live.py <链接或口令> …
  … --rules-dir <dir>       用指定规则目录（内含 plugins/ 与 *.json）
  … --no-packs              不装官方包，只跑 <dir> 里已有的规则
  … --keep                  保留临时目录，方便看插件缓存/落盘文件

判据看输出里的 via：`<包名>/<规则 id>` = 走了 JS 规则包，`<规则名>` 不带斜杠 =
走的声明式 JSON 规则。程序里没有编译进去的站点规则，所以「没装包 ⇒ 什么站点都
解析不了」是预期行为，不是 bug——默认会把 repo 根 ``rule-packs/dist/`` 的包当
用户导入的 zip 装进临时目录，装之前先比 index.json 里的 sha256。
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hancast-backend"))

PACKS_DIR = os.path.join(ROOT, "rule-packs")
DIST_DIR = os.path.join(PACKS_DIR, "dist")
INDEX_PATH = os.path.join(PACKS_DIR, "index.json")

from hancast_sidecar import params                            # noqa: E402
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


def install_official_packs(mgr) -> None:
    """把 repo 根的官方包当用户导入的 zip 装进去——走的就是产品里那条路

    dist/ 是 ``tools/build-rule-packs.py`` 的产物；这里先比 index.json 的 sha256，
    摘要不符就当场失败，免得拿一份过期产物来验「包能用」。
    """
    if not os.path.isfile(INDEX_PATH):
        print(f"缺少索引: {INDEX_PATH} → 先跑 python3 tools/build-rule-packs.py",
              file=sys.stderr)
        raise SystemExit(2)
    index = json.load(open(INDEX_PATH))
    for entry in index["packs"]:
        path = os.path.join(DIST_DIR, os.path.basename(entry["url"]))
        if not os.path.isfile(path):
            print(f"缺少产物: {path} → 先跑 python3 tools/build-rule-packs.py",
                  file=sys.stderr)
            raise SystemExit(2)
        with open(path, "rb") as f:
            blob = f.read()
        got = hashlib.sha256(blob).hexdigest()
        if got != entry["sha256"]:
            print(f"{entry['name']}: 产物摘要 {got[:12]}… ≠ 索引 {entry['sha256'][:12]}…",
                  file=sys.stderr)
            raise SystemExit(2)
        mgr.import_zip_bytes(blob)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="+", help="链接或分享口令文本")
    ap.add_argument("--rules-dir", default="",
                    help="规则目录（含 plugins/ 与 *.json）；默认临时目录")
    ap.add_argument("--no-packs", action="store_true",
                    help="不装 repo 根的官方包，只用目录里已有的规则")
    ap.add_argument("--pull", action="store_true",
                    help="对回源地址真拉 256KB，验证地址可用")
    ap.add_argument("--quality", default="",
                    help="点名画质档位 id（B 站是 qn：16/32/64/80…），空＝包自己定（通常最高）")
    ap.add_argument("--keep", action="store_true", help="保留临时规则目录")
    ap.add_argument("--verbose", action="store_true",
                    help="打开 sidecar 日志，看规则让路/失败的具体原因")
    args = ap.parse_args()

    if args.verbose:
        import logging
        logging.basicConfig(level=logging.INFO,
                            format="%(levelname)s %(name)s: %(message)s")

    temp_dir = ""
    if args.rules_dir:
        rules_dir = os.path.abspath(args.rules_dir)
    else:
        temp_dir = tempfile.mkdtemp(prefix="hancast-rulepack-", dir="/tmp")
        rules_dir = temp_dir

    try:
        resolver = ShareResolver(StubMediaServer(), rules_dir=rules_dir)
        # 离线验收没有宿主进程：host.render/sniff 在这里只能等到超时，还会往 stdout
        # 写协议事件行污染输出。关掉通道，让依赖渲染的规则立刻降级回 HTTP 路径——
        # 这类包要验渲染那一段只能在真机 GUI 里跑。
        params.apply({"browser.enabled": False})
        mgr = resolver.plugin_mgr
        if not args.no_packs:
            install_official_packs(mgr)
        mgr.refresh()
        packs = mgr.list_info()
        print(f"规则目录: {rules_dir}")
        print("已装规则包:" if packs else "已装规则包: （空——站点规则全靠装包）")
        for info in packs:
            rules = "、".join(
                f"{r['name']}[{'开' if r['enabled'] else '关'}]" for r in info["rules"])
            err = f" 加载失败: {info['error']}" if info.get("error") else ""
            print(f"  - {info['name']} {info['version']} {rules}{err}")

        failures = 0
        for target in args.targets:
            print(f"\n=== {target}")
            proxy_count = len(resolver.media_server.proxies)
            started = time.monotonic()
            try:
                info = resolver.resolve(target, args.quality)
            except Exception as e:
                failures += 1
                print(f"  FAIL {type(e).__name__}: {e}")
                continue
            print(f"  via    : {info.get('via')}")
            print(f"  标题   : {(info.get('title') or '')[:60]}")
            print(f"  播放   : {(info.get('uri') or '')[:110]}")
            print(f"  mime   : {info.get('mime_type')}  "
                  f"耗时 {time.monotonic() - started:.1f}s")
            if info.get("qualities"):
                # 档位表是「重解析的钥匙」，这里同时标出本次实际交付的是哪一档
                table = "、".join(
                    f"{q['label']}{'[本次]' if q['id'] == info.get('quality') else ''}"
                    for q in info["qualities"])
                print(f"  画质   : {table}")
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
