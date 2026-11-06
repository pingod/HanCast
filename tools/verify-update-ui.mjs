#!/usr/bin/env node
/**
 * 应用内更新弹窗的浏览器验收（v2.4.12）
 *
 * 验的是**这条链路在界面上真的能用**，不是「代码写完了」：
 *   发现新版 → 挑产物 → 镜像开关 → 下载进度 → sha256 校验 → 退出并安装 / 复制命令
 * 全程走 vite dev 的 mock 通道（`src/api/commands.ts` 的 `update_*` 分支），
 * 它按 `update/manager.py` 的返回形状造数据，所以字段名与状态机都是真实形状。
 *
 * 三条判据是为本次修复本身服务的（跑之前先记住它们为什么存在）：
 * 1. **弹窗里不许出现 gitee / 原作者地址**：v2.4.11 的 DownloadModal 挂着
 *    `gitee.com/buxiangqumingzi/han-cast`（那是原作者那条线，releases 停在 v2.1.0），
 *    用户报「点更新出 gitee 这类原始作者的地址」。这里直接扫全文 DOM。
 * 2. **不需要用户离开应用**：主按钮必须是「立即下载并安装」，不能只是「前往下载」。
 * 3. **国内镜像开关就在一等位置**：不打开设置页也能改，改完前缀要落回参数。
 *
 * 用法: node tools/verify-update-ui.mjs
 *   自己起 vite（临时端口），退出时收掉。截图落 .tmp-win/hancast-update-*.png
 * 退出码 0 = 全部断言通过
 *
 * 前置：本机有 chrome-headless-shell（`npx puppeteer browsers install chrome-headless-shell`）
 */

import { spawn } from 'node:child_process'
import { mkdtempSync, mkdirSync, rmSync, readdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const PORT = Number(process.env.HC_VITE_PORT || 5213)
const CDP = 9343
const WIDTH = 460
const BASE = `http://localhost:${PORT}`

let fails = 0
const pass = (n, d = '') => console.log(`  PASS  ${n}${d ? ` (${d})` : ''}`)
const fail = (n, d = '') => { fails++; console.log(`  FAIL  ${n}${d ? ` — ${d}` : ''}`) }
const chk = (n, got, want) => {
  const same = got === want || JSON.stringify(got) === JSON.stringify(want)
  return same ? pass(n, JSON.stringify(got)) : fail(n, `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`)
}

function findShell() {
  // 现读的浏览器：本机缓存目录、env 覆盖、仓库内的下载件都算，不把「某台机器有
  // puppeteer 缓存」当环境契约（tools/README.md 的自适应要求）
  if (process.env.HC_CHROME_SHELL) return process.env.HC_CHROME_SHELL
  const local = path.join(ROOT, '.tmp-win/browsers/chrome-headless-shell-mac-arm64/chrome-headless-shell')
  try { if (readdirSync(path.dirname(local)).includes('chrome-headless-shell')) return local } catch { /* 没下载过 */ }
  const root = path.join(process.env.HOME ?? tmpdir(), '.cache/puppeteer/chrome-headless-shell')
  for (const v of readdirSync(root).filter((d) => d.startsWith('mac_arm-')).sort().reverse()) {
    const dir = path.join(root, v, 'chrome-headless-shell-mac-arm64')
    try { if (readdirSync(dir).includes('chrome-headless-shell')) return path.join(dir, 'chrome-headless-shell') } catch { /* 下一个版本 */ }
  }
  throw new Error('找不到 chrome-headless-shell：npx puppeteer browsers install chrome-headless-shell，或 HC_CHROME_SHELL=/path/to/chrome-headless-shell')
}

const shotDir = path.join(ROOT, '.tmp-win')
mkdirSync(shotDir, { recursive: true })
const profile = mkdtempSync('/tmp/hancast-update-ui-')

const vite = spawn(process.execPath, [path.join(ROOT, 'node_modules/vite/bin/vite.js'),
  '--port', String(PORT), '--strictPort'], { cwd: ROOT, stdio: 'ignore' })
const child = spawn(findShell(), [
  `--remote-debugging-port=${CDP}`,
  `--user-data-dir=${profile}`,
  '--no-first-run', '--no-default-browser-check',
  `--window-size=${WIDTH},900`,
  // 这两条是**这台机器上的硬要求**，不是习惯性加料：
  // 不带 --no-sandbox 时 chromium 的进程沙箱初始化直接 `Operation not permitted`，
  // 不带 --disable-gpu 时 GPU 进程连崩六次后 `FATAL: GPU process isn't usable`，
  // 表现就是 CDP 端口永远等不到东西。本脚本只开 localhost 页面、判据全在 DOM 与
  // 几何上（不看像素），所以放宽这两条不降低判据强度。
  '--no-sandbox', '--disable-gpu',
  'about:blank',
], { stdio: 'ignore' })

async function waitVite(timeoutMs = 25000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try { const r = await fetch(BASE); if (r.ok) return true } catch { /* 还没起来 */ }
    await new Promise((r) => setTimeout(r, 200))
  }
  return false
}

