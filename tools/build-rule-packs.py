#!/usr/bin/env python3
"""打包规则包并生成官方索引 ``rule-packs/index.json``。

程序里没有任何编译进去的站点规则，「装包才能解析」是产品契约；这个脚本就是
官方包源的全部生产工具：``rule-packs/<name>/`` → ``rule-packs/dist/<name>-<version>.zip``
+ 一份 ``index.json``（设置页「检查规则包更新」读的就是它）。

用法:
  python3 tools/build-rule-packs.py            # 重建 dist + index.json
  python3 tools/build-rule-packs.py --check    # 只校验产物与源是否一致（CI/验收用）
  python3 tools/build-rule-packs.py --clean    # 删掉 dist 里源目录已不存在的孤儿 zip

zip 用固定时间戳（1980-01-01）与固定条目顺序生成：内容没改 ⇒ sha256 不变 ⇒
index.json 不跟着无意义地跳，``--check`` 才有意义。

索引里的下载地址必须 https（``jsplugins.fetch_index`` 会丢掉 http 条目），
摘要是硬约束：``pull`` 下载后先比 sha256，不匹配就拒装——所以改了包里任何字节
都得重跑这个脚本，否则装包会因摘要不符被拦下。
"""

import argparse
import hashlib
import io
import json
import os
import stat
import sys
import zipfile
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKS_DIR = os.path.join(ROOT, "rule-packs")
DIST_DIR = os.path.join(PACKS_DIR, "dist")
INDEX_PATH = os.path.join(PACKS_DIR, "index.json")

# 产物发布在仓库 main 分支上，raw 地址即官方索引；分支不是 main 时产物装不上，
# 所以这里宁可直接写死，让「换了分支忘了改」当场炸出来而不是静默产出坏索引。
RAW_BASE = "https://raw.githubusercontent.com/pingod/HanCast/main/rule-packs/dist/"

_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_ALLOWED = ("manifest.json", ".js")


class BuildError(Exception):
    pass


def pack_sources() -> list[tuple[str, str]]:
    """``[(包名, 源目录), …]``，按名排序；跳过 dist 自身和非包目录"""
    if not os.path.isdir(PACKS_DIR):
        raise BuildError(f"找不到规则包目录: {PACKS_DIR}")
    out = []
    for name in sorted(os.listdir(PACKS_DIR)):
        d = os.path.join(PACKS_DIR, name)
        if not os.path.isdir(d) or name == "dist":
            continue
        if os.path.exists(os.path.join(d, "manifest.json")):
            out.append((name, d))
    return out


def read_manifest(src_dir: str, dirname: str) -> dict:
    with open(os.path.join(src_dir, "manifest.json"), encoding="utf-8-sig") as f:
        manifest = json.load(f)
    # 目录名必须等于 manifest.name：import_zip_bytes 落盘用的是 manifest.name，
    # 两者不一致时索引里的包名和用户机器上的目录名就对不上了
    if manifest.get("name") != dirname:
        raise BuildError(f"{dirname}: manifest.name={manifest.get('name')!r} 与目录名不符")
    version = str(manifest.get("version") or "")
    if not version:
        raise BuildError(f"{dirname}: manifest.version 不能为空")
    scripts = manifest.get("scripts") or (
        [manifest["main"]] if manifest.get("main") else [])
    if not scripts:
        raise BuildError(f"{dirname}: manifest 没声明任何脚本")
    for s in scripts:
        if not os.path.isfile(os.path.join(src_dir, s)):
            raise BuildError(f"{dirname}: scripts 引用了不存在的文件 {s!r}")
    return manifest


def pack_files(src_dir: str, manifest: dict) -> list[str]:
    """zip 内的文件（相对包目录），顺序固定：manifest 在前，脚本按名排序"""
    files = ["manifest.json"]
    declared = sorted(manifest.get("scripts") or
                      ([manifest["main"]] if manifest.get("main") else []))
    files.extend(declared)
    # 未声明的 .js 也会被导入端收进 zip 白名单，却不会进执行清单——静默丢包不如报错
    for entry in sorted(os.listdir(src_dir)):
        if entry.endswith(".js") and entry not in declared:
            raise BuildError(f"{src_dir}: {entry} 未被 manifest.scripts 声明")
    return files


