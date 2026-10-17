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

/**
 * 从 build_sidecar.py 读出 Nuitka 点名打包的 Python 包（--include-package=X）。
 *
 * 为什么要在资源校验里管这个：bundle.resources 是三端手写的白名单，
 * Nuitka 把依赖「提升」到 src-tauri/ 根之后，只有列进白名单的目录才会进 .app。
 * v2.3.7 就栽在这：curl_cffi 的指纹二进制 `_wrapper.abi3.so` 在 curl_cffi/ 目录里，
 * 顶层 `*.so` 抓不到它，于是产物里只剩 `_cffi_backend.so`——import 失败、
 * 代码按设计降级回 requests，表现是「解析一切正常，但 JA3 还是 Python 的」，
 * 不看 bundle 里的文件永远发现不了。所以这里把「产出里有这个目录」和
 * 「白名单里有这个目录」强制对上，不让再加一个二进制包时漏改某个平台。
 */
function includePackages() {
  const py = path.resolve(__dirname, '..', 'hancast-backend', 'scripts', 'build_sidecar.py');
  if (!fs.existsSync(py)) return [];
  const src = fs.readFileSync(py, 'utf8');
  const found = new Set();
  for (const m of src.matchAll(/--include-package=([A-Za-z0-9_.-]+)/g)) found.add(m[1]);
  return [...found].sort();
}

function checkBinaryPackages(resources) {
  const listed = new Set(
    resources
      .filter((p) => p.endsWith('/**/*'))
      .map((p) => p.split('/')[0])
  );
  let failed = 0;
  for (const pkg of includePackages()) {
    const { files } = match(`${pkg}/**/*`);
    if (files.length === 0) {
      // 目录形态不存在：纯 Python 会被编进可执行文件，只有顶层 .so/.pyd 的靠 *.so / *.pyd 收
      console.log(`  ok       ${pkg}  -> 产出无独立目录`);
      continue;
    }
    if (!listed.has(pkg)) {
      failed += 1;
      console.error(`  MISSING  ${pkg}/**/*  -> 产出里有 ${files.length} 个文件，但 bundle.resources 没列它`);
      console.error(`           这些文件不会进安装包，运行时 import 失败后代码会静默降级。`);
      continue;
    }
    console.log(`  ok       ${pkg}  -> ${files.length} file(s) 已列入 resources`);
  }
  return failed;
}

/**
 * Linux 专属结构判据。为什么要有这一条：v2.3.16 的 Linux 构建在 CI 上是【全绿】的
 * （所有 glob 都命中、pacman 也装上了），但装到 CachyOS 上双击就崩——
 * 因为 tauri 的 Linux bundler 把 externalBin 装进 /usr/bin、resources 装进
 * /usr/lib/HanCast，而 Nuitka standalone 只从「可执行文件所在目录」解析依赖：
 *   error while loading shared libraries: libpython3.12.so.1.0
 *   ImportError: /usr/bin/lxml/etree.so: cannot open shared object file
 * sidecar 退出码 127。glob 命中数量看不出这种错，只能直接验布局。
 * 修法见 build_sidecar.py 的 flatten_dependencies(copy_executable=True)
 * 与 tauri.linux.conf.json（externalBin 置空 + resources 列出 hancast-sidecar）。
 */
function checkLinuxSidecarLayout(resources) {
  if (PLATFORM !== 'linux') return 0;
  let failed = 0;

  const exe = path.join(TAURI_DIR, 'hancast-sidecar');
  const st = statOf(exe);
  if (!st) {
    failed += 1;
    console.error('  MISSING  hancast-sidecar  -> src-tauri/ 根没有 sidecar 可执行文件');
    console.error('           先跑 npm run build:sidecar；Linux 上它会连 exe 一起提升');
  } else if (!st.isFile()) {
    failed += 1;
    console.error('  MISSING  hancast-sidecar  -> 那是目录不是文件，bundle.resources 会丢掉它');
    console.error('           Linux 的 Nuitka dist 应该落在 src-tauri/target/sidecar-dist/');
  } else if (!(st.mode & 0o111)) {
    failed += 1;
    console.error(`  MISSING  hancast-sidecar  -> mode ${(st.mode & 0o777).toString(8)} 没有可执行位`);
    console.error('           Rust 的 find_sidecar_binary 会跳过无 x 位的候选');
  } else {
    console.log(`  ok       hancast-sidecar  -> exe (${(st.size / 1048576).toFixed(1)} MiB, mode ${(st.mode & 0o777).toString(8)})`);
  }

  if (!resources.includes('hancast-sidecar')) {
    failed += 1;
    console.error('  MISSING  resources 没列出 hancast-sidecar  -> sidecar 进不了包');
  }

  const overlay = readJson(PLATFORM_FILE.linux);
  const externalBin = overlay?.bundle?.externalBin;
  if (!Array.isArray(externalBin) || externalBin.length !== 0) {
    failed += 1;
    console.error('  MISSING  tauri.linux.conf.json 的 bundle.externalBin 必须是空数组');
    console.error('           非空就会在 /usr/bin 再放一份与依赖拆开的 sidecar（启动即 127）');
  }

  return failed;
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

  // 白名单之外还要管「二进制包目录有没有被列进白名单」——见 checkBinaryPackages 的注释
  const pkgFailed = checkBinaryPackages(resources);
  // Linux 还要管「sidecar 与依赖是不是同级」——见 checkLinuxSidecarLayout 的注释
  const layoutFailed = checkLinuxSidecarLayout(resources);

  if (failed > 0 || pkgFailed > 0 || layoutFailed > 0) {
    const distDir =
      PLATFORM === 'linux' ? path.join(TAURI_DIR, 'target', 'sidecar-dist') : path.join(TAURI_DIR, 'hancast-sidecar');
    console.error(
      `\n${failed + pkgFailed + layoutFailed} resource check(s) failed. tauri build WILL fail (or silently drop a dependency).`
    );
    dump('src-tauri/ top level:', fs.readdirSync(TAURI_DIR).sort());
    dump(`Nuitka dist (${path.relative(path.resolve(__dirname, '..'), distDir)}/):`, isDir(distDir) ? fs.readdirSync(distDir).sort() : []);
    process.exit(1);
  }

  console.log('\nAll resource globs matched at least one file, and every binary package dir is listed.');
  if (warned > 0) {
    console.warn(`${warned} glob(s) matched directories that tauri will silently drop.`);
  }
}

main();
