#!/usr/bin/env node
/**
 * 把 Tauri 产出的 .deb 转成 Arch 的 .pkg.tar.zst，并在同一次容器调用里用 pacman 验收。
 *
 * 为什么需要它：
 *   Tauri v2 的 Linux bundler 只有 deb / rpm / AppImage 三种，`bundle.targets: "all"`
 *   永远不会产出 pacman 能装的包（crates/tauri-bundler/src/bundle/linux/ 下只有
 *   appimage/、debian.rs、rpm.rs）。仓库里也没有任何打包工具能替我们做这件事，
 *   所以只能自己补一步——但补在【构建之后】而不是【重做一遍安装布局】。
 *
 * 为什么以 .deb 为输入，而不是照着 bundle.resources 自己拼目录：
 *   deb 的 data.tar.gz 就是 tauri-bundler 里 debian::generate_data 算好的安装树
 *   （/usr/bin/HanCast、/usr/bin/hancast-sidecar、/usr/lib/HanCast/**、hicolor 图标、
 *   .desktop）。自己按 glob 重拼等于复刻那份布局逻辑，改了 bundle.resources 不会同步报错，
 *   只会打出一个缺文件的包。复用 deb 则 deb 对了包就对了。
 *
 * 为什么进容器跑 makepkg，而不是手写 .PKGINFO + tar+zstd：
 *   .PKGINFO/.MTREE 的字段和压缩约定是 pacman 的对外契约；手写最坏的情况不是装不上
 *   （装不上看得见），而是 `pacman -Qkk` 永远报「missing MTREE data」这类静默劣化。
 *   用 Arch 自己的 makepkg 生成，契约由工具链保证。
 *
 * 验收是同一条命令的一部分（可用 --no-verify 关掉）：
 *   1. `pacman -Si` 逐个解析 depends —— 证明包名在 Arch 仓库里真的存在（写错依赖名
 *      是 Arch 包最常见的翻车方式，deb 的 `libwebkit2gtk-4.1-0` 直接抄过来在 Arch 不存在）。
 *   2. `pacman -U` 真装进容器，再 `pacman -Qkk` 校验 .MTREE 与实际文件一致、
 *      `pacman -Qo` 证明 /usr/bin/HanCast 归属本包。
 *   这里会把依赖真装上：原打算加 --nodeps 跳过，实测 pacman 7.1 的 -U 不吃这一套
 *   （照样 resolve 出 124 个包并安装，CI 一次 9 秒、几百 MB 从镜像站拉完），
 *   既然跳不过就顺势当成证据——Depends 名不但在仓库里存在，还真的能被解析并装进系统。
 *
 * 用法：
 *   node scripts/build-arch-pkg.cjs                      # 自动找 bundle/deb/*.deb
 *   node scripts/build-arch-pkg.cjs --deb <path> --out <dir>
 *   ARCH_ZSTD_LEVEL=12 node scripts/build-arch-pkg.cjs   # 本地冒烟用（默认 19，与官方包一致）
 * 其他：--image <ref> --arch <pkgarch> --keep-stage --no-verify
 *
 * 前置：docker（CI 的 ubuntu-latest 自带；本地 OrbStack/Docker Desktop 任一在跑即可）。
 *
 * 镜像默认 ghcr.io/archlinux/archlinux:base-devel，两处都是实测出来的：
 *   1. 用 ghcr 而不是 docker.io：同名 tag 内容逐字节相同（config digest
 *      sha256:689ecdd6…，层 db96cd089ee2 + db0a66162ecf，2026-10-16 核对 manifest），
 *      但 docker.io 对匿名拉取有配额，共享 runner 出口 IP 撞上限时整条 Linux 构建会连带失败；
 *      ghcr 与 runner 同源，不受该配额影响。
 *   2. 用 base-devel 而不是 latest：makepkg 需要 fakeroot，而 fakeroot 在 base-devel 里，
 *      纯 :latest 镜像进来就得先 `pacman -Sy base-devel`（多一次联网、多一个失败面）。
 *      脚本里仍保留缺 fakeroot 时现补的兜底，所以 --image …:latest 也能跑。
 * 换镜像源/回到 docker.io 用 --image 或 ARCH_PKG_IMAGE（环境变量优先，方便 CI 一处改全局）。
 *
 * 本地跑注意：Apple Silicon 上这是 x86_64 容器，走 Rosetta 模拟，
 * pacman 7 的下载沙箱会报「error restricting syscalls via seccomp: 22」——
 * 那是模拟层不支持在已有 seccomp 过滤器上再收窄，不是包的问题（原生 Linux 上同一条命令正常）。
 * 因此本地能验到「makepkg 出包 + .PKGINFO/.MTREE + 归档属主」，
 * 装端（pacman -U/-Qkk）交给 CI；只跑到出包用 --no-verify。
 */
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const REPO = path.resolve(__dirname, '..');