def build_zip(name: str, src_dir: str, files: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            with open(os.path.join(src_dir, rel), "rb") as f:
                data = f.read()
            info = zipfile.ZipInfo(f"{name}/{rel}", date_time=_ZIP_DATE)
            # 导入端拒收软链接；这里固定成普通文件位，产物就不可能带出软链接
            info.create_system = 3  # unix
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            zf.writestr(info, data)
    return buf.getvalue()


def zip_name(name: str, version: str) -> str:
    return f"{name}-{version}.zip"


def zip_url(filename: str) -> str:
    # 包名允许下划线/连字符，无需转义；真出现空格也不能产出坏地址
    return RAW_BASE + quote(filename)


def scan() -> list[dict]:
    """读源目录，产出 ``[{name, version, filename, files}, …]``"""
    packs = []
    for name, src in pack_sources():
        manifest = read_manifest(src, name)
        packs.append({
            "name": name,
            "version": str(manifest["version"]),
            "description": str(manifest.get("description") or ""),
            "filename": zip_name(name, str(manifest["version"])),
            "files": pack_files(src, manifest),
        })
    return packs


def build(write: bool) -> int:
    os.makedirs(DIST_DIR, exist_ok=True)
    expected: set[str] = set()
    entries, written = [], []
    for pack in scan():
        src = os.path.join(PACKS_DIR, pack["name"])
        blob = build_zip(pack["name"], src, pack["files"])
        digest = hashlib.sha256(blob).hexdigest()
        path = os.path.join(DIST_DIR, pack["filename"])
        expected.add(pack["filename"])
        current = (open(path, "rb").read() if os.path.isfile(path) else None)
        if current != blob:
            written.append(pack["filename"])
            if write:
                with open(path, "wb") as f:
                    f.write(blob)
        entries.append({"name": pack["name"], "version": pack["version"],
                        "url": zip_url(pack["filename"]), "sha256": digest,
                        "description": pack["description"]})

    rendered = json.dumps({"packs": entries}, ensure_ascii=False, indent=2) + "\n"
    existing = (open(INDEX_PATH, encoding="utf-8").read()
                if os.path.isfile(INDEX_PATH) else "")
    orphans = sorted(fn for fn in os.listdir(DIST_DIR)
                     if fn.endswith(".zip") and fn not in expected)

    if not write:
        if not written and existing == rendered and not orphans:
            print(f"规则包产物与索引均已同步（{len(entries)} 个包）")
            return 0
        for fn in written:
            print(f"需要重建: dist/{fn}（源已变，摘要与产物不符）")
        if existing != rendered:
            print("需要重建: index.json（与源不一致）")
        if orphans:
            print(f"孤儿产物: dist/{', '.join(orphans)} → --clean")
        print("→ 跑 python3 tools/build-rule-packs.py")
        return 1

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(rendered)
    for fn in written:
        print(f"写入 dist/{fn}")
    print(f"索引: {os.path.relpath(INDEX_PATH, ROOT)}（{len(entries)} 个包）"
          + ("" if existing == rendered else " [已更新]"))
    for e in entries:
        print(f"  {e['name']} v{e['version']} {e['sha256'][:12]}…")
    if orphans:
        print(f"孤儿产物（源目录已不存在）: {', '.join(orphans)} → --clean")
    return 0


def clean() -> int:
    keep = {pack["filename"] for pack in scan()}
    removed = []
    if os.path.isdir(DIST_DIR):
        for fn in sorted(os.listdir(DIST_DIR)):
            if fn.endswith(".zip") and fn not in keep:
                os.remove(os.path.join(DIST_DIR, fn))
                removed.append(fn)
    print(f"清除孤儿产物: {len(removed)} 个" + (f" {removed}" if removed else ""))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="只校验产物/索引与源是否一致，不写文件")
    ap.add_argument("--clean", action="store_true",
                    help="删掉 dist 里源目录已不存在的 zip")
    args = ap.parse_args(argv)
    try:
        if args.clean:
            return clean()
        return build(write=not args.check)
    except BuildError as e:
        print(f"失败: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
