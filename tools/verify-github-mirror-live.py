#!/usr/bin/env python3
"""GitHub 镜像开关的联网验收：镜像取回来的必须和直连一模一样

判据不是「镜像站打得开」，而是四条契约：

1. 开关打开时请求**真的先落到镜像前缀**（从日志里认，不看代码猜）；
2. 镜像给的字节与直连**逐字节一致**（索引 JSON、规则包 zip），zip 还要对得上
   官方索引里声明的 sha256；
3. ``api.github.com`` 这类只有部分镜像代理的形态，开关打开后检查更新照样拿得到
   最新版本号；
4. 镜像死了（前缀指到 127.0.0.1:1）**自动回退直连**，功能不因此中断。

只读：不装包、不写台账、不碰用户配置——开关用 ``params`` 的内存覆盖位演，
落盘的 ``hancast_setting.json`` 一个字节都不动。

用法（需要能出网；本机走代理时带上 HTTPS_PROXY）:
    hancast-backend/.venv/bin/python tools/verify-github-mirror-live.py
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import sys
import tempfile
import zipfile
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hancast-backend"))

import requests                                     # noqa: E402

from hancast_sidecar import params                  # noqa: E402
from hancast_sidecar.media.rules import jsplugins   # noqa: E402
from hancast_sidecar.params import DEFAULT_GITHUB_MIRROR   # noqa: E402
from hancast_sidecar.utils import ghmirror          # noqa: E402

DEAD_PREFIX = "http://127.0.0.1:1/"
TIMEOUT = 45

fails = 0


def ok(name: str, detail: str = "") -> None:
    print(f"  PASS  {name}{f'  [{detail}]' if detail else ''}")


def bad(name: str, detail: str = "") -> None:
    global fails
    fails += 1
    print(f"  FAIL  {name} — {detail}")


def check(name: str, cond: bool, detail: str = "") -> None:
    ok(name, detail) if cond else bad(name, detail or "条件不成立")


def get(url: str) -> bytes:
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "HanCast/mirror-probe"})
    r.raise_for_status()
    return r.content


class LogSpy(logging.Handler):
    """把 ghmirror/jsplugins 的日志收进来，用来证明「这一趟真的走了镜像」"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    def has(self, needle: str) -> bool:
        return any(needle in line for line in self.lines)


def set_switch(enabled: bool, prefix: str) -> None:
    """只改内存覆盖位，不落盘（用户配置里不该留下探针的痕迹）"""
    params._overrides[ghmirror.PARAM_ENABLED] = enabled
    params._overrides[ghmirror.PARAM_PREFIX] = prefix