function arg(flag, fallback) {
  const i = process.argv.indexOf(flag);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
const has = (flag) => process.argv.includes(flag);

// Arch 侧的运行期依赖：由 deb 的 Depends 逐个映射而来，映射关系
//   libgtk-3-0             -> gtk3
//   libwebkit2gtk-4.1-0    -> webkit2gtk-4.1
//   libayatana-appindicator3-1 -> libayatana-appindicator
// hicolor-icon-theme 是 Arch 侧约定（deb 靠 dpkg trigger 隐式满足，Arch 要显式写）。
// 名字对不对不靠推断——verify 阶段用 `pacman -Si` 真查一遍仓库。
const DEPENDS = ['gtk3', 'webkit2gtk-4.1', 'libayatana-appindicator', 'hicolor-icon-theme'];

const PKGDESC =
  'HanCast: A cross-platform media casting application (fork of xfangfang/Macast)';
const PKGURL = 'https://github.com/pingod/HanCast';
const MAINTAINER = 'lanzeweie@foxmail.com';

function fail(msg) {
  console.error(`\n[arch-pkg] ERROR: ${msg}`);
  process.exit(1);
}

function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { stdio: opts.stdio ?? 'inherit', cwd: opts.cwd, env: opts.env });
  if (r.error) fail(`${cmd} failed to start: ${r.error.message}`);
  if (r.status !== 0) fail(`${cmd} ${args.join(' ')} exited with ${r.status}`);
  return r;
}

/** 版本 SSOT 仍是 package.json；这里只读，不改任何契约点。 */
function readVersion() {
  const v = JSON.parse(fs.readFileSync(path.join(REPO, 'package.json'), 'utf8')).version;
  if (!v || !/^\d+(\.\d+)*$/.test(v)) fail(`bad version from package.json: ${v}`);
  return v;
}

/** pkgver 只能是 [A-Za-z0-9_.+] 且必须以数字开头；deb 里的 2.3.15 天然满足。 */
function toPkgver(version) {
  const v = version.replace(/[^A-Za-z0-9_.+]/g, '.');
  if (!/^[0-9][A-Za-z0-9_.+]*$/.test(v)) fail(`cannot derive a valid pkgver from ${version} (got ${v})`);
  return v;
}

function findDeb(version, pkgarch) {
  if (arg('--deb')) {
    const p = path.resolve(process.cwd(), arg('--deb'));
    if (!fs.existsSync(p)) fail(`--deb path does not exist: ${p}`);
    return p;
  }
  const dir = path.join(REPO, 'src-tauri', 'target', 'release', 'bundle', 'deb');
  if (!fs.existsSync(dir)) fail(`no deb bundle dir at ${dir} — run the tauri build first, or pass --deb`);
  // Tauri 的 deb 命名是 <product>_<version>_<debarch>.deb；容忍大小写与旧命名，
  // 按 mtime 取最新，避免这里再硬编码一份会漂移的文件名。
  const cands = fs
    .readdirSync(dir)
    .filter((f) => f.toLowerCase().endsWith('.deb'))
    .map((f) => path.join(dir, f))
    .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
  if (!cands.length) fail(`no *.deb found under ${dir}`);
  const picked = cands.find((p) => p.includes(`_${pkgarch === 'x86_64' ? 'amd64' : pkgarch}`)) ?? cands[0];
  console.log(`[arch-pkg] using deb: ${picked}`);
  return picked;
}

