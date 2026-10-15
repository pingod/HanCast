#!/usr/bin/env python3
"""把一个官方规则包按出货形态装进临时目录，再用真实网络解析地址

为什么需要它：`tools/build-rule-packs.py --check` 只能证明产物与源一致，
`pytest` 全程离线——**「包里的 JS 在真站上还抓得到地址」这件事只有联网才能证明**。
采集站这类兜底规则尤其危险：一套 CMS 换皮，模板一改规则就静默失效，
而用户在程序里看到的只是「解析失败」。每次发版前该重跑一次。

做法与产品一致，不走任何后门：读 `rule-packs/dist/<name>-<version>.zip`
（就是索引里那个文件）→ `PluginManager.import_zip_bytes` → `refresh()` 注册 →
`ShareResolver.resolve()`。只有本地媒体服务换成替身（`_publish` 会真起代理，
验收要的是解析结果不是供流）。

用法：
    hancast-backend/.venv/bin/python tools/verify-rule-pack-live.py                  # 默认包与默认地址表
    hancast-backend/.venv/bin/python tools/verify-rule-pack-live.py --pack hancast-cms
    hancast-backend/.venv/bin/python tools/verify-rule-pack-live.py --url https://x.example/play/1
    # 站点自己挂了 / 上了验证码闸门时，记进 EXPECTED_FAIL 并注明取证日期，别把环境故障算成包坏了

需要访问外网。本机走代理时显式带上：HTTPS_PROXY=http://127.0.0.1:9565
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hancast-backend"))

from hancast_sidecar.media.rules.jsplugins import PluginError, PluginManager  # noqa: E402
from hancast_sidecar.media.share_resolver import ShareResolver  # noqa: E402

# 这些地址来自解析失败台账（Windows 测试机 share_rules 之外的真实用户输入），
# 每条都对应包里的一条规则；站点换皮导致解析不到时，这一跑就该红。
CASES = {
    "hancast-cms": [
        "https://www.66dpw.vip/vodplay/330-1-1.html",      # 苹果CMS player_aaaa
        "https://www.lvsc168.com/play/14439-1-0.html",     # myui share 播放器页
        "https://www.qiakon.com/dsxpy/275815-1-1.html",    # 已知不可解，见 EXPECTED_FAIL
    ],
}

# 已知拿不下、且**不是包的问题**的地址（2026-10-15 取证）：
# www.qiakon.com 前置 XCDN PoW 验证码（qiakon.com 下发 challenge/difficulty），
# 过了验证码源站又间歇回 502 —— 任何规则包都不可能在页面正文里拿到流地址。
EXPECTED_FAIL = {
    "hancast-cms": [
        ("https://www.qiakon.com/dsxpy/275815-1-1.html", "XCDN PoW + 源站 502"),
    ],
}


class _NoPublish:
    """替身：解析出地址就够，不需要真把流代理起来"""

    def __init__(self):
        self.proxies = []

    def register_proxy(self, remote_url, headers=None, suffix=""):
        self.proxies.append(remote_url)
        return "http://127.0.0.1:0/proxy/%d%s" % (len(self.proxies), suffix)


def dist_zip(pack: str) -> str:
    with open(os.path.join(ROOT, "rule-packs", pack, "manifest.json"),
              encoding="utf-8-sig") as f:
        version = json.load(f)["version"]
    path = os.path.join(ROOT, "rule-packs", "dist", f"{pack}-{version}.zip")
    if not os.path.isfile(path):
        raise SystemExit(f"[FAIL] 产物不存在：{path}\n"
                         "       → 先跑 python3 tools/build-rule-packs.py")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pack", default="hancast-cms", help="规则包名（rule-packs/ 下的目录名）")
    ap.add_argument("--url", action="append", default=[],
                    help="额外要解析的地址（可重复）；给了就不再用内置地址表，"
                         "且已知不可解清单同时失效")
    args = ap.parse_args()

    zip_path = dist_zip(args.pack)
    tmp = tempfile.mkdtemp(prefix="hancast-packcheck-", dir="/tmp")
    failed = 0
    try:
        rules_dir = os.path.join(tmp, "share_rules")
        os.makedirs(rules_dir)
        mgr = PluginManager(os.path.join(rules_dir, "plugins"))
        with open(zip_path, "rb") as f:
            blob = f.read()
        try:
            info = mgr.import_zip_bytes(blob)
        except PluginError as e:
            print(f"[FAIL] 产物装不进程序（导入侧白名单/限额/manifest 校验）: {e}")
            return 1
        if info.get("error"):
            print(f"[FAIL] 包加载失败: {info['error']}")
            return 1
        rules = mgr.refresh()
        print(f"[1/3] 装入 {os.path.basename(zip_path)}："
              f"{len(rules)} 条规则")
        for r in rules:
            print(f"      - {r.name} hosts={list(r.hosts)} stage={r.stage} "
                  f"priority={r.priority}")
        if not rules:
            print("[FAIL] 包里没有可用规则")
            return 1

        urls = args.url or CASES.get(args.pack, [])
        known_fail = {} if args.url else dict(EXPECTED_FAIL.get(args.pack, []))
        print(f"[2/3] 联网解析 {len(urls)} 条"
              + (f"（另有 {len(known_fail)} 条已知不可解）" if known_fail else ""))
        resolver = ShareResolver(_NoPublish(), rules_dir=rules_dir)
        failed = _run(resolver, urls, known_fail, args.pack)
        print(f"[3/3] {'全部通过' if failed == 0 else f'{failed} 条未通过'}")
        return 1 if failed else 0
    finally:
        shutil.rmtree(tmp, True)


def _run(resolver, urls, known_fail, pack) -> int:
    bad = 0
    for url in urls:
        try:
            info = resolver.resolve(url)
        except Exception as e:
            note = known_fail.get(url)
            if note:
                print(f"      SKIP(已知) {url}\n            {note}；本轮报错：{str(e)[:120]}")
                continue
            print(f"      [FAIL] {url}\n            {type(e).__name__}: {str(e)[:300]}")
            bad += 1
            continue
        via = str(info.get("via") or "")
        if not via.startswith(pack):
            print(f"      [FAIL] {url} 解出来了，但不是这个包的规则拿的（via={via}）")
            bad += 1
            continue
        print(f"      [PASS] {url}\n            via={via} uri={str(info.get('uri'))[:120]}"
              f"\n            title={str(info.get('title'))[:60]} mime={info.get('mime_type')}")
    for url, note in known_fail.items():
        if url not in urls:
            print(f"      [WARN] 已知不可解的地址不在本轮列表里: {url}（{note}）")
    return bad


if __name__ == "__main__":
    raise SystemExit(main())
