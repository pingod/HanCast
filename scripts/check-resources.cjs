#!/usr/bin/env node
/**
 * 校验 tauri.conf.json 里 bundle.resources 的每个 glob 是否至少匹配到一个文件。
 *
 * 为什么需要它：
 *   tauri-build 对【任何】包含 `*` 的 glob，只要匹配结果为空就直接失败：
 *     glob pattern <pattern> path not found or didn't match any files.
 *   而且这个报错发生在 build.rs 阶段，第一个失败的 glob 就会中断，
 *   导致后面的 glob 从未被校验，排查成本很高。
 *
 * 本脚本在 tauri build 之前按平台做等价解析（base + 平台覆盖文件的数组替换语义），
 * 打印每个 glob 的命中数量；命中为 0 时打印 src-tauri/ 实际内容并以非 0 退出。
 *
 * 用法：node scripts/check-resources.cjs
 */
const fs = require('node:fs');
const path = require('node:path');

const TAURI_DIR = path.resolve(__dirname, '..', 'src-tauri');

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
  const overlayFile = PLATFORM_FILE[process.env.TAURI_PLATFORM || process.platform];
  const overlay = overlayFile ? readJson(overlayFile) : null;
  if (overlay?.bundle?.resources) {
    return { resources: overlay.bundle.resources, source: overlayFile };
  }
  return { resources: base.bundle?.resources ?? [], source: 'tauri.conf.json' };
}

function segToRegExp(seg) {
  const escaped = seg.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '[^/]*');
  return new RegExp(`^${escaped}$`);
}

function isDir(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

function walk(current, segs, idx) {
  if (idx === segs.length) return [current];
  const seg = segs[idx];
  const out = [];
  if (seg === '**') {
    out.push(...walk(current, segs, idx + 1)); // ** 匹配 0 层
    if (isDir(current)) {
      for (const entry of fs.readdirSync(current)) {
        const child = path.join(current, entry);
        if (isDir(child)) out.push(...walk(child, segs, idx)); // ** 吃掉一层
      }
    }
    return out;
  }
  if (!isDir(current)) return out;
  const re = segToRegExp(seg);
  for (const entry of fs.readdirSync(current)) {
    if (re.test(entry)) out.push(...walk(path.join(current, entry), segs, idx + 1));
  }
  return out;
}

function match(pattern) {
  const segs = pattern.split('/').filter(Boolean);
  return walk(TAURI_DIR, segs, 0);
}

function main() {
  const { resources, source } = resolveResources();
  console.log(`platform        : ${process.platform}`);
  console.log(`resources source: ${source}`);
  console.log(`resources count : ${resources.length}\n`);

  let failed = 0;
  for (const pattern of resources) {
    const hits = match(pattern);
    if (hits.length === 0) {
      failed += 1;
      console.error(`  MISSING  ${pattern}  -> 0 matches`);
    } else {
      console.log(`  ok       ${pattern}  -> ${hits.length} match(es)`);
    }
  }

  if (failed > 0) {
    console.error(`\n${failed} resource glob(s) matched nothing. tauri build WILL fail.`);
    console.error('src-tauri/ top level:');
    for (const entry of fs.readdirSync(TAURI_DIR).sort()) {
      console.error(`  ${entry}`);
    }
    console.error('\nNuitka dist (src-tauri/hancast-sidecar/):');
    const dist = path.join(TAURI_DIR, 'hancast-sidecar');
    if (isDir(dist)) {
      for (const entry of fs.readdirSync(dist).sort()) console.error(`  ${entry}`);
    } else {
      console.error('  (missing)');
    }
    process.exit(1);
  }
  console.log('\nAll resource globs matched at least one path.');
}

main();