async function waitForTarget(timeoutMs = 40000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try { const r = await fetch(`http://127.0.0.1:${CDP}/json/version`); if (r.ok) return r.json() } catch { /* 还没起来 */ }
    await new Promise((r) => setTimeout(r, 300))
  }
  throw new Error('CDP 未就绪')
}

class Session {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map()
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result)
      }
    })
  }
  send(method, params = {}) {
    const id = ++this.id
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.ws.send(JSON.stringify({ id, method, params }))
    })
  }
  async evaluate(expression) {
    const r = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (r.exceptionDetails) throw new Error(`页面脚本异常: ${r.exceptionDetails.exception?.description ?? r.exceptionDetails.text}`)
    return r.result.value
  }
  async settle(expression, tries = 60) {
    for (let i = 0; i < tries; i++) {
      if (await this.evaluate(expression)) return true
      await new Promise((r) => setTimeout(r, 150))
    }
    return false
  }
}

// 弹窗里的全部可见文本（判「有没有出现第三方/原作者地址」用）
const SHOT_TEXT = `(() => {
  const m = document.querySelector('.modal-overlay')
  return m ? m.innerText : ''
})()`

const PRIMARY = `(() => {
  const btns = [...document.querySelectorAll('.modal-overlay .modal-actions button')]
  const b = btns[btns.length - 1]
  return b ? { text: b.innerText.trim(), disabled: b.disabled } : null
})()`

const SNAPSHOT = `(() => {
  const app = document.querySelector('#app')?.__vue_app__
  const st = app?._context?.config?.globalProperties?.$pinia?.state?.value?.update || {}
  const i = document.querySelector('.upd-bar i')
  return JSON.stringify({
    barStyle: i ? (i.getAttribute('style') || '(无 style)') : '(没有 .upd-bar i)',
    barClass: i ? i.className : '',
    task: st.downloadTask ? { s: st.downloadTask.status, g: st.downloadTask.stage, p: st.downloadTask.percent } : null,
    downloading: st.downloading, applying: st.applying, plan: st.plan ? st.plan.kind : null,
    applyingEl: !!document.querySelector('.upd-applying'),
    verifyEl: document.querySelector('.upd-verify')?.innerText || '',
    errEl: document.querySelector('.upd-error-text')?.innerText || '',
    storeErr: st.error || '',
    btns: [...document.querySelectorAll('.modal-actions button')].map((b) => b.innerText.trim()),
  })
})()`

/** 断言失败时打一行现场，避免「红了但看不出为什么红」 */
async function diag(s, label) {
  try { console.log(`        ↳ ${label} 现场: ${await s.evaluate(SNAPSHOT)}`) } catch (e) { console.log(`        ↳ 现场取不到: ${e}`) }
}

async function shot(s, name) {
  try {
    const r = await s.send('Page.captureScreenshot', { format: 'png' })
    const file = path.join(shotDir, `hancast-update-${name}.png`)
    writeFileSync(file, Buffer.from(r.data, 'base64'))
    pass(`截图 ${name}`, file)
  } catch (e) { fail(`截图 ${name} 失败`, String(e)) }
}

