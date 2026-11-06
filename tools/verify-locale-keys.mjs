#!/usr/bin/env node
/**
 * 文案键闸门：`t('…')` 用到的键必须真的存在于 zh-CN 与 en-US，且两份键集一致
 *
> 为什么要有这条：本仓**从来没有**校验过中英文键集（`tools/check-doc-freshness.py:199,208`
> 只把「zh 键数」当一个硬数字核对），所以漏加英文不会红牌。而漏加**中文**键更糟——
> vue-i18n 拿不到键就把键名原样渲染到界面上。本轮写 `UpdateModal.vue` 时就是这么
> 踩中的：主按钮上直接显示了 `update.cancel` 这七个字符，用户看到的是英文键名。
>
> 判据三条，任一不成立就非 0 退出：
> 1. 静态 `t('a.b')` / `tt('a.b')` 的每个键都在 zh 与 en 里存在；
> 2. 动态形态 `t(\`prefix.${x}\`)` 的每一个可能取值都要有对应键（取值来自代码里的
>    白名单数组，白名单与键表同时校验——只查前缀会漏掉整段缺失）；
> 3. zh 与 en 键集**逐键相等**（顺序不要求）。
>
> 用法: node tools/verify-locale-keys.mjs
> 退出码 0 = 通过
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const SRC = path.join(ROOT, 'src')

let fails = 0
const pass = (n, d = '') => console.log(`  PASS  ${n}${d ? ` (${d})` : ''}`)
const fail = (n, d = '') => { fails++; console.log(`  FAIL  ${n}${d ? ` — ${d}` : ''}`) }

const zh = JSON.parse(readFileSync(path.join(SRC, 'locales/zh-CN.json'), 'utf-8'))
const en = JSON.parse(readFileSync(path.join(SRC, 'locales/en-US.json'), 'utf-8'))

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = path.join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(vue|ts)$/.test(name)) out.push(p)
  }
  return out
}

const files = walk(SRC)

// ① 静态键：t('a.b') / t("a.b") / tt('a.b')
// 收尾必须紧跟 `,` 或 `)`：`t('a.b.' + x)` 这种拼接形态键名是不完整的，
// 归下面的动态前缀判据管（否则它会被当成一个「缺键」误报，而真正的动态键反而没人查）
const STATIC = /\b(?:t|tt)\(\s*(['"])([a-zA-Z0-9_.]+)\1\s*[,)]/g
const staticHits = new Map()   // key -> [file:line]
for (const f of files) {
  const text = readFileSync(f, 'utf-8')
  let m
  STATIC.lastIndex = 0
  while ((m = STATIC.exec(text))) {
    const key = m[2]
    if (!staticHits.has(key)) staticHits.set(key, [])
    const line = text.slice(0, m.index).split('\n').length
    staticHits.get(key).push(`${path.relative(ROOT, f)}:${line}`)
  }
}

// ② 动态形态：t(`prefix.${expr}`) —— 收集 prefix，再按代码里的白名单数组展开
const DYNAMIC = /\b(?:t|tt)\(\s*`([^`$]*)\$\{[^}]*\}`/g
const dynamicPrefixes = new Map()
for (const f of files) {
  const text = readFileSync(f, 'utf-8')
  let m
  DYNAMIC.lastIndex = 0
  while ((m = DYNAMIC.exec(text))) {
    const prefix = m[1]
    if (!prefix.includes('.')) continue          // 纯变量拼出来的（如 t(group + key)）另说
    dynamicPrefixes.set(prefix, path.relative(ROOT, f))
  }
}

// 白名单：代码里用 `X.includes(kind) ? t(\`update.kind.${kind}\`) : kind` 这种写法时，
// 取值集合就写在那个数组里。这里按前缀点名，新增形态就再加一条——宁可显式也别漏判。
const ENUMS = {
  'update.stage.': { source: 'src/components/UpdateModal.vue', values: ['connecting', 'downloading', 'verifying', 'done', 'failed', 'cancelling', 'cancelled'] },
  'update.kind.': { source: 'src/components/UpdateModal.vue', values: ['dmg', 'nsis', 'msi', 'deb', 'rpm', 'arch', 'appimage'] },
  'screencast.deps.downloadStage.': { source: 'src/components/FfmpegDepsModal.vue', values: ['connecting', 'downloading', 'extracting', 'verifying'] },
  'screencast.deps.source.': { source: 'src/components/FfmpegDepsModal.vue', values: ['manual', 'system', 'downloaded'] },
}

// ②b 拼接形态 `t('settings.paramsGroup.' + section)`：取值集合从**代码现场**读，
// 不写死名单——`params.py` 新加一个 section 而没加标题键，这条就会红（以前只能靠人记得）
const paramsPy = readFileSync(path.join(ROOT, 'hancast-backend/hancast_sidecar/params.py'), 'utf-8')
const sections = [...new Set([...paramsPy.matchAll(/"section":\s*"([a-z_]+)"/g)].map((m) => m[1]))].sort()
const GROUP_PREFIX = 'settings.paramsGroup.'
if (sections.length) {
  const lack = sections.filter((x) => !(GROUP_PREFIX + x in zh) || !(GROUP_PREFIX + x in en))
  if (lack.length) fail(`${GROUP_PREFIX}* 缺分组标题`, lack.join(', '))
  else pass(`${GROUP_PREFIX}* 与 params.py 的 section 逐一对上`, `${sections.length} 组：${sections.join('/')}`)
} else {
  fail('读不到 params.py 的 section 清单', '判据失效，宁可红牌')
}

const missingZh = [...staticHits.keys()].filter((k) => !(k in zh))
const missingEn = [...staticHits.keys()].filter((k) => !(k in en))
if (missingZh.length) fail('zh-CN 缺静态键', missingZh.map((k) => `${k} ← ${staticHits.get(k)[0]}`).join(' ; '))
else pass(`zh-CN 覆盖全部静态键（${staticHits.size} 个）`)
if (missingEn.length) fail('en-US 缺静态键', missingEn.map((k) => `${k} ← ${staticHits.get(k)[0]}`).join(' ; '))
else pass(`en-US 覆盖全部静态键（${staticHits.size} 个）`)

for (const [prefix, src] of dynamicPrefixes) {
  const spec = ENUMS[prefix]
  if (!spec) {
    fail('动态键前缀没有白名单', `${prefix} (${src}) —— 在 ENUMS 里补一条，判据才不会漏`)
    continue
  }
  const lackZh = spec.values.filter((v) => !(prefix + v in zh))
  const lackEn = spec.values.filter((v) => !(prefix + v in en))
  if (lackZh.length || lackEn.length) {
    fail(`动态键 ${prefix}* 有缺失`, `zh:${lackZh.join(',')} en:${lackEn.join(',')}`)
  } else {
    pass(`动态键 ${prefix}* 全齐`, `${spec.values.length} 个取值`)
  }
}

const onlyZh = Object.keys(zh).filter((k) => !(k in en))
const onlyEn = Object.keys(en).filter((k) => !(k in zh))
if (onlyZh.length || onlyEn.length) {
  fail('中英文键集不一致', `仅 zh: [${onlyZh.join(', ')}] 仅 en: [${onlyEn.join(', ')}]`)
} else {
  pass('中英文键集逐键一致', `${Object.keys(zh).length} 键`)
}

// ③ 空串与纯空白文案等于「界面上什么都没有」，也算漏
const blank = Object.entries(zh).filter(([, v]) => typeof v === 'string' && !v.trim())
  .concat(Object.entries(en).filter(([, v]) => typeof v === 'string' && !v.trim()))
  .map(([k]) => k)
if (blank.length) fail('存在空白文案', blank.join(', '))
else pass('没有空白文案')

console.log(fails ? `\n${fails} 项 FAIL` : '\nALL PASS')
process.exit(fails ? 1 : 0)
