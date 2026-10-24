#!/usr/bin/env python3
"""文档新鲜度与数字一致性闸门。

`tools/audit-doc-refs.py` 只回答「文件在不在、行号越不越界」，所以**行号还在、指的行已经不是
那回事**的引用会全部静默通过——这个项目已经为此返工过三次（v2.3.19 的 build.yml +47 行、
v2.3.21 的 locale 插键、v2.3.23 复查时发现 docs/投屏确认.md 有 26 处 commands.py 引用整体漂移）。

本脚本补三件它查不了的事：

1. **内容校验**：引用旁边声称的符号（反引号里的标识符）必须真的落在那个行号窗口里，
   否则报「行号对、内容不对」。归属规则沿用 audit-doc-refs 的「就近在前」：一个符号只归属于
   它右边最近的引用，避免一行里同时引两个文件时互相误伤。
2. **校对戳**：每份文档头部的「最后校对：日期，对应版本 vX.Y.Z」落后于 `package.json` 即报，
   超过 `--max-lag` 个补丁版算 FAIL（默认 0，即必须与当前版一致；只改代码不碰文档的版也要盖章）。
3. **硬数字**：文档里反复出现的契约计数（壳层命令数、Sidecar 注册数、后台参数数、组件数、
   store 数、locale 键数、官方规则包数、测试文件数）逐项与代码实测比对，写死在文档里的旧数一律红牌。

用法：
    python3 tools/check-doc-freshness.py              # 全仓，FAIL 即退出 1
    python3 tools/check-doc-freshness.py docs/投屏确认.md
    python3 tools/check-doc-freshness.py --numbers    # 只查数字
    python3 tools/check-doc-freshness.py --max-lag 3  # 放宽校对戳
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 只写文件名时的解析顺序：与 audit-doc-refs.py 一致，再加几个常用目录
SEARCH_SUBDIRS = [
    ".", "src-tauri/src", "hancast-backend/hancast_sidecar",
    "hancast-backend/hancast_sidecar/media", "hancast-backend/hancast_sidecar/media/rules",
    "hancast-backend/hancast_sidecar/protocol", "hancast-backend/hancast_sidecar/renderer",
    "hancast-backend/hancast_sidecar/screencast", "hancast-backend/hancast_sidecar/security",
    "hancast-backend/hancast_sidecar/models", "hancast-backend/hancast_sidecar/utils",
    "hancast-backend/tests", "src/api", "src/stores", "src/components", "src/views",
    "scripts", "linux-capture/src",
]

# 历史/否定语气：这些行是在讲「曾经如何」，不参与内容校验
HISTORY = re.compile(r"曾经|此前|已删|已于|已被取代|不再|历史上|旧版|遗留|当时|当年|不是.*现在|从不出现在")
# 运行时才存在、或不在仓库里的路径
RUNTIME = re.compile(r"hancast_setting\.json|window_config\.json|hancast\.log|\.db$|/usr/|/Applications/|__pycache__")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{3,40}$")
# 这些词在文档里是「返回值/类型/字段」而不是「被引用的符号」，参与归因只会制造噪音
STOP = {
    "false", "true", "none", "null", "self", "data", "params", "value", "values", "error",
    "errors", "ok", "key", "keys", "name", "type", "text", "list", "dict", "str", "int",
    "json", "device", "devices", "session", "sessions", "state", "status", "result",
    "success", "reason", "reasons", "target", "source", "media", "url", "urls", "path",
    "version", "enabled", "default", "options", "label", "help", "note", "example",
    "sha256", "playlist", "direct", "cancelled", "pending", "timeout", "ffmpeg", "mpv",
    "cast", "casts", "events", "command", "commands", "response", "request", "string",
}
CIT = re.compile(r"(?:^|[\s\[/（(>`])([A-Za-z0-9_./-]+\.(?:py|rs|ts|vue|json|cjs|mjs|sh|cmd|ps1|toml|yml|yaml|csv|html|conf|lock|md))(?::(\d+))?(?:-(\d+))?")
# 已声明退役的整份草案（与 audit-doc-refs.py 同一口径）：头部 15 行里出现这些词就不要求校对戳，
# 因为它是留档而不是契约——给它盖章反而谎称「核对过当前代码」。
SUPERSEDED = re.compile(r"已被|取代|历史稿|草案|superseded|不再维护|仅作留档|留档")
QUOTED = re.compile(r"`([^`\n]{2,60})`")
# --defscan：一次性返工用的加细档。引用窗口里出现了这个符号，但出现的只是调用/子串、
# 连一行定义都不是 → 打软提示（不计入 FAIL），用来抓「行号还在、但已经从定义漂到调用」的引用。
DEFSCAN = False


def resolve(name: str) -> pathlib.Path | None:
    p = ROOT / name
    if p.is_file():
        return p
    base = name.split("/")[-1]
    for sub in SEARCH_SUBDIRS:
        cand = ROOT / sub / name
        if cand.is_file():
            return cand
    hits = list(ROOT.glob(f"**/{base}"))
    hits = [h for h in hits if "node_modules" not in str(h) and "/target/" not in str(h)]
    return hits[0] if len(hits) == 1 else None


def strip_code_blocks(text: str) -> list[tuple[int, str]]:
    """返回 (行号, 内容)，跳过 ``` 围栏内的行——代码块里的冒号数字不是引用。"""
    out, fence = [], False
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            out.append((i, line))
    return out


def def_lines(lines: list[str], ident: str) -> list[int]:
    """符号的定义行（Python/TS/Rust/Vue 常见写法），用于区分「指定义」还是「指调用」。"""
    rx = rf"(def|fn|func|const|let|var|function|class|interface|type)\s+{re.escape(ident)}\b" \
         rf"|^\s*{re.escape(ident)}\s*[:(]"
    return [i + 1 for i, l in enumerate(lines) if re.search(rx, l)]


def check_citations(files: list[pathlib.Path]) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    soft: list[str] = []
    cache: dict[str, list[str] | None] = {}
    for doc in files:
        rel_doc = doc.relative_to(ROOT)
        for lineno, line in strip_code_blocks(doc.read_text(encoding="utf-8")):
            if HISTORY.search(line):
                continue
            cites = list(CIT.finditer(line))
            for idx, m in enumerate(cites):
                name, a, b = m.group(1), m.group(2), m.group(3)
                if RUNTIME.search(name):
                    continue
                key = name.split("/")[-1] if "/" not in name else name
                if key not in cache:
                    f = resolve(name)
                    cache[key] = f.read_text(encoding="utf-8").splitlines() if f else None
                lines = cache[key]
                if lines is None:
                    continue  # 文件缺失由 audit-doc-refs 负责报
                if not a:
                    continue
                start = int(a)
                end = int(b) if b else start
                window = "\n".join(lines[max(start - 1, 0):min(end, len(lines))])
                # 归因要紧：只有**贴着引用**的反引号符号才算「这句在声称什么」。
                # 松归因会把表格第一列的命令名算给右边的 commands.ts 行号，误报一大片。
                left = line[max(0, m.start() - 80):m.start()]
                right = line[m.end():m.end() + 80]
                # 允许符号与引用之间夹最多 4 个标点/空白（中文括号、顿号、空格都算）
                claimed = [x for x in re.findall(r"`([^`\n]{2,60})`[\s（(、,。:：=]{0,4}`?$", left)] + \
                          [x for x in re.findall(r"^[\s（(、,。:：=]{0,4}`([^`\n]{2,60})`", right)]
                for sym in claimed:
                    ident = sym.split(".")[-1].strip("()").rstrip("（(")
                    if not IDENT.match(ident) or len(ident) < 6 or ident.isdigit():
                        continue
                    if ident.lower() in STOP:
                        continue
                    # Python/Rust 里私有实现常带下划线前缀：`save_guard_settings` 命中 `_save_guard_settings`
                    rx = rf"(?<![\w])_?{re.escape(ident)}(?![\w])"
                    if re.search(rx, window):
                        if DEFSCAN:
                            dl = def_lines(lines, ident)
                            if dl and not any(start <= d <= end for d in dl):
                                soft.append(
                                    f"{rel_doc}:{lineno}  引 {name}:{a}{'' if not b else '-' + b} "
                                    f"里的 {ident} 只是调用/子串，定义在 {dl[:3]}"
                                )
                        continue
                    hits = [i + 1 for i, l in enumerate(lines) if re.search(rx, l)]
                    where = f"，实际在 {hits[:3]}" if hits else "，全文件搜不到这个词"
                    problems.append(
                        f"{rel_doc}:{lineno}  引 {name}:{a}{'' if not b else '-' + b} "
                        f"声称 {ident}，窗口里没有{where}\n      原文: {line.strip()[:110]}"
                    )
    return problems, soft


def semver(s: str) -> tuple[int, int, int]:
    try:
        a, b, c = (s.split(".") + ["0", "0"])[:3]
        return (int(a), int(b), int(c))
    except Exception:
        return (0, 0, 0)


def check_stamps(files: list[pathlib.Path], max_lag: int) -> list[str]:
    current = semver(json.loads((ROOT / "package.json").read_text())["version"])
    bad = []
    for doc in files:
        text = doc.read_text(encoding="utf-8")
        head = "\n".join(text.splitlines()[:15])
        if SUPERSEDED.search(head):
            continue  # 与 audit-doc-refs 同一口径：已声明退役的整份草案不要求校对戳
        m = re.search(r"最后校对[：:]\s*(\d{4}-\d{2}-\d{2})[^\n]*?v?(\d+\.\d+\.\d+)", text)
        if not m:
            bad.append(f"{doc.relative_to(ROOT)}  头部没有「最后校对：<日期>，对应版本 vX.Y.Z」这一行"
                       "（门面 README 可用 HTML 注释藏起来，历史稿要在前 15 行声明「已被…取代」）")
            continue
        v = semver(m.group(2))
        if v < current:
            lag = (current[0] - v[0]) * 10000 + (current[1] - v[1]) * 100 + (current[2] - v[2])
            tag = "FAIL" if lag > max_lag else "WARN"
            bad.append(f"{tag} {doc.relative_to(ROOT)}  校对戳停在 v{m.group(2)}（当前 v{'.'.join(map(str, current))}，差 {lag}）")
    return bad


def measure() -> dict[str, int]:
    """从代码里实测契约计数——文档写的必须等于这些。"""
    lib = (ROOT / "src-tauri/src/lib.rs").read_text()
    cmd = (ROOT / "hancast-backend/hancast_sidecar/commands.py").read_text()
    reg = re.search(r"self\._commands[^=]*= \{(.*?)\n        \}", cmd, re.S)
    registry = len(re.findall(r'^\s{12}"([a-z_0-9]+)":', reg.group(1), re.M)) if reg else -1
    params = (ROOT / "hancast-backend/hancast_sidecar/params.py").read_text()
    keys = re.findall(r'^\s{4}\{"key": "([\w.]+)"', params, re.M)
    locale = json.loads((ROOT / "src/locales/zh-CN.json").read_text())
    index = json.loads((ROOT / "rule-packs/index.json").read_text())
    tests = list((ROOT / "hancast-backend/tests").glob("test_*.py"))
    return {
        "壳层 #[tauri::command]": lib.count("#[tauri::command]"),
        "Sidecar 注册表": registry,
        "后台参数 PARAM_SPECS": len(keys),
        "前端组件 .vue": len(list((ROOT / "src/components").glob("*.vue"))),
        "Pinia store": len(list((ROOT / "src/stores").glob("*.ts"))),
        "locale 键数（zh）": len(locale),
        "官方规则包": len(index.get("packs", [])),
        "后端 test_*.py": len(tests),
        "tools 顶层脚本": len([f for f in (ROOT / "tools").iterdir()
                          if f.is_file() and f.suffix in {".py", ".sh", ".cmd", ".ps1", ".mjs", ".cjs"}]),
        "浏览器 mock 参数项": len(re.findall(
            r"key: '",
            (lambda b: b[b.index('function mockParamSpecs'):b.index("\nfunction ", b.index('function mockParamSpecs') + 10)])(
                (ROOT / "src/api/commands.ts").read_text()))),
    }


# 文档里以「数字 + 名词」形式写死的契约计数：模式命中就必须等于实测值
# 只有这些**总量口径**的写法才参与比对；分组小计（「规则包管理（11 条）」）与表格行号不算
NUMBER_PATTERNS = [
    (r"(\d+)\s*条\s*#\[tauri::command\]", "壳层 #[tauri::command]"),
    (r"(\d+)\s*个\s*#\[tauri::command\]", "壳层 #[tauri::command]"),
    (r"(\d+)\s*条 invoke", "壳层 #[tauri::command]"),
    (r"invoke 命令[（(](\d+)\s*条", "壳层 #[tauri::command]"),
    (r"注册\s*(\d+)\s*条命令", "Sidecar 注册表"),
    (r"注册表[^\d]{0,10}(\d+)\s*条", "Sidecar 注册表"),
    (r"registry of (\d+) commands", "Sidecar 注册表"),
    (r"(\d+)\s*项后台参数", "后台参数 PARAM_SPECS"),
    (r"PARAM_SPECS[^0-9]{0,14}(\d+)\s*项", "后台参数 PARAM_SPECS"),
    (r"(\d+)\s*background parameters", "后台参数 PARAM_SPECS"),
    (r"(\d+)\s*个组件", "前端组件 .vue"),
    (r"(\d+)\s*个 \.vue", "前端组件 .vue"),
    (r"(\d+)\s*个\s*(?:Pinia\s*)?store", "Pinia store"),
    (r"(\d+)\s*个官方规则包", "官方规则包"),
    (r"官方规则包[（(](\d+)\s*个", "官方规则包"),
    (r"(\d+)\s*个\s*test_\*", "后端 test_*.py"),
    (r"(\d+)\s*个顶层脚本", "tools 顶层脚本"),
    (r"mockParamSpecs[^\d]{0,10}(\d+)\s*项", "浏览器 mock 参数项"),
]


def check_numbers(files: list[pathlib.Path]) -> tuple[list[str], list[str]]:
    truth = measure()
    bad, ok = [], []
    for doc in files:
        rel = doc.relative_to(ROOT)
        for lineno, line in strip_code_blocks(doc.read_text(encoding="utf-8")):
            if HISTORY.search(line) or "口径" in line:
                continue
            for pat, key in NUMBER_PATTERNS:
                for m in re.finditer(pat, line):
                    want = truth[key]
                    if want < 0:
                        continue
                    got = int(m.group(1))
                    seg = f"{rel}:{lineno}  「{m.group(0)[:44]}」写 {got}，代码实测 {key}={want}"
                    (bad if got != want else ok).append(seg)
    return bad, ok


def check_tools_inventory(files: list[pathlib.Path]) -> tuple[list[str], list[str]]:
    """tools/ 的分类表必须**逐个脚本点名**——「55 个」这种总数只有配上无遗漏的清单才是判据。

    v2.3.23 立项这一道的理由：分类表口径改过三次，每次加脚本都只改总数不改表，
    于是「数字与代码一致」的自检根本发现不了某一行整条不存在。
    """
    tdir = ROOT / "tools"
    if not tdir.is_dir():
        return [], []
    scripts = sorted(f.name for f in tdir.iterdir()
                     if f.is_file() and f.suffix in {".py", ".sh", ".cmd", ".ps1", ".mjs", ".cjs"})
    md = (tdir / "README.md").read_text(encoding="utf-8")
    missing = [s for s in scripts if f"`{s}`" not in md]
    bad, ok = [], []
    # 总数这里**故意不走 check_numbers**：那一道跳过含「口径」的行，而项目里恰好在讲口径的行才写着总数
    # （AGENTS.md 的两处、tools/README.md 的第 9 行都因此躲过检查）。表里有没有这一行是另一件事，一起查。
    for doc in files:
        rel = doc.relative_to(ROOT)
        for lineno, line in strip_code_blocks(doc.read_text(encoding="utf-8")):
            for m in re.finditer(r"(\d+)\s*个顶层脚本", line):
                got = int(m.group(1))
                seg = f"{rel}:{lineno}  「{m.group(0)}」写 {got}，实测 tools/ 顶层脚本 {len(scripts)} 个"
                (bad if got != len(scripts) else ok).append(seg)
    bad += [f"tools/README.md  分类表里没有 `{x}` 这一行（脚本存在但没登记）" for x in missing]
    if missing:
        bad.append(f"口径：tools/ 顶层脚本 {len(scripts)} 个，缺 {len(missing)} 行")
    return bad, ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="只查这些文件/目录（默认全仓 md）")
    ap.add_argument("--max-lag", type=int, default=0, help="校对戳允许的滞后（补丁版个数）")
    ap.add_argument("--numbers", action="store_true", help="只查硬数字")
    ap.add_argument("--stamps", action="store_true", help="只查校对戳")
    ap.add_argument("--citations", action="store_true", help="只查引用内容")
    ap.add_argument("--defscan", action="store_true",
                    help="返工用：额外报「窗口里只有调用、没有定义行」的引用（软提示，不影响退出码）")
    args = ap.parse_args()
    global DEFSCAN
    DEFSCAN = args.defscan

    if args.paths:
        files: list[pathlib.Path] = []
        for p in args.paths:
            path = (ROOT / p).resolve()
            files += [path] if path.is_file() else sorted(path.rglob("*.md"))
    else:
        files = sorted(
            f for d in ("docs", "tools", ".github") for f in (ROOT / d).rglob("*.md")
        ) + [ROOT / n for n in ("README.md", "README_en.md", "AGENTS.md", "CHANGELOG.md",
                                "hancast-backend/README.md", "rule-packs/README.md")]
    files = [f for f in files if f.is_file() and "node_modules" not in str(f)]

    only = args.numbers or args.stamps or args.citations
    fails: list[str] = []
    warns: list[str] = []
    soft: list[str] = []
    if not only or args.citations:
        hard, soft = check_citations(files)
        fails += [f"内容不符: {x}" for x in hard]
    if args.defscan:
        print(f"\ndefscan（软提示：窗口里只有调用/子串，没有定义行）{len(soft)} 条")
        for x in soft:
            print("  SOFT", x)
    if not only or args.stamps:
        for x in check_stamps(files, args.max_lag):
            if x.startswith("WARN"):
                warns.append(x[5:].strip())
            else:
                fails.append(x[5:].strip() if x.startswith("FAIL") else x)
    if not only or args.numbers:
        bad, ok = check_numbers(files)
        tb, tok = check_tools_inventory(files)
        bad += tb
        ok += tok
        fails += [f"数字不符: {x}" for x in bad]
        print(f"数字校验：{len(ok)} 处与代码一致，{len(bad)} 处不符")
        if args.numbers:
            for x in ok:
                print("  OK  ", x)

    print(f"\n检查 {len(files)} 份文档：FAIL {len(fails)}，WARN {len(warns)}")
    for x in fails[:60]:
        print("  FAIL", x)
    if len(fails) > 60:
        print(f"  …另有 {len(fails) - 60} 条")
    for x in warns[:20]:
        print("  WARN", x)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