async function openSession({ query = '/?mockUpdate=1', userAgent = '' } = {}) {
  // 先开一个空白 target，设好 UA 再导航：mock 的产物清单是按 navigator.userAgent
  // 现算的，想演 Linux 的四选一必须让页面从第一帧就是 Linux
  const blank = await (await fetch(`http://127.0.0.1:${CDP}/json/new?about:blank`, { method: 'PUT' })).json()
  const ws = new WebSocket(blank.webSocketDebuggerUrl)
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej) })
  const s = new Session(ws)
  await s.send('Page.enable')
  await s.send('Runtime.enable')
  await s.send('Emulation.setDeviceMetricsOverride', {
    width: WIDTH, height: 900, deviceScaleFactor: 2, mobile: false,
  })
  if (userAgent) await s.send('Emulation.setUserAgentOverride', { userAgent })
  await s.send('Page.navigate', { url: BASE + query })
  if (!(await s.settle(`!!document.querySelector('#app')`, 120))) throw new Error('前端没渲染')
  // 设置页 → 检查更新，与用户点的那条路径一致（启动自检的事件在浏览器里没有）
  await s.evaluate(`location.hash = '#/settings'`)
  if (!(await s.settle(`[...document.querySelectorAll('button')].some(b => b.innerText.includes('检查更新'))`, 120))) {
    throw new Error('设置页没有「检查更新」按钮')
  }
  await s.evaluate(`(() => {
    const b = [...document.querySelectorAll('button')].find(x => x.innerText.includes('检查更新'))
    b.click()
  })()`)
  if (!(await s.settle(`!!document.querySelector('.upd')`, 80))) throw new Error('更新弹窗没出现（mock 没给新版？）')
  return s
}

