#!/usr/bin/env node
/**
 * sync-version.cjs
 * 以 package.json 为单一真相源（SSOT），同步版本号到所有项目文件。
 *
 * 用法：
 *   node scripts/sync-version.cjs          # 同步当前版本
 *   node scripts/sync-version.cjs 2.0.0    # 设置新版本并同步
 */

const fs = require('fs')
const path = require('path')

const ROOT = path.resolve(__dirname, '..')

// ── 读取当前版本 ──
const pkgPath = path.join(ROOT, 'package.json')
const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf-8'))
const oldVersion = pkg.version

// ── 确定目标版本 ──
const newVersion = process.argv[2] || oldVersion

if (!/^\d+\.\d+\.\d+/.test(newVersion)) {
  console.error(`❌ 无效版本号: "${newVersion}"，格式应为 x.y.z`)
  process.exit(1)
}

if (process.argv[2]) {
  // 写回 package.json
  pkg.version = newVersion
  fs.writeFileSync(pkgPath, JSON.stringify(pkg, null, 2) + '\n', 'utf-8')
  console.log(`📝 package.json: ${oldVersion} → ${newVersion}`)
} else {
  console.log(`📦 当前版本: ${newVersion}`)
}

// ── 同步目标 ──
const targets = [
  {
    file: 'src-tauri/tauri.conf.json',
    replace: (content) => content.replace(
      /("version"\s*:\s*")[\d.]+(")/,
      `$1${newVersion}$2`
    ),
  },
  {
    file: 'src-tauri/Cargo.toml',
    replace: (content) => content.replace(
      /^(version\s*=\s*")[\d.]+(")/m,
      `$1${newVersion}$2`
    ),
  },
  {
    file: 'hancast-backend/pyproject.toml',
    replace: (content) => content.replace(
      /^(version\s*=\s*")[\d.]+(")/m,
      `$1${newVersion}$2`
    ),
  },
  {
    file: 'hancast-backend/hancast_sidecar/utils/config.py',
    replace: (content) => content.replace(
      /(self\.version\s*=\s*")[\d.]+(")/,
      `$1${newVersion}$2`
    ),
  },
  {
    file: 'src/api/commands.ts',
    replace: (content) => {
      // 只改 MOCK_SETTINGS 的 version（那是 dev 模式下应用自报的版本）。
      // 这里曾经用全局 /version:\s*'x\.y\.z'/g，每次发布把同一文件里的「规则包版本」
      // （假索引项、mock 插件）一起刷成应用版本——假索引两条都变成同版本，
      // 设置页的「一条可更新」当场失效，包版本成了没人看的垃圾数据。
      content = content.replace(
        /(const MOCK_SETTINGS: AppSettings = \{[\s\S]*?version:\s*')[\d.]+(')/,
        `$1${newVersion}$2`
      )
      // mock check_update 的 current/latest
      content = content.replace(
        /(current:\s*')[\d.]+(')/g,
        `$1${newVersion}$2`
      )
      content = content.replace(
        /(latest:\s*')[\d.]+(')/g,
        `$1${newVersion}$2`
      )
      return content
    },
    // 契约点：漏了就是漏了，别用「已是最新」蒙过去
    expect: (content) =>
      /const MOCK_SETTINGS: AppSettings = \{[\s\S]*?version:\s*'[\d.]+(')/.test(content)
      && /current:\s*'[\d.]+(',)/.test(content),
  },
  {
    file: 'src/stores/settings.ts',
    replace: (content) => content.replace(
      /(version:\s*')[\d.]+(')/,
      `$1${newVersion}$2`
    ),
  },
]

let changed = 0
let missed = 0

for (const target of targets) {
  const filePath = path.join(ROOT, target.file)
  if (!fs.existsSync(filePath)) {
    console.log(`⚠️  跳过 ${target.file}（文件不存在）`)
    continue
  }

  const original = fs.readFileSync(filePath, 'utf-8')
  const updated = target.replace(original)

  if (updated !== original) {
    fs.writeFileSync(filePath, updated, 'utf-8')
    console.log(`✅ ${target.file}`)
    changed++
  } else {
    console.log(`⏭️  ${target.file}（已是最新）`)
  }

  // 替换后仍不含目标形状 ⇒ 这一处根本没被脚本覆盖到（改名/挪位置），
  // 静默漏改会让真机上的「版本号」不再能证明装的是哪版。
  if (target.expect && !target.expect(fs.readFileSync(filePath, 'utf-8'))) {
    console.error(`❌ ${target.file}: 找不到应被刷新的版本字段，脚本正则已失效`)
    missed++
  }
}

if (missed) {
  console.error(`\n💥 ${missed} 个版本契约点没匹配上，未更新任何后续流程`)
  process.exit(1)
}

console.log(`\n🎉 版本同步完成: ${newVersion}，更新了 ${changed} 个文件`)
