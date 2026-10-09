#!/usr/bin/env node
/**
 * 校验 tauri.conf.json 里 bundle.resources 的每个 glob 是否至少匹配到一个【文件】。
 *
 * 为什么需要它：
 *   tauri-build 对【任何】包含 `*` 的 glob，只要匹配结果为空就直接失败：
 *     glob pattern <pattern> path not found or didn't match any files.
 *   而且这个报错发生在 build.rs 阶段，第一个失败的 glob 就会中断，
 *   导致后面的 glob 从未被校验，排查成本很高。
 *
 * 这里严格复刻 tauri-utils 的两套 glob 语义（tauri-utils-2.10.1/src/resources.rs）：
 *   1. glob 不递归：`glob::glob("mpv/*")` 只列 mpv/ 的【直接子项】，
 *      即使 require_literal_separator=false，`*` 也不会跨目录层级。
 *      要递归必须显式写 `**`。这一点用 tauri 真实依赖的 glob crate 验证过。
 *   2. 命中目录会被直接丢弃（resources.rs L195 `if entry.is_dir() { skip }`），
 *      不报错、不递归。
 *   3. 因此 GlobPathNotFound 的触发条件是「一个文件都没匹配到」，
 *      而不是「一个路径都没匹配到」——只匹配到目录同样会构建失败。
 *
 * 第 2 条曾经咬人：`mpv/*` 能看到 macOS 的 mpv.app/ 目录，但 mpv.app 本身是目录，
 * 被静默丢弃，app 打出来没有 mpv；反倒被一起拷进去的 mpv.tar.gz 进了 bundle。
 * 所以这里除了报 MISSING，还会把「命中但被丢弃的目录」单独列出来。
 *
 * 用法：node scripts/check-resources.cjs
 * 指定平台：TAURI_PLATFORM=win32|linux|darwin node scripts/check-resources.cjs
 */
const fs = require('node:fs');
const path = require('node:path');

const TAURI_DIR = path.resolve(__dirname, '..', 'src-tauri');
const PLATFORM = process.env.TAURI_PLATFORM || process.platform;

const PLATFORM_FILE = {
  darwin: 'tauri.macos.conf.json',
  win32: 'tauri.windows.conf.json',
  linux: 'tauri.linux.conf.json',
};

function readJson(file) {
  const full = path.join(TAURI_DIR, file);
  return fs.existsSync(full) ? JSON.parse(fs.readFileSync(full, 'utf8')) : null;
}

/** 与 tauri 一致：平台配置文件里的数组整体替换 base 的数组（不做合并）。 */
function resolveResources() {
  const base = readJson('tauri.conf.json');
  if (!base) throw new Error('src-tauri/tauri.conf.json not found');
  const overlayFile = PLATFORM_FILE[PLATFORM];
  const overlay = overlayFile ? readJson(overlayFile) : null;
  if (overlay?.bundle?.resources) {
    return { resources: overlay.bundle.resources, source: overlayFile };
  }
  return { resources: base.bundle?.resources ?? [], source: 'tauri.conf.json' };
}

/** 单个路径段转正则：`*` 只吃本段，不跨 `/`。 */
function segToRegExp(seg) {
  const escaped = seg.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '[^/]*');
  return new RegExp(`^${escaped}$`);
}

function statOf(p) {
  try {
    return fs.statSync(p);
  } catch {
    return null;
  }
}

function isDir(p) {
  const st = statOf(p);
  return st !== null && st.isDirectory();
}

/**
 * 按 glob 语义遍历，收集所有命中项（目录也收，因为目录会被 tauri 丢弃，
 * 需要单独报出来）。
 */
function walk(current, segs, idx, out) {
  if (idx === segs.length) {
    out.push(current);
    return;
  }
  const seg = segs[idx];
  if (seg === '**') {
    // `**` 可以匹配 0 层
    walk(current, segs, idx + 1, out);
    if (isDir(current)) {
      for (const entry of fs.readdirSync(current)) {
        const child = path.join(current, entry);
        if (isDir(child)) walk(child, segs, idx, out);
      }
    }
    return;
  }
  if (!isDir(current)) return;
  const re = segToRegExp(seg);
  for (const entry of fs.readdirSync(current)) {
    if (re.test(entry)) walk(path.join(current, entry), segs, idx + 1, out);
  }
}

function match(pattern) {
  const segs = pattern.split('/').filter(Boolean);
  const hits = [];
  walk(TAURI_DIR, segs, 0, hits);
  const files = [];
  const dirs = [];
  for (const hit of hits) (isDir(hit) ? dirs : files).push(path.relative(TAURI_DIR, hit));
  return { files, dirs };
}

function dump(label, entries) {
  console.error(`${label}`);
  if (entries.length === 0) {
    console.error('  (missing)');
    return;
  }
  for (const entry of entries.slice(0, 50)) console.error(`  ${entry}`);
  if (entries.length > 50) console.error(`  ... and ${entries.length - 50} more`);
}

function main() {
  const { resources, source } = resolveResources();
  console.log(`platform        : ${PLATFORM}`);
  console.log(`resources source: ${source}`);
  console.log(`resources count : ${resources.length}\n`);

  let failed = 0;
  let warned = 0;
  let bundled = 0;

  const results = resources.map((pattern) => ({ pattern, ...match(pattern) }));
  const allFiles = new Set(results.flatMap((r) => r.files));

  for (const { pattern, files, dirs } of results) {
    if (files.length === 0) {
      // tauri 的 GlobPathNotFound 触发条件：迭代器一个文件都没产出。
      failed += 1;
      const why = dirs.length > 0 ? `only ${dirs.length} dir(s), no file` : '0 matches';
      console.error(`  MISSING  ${pattern}  -> ${why}`);
      continue;
    }

    bundled += files.length;
    console.log(`  ok       ${pattern}  -> ${files.length} file(s)`);

    // tauri 丢弃命中的目录（resources.rs L195）。目录本身丢掉无所谓，
    // 但如果它下面一个文件都不会进 bundle，就说明这里的 glob 写错了
    // —— macOS 漏打 mpv.app 就是这么来的。
    const hasFileUnder = (dir) => {
      const prefix = `${dir}/`;
      for (const file of allFiles) {
        if (file.startsWith(prefix)) return true;
      }
      return false;
    };
    const orphaned = dirs.filter((dir) => !hasFileUnder(dir));
    if (orphaned.length > 0) {
      warned += 1;
      console.warn(`  warn     ${pattern}  -> matched but NOTHING under:`);
      for (const dir of orphaned) {
        console.warn(`             ${dir}/`);
      }
      console.warn('             tauri drops directories, so these will not ship.');
      console.warn(`             Replace \`${pattern}\` with \`${pattern.replace(/\/?\*$/, '')}/**/*\`.`);
    }
  }

  console.log(`\ntotal files that will be bundled: ${bundled}`);

  if (failed > 0) {
    console.error(`\n${failed} resource glob(s) matched no file. tauri build WILL fail.`);
    dump('src-tauri/ top level:', fs.readdirSync(TAURI_DIR).sort());
    dump('Nuitka dist (src-tauri/hancast-sidecar/):', isDir(path.join(TAURI_DIR, 'hancast-sidecar')) ? fs.readdirSync(path.join(TAURI_DIR, 'hancast-sidecar')).sort() : []);
    process.exit(1);
  }

  console.log('\nAll resource globs matched at least one file.');
  if (warned > 0) {
    console.warn(`${warned} glob(s) matched directories that tauri will silently drop.`);
  }
}

main();
