#!/usr/bin/env python3
"""
文档引用体检：扫描 docs/**.md 与顶层 README/CLAUDE 里的 `file.ext:line` 引用，
校验（1）文件存在（2）行号落在文件实际行数内（3）可选打印被引原文供人工判语义。

三类命中会被放行，各自计入统计并在结尾说明：
  - URL 整段（扫每行前先用 URL_RE 把 http/www 片段挖掉）
  - 运行时/本地产物：名字在 RUNTIME_NAMES 里，或被 `.gitignore` 忽略（`git check-ignore`），
    或落在 Windows 盘符路径里（`D:/Temp/xxx.cmd`——在 .68 上跑的产物，见 WINPATH_RE）
  - 历史论断：同一行带 HIST_MARKERS（「曾」「已删除」「上游」等），说明作者刻意引用
    一个已退役或不在本仓的文件名

用法:
  python3 tools/audit-doc-refs.py                        # 全量
  python3 tools/audit-doc-refs.py docs/故障排查.md --show  # 单份，并打印被引原文
"""
import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict

# `@` 与 `%` 必须在字符类里：`128x128@2x.png`、ffmpeg 的 `seg%03d.ts` 会被截断成假文件名。
# 前置 `(?<!\.)` 避免把 `req.cmd` 这类属性访问当成文件引用。
REF_RE = re.compile(r'(?<!\.)([A-Za-z0-9_\-./@%\u4e00-\u9fff]+\.(?:py|rs|vue|ts|cjs|mjs|sh|md|json|toml|png|html|cmd|ps1|css|conf))(?::(\d+)(?:-(\d+))?)?(?![A-Za-z0-9_])')

# 运行时落盘文件（不在库里，由程序自己生成），提到它们不算引用失效
RUNTIME_NAMES = {'hancast_setting.json', 'cookies.json', 'device_identity.json',
                 'rules.json', 'manifest.json', 'package-lock.json', 'tsconfig.json',
                 'window_config.json', 'state.json', 'knowledge-graph.json',
                 'settings.json', 'config.json'}

# 行内含这些词时，指向不存在的文件是刻意的（历史、上游、或「没有这个文件」的否定句）
HIST_MARKERS = ('曾', '删', '已于', '移除', '退役', '历史', '原 ', '上游',
                '移植', '废弃', '不再', 'Macast 时期', '早期', '重写', '复用到',
                '没有', '不存在', '已被', '取代')

# 依赖包/上游 crate 内部的路径，本来就不在本仓
DEPENDENCY_PREFIXES = ('macast/', 'macast_renderer/', 'tauri-utils',
                       'lxml', 'certifi/', 'charset_normalizer/', 'curl_cffi/',
                       'urllib3/')

# 运行时产物或占位名：HLS 切片、纯 x/数字组成的示例文件名
RUNTIME_BASENAME_RE = re.compile(r'^(?:seg[\w%]*|\d[\dxX%]*|[xX]+)\.(?:ts|png|jpg|jpeg|m4s)$')

# 文档头部（前 15 行）出现这些词 => 整份是历史草案/已取代文档，
# 里面引用「当年设想但从未实现」的文件名是刻意的；只降级不报错，行号越界仍然算缺陷。
SUPERSEDED_MARKERS = ('已被', '取代', '历史', '草案', 'superseded', '不要按它们改代码')


def normalize(name):
    return re.sub(r'^(\.\./|\./|/)+', '', name)


URL_RE = re.compile(r'\S*(?:https?://|www\.)\S*', re.IGNORECASE)

# Windows 盘符路径（D:/Temp/x.cmd、C:\Users\Administrator\...）：跨机器的运行时产物，
# 不是仓库引用。`:` 不在 REF_RE 的字符类里，这类路径会以 `/Temp/x.cmd` 或裸 `x.cmd`
# 的形态被误捕（2026-10-16 实测 tools/README.md:75-76 两处误报，:73 靠行内历史词侥幸放行）；
# 先按整段盘符路径划 span，落进 span 的命中计入 skipped_runtime。
WINPATH_RE = re.compile(r'[A-Za-z]:[\\/][^\s`|]*')


def strip_urls(text):
    """把整段 URL 挖掉再扫——URL 尾巴上的 index.json 不是代码引用"""
    return URL_RE.sub(' ', text)


def build_index(root):
    """文档里出现的裸文件名（basename）-> 仓库内真实路径索引"""
    index = defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ('node_modules', '.git', 'target', '__pycache__',
                                    '.venv', 'dist', 'build', '.next')]
        for fn in filenames:
            index[fn].append(os.path.relpath(os.path.join(dirpath, fn), root))
    return index