async function main() {
  if (!(await waitVite())) { fail('vite 没起来', BASE); return }
  await waitForTarget()

  // ── 场景 A：有新版，完整下载流程（macOS mock：单产物 + 可自动装）──
  let s
  try {
    s = await openSession()
  } catch (e) { fail('场景 A 打不开弹窗', String(e)); return }

  const text = await s.evaluate(SHOT_TEXT)
  chk('弹窗显示版本对（当前 → 最新）', /2\.4\.11[\s\S]*2\.5\.0/.test(text), true)
  const foreign = ['gitee.com', 'Gitee', 'buxiangqumingzi', 'xfangfang', 'Macast', 'apps.microsoft.com']
    .filter((w) => text.includes(w))
  chk('弹窗内无 gitee / 原作者 / 商店地址', foreign, [])
  chk('主按钮不是「前往下载」而是下载并安装', /立即下载并安装/.test((await s.evaluate(PRIMARY))?.text || ''), true)
  chk('镜像开关就在弹窗里', /GitHub 镜像/.test(text), true)

  // 镜像开关：勾上 → 前缀输入框出现 → 保存要落到参数
  await s.evaluate(`(() => {
    const box = document.querySelector('.upd-mirror input[type=checkbox]')
    box.click()
  })()`)
  if (!(await s.settle(`!!document.querySelector('.upd-mirror-prefix input')`))) {
    fail('勾选镜像后没有露出前缀输入框')
  } else {
    pass('勾选镜像后露出前缀输入框')
    const prefilled = await s.settle(
      `(() => { const i = document.querySelector('.upd-mirror-prefix input')
         return !!i && i.value.startsWith('https://') })()`, 30)
    chk('前缀默认值来自参数表（异步读回）', prefilled, true)
    await s.evaluate(`(() => {
      const i = document.querySelector('.upd-mirror-prefix input')
      i.value = 'https://gh-proxy.com/'
      i.dispatchEvent(new Event('input', { bubbles: true }))
      document.querySelector('.upd-mirror-prefix button').click()
    })()`)
    const saved = await s.settle(`!document.querySelector('.upd-hint--bad')`)
    chk('保存镜像前缀没有报错', saved, true)
  }

  // 产物清单：本平台（mac mock 只给 dmg）应当恰好一项且写着大小
  const assetText = await s.evaluate(`(() => {
    const el = document.querySelector('.upd-asset')
    return el ? el.innerText : ''
  })()`)
  chk('产物区列出本平台安装包', /\.dmg/.test(assetText), true)

  // 开始下载 → 进度条必须真的在动
  await s.evaluate(`(() => {
    const btns = [...document.querySelectorAll('.modal-overlay .modal-actions button')]
    btns[btns.length - 1].click()
  })()`)
  // 起下载的**那一瞬**任务表还是本地占位形状（没有 percent），界面走的是不定长滑动条；
  // 要量「宽度在增长」必须等第一次回包落到，否则量到的是那条没有 style 的 unknown 条
  const gotBar = await s.settle(`(() => {
    const i = document.querySelector('.upd-bar i')
    return !!i && /%/.test(i.getAttribute('style') || '')
  })()`, 40)
  if (!gotBar) {
    await diag(s, '进度条（等 style 落定）')
    fail('点下载后没有出现确定进度的进度条')
  } else {
    const read = `(() => {
      const i = document.querySelector('.upd-bar i')
      const t = document.querySelector('.upd-size')
      return { w: i ? i.style.width : '', text: t ? t.innerText : '',
               stage: document.querySelector('.upd-stage')?.innerText || '' }
    })()`
    const a = await s.evaluate(read)
    // 轮询是 1 秒一次，回包还要一个 tick 才落到界面；1.2 秒的窗口太紧，
    // 会拿「同一次回包」当两次读数比出「进度没动」的假红（实测就是差半秒）
    await new Promise((r) => setTimeout(r, 2600))
    const b = await s.evaluate(read)
    const wa = parseFloat(a.w), wb = parseFloat(b.w)
    if (!(Number.isFinite(wa) && Number.isFinite(wb) && wb > wa)) {
      await diag(s, '进度条')
      fail('进度条宽度在增长', `a=${JSON.stringify(a.w)} b=${JSON.stringify(b.w)} stage=${b.stage}`)
    } else {
      pass('进度条宽度在增长', `${a.w} → ${b.w}`)
    }
    chk('进度行带速度与总量', /已下载 .*\/ .*（.*\/s）/.test(b.text), true)
    await shot(s, 'progress')
  }

  // 等它下完：应当报「已按 GitHub 声明的 sha256 校验」，主按钮变「退出并安装」
  const done = await s.settle(`(() => {
    const v = document.querySelector('.upd-verify')
    return !!v && v.innerText.includes('sha256')
  })()`, 80)
  chk('下载完成后显示摘要校验结论', done, true)
  const after = await s.evaluate(PRIMARY)
  chk('主按钮切到「退出并安装」', /退出并安装/.test(after?.text || ''), true)
  chk('下载完「打开所在目录」可用', await s.evaluate(`(() => {
    const b = [...document.querySelectorAll('.modal-overlay .modal-actions button')].find(x => x.innerText.includes('打开所在目录'))
    return b ? !b.disabled : null
  })()`), true)
  await shot(s, 'done')

  // 点退出并安装 → 界面要立刻把话说完（应用真的会走）
  // 装法（plan）是下载完才去取的一次独立请求，那之前主按钮按判据是禁用的；
  // 不等它解禁就点等于点对灰按钮，判据会红而代码没坏
  const enabled = await s.settle(`(() => {
    const b = [...document.querySelectorAll('.modal-overlay .modal-actions button')].pop()
    return !!b && !b.disabled && /退出并安装/.test(b.innerText)
  })()`, 60)
  if (!enabled) await diag(s, '退出并安装（等按钮解禁）')
  chk('下载完主按钮解禁成「退出并安装」', enabled, true)
  await s.evaluate(`(() => {
    const btns = [...document.querySelectorAll('.modal-overlay .modal-actions button')]
    btns[btns.length - 1].click()
  })()`)
  const applying = await s.settle(`!!document.querySelector('.upd-applying')`)
  if (!applying) await diag(s, '退出并安装')
  chk('点击后出现「正在退出并安装…」提示', applying, true)

  // ── 场景 B：Linux 四种包格式四选一，且默认给免 root 的 AppImage ──
  let sLinux
  try {
    sLinux = await openSession({ userAgent: 'Mozilla/5.0 (X11; Linux x86_64) hancast-verify' })
  } catch (e) {
    fail('场景 B 打不开弹窗', String(e))
  }
  if (sLinux) {
    const txt = await sLinux.evaluate(SHOT_TEXT)
    chk('Linux 弹窗列出多产物选择器', await sLinux.evaluate(`!!document.querySelector('.upd-asset select, .upd-asset [role="combobox"], .custom-select')`), true)
    chk('Linux 默认产物是免 root 的 AppImage', /AppImage/.test(txt), true)
    // 选到 deb：应当切到「要管理员权限」那条命令，而不是假装能自动装
    await sLinux.evaluate(`(() => {
      const trig = document.querySelector('.upd-asset .custom-select__trigger') || document.querySelector('.upd-asset button')
      if (trig) trig.click()
    })()`)
    const picked = await sLinux.settle(`(() => {
      const opts = [...document.querySelectorAll('.upd-asset [role="option"], .custom-select__option')]
      const deb = opts.find(o => /deb/.test(o.innerText))
      if (!deb) return false
      deb.click(); return true
    })()`, 20)
    chk('能选到 deb 这一项', picked, true)
    const enabledLinux = await sLinux.settle(`(() => {
      const b = [...document.querySelectorAll('.modal-overlay .modal-actions button')].pop()
      return !!b && !b.disabled
    })()`, 60)
    chk('Linux 下载完成后主按钮解禁', enabledLinux, true)
    await sLinux.evaluate(`(() => {
      const btns = [...document.querySelectorAll('.modal-overlay .modal-actions button')]
      btns[btns.length - 1].click()
    })()`)
    const doneB = await sLinux.settle(`(() => {
      const v = document.querySelector('.upd-verify')
      return !!v && !!document.querySelector('.upd-cmd')
    })()`, 80)
    chk('deb 下完后给的是安装命令而不是假成功', doneB, true)
    const cmd = await sLinux.evaluate(`document.querySelector('.upd-cmd')?.innerText || ''`)
    chk('命令里就是 sudo apt install + 文件路径', /sudo apt install .*deb/.test(cmd), true)
    const prim = await sLinux.evaluate(PRIMARY)
    chk('主按钮变成「复制安装命令」', /复制安装命令/.test(prim?.text || ''), true)
    await shot(sLinux, 'linux-deb-command')
  }

  // ── 场景 C：下载失败要把原因原样摆出来 ──
  let sFail
  try {
    sFail = await openSession({ query: '/?mockUpdate=fail' })
  } catch (e) {
    fail('场景 C 打不开弹窗', String(e))
  }

  if (sFail) {
    await sFail.evaluate(`(() => {
      const btns = [...document.querySelectorAll('.modal-overlay .modal-actions button')]
      btns[btns.length - 1].click()
    })()`)
    const err = await sFail.settle(`(() => {
      const e = document.querySelector('.upd-error-text')
      return !!e && e.innerText.includes('下载被截断')
    })()`, 60)
    chk('下载失败时显示 Sidecar 的原因原文', err, true)
    const p = await sFail.evaluate(PRIMARY)
    chk('失败后主按钮回到「立即下载并安装」（可重试）', /立即下载并安装/.test(p?.text || ''), true)
    await shot(sFail, 'failure')
  }

  // ── 场景 D：慢源上点「取消下载」，界面必须承认这是请求而不是结果 ──
  let sSlow
  try {
    sSlow = await openSession({ query: '/?mockUpdate=slow' })
  } catch (e) {
    fail('场景 D 打不开弹窗', String(e))
  }
  if (sSlow) {
    await sSlow.evaluate(`(() => {
      const btns = [...document.querySelectorAll('.modal-actions button')]
      btns[btns.length - 1].click()
    })()`)
    const started = await sSlow.settle(`(() => {
      const b = [...document.querySelectorAll('.modal-actions button')].pop()
      return !!b && /取消下载/.test(b.innerText)
    })()`, 40)
    chk('下载中主按钮切成「取消下载」', started, true)
    await sSlow.evaluate(`(() => {
      const btns = [...document.querySelectorAll('.modal-actions button')]
      btns[btns.length - 1].click()
    })()`)
    const cancelled = await sSlow.settle(`(() => {
      const st = document.querySelector('#app').__vue_app__._context.config.globalProperties.$pinia.state.value.update
      return st.downloadTask && st.downloadTask.status === 'cancelled'
    })()`, 40)
    chk('取消后任务落到 cancelled 终态', cancelled, true)
    const stopped = await sSlow.evaluate(`(() => {
      const st = document.querySelector('#app').__vue_app__._context.config.globalProperties.$pinia.state.value.update
      return { downloading: st.downloading, bar: !!document.querySelector('.upd-bar') }
    })()`)
    chk('取消后不再显示进行中的进度条', stopped, { downloading: false, bar: false })
    await shot(sSlow, 'cancelled')
  }

  console.log(fails ? `\n${fails} 项 FAIL` : '\nALL PASS')
  return fails ? 1 : 0
}

main()
  .then((code) => {
    try { child.kill('SIGTERM') } catch { /* 已退出 */ }
    try { vite.kill('SIGTERM') } catch { /* 已退出 */ }
    setTimeout(() => { try { rmSync(profile, { recursive: true, force: true }) } catch { /* 留著也无妨 */ } }, 500)
    process.exit(code)
  })
  .catch(async (e) => {
    console.error('脚本本身出错：', e)
    try { child.kill('SIGTERM') } catch { /* 已退出 */ }
    try { vite.kill('SIGTERM') } catch { /* 已退出 */ }
    process.exit(1)
  })