def main() -> int:
    spy = LogSpy()
    for name in ("hancast.github", "hancast.media.jsplugin"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.addHandler(spy)

    index_url = params.get("rules.index_url")
    print(f"索引地址: {index_url}")
    host = urlparse(index_url).hostname
    check("索引地址在 GitHub 主机上（否则这个探针没有意义）",
          ghmirror.is_github(index_url), str(host))

    # ── 基线：直连 ──
    try:
        direct_index = get(index_url)
    except Exception as e:                       # noqa: BLE001
        bad("直连取官方索引", f"{type(e).__name__}: {e}")
        return 1
    ok("直连取官方索引", f"{len(direct_index)} 字节")
    index = json.loads(direct_index.decode("utf-8"))
    packs = index.get("packs") or []
    check("索引里有可测的包", bool(packs), f"{len(packs)} 个")
    if not packs:
        return 1

    # ── 1) 开关打开：镜像先落地，且字节一致 ──
    set_switch(True, DEFAULT_GITHUB_MIRROR)
    prefix = params.get(ghmirror.PARAM_PREFIX)
    check("candidates 第一条是镜像",
          ghmirror.candidates(index_url)[0] == ("镜像", prefix + index_url),
          ghmirror.candidates(index_url)[0][0])
    try:
        mirrored = get(prefix + index_url)
    except Exception as e:                       # noqa: BLE001
        bad("镜像取索引", f"{type(e).__name__}: {e}")
        mirrored = b""
    check("镜像索引与直连逐字节一致", mirrored == direct_index,
          f"{len(mirrored)} vs {len(direct_index)}")

    # 真代码路径：fetch_index 走镜像，日志要认得出来
    mgr = jsplugins.PluginManager(os.path.join(tempfile.mkdtemp(prefix="hancast-mirror-"), "plugins"))
    spy.lines.clear()
    try:
        idx = mgr.fetch_index(index_url)
        check("fetch_index 经镜像成功", bool(idx["packs"]),
              f"{len(idx['packs'])} 个包 / 日志命中={spy.has('规则包索引经镜像取得')}")
        check("日志证明这一趟走了镜像", spy.has("规则包索引经镜像取得"))
    except Exception as e:                       # noqa: BLE001
        bad("fetch_index 经镜像", f"{type(e).__name__}: {e}")

    # ── 2) 规则包 zip：镜像字节 == 直连字节 == 索引声明的 sha256 ──
    entry = next((p for p in packs if p.get("sha256")), packs[0])
    zip_url, want = str(entry["url"]), str(entry.get("sha256") or "")
    try:
        direct_zip = get(zip_url)
        mirrored_zip = get(prefix + zip_url)
    except Exception as e:                       # noqa: BLE001
        bad("取规则包 zip", f"{type(e).__name__}: {e}")
        direct_zip = mirrored_zip = b""
    check("镜像 zip 与直连逐字节一致", mirrored_zip == direct_zip,
          f"{entry['name']} {len(direct_zip)} 字节")
    got = hashlib.sha256(mirrored_zip).hexdigest()
    check("镜像 zip 对得上官方 sha256", not want or got == want, got[:12])
    try:
        with zipfile.ZipFile(io.BytesIO(mirrored_zip)) as zf:
            names = zf.namelist()
            # 产物是「<包名>/manifest.json + 脚本」的目录形态（import_zip_bytes 认这个）
            check("镜像 zip 能正常解包",
                  sum(1 for n in names if n.endswith("manifest.json")) == 1
                  and any(n.endswith(".js") for n in names),
                  ",".join(names))
    except Exception as e:                       # noqa: BLE001
        bad("镜像 zip 解包", f"{type(e).__name__}: {e}")

    # import_url 走真代码：镜像装包，摘要校验照旧生效
    spy.lines.clear()
    try:
        info = mgr.import_url(zip_url, expected_sha256=want)
        check("import_url 经镜像装包成功", info.get("name") == entry["name"],
              f"{info.get('name')}@{info.get('version')} / 日志命中={spy.has('规则包经镜像下载')}")
    except Exception as e:                       # noqa: BLE001
        bad("import_url 经镜像装包", f"{type(e).__name__}: {e}")

    # ── 3) api.github.com（只有部分镜像代理转发这一形态）──
    api = "https://api.github.com/repos/pingod/HanCast/releases/latest"
    try:
        direct_tag = json.loads(get(api).decode()).get("tag_name", "")
    except Exception as e:                       # noqa: BLE001
        bad("直连 api.github.com", f"{type(e).__name__}: {e}")
        direct_tag = ""
    try:
        mirrored_tag = json.loads(get(prefix + api).decode()).get("tag_name", "")
    except Exception as e:                       # noqa: BLE001
        bad("镜像 api.github.com", f"{type(e).__name__}: {e}")
        mirrored_tag = ""
    check("镜像与直连给出同一个最新版本", bool(direct_tag) and mirrored_tag == direct_tag,
          f"{mirrored_tag or '—'} vs {direct_tag or '—'}")

    # ── 4) 镜像死了必须回退直连 ──
    set_switch(True, DEAD_PREFIX)
    spy.lines.clear()
    try:
        idx = mgr.fetch_index(index_url)
        check("镜像死了仍能取到索引（回退直连）", bool(idx["packs"]),
              f"日志命中={spy.has('索引走镜像失败，回退直连')}")
        check("回退这件事留了日志", spy.has("索引走镜像失败，回退直连"))
    except Exception as e:                       # noqa: BLE001
        bad("镜像死掉后的回退", f"{type(e).__name__}: {e}")

    try:
        info = mgr.import_url(zip_url, expected_sha256=want)
        check("镜像死了仍能装包（回退直连）", info.get("name") == entry["name"])
    except Exception as e:                       # noqa: BLE001
        bad("镜像死掉后的装包回退", f"{type(e).__name__}: {e}")

    # ── 5) 关掉开关：一条镜像请求都不该发出去 ──
    set_switch(False, prefix)
    spy.lines.clear()
    idx = mgr.fetch_index(index_url)
    check("关掉开关后不走镜像", bool(idx["packs"]) and not spy.has("经镜像"),
          f"{len(idx['packs'])} 个包")

    print(f"\nRESULT: {'ALL PASS' if fails == 0 else str(fails) + ' FAIL'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        params._overrides.clear()