def git_ignored(root, name):
    """名字被 .gitignore 忽略 => 本地/运行时产物，不计入缺陷。缓存查询结果。"""
    if name in git_ignored.cache:
        return git_ignored.cache[name]
    try:
        rc = subprocess.run(['git', 'check-ignore', '-q', '--', name],
                            cwd=root, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL).returncode
    except OSError:
        rc = 1
    verdict = (rc == 0)
    git_ignored.cache[name] = verdict
    return verdict


git_ignored.cache = {}


def doc_files(root, explicit):
    if explicit:
        return explicit
    out = []
    for base in ('docs', '.'):
        for dirpath, _, filenames in os.walk(os.path.join(root, base)):
            if dirpath != os.path.join(root, '.') and 'docs' not in dirpath:
                continue
            for fn in filenames:
                if fn.endswith('.md'):
                    p = os.path.relpath(os.path.join(dirpath, fn), root)
                    if p not in out and not p.startswith('docs/._'):
                        out.append(p)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='*')
    ap.add_argument('--show', action='store_true', help='打印每条引用指向的原文')
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    index = build_index(root)
    line_cache = {}

    def lines_of(rel):
        if rel not in line_cache:
            try:
                with open(os.path.join(root, rel), encoding='utf-8') as f:
                    line_cache[rel] = f.read().splitlines()
            except Exception:
                line_cache[rel] = None
        return line_cache[rel]

    total = bad = skipped_url = skipped_runtime = skipped_hist = 0
    for doc in doc_files(root, args.files):
        path = os.path.join(root, doc)
        if not os.path.exists(path):
            print(f'[skip] {doc} 不存在')
            continue
        with open(path, encoding='utf-8') as fh:
            body = fh.read()
        doc_lines = body.splitlines()
        superseded = any(k in '\n'.join(doc_lines[:15]) for k in SUPERSEDED_MARKERS)
        issues = []
        prev = ''
        for lineno, text in enumerate(doc_lines, 1):
            # 长句换行后标记词落在上一行：整段视为同一条论断
            line_markers = HIST_MARKERS
            mark_hit = any(k in text for k in line_markers) or any(k in prev for k in line_markers)
            prev = text if text.strip() else prev
            scannable = strip_urls(text)
            win_spans = [mm.span() for mm in WINPATH_RE.finditer(scannable)]
            for m in REF_RE.finditer(scannable):
                name, start, end = normalize(m.group(1)), m.group(2), m.group(3)
                if any(a <= m.start() < b for a, b in win_spans):
                    skipped_runtime += 1
                    continue
                if name.startswith('.'):
                    skipped_url += 1
                    continue
                base = os.path.basename(name)
                if (name in RUNTIME_NAMES or base in RUNTIME_NAMES
                        or RUNTIME_BASENAME_RE.match(base)
                        or name.startswith(DEPENDENCY_PREFIXES)
                        or git_ignored(root, name)):
                    skipped_runtime += 1
                    continue
                cands = index.get(os.path.basename(name))
                if name != os.path.basename(name):
                    cands = [p for p in (index.get(os.path.basename(name), []))
                             if p == name or p.endswith('/' + name)]
                if not cands:
                    if mark_hit or superseded:
                        skipped_hist += 1
                        continue
                    issues.append(f'{doc}:{lineno} 文件找不到: {name}')
                    bad += 1
                    continue
                rel = cands[0]
                exact = [p for p in cands if p.endswith(name)] or cands
                if not start:
                    if args.show:
                        print(f'  {doc}:{lineno} -> {rel} (整文件)')
                    total += 1
                    continue
                hi = int(end) if end else int(start)
                # 同名文件多处时，只要**任一**候选能容纳该行号就放行——文档常写裸文件名
                ok = False
                chosen = None
                for cand in exact:
                    src = lines_of(cand)
                    if src and 1 <= int(start) <= min(hi, len(src)):
                        ok, chosen = True, cand
                        break
                if not ok:
                    issues.append(f'{doc}:{lineno} 行号越界: {name}:{start}-{end} '
                                  f'(候选 {len(cands)} 个，最长 '
                                  f'{max((len(lines_of(c) or []) for c in cands), default=0)} 行)')
                    bad += 1
                    continue
                total += 1
                if args.show:
                    src = lines_of(chosen)
                    snippet = ' | '.join(src[int(start) - 1:min(hi, len(src))]).strip()[:120]
                    print(f'  {doc}:{lineno} -> {chosen}:{start}  "{snippet}"')
        for msg in issues:
            print('  [BAD] ' + msg)
    print(f'\n合计引用 {total} 处，可疑 {bad} 处。'
          f'（放行：URL 片段与占位名 {skipped_url}、运行时/依赖包内/被 gitignore 的路径 {skipped_runtime}、'
          f'含历史或否定标记的行 {skipped_hist}）')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