function pkgbuild({ pkgver, pkgrel, debName, licenseName, pkgarch }) {
  return `# Generated by scripts/build-arch-pkg.cjs — do not edit by hand.
# 输入是 tauri-bundler 产出的 .deb，安装树原样搬过来，本文件只负责元数据与解包。
pkgname=hancast
pkgver=${pkgver}
pkgrel=${pkgrel}
pkgdesc="${PKGDESC}"
arch=('${pkgarch}')
url="${PKGURL}"
license=('GPL-3.0-or-later')
maintainer="${MAINTAINER}"
depends=(${DEPENDS.map((d) => `'${d}'`).join(' ')})
source=('${debName}' '${licenseName}')
sha256sums=('SKIP' 'SKIP')
# !strip : 产物里的 ELF 本来就没有 .debug 段（Nuitka/dpkg 侧已处理），再 strip 只是白等
# !debug : 不拆 debug 子包，否则每次 CI 多一个没用的 hancast-debug 包
# !emptydirs / !libtool / !staticlibs : 默认清理对本包无意义（没有 .la/.a/空目录）
options=('!strip' '!debug' '!emptydirs' '!libtool' '!staticlibs')

package() {
  # --noextract 下 makepkg 不把源文件拷进 \$srcdir，文件的实际位置随版本在
  # \$srcdir / \$startdir / \$SRCDEST 之间漂（本地实测：srcdir 存在但里面没有 deb）。
  # 按候选目录找一次，找到才算开始，找不到就失败——不要静默用错的那份。
  local s="" d
  for d in "\$srcdir" "\$startdir" "\${SRCDEST:-}"; do
    [ -n "\$d" ] && [ -f "\$d/${debName}" ] && { s="\$d"; break; }
  done
  [ -n "\$s" ] || { echo "FATAL: ${debName} not found under \$srcdir/\$startdir/\$SRCDEST"; return 1; }
  echo "  sources from \$s"

  # 解包落在临时目录，不写回挂载点（那是宿主的暂存目录）。
  # '*data.tar.gz' 同时兼容 ar 成员名写成 "data.tar.gz" 与 "./data.tar.gz" 两种形态。
  local tmp; tmp="\$(mktemp -d)"
  bsdtar -x -f "\$s/${debName}" -C "\$tmp" '*data.tar.gz'
  bsdtar -x -f "\$tmp/data.tar.gz" -C "\$pkgdir"
  rm -rf "\$tmp"

  # deb 里根本不带 license 文件（Tauri 的 debian 布局没有 /usr/share/licenses），
  # Arch 侧这是打包规范里的硬要求，从仓库根的 LICENSE 补一份。
  install -Dm644 "\$s/${licenseName}" "\$pkgdir/usr/share/licenses/\$pkgname/COPYING"
}
`;
}

