#!/usr/bin/env node
/**
 * 「解析失败原因」整行可见的浏览器验收
 *
 * 改动点:
 * - 走 vite dev 的 mock 通道：喂一个非媒体网页地址，mock 后端抛的就是真后端那条
 *   长错误（站点 + 原因 + 下一步动作），所以错误行样式与文案都是真实形状
 * - 判据用几何而不是文本：DOM 里 `textContent` 永远是完整的，被截断只发生在渲染上，
 *   所以量「换了几行」「有没有一行的右边界超出元素自己的盒子」
 * - 用真实 Chromium（headless shell + CDP）在 420px 窄窗里测——报错被截断正是在这种
 *   窄面板上暴露的，宽窗口会把 nowrap 的老样式也放过
 *
 * 用法: node tools/verify-error-wrap-ui.mjs [base-url]
 *   base-url 默认 http://localhost:5199
 *   需先起 vite：node node_modules/vite/bin/vite.js --port 5199
 * 退出码 0 = 全部断言通过；截图落在 /tmp/hancast-error-wrap.png
 */

import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync, readdirSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

const BASE = process.argv[2] ?? 'http://localhost:5199'
const PORT = 9339
const WIDTH = 420

// mock 里「网页地址解不出可播源」的报错模板结尾（commands.ts 的 resolve_share 分支）
const TAIL = '或在设置页安装规则包）'

let fails = 0
const pass = (n, d = '') => console.log(`  PASS  ${n}${d ? ` (${d})` : ''}`)
const fail = (n, d = '') => { fails++; console.log(`  FAIL  ${n}${d ? ` — ${d}` : ''}`) }
const chk = (n, got, want) => (got === want ? pass(n, String(got)) : fail(n, `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`))

function findShell() {
  const root = path.join(process.env.HOME ?? tmpdir(), '.cache/puppeteer/chrome-headless-shell')
  for (const v of readdirSync(root).filter((d) => d.startsWith('mac_arm-')).sort().reverse()) {
    const dir = path.join(root, v, 'chrome-headless-shell-mac-arm64')
    try { if (readdirSync(dir).includes('chrome-headless-shell')) return path.join(dir, 'chrome-headless-shell') } catch { /* 下一个版本 */ }
  }
  throw new Error('找不到 chrome-headless-shell，先 npx puppeteer browsers install chrome-headless-shell')
}

const profile = mkdtempSync(path.join(tmpdir(), 'hancast-errwrap-'))
const child = spawn(findShell(), [
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  '--no-first-run', '--no-default-browser-check',
  `--window-size=${WIDTH},760`,
  'about:blank',
], { stdio: 'ignore' })

async function waitForTarget(timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`); if (r.ok) return r.json() } catch { /* 还没起来 */ }
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

const MEASURE = `(() => {
  const el = document.querySelector('.media-input__error')
  if (!el) return null
  const cs = getComputedStyle(el)
  const box = el.getBoundingClientRect()
  const range = document.createRange()
  range.selectNodeContents(el)
  const rects = [...range.getClientRects()].filter(r => r.width > 1 && r.height > 1)
  const lines = new Set(rects.map(r => Math.round(r.top))).size
  const clipRight = rects.some(r => r.right > box.right + 0.5)
  const clipLeft = rects.some(r => r.left < box.left - 0.5)
  const overflow = el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1
  return {
    text: el.textContent.trim(), lines, clipRight, clipLeft, overflow,
    whiteSpace: cs.whiteSpace, textOverflow: cs.textOverflow,
    elWidth: Math.round(box.width), panelWidth: Math.round(document.querySelector('.media-input').getBoundingClientRect().width),
  }
})()`

async function main() {
  await waitForTarget()
  const page = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(BASE)}`, { method: 'PUT' })).json()
  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej) })
  const s = new Session(ws)
  await s.send('Page.enable')
  await s.send('Emulation.setDeviceMetricsOverride', {
    width: WIDTH, height: 760, deviceScaleFactor: 2, mobile: false,
  })
  if (!(await s.settle(`!!document.querySelector('.media-input__urlinput')`))) {
    fail('首页输入区未渲染', BASE); return
  }

  // 喂一个网页地址：mock 后端按真后端的形状抛长错误
  await s.evaluate(`(() => {
    const box = document.querySelector('.media-input__urlinput')
    box.value = 'https://www.66dpw.vip/show/202466.html'
    box.dispatchEvent(new Event('input', { bubbles: true }))
  })()`)
  if (!(await s.settle(`!document.querySelector('.media-input__parse').disabled`))) {
    fail('「解析」按钮没有解禁'); return
  }
  await s.evaluate(`document.querySelector('.media-input__parse').click()`)
  if (!(await s.settle(`!!document.querySelector('.media-input__error')`))) {
    fail('没有出现报错行（mock 没按网页地址失败？）'); return
  }
  // 错误文案是异步落到 store 的，先等结尾那句出现，再量几何
  const hasTail = await s.settle(
    `document.querySelector('.media-input__error')?.textContent.includes(${JSON.stringify(TAIL)})`)
  chk('报错文案完整落到 DOM（含最后一步指引）', hasTail, true)

  const m = await s.evaluate(MEASURE)
  if (!m) { fail('读不到报错行的几何'); return }
  pass('量到报错行', `${m.lines} 行 / 宽 ${m.elWidth}px，面板内宽 ${m.panelWidth}px`)
  chk('不再单行截断（white-space 可折行）', m.whiteSpace !== 'nowrap', true)
  chk('不再用省略号收尾（text-overflow 非 ellipsis）', m.textOverflow !== 'ellipsis', true)
  chk('长文本真的折到了第二行以上', m.lines >= 2, true)
  chk('没有一行的右边界超出元素盒子', m.clipRight, false)
  chk('没有一行的左边界超出元素盒子', m.clipLeft, false)
  chk('元素自身没有内容溢出（scroll 尺寸 == 盒子）', m.overflow, false)
  chk('元素宽度没超出投屏输入卡', m.elWidth <= m.panelWidth, true)

  await s.evaluate(`document.querySelector('.media-input__error').scrollIntoView({ block: 'center' })`)
  await new Promise((r) => setTimeout(r, 300))
  await s.send('Emulation.clearDeviceMetricsOverride')
  const shot = await s.send('Page.captureScreenshot', { format: 'png' })
  writeFileSync('/tmp/hancast-error-wrap.png', Buffer.from(shot.data, 'base64'))
  console.log('  截图: /tmp/hancast-error-wrap.png')
  console.log(`\nRESULT: ${fails === 0 ? 'ALL PASS' : `${fails} FAIL`}`)
}

try { await main() } finally {
  child.kill('SIGKILL')
  rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 })
}
process.exit(fails === 0 ? 0 : 1)
