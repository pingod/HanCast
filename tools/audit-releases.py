#!/usr/bin/env python3
"""发布面对账：GitHub tag / Release / CHANGELOG 三者是否互相自洽

存在的理由（2026-11-06 实测数字）：仓库有 63 个 tag、42 个 Release，其中 **24 个是
从未 publish 的 draft**，还有 23 个 tag 一个 Release 都没有。而 `releases/latest` 这个
API **只返回非 draft 的最新版**——draft 攒着不发，等于「检查更新」永远报旧版本；
用户看到的那个「最新版本」和 CHANGELOG 里写着「已发布」的那一版对不上。

**draft 从哪来的**：CI 的 release job 历史上写死 `draft: true`（按 DEC-006「先装端验收、
后 publish」），publish 是人点的。这条设计的代价就是上面那 22 个孤儿——验收没做完就
被下一版取代，draft 于是永远烂在仓库里，而 `releases/latest` 偏偏看不见 draft。
自本轮起 build.yml 改成**推 tag 即 publish、产物数不足就失败**，手动 dispatch 才留 draft。

CI（`.github/workflows/build.yml` 的 release job）自本轮起在推 tag 时直接 publish，
draft 只留给手动 dispatch，所以「废弃 draft 堆在仓库里」不该再发生。这个脚本是那件事的
**体检闸门**：它只读，不改任何东西；清理动作由人明确点出来（见 `--cleanup-commands`）。

判据（每条独立打印，全绿退出 0；任一 FAIL 退出 1）：

1. `draft`  —— 已 publish 的最新版**之后**还存在 draft：多半是验收没做完就忘了，
   或者被后续版本取代的孤儿。之后 = tag 版本号更大。
2. `orphan` —— 有 tag 没 Release。v2.2.19 及更早属**历史豁免**（那时 CI 还没有
   release job），只打 INFO 不计 FAIL；豁免线之后（含）出现就是链路漏了。
3. `missing-asset` —— 已 publish 的 Release 缺必需产物之一：dmg / -setup.exe / deb /
   rpm / AppImage 五样都要有，`v2.3.16` 起再额外要 Arch 的 `.pkg.tar.zst`。
   规则包 zip 不计入必需项——它的版本号是包自己的，混进比对会每版都误报。
4. `version-mismatch` —— Release 的 tag 名与资产文件名里的版本不一致（例如
   `v2.4.6` 的 Release 里躺着 `HanCast_2.4.5_…`），意味着资产来自别的 run。
5. `changelog` —— CHANGELOG.md 里写了某 tag 的发布记录，但 GitHub 上找不到
   对应 **已 publish** 的 Release（或反之：Release 已 publish 却没记账）。

用法::

    python3 tools/audit-releases.py                 # 全量体检（要网络 + gh 或 GITHUB_TOKEN）
    python3 tools/audit-releases.py --cleanup-commands  # 只打印该删的 draft 的 gh 命令
    python3 tools/audit-releases.py --json          # 机器可读，CI 里当闸门

前置：token 来源三选一 —— `GITHUB_TOKEN` 环境变量 / 已登录的 `gh`（`gh auth token`）/
留空则匿名访问 api.github.com（60 次/小时，本脚本单次约 15 个请求，够用但会被限流）。

豁免线 `LEGACY_TAG_LINE = "v2.2.19"`：这一版及更早的 tag 允许没有 Release。
要收紧就往下降，改这里而不是往判据里塞特例。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "pingod/HanCast"
API = f"https://api.github.com/repos/{REPO}"
# build.yml 的 release job 第一次真正跑起来的那一版；之前只有 tag + 手工产物。
LEGACY_TAG_LINE = "v2.2.19"
# 明确决定「不公开」的 tag（2026-11-06 逐条核对）。共同点：CI 建过 draft Release，
# 但真机验收没做完就被更高版本取代，CHANGELOG 里写着「不再单独发布」（v2.4.1/v2.4.2
# 见 v2.4.3 那节，v2.4.5/v2.4.6 见各自小节的「暂留 draft」）。
# 处置 = 删 draft、**tag 保留**：tag 指向那版的发布提交，属代码历史，不该为发布面的
# 整洁去改写。名单之外再出现「有 tag 无公开 Release」就是发布链路漏了，判 FAIL。
DECLARED_UNRELEASED = frozenset({
    # v2.3.8：产物从未产出（见下方「latest 指针」一节）。对账时一度建过一条无产物的说明
    # release，实测把 `/releases/latest` 指回 2.3.8 并让更新检查从此报「已是最新」，
    # 5 分钟内删掉恢复。**不要**为「补齐 tag↔release 对应」而 publish 一条旧版无产物记录。
    "v2.3.8",
    # v2.4.12：tag 推上去 26 秒就挂，且与代码无关——GitHub 于 2026-11-02 退役 macOS 14
    # image，矩阵里钉死的 `macos-14` 变成不可用配置（作业 0 秒红：
    # "The configuration 'macos-14-arm64-us-default' is not supported"），macOS 产物没产出，
    # release job 的产物闸门因此没建 Release。处置与 v2.3.8 同一条路：**tag 保留、不补建
    # 说明性 release**，内容原样往前发（v2.4.13 = v2.4.12 全部内容 + CI runner 标签修复）。
    # 详见 docs/构建与发布.md §4.1 的实测教训。
    "v2.4.12",
    "v2.2.21",
    "v2.2.22",
    "v2.2.23",
    "v2.2.24",
    "v2.2.25",
    "v2.3.0",
    "v2.3.1",
    "v2.3.2",
    "v2.3.3",
    "v2.3.4",
    "v2.3.5",
    "v2.3.6",
    "v2.3.7",
    "v2.3.9",
    "v2.3.10",
    "v2.3.11",
    "v2.3.12",
    "v2.3.13",
    "v2.4.1",
    "v2.4.2",
    "v2.4.5",
    "v2.4.6",
})
# 已 publish 的 Release 必须齐这五样（后缀小写匹配），与 build.yml 的
# `Require a complete artifact set before publishing` 同源；只差 Arch 那一样按版本另算
# （`ARCH_ASSET_SINCE`：v2.3.16 起才有 `scripts/build-arch-pkg.cjs` 的产物，
# 实测 v2.2.20 / v2.3.14 / v2.3.15 分别只有 5 项、9 项，拿这条去要就是冤枉账）。
REQUIRED_ASSET_SUFFIXES = (".dmg", "-setup.exe", ".deb", ".rpm", ".appimage")
ARCH_ASSET_SINCE = "v2.3.16"
ARCH_ASSET_SUFFIX = ".pkg.tar.zst"
# **声明**为「无产物」的 Release（发布链路当时就断了）。不豁免就会永远 FAIL，
# 于是闸门只能靠调数组/名单来放行——那就不是闸门了。名单之外缺任何一样都算 FAIL。
# 当前为空：v2.3.8 走上面的 DECLARED_UNRELEASED（它压根没有 release），因为**补一条
# 无产物的旧版 release 会让 GitHub 的 latest 指针漂回去**（实测见上一段注释）。
# 这条判据留着，是为了将来真出现「标了无产物却带资产」的情况时仍然能钉住。
DECLARED_ASSETLESS: frozenset = frozenset()

FAIL = "FAIL"
INFO = "INFO"
PASS = "PASS"


def _vkey(tag: str) -> tuple:
    """`v2.4.11` -> (2, 4, 11)；解析不了的排在最前（负数），保证它不会当「最新」。"""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", tag.strip())
    return tuple(map(int, m.groups())) if m else (-1, -1, -1)


def gh_get(path: str, token: str, page: int = 1) -> list:
    url = f"{API}{path}{'&' if '?' in path else '?'}per_page=100&page={page}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gh_get_all(path: str, token: str) -> list:
    out, page = [], 1
    while True:
        chunk = gh_get(path, token, page)
        if not isinstance(chunk, list) or not chunk:
            return out
        out.extend(chunk)
        if len(chunk) < 100:
            return out
        page += 1


def resolve_token() -> tuple[str, str]:
    env = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if env:
        return env.strip(), "环境变量"
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip(), "gh CLI"
    except (OSError, subprocess.SubprocessError):
        pass
    return "", "匿名（受 60 次/小时限流）"


def fetch_tags(token: str) -> list[str]:
    refs = gh_get_all("/tags", token)
    return sorted((r["name"] for r in refs), key=_vkey)


def fetch_releases(token: str) -> list[dict]:
    rels = gh_get_all("/releases", token)
    return [
        {
            "tag": r.get("tag_name") or "",
            "draft": bool(r.get("draft")),
            "prerelease": bool(r.get("prerelease")),
            "published": r.get("published_at"),
            "name": r.get("name") or "",
            "assets": [
                {
                    "name": a.get("name") or "",
                    "size": a.get("size"),
                    "digest": (a.get("digest") or "").replace("sha256:", ""),
                }
                for a in (r.get("assets") or [])
            ],
        }
        for r in rels
    ]


def changelog_text() -> str:
    for p in (Path(__file__).resolve().parent.parent / "CHANGELOG.md", Path("CHANGELOG.md")):
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return ""


def changelog_tags() -> list[str]:
    """CHANGELOG.md 里 `## [x.y.z]` 形式的条目（发布记账的真相源）。"""
    return [m.group(1) for m in re.finditer(r"^##\s*\[?v?(\d+\.\d+\.\d+)\]?",
                                            changelog_text(), re.M)]


def find_latest_drift(published: list[dict], latest_api: dict | None) -> dict:
    """GitHub 的 `releases/latest` 指针 == 语义版本最大的那一版吗

    2026-11-06 实测：GitHub 把「latest」给的是**最近一次 publish** 的那条，不是版本号
    最大的那条。所以「给旧版补一条说明性 release」这个看起来无害的动作，后果是
    `/releases/latest` 立刻回指旧版，而更新检查用的正是这个接口 —— 用户侧表现成
    「明明发了新版，程序却说已是最新」，界面上一行错都不报。
    同一条也解释了为什么 draft 烂在那儿会让更新检查失效：latest 不看 draft。
    """
    newest = max((r["tag"] for r in published), key=_vkey) if published else None
    pointer = (latest_api or {}).get("tag_name") or ""
    if not pointer or not newest:
        return {"newest_by_version": newest or "-", "latest_pointer": pointer or "-",
                "drift": bool(newest) and not pointer}
    return {"newest_by_version": newest, "latest_pointer": pointer,
            "drift": pointer != newest}


def find_issues(releases: list[dict], tags: list[str], latest_api: dict | None = None) -> dict:
    published = [r for r in releases if not r["draft"]]
    drafts = [r for r in releases if r["draft"]]
    newest_pub = max((r["tag"] for r in published), key=_vkey) if published else None

    # ① 最新版之后还挂着的 draft = 当前版本的发布没走完，是真问题。
    pending_drafts = sorted(
        (r["tag"] for r in drafts if newest_pub and _vkey(r["tag"]) > _vkey(newest_pub)),
        key=_vkey,
    )
    # ② tag 比最新已发布版旧的 draft = 已被取代的孤儿：产物永远不会被任何人下载，
    #    `releases/latest` 也看不见它们，只会让发布面看起来乱。
    superseded_drafts = sorted(
        (r["tag"] for r in drafts if newest_pub and _vkey(r["tag"]) <= _vkey(newest_pub)),
        key=_vkey,
    )

    rel_tags = {r["tag"] for r in releases}
    no_release = [t for t in tags if t not in rel_tags]
    declared_no_release = [t for t in no_release if t in DECLARED_UNRELEASED]
    rest = [t for t in no_release if t not in DECLARED_UNRELEASED]
    modern_no_release = [t for t in rest if _vkey(t) > _vkey(LEGACY_TAG_LINE)]
    legacy_no_release = [t for t in rest if _vkey(t) <= _vkey(LEGACY_TAG_LINE)]

    missing_assets = []
    unexpected = []
    version_mismatch = []
    for r in published:
        names = [a["name"].lower() for a in r["assets"]]
        if r["tag"] in DECLARED_ASSETLESS:
            if names:  # 说没有产物却躺着东西，同样要说清
                unexpected.append({"tag": r["tag"], "assets": len(names)})
        else:
            wanted = list(REQUIRED_ASSET_SUFFIXES)
            if _vkey(r["tag"]) >= _vkey(ARCH_ASSET_SINCE):
                wanted.append(ARCH_ASSET_SUFFIX)
            lack = [x for x in wanted if not any(n.endswith(x) for n in names)]
            if lack:
                missing_assets.append({"tag": r["tag"], "lack": lack})
        ver = r["tag"].lstrip("vV")
        # 只看安装包：规则包 zip 的名字里带的是**包自己的**版本（hancast-bilibili-1.1.0.zip），
        # 混进比对会让每一版 Release 都误报。
        installers = [n for n in names if n.endswith((".dmg", ".exe", ".deb", ".rpm",
                                                     ".appimage", ".tar.zst"))]
        foreign = sorted(
            {
                m.group(1)
                for n in installers
                for m in [re.search(r"[-_]((?:\d+\.)+\d+)(?:[-_]|\.tar|\.zst|$)", n)]
                if m and m.group(1) != ver
            }
        )
        if foreign:
            version_mismatch.append({"tag": r["tag"], "asset_versions": foreign})

    pub_set = {r["tag"].lstrip("vV") for r in published}
    draft_set = {r["tag"].lstrip("vV") for r in drafts}
    text = changelog_text()
    log_set = {t.lstrip("vV") for t in changelog_tags()}
    # CHANGELOG 记了账、GitHub 上却还是 draft：只有「当前最新版」这一种情况是真问题
    # （记账说发布了、`releases/latest` 却不给），其余属历史遗留 —— v2.4.1/v2.4.2 那批
    # 在 CHANGELOG 里就明写「不再单独发布」，判据认这句话并把它们并进 superseded draft。
    logged_draft = sorted((t for t in log_set if t in draft_set), key=_vkey)
    logged_unpublished_pending = [t for t in logged_draft
                                 if newest_pub and _vkey(f"v{t}") > _vkey(newest_pub)]
    logged_draft_historic = [t for t in logged_draft if t not in logged_unpublished_pending]
    # 已 publish 但 CHANGELOG 没有一级标题：正文里提到过（v2.3.24 被并进 v2.4.0 那一节）
    # 算 INFO，说明记账口径是「一节覆盖两版」；完全没提到才是漏记账。
    published_unlogged = sorted(
        (t for t in pub_set if t not in log_set and f"v{t}" not in text), key=_vkey
    )
    published_mentioned = sorted(
        (t for t in pub_set if t not in log_set and f"v{t}" in text), key=_vkey
    )

    return {
        "counts": {
            "tags": len(tags),
            "releases": len(releases),
            "published": len(published),
            "drafts": len(drafts),
            "newest_published": newest_pub or "-",
        },
        "pending_drafts": pending_drafts,
        "superseded_drafts": superseded_drafts,
        "modern_tags_without_release": modern_no_release,
        "legacy_tags_without_release": legacy_no_release,
        "declared_no_release": declared_no_release,
        "missing_assets": missing_assets,
        "assetless_with_assets": unexpected,
        "version_mismatch": version_mismatch,
        "logged_but_still_draft": logged_unpublished_pending,
        "historic_draft_logged": logged_draft_historic,
        "published_but_not_logged": published_unlogged,
        "published_mentioned_only": published_mentioned,
        "latest_pointer": find_latest_drift(published, latest_api),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="GitHub tag/Release/CHANGELOG 对账（只读）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--cleanup-commands", action="store_true",
                    help="把该删的 draft 打成 `gh release delete` 命令行，交人核对后执行")
    args = ap.parse_args()

    token, token_src = resolve_token()
    try:
        tags = fetch_tags(token)
        releases = fetch_releases(token)
        try:
            latest_api = gh_get("/releases/latest", token)
        except urllib.error.HTTPError as e:
            # 一条 release 都没有时 GitHub 回 404——那不是漂移，是没东西可指
            latest_api = None if e.code == 404 else None
            if e.code != 404:
                raise
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        print(f"[audit] 读 GitHub 失败：HTTP {e.code} {e.reason}（token: {token_src}）{body}",
              file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"[audit] 读 GitHub 失败：{e}（token: {token_src}）", file=sys.stderr)
        return 2

    report = find_issues(releases, tags, latest_api)

    if args.cleanup_commands:
        # **只打印、不执行**：删 release 连带删掉那份 CI 产物，是不可逆动作，
        # 必须由人点名确认（这也是本脚本「只读」承诺的边界）。
        todo = report["superseded_drafts"] + report["pending_drafts"]
        if not todo:
            print("[audit] 没有 draft Release 需要清理")
            return 0
        print(f"# 已被取代的 draft（{len(report['superseded_drafts'])} 个）：产物没人会下载，建议删")
        for t in report["superseded_drafts"]:
            print(f"gh release delete {t} --yes --repo {REPO}")
        if lp_drift := report["latest_pointer"]["drift"]:
            print("# `releases/latest` 指针漂了（GitHub 把 latest 给最近 publish 的那条）："
                  f"现在指向 {report['latest_pointer']['latest_pointer']}，"
                  f"语义最新是 {report['latest_pointer']['newest_by_version']}")
            print(f"# 修法：把指针那一版改回 draft 或直接删掉，再把语义最新的重新 publish 一次")
        if report["pending_drafts"]:
            print(f"# 最新版之后仍挂着的 draft（{len(report['pending_drafts'])} 个）："
                  "先补装端验收再 publish，别直接删")
            for t in report["pending_drafts"]:
                print(f"# gh release edit {t} --draft=false --repo {REPO}")
        return 0

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        c = report["counts"]
        print(f"[audit] {REPO} · tag {c['tags']} / release {c['releases']} "
              f"（已发布 {c['published']}、draft {c['drafts']}）"
              f"· 最新已发布 {c['newest_published']} · token: {token_src}")

        def line(label: str, items, level: str) -> None:
            mark = PASS if not items else level
            detail = "" if not items else f" → {', '.join(str(i) for i in items[:30])}"
            print(f"  [{mark}] {label}: {len(items)}{detail}")

        line("最新版之后仍挂着的 draft（补验收后 publish 或删）",
             report["pending_drafts"], FAIL)
        line("已被取代的 draft（孤儿产物，该删）", report["superseded_drafts"], FAIL)
        line("有 tag 但从未有公开 Release（豁免线之后才算 FAIL）",
             report["modern_tags_without_release"], FAIL)
        line(f"历史豁免：{LEGACY_TAG_LINE} 及更早无 release",
             report["legacy_tags_without_release"], INFO)
        line("明确声明不公开的 tag（见 DECLARED_UNRELEASED）",
             report["declared_no_release"], INFO)
        line("已发布但缺平台产物",
             [f"{m['tag']}({','.join(m['lack'])})" for m in report["missing_assets"]], FAIL)
        line("声明无产物却躺着资产的 Release",
             [f"{m['tag']}({m['assets']} 项)" for m in report["assetless_with_assets"]], FAIL)
        line("release tag 与安装包文件名版本不一致",
             [f"{m['tag']}≠{','.join(m['asset_versions'])}" for m in report["version_mismatch"]],
             FAIL)
        line("CHANGELOG 记了账、GitHub 却还是 draft（当前版）",
             report["logged_but_still_draft"], FAIL)
        line("CHANGELOG 提到但没写成一级标题的已发布版",
             report["published_mentioned_only"], INFO)
        line("GitHub 已 publish 而 CHANGELOG 完全没提",
             report["published_but_not_logged"], FAIL)

    lp = report["latest_pointer"]
    print(f"  [{'FAIL' if lp['drift'] else 'PASS'}] `releases/latest` 指针："
          f"{lp['latest_pointer']}（语义最新 {lp['newest_by_version']}）"
          + ("" if not lp["drift"] else " ← GitHub 按「最近 publish」选，不按版本号；"
                                           "更新检查用的就是这个接口"))

    bad = (
        report["pending_drafts"]
        or report["superseded_drafts"]
        or report["modern_tags_without_release"]
        or report["missing_assets"]
        or report["assetless_with_assets"]
        or report["version_mismatch"]
        or report["logged_but_still_draft"]
        or report["published_but_not_logged"]
        or lp["drift"]
    )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