function containerScript({ debName, pkgarch, zstdLevel, verify }) {
  return `set -euo pipefail
cd /work

# makepkg 除了 pacman 还要 fakeroot（在 base-devel 里）；纯 archlinux/archlinux:latest
# 镜像没有，所以这里现补，而不是把默认镜像锁死成 base-devel——镜像可覆盖时这条兜底
# 比「换个 tag」好维护，CI 用的默认镜像本来就自带。
if ! command -v makepkg >/dev/null || ! command -v fakeroot >/dev/null; then
  echo "== installing base-devel (image has no makepkg/fakeroot) =="
  pacman -Sy --noconfirm --disable-download-timeout base-devel
fi

command -v makepkg >/dev/null || { echo "makepkg missing (pacman package)"; exit 1; }
command -v fakeroot >/dev/null || { echo "fakeroot missing (base-devel)"; exit 1; }
command -v bsdtar  >/dev/null || { echo "bsdtar missing (libarchive, pacman dep)"; exit 1; }
command -v zstd    >/dev/null || { echo "zstd missing"; exit 1; }

# makepkg 拒绝以 root 运行，所以固定落到一个非特权用户上。
# ⚠️ 这一步会把挂载进去的暂存目录属主从调用者（宿主用户）抢走：容器里的 build 是
# uid 1000，而 GitHub runner 的宿主 uid 也是 1000 吗——实测不是，容器结束后宿主侧
# readdirSync 直接 EACCES（CI 第一次跑就是这么红的：包已经出好、装端四条全绿，
# 死的却是收尾清理）。所以退出时必须按传进来的 HOST_UID/HOST_GID 还原，
# 而且要挂在 EXIT trap 上——构建失败时同样得把目录还给宿主，否则 rmSync 连
# 清理都清不掉，/tmp 里会留下一堆别人删不了的目录。
id build >/dev/null 2>&1 || useradd -m -u 1000 build
restore() {
  chown -R "\${HOST_UID:-0}:\${HOST_GID:-0}" /work 2>/dev/null || true
  chmod -R u+rwX,go+rX /work 2>/dev/null || true
}
trap restore EXIT
chown -R build:build /work

# zstd 级别可覆盖：默认 19 与官方仓库一致，本地冒烟用 12 换取时间。
# PACKAGER 必须显式给：否则 .PKGINFO 里是 "Unknown Packager"，
# 官方仓库的包都有这一行，缺了它 pkginfo 的读者（aurutils / octopi 之类）会显示空。
cat > /home/build/.makepkg.conf <<'MK'
PACKAGER="HanCast CI <${MAINTAINER}>"
COMPRESSZSTD=(zstd -T0 -${zstdLevel})
MK
chown build:build /home/build/.makepkg.conf

# --nodeps：本包的 package() 只是搬运 deb 的安装树，构建期不需要 gtk/webkit 在场；
# 让 makepkg 查本地数据库只会逼我们在构建前跑一次 -Sy（而依赖名是否真的存在，
# 由下面 verify 里的 pacman -Si 逐条判，那个才是有效的检查）。
su - build -c 'cd /work && makepkg -f --nodeps --skipchecksums --noextract' 2>&1 | tail -40

pkg="$(ls -1 hancast-*.pkg.tar.zst | head -1)"
[ -n "$pkg" ] || { echo "makepkg produced no .pkg.tar.zst"; exit 1; }
echo "== produced: $pkg ($(stat -c %s "$pkg") bytes) =="

# 结构自查：.PKGINFO 必须在归档根，.MTREE 决定 -Qkk 能不能真校验
echo "== archive metadata entries =="
bsdtar -tf "$pkg" | grep -E '^\\.[A-Z]' || { echo "FATAL: no .PKGINFO/.MTREE in package"; exit 1; }
bsdtar -xOf "$pkg" .PKGINFO | sed 's/^/  PKGINFO: /'

${verify ? `
set -euo pipefail
pacman -Sy --noconfirm --disable-download-timeout >/dev/null 2>&1 || pacman -Sy --noconfirm

echo "== depends resolve in Arch repos =="
for d in ${DEPENDS.join(' ')}; do
  line="$(pacman -Si --noconfirm "$d" 2>/dev/null | awk -F' : ' '/^Name|^Version/{printf "%s=%s ", $1, $2}')"
  [ -n "$line" ] || { echo "FATAL: depends entry not found in repos: $d"; exit 1; }
  echo "  ok  $line"
done

echo "== pacman -U (real install, deps resolved & installed from Arch repos) =="
# 不加 --nodeps：实测 pacman 7.1 的 -U 依然会解析并安装依赖，加了是假开关（见文件头注释）
pacman -U --noconfirm "/work/$pkg"

echo "== integrity / ownership =="
pacman -Q hancast
# -Qkk 拿 .MTREE 对比实际落盘文件；MTREE 缺失或不符都会在这里露出来
if ! pacman -Qkk hancast; then
  echo "FATAL: pacman -Qkk reported problems"; exit 1
fi
pacman -Qo /usr/bin/HanCast
test -x /usr/bin/hancast-sidecar && echo "  ok  /usr/bin/hancast-sidecar executable"
test -f /usr/lib/HanCast/mpv/mpv && echo "  ok  bundled mpv present"
test -f /usr/share/applications/HanCast.desktop && echo "  ok  desktop entry present"
test -f /usr/share/licenses/hancast/COPYING && echo "  ok  license file present"

# makepkg 以非 root 运行，归档里的 uid/gid 是否被重映射成 0 必须实测：
# 若写成 build(1000)，真装到用户机器上 /usr/bin/HanCast 就成了普通用户所有。
echo "== recorded owners after install =="
for f in /usr/bin/HanCast /usr/bin/hancast-sidecar /usr/lib/HanCast/mpv/mpv; do
  owner="$(stat -c '%U:%G' "$f")"
  echo "  $f -> $owner"
  [ "$owner" = "root:root" ] || { echo "FATAL: $f owned by $owner, package must record uid/gid 0"; exit 1; }
done

echo "== uninstall round-trip =="
pacman -Rn --noconfirm hancast >/dev/null && echo "  ok  pacman -R clean"
` : `echo "(verify skipped)"
`}
echo "== done (arch: ${pkgarch}) =="
`;
}

function main() {
  const version = readVersion();
  const pkgver = toPkgver(version);
  const pkgrel = '1';
  const pkgarch = arg('--arch', 'x86_64');
  const image =
    process.env.ARCH_PKG_IMAGE ||
    arg('--image', 'ghcr.io/archlinux/archlinux:base-devel');
  const zstdLevel = process.env.ARCH_ZSTD_LEVEL || '19';
  const verify = !has('--no-verify');

  const debPath = findDeb(version, pkgarch);
  const debName = path.basename(debPath);
  const licenseSrc = path.join(REPO, 'LICENSE');
  if (!fs.existsSync(licenseSrc)) fail(`LICENSE not found at ${licenseSrc}`);

  // 落点政策（AGENTS.md「临时落点二选一」）：即抛件走 /tmp 显式路径，由本脚本自己清。
  // 裸 os.tmpdir() 在 macOS 是 $TMPDIR 的 /var/folders/… 随机路径——用户找不到、
  // 系统清扫又不认，v2.3.15 刚把这类落点全仓清零，这里不能重新长出来一个。
  const stage = fs.mkdtempSync(
    path.join(process.platform === 'win32' ? os.tmpdir() : '/tmp', `hancast-arch-${version}-`)
  );
  const outDir = path.resolve(
    process.cwd(),
    arg('--out', path.join(REPO, 'src-tauri', 'target', 'release', 'bundle', 'arch'))
  );

  console.log(`[arch-pkg] version=${version} pkgver=${pkgver} pkgrel=${pkgrel} arch=${pkgarch}`);
  console.log(`[arch-pkg] stage=${stage}`);
  console.log(`[arch-pkg] image=${image} zstd=${zstdLevel} verify=${verify}`);

  fs.copyFileSync(debPath, path.join(stage, debName));
  fs.copyFileSync(licenseSrc, path.join(stage, 'LICENSE'));
  fs.writeFileSync(path.join(stage, 'PKGBUILD'), pkgbuild({ pkgver, pkgrel, debName, licenseName: 'LICENSE', pkgarch }));
  fs.writeFileSync(path.join(stage, 'ci-arch-build.sh'), containerScript({ debName, pkgarch, zstdLevel, verify }));

  try {
    run('docker', [
      'run',
      '--rm',
      // deb 是 amd64 产物，容器必须按 x86_64 跑，否则 Apple Silicon 上会拉 arm64 镜像
      '--platform',
      pkgarch === 'x86_64' ? 'linux/amd64' : `linux/${pkgarch === 'aarch64' ? 'arm64' : pkgarch}`,
      // pacman 7 下载时会给自己的子进程挂 seccomp 过滤器（"restricting syscalls via seccomp"），
      // 而 Docker 默认的 seccomp profile 不允许在已有过滤器之上再收窄——
      // 不解除就报 "error restricting syscalls via seccomp: 22 / switching to sandbox
      // user 'alpm' failed"，`pacman -Sy` 直接失败，后面的 -U/-Qkk 全无从谈起。
      // 只对这一条一次性构建容器放开，容器里跑的是本仓生成的 PKGBUILD 与 makepkg，
      // 不接触任何密钥或宿主目录（唯一挂载点是暂存目录）。
      '--security-opt',
      'seccomp=unconfined',
      // 容器里 chown 过暂存目录，退出时靠这两个值还原给真正的调用者（见容器脚本的 trap）
      '-e',
      `HOST_UID=${process.getuid ? process.getuid() : 0}`,
      '-e',
      `HOST_GID=${process.getgid ? process.getgid() : 0}`,
      '-v',
      `${stage}:/work`,
      '-w',
      '/work',
      image,
      'bash',
      '/work/ci-arch-build.sh',
    ]);

    const produced = fs.readdirSync(stage).filter((f) => /^hancast-.*\.pkg\.tar\.zst$/.test(f));
    if (!produced.length) fail(`no .pkg.tar.zst in stage dir after container run: ${stage}`);
    if (produced.length > 1) console.warn(`[arch-pkg] note: several packages produced, taking ${produced[0]}`);

    fs.mkdirSync(outDir, { recursive: true });
    const dest = path.join(outDir, produced[0]);
    fs.copyFileSync(path.join(stage, produced[0]), dest);
    const bytes = fs.statSync(dest).size;
    console.log(`\n[arch-pkg] wrote ${path.relative(REPO, dest)} (${(bytes / 1048576).toFixed(1)} MiB)`);
  } finally {
    if (has('--keep-stage')) console.log(`[arch-pkg] stage kept at ${stage}`);
    else fs.rmSync(stage, { recursive: true, force: true });
  }
}

main();
