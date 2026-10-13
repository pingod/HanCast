#!/usr/bin/env node
/**
 * 设置页「后台参数」区块的浏览器验收
 *
 * 改动点:
 * - 用真实 Chromium 渲染（headless shell + CDP），不靠快照推断布局
 * - 走 vite dev 的 mock 通道：只验 UI 行为（展开/三类控件/待保存计数/保存回落/恢复默认），
 *   后端契约由 hancast-backend/tests/test_params*.py 与 tools/accept-macos-artifact.sh 负责
 *
 * 用法: node tools/verify-params-ui.mjs [base-url]
 *   base-url 默认 http://localhost:5199（需先起 vite：node node_modules/vite/bin/vite.js --port 5199）
 * 退出码 0 = 全部断言通过；截图落在 /tmp/hancast-params-ui-*.png
 */

import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync, readdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

const BASE = process.argv[2] ?? 'http://localhost:5199'
const PORT = 9337

function findShell() {
  const root = path.join(os_home(), '.cache/puppeteer/chrome-headless-shell')
  const versions = readdirSync(root).filter((d) => d.startsWith('mac_arm-')).sort().reverse()
  for (const v of versions) {
    const dir = path.join(root, v, 'chrome-headless-shell-mac-arm64')
    const exe = path.join(dir, 'chrome-headless-shell')
    try {
      readdirSync(dir).find((f) => f === 'chrome-headless-shell')
      return exe
    } catch { /* 换下一个版本 */ }
  }
  throw new Error('找不到 chrome-headless-shell，先 npx puppeteer browsers install chrome-headless-shell')
}

function os_home() {
  return process.env.HOME ?? process.env.USERPROFILE ?? tmpdir()
}

const profile = mkdtempSync(path.join(tmpdir(), 'hancast-ui-'))
const chrome = findShell()
const child = spawn(chrome, [
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  '--no-first-run', '--no-default-browser-check',
  '--window-size=420,760',
  'about:blank',
], { stdio: 'ignore' })

let fails = 0
const pass = (name, detail = '') => console.log(`  PASS  ${name}${detail ? ` (${detail})` : ''}`)
const fail = (name, detail = '') => { fails++; console.log(`  FAIL  ${name}${detail ? ` — ${detail}` : ''}`) }
const chk = (name, got, want) => (got === want ? pass(name, String(got)) : fail(name, `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`))

async function waitForTarget(timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/version`)
      if (res.ok) return res.json()
    } catch { /* 还没起来 */ }
    await new Promise((r) => setTimeout(r, 300))
  }
  throw new Error('CDP 未就绪')
}

async function newPage(url) {
  const res = await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(url)}`, { method: 'PUT' })
  if (!res.ok) throw new Error(`开页失败 ${res.status}`)
  return res.json()
}

class Session {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
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
    const r = await this.send('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true,
    })
    if (r.exceptionDetails) {
      throw new Error(`页面脚本异常: ${r.exceptionDetails.exception?.description ?? r.exceptionDetails.text}`)
    }
    return r.result.value
  }

  async settle(expression) {
    for (let i = 0; i < 40; i++) {
      if (await this.evaluate(expression)) return true
      await new Promise((r) => setTimeout(r, 100))
    }
    return false
  }
}

async function main() {
  await waitForTarget()
  const page = await newPage(`${BASE}/#/settings`)
  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve)
    ws.addEventListener('error', reject)
  })
  const s = new Session(ws)
  await s.send('Page.enable')
  await s.send('Emulation.setDeviceMetricsOverride', {
    width: 420, height: 760, deviceScaleFactor: 2, mobile: false,
  })

  if (!(await s.settle(`!!document.querySelector('.settings__section')`))) {
    fail('设置页未渲染', BASE); return
  }

  // 1. 区块存在且默认收起
  chk('「后台参数」分组标题', await s.evaluate(
    `[...document.querySelectorAll('.settings__section-title')].map(h=>h.textContent.trim()).includes('后台参数')`), true)
  chk('展开前面板不可见', await s.evaluate(
    `!!document.querySelector('.settings__params')`), false)

  // 2. 展开
  await s.evaluate(
    `[...document.querySelectorAll('.settings__item--clickable')]
      .find(b => b.textContent.includes('高级参数')).click()`)
  if (!(await s.settle(`!!document.querySelector('.settings__params')`))) {
    fail('点击「高级参数」没有展开面板'); return
  }
  // 面板先出现骨架，登记表数据是异步读的，等行或错误落地再断言
  if (!(await s.settle(`!!document.querySelector('.settings__param, .settings__params-error')`))) {
    fail('展开后没有读到参数'); return
  }
  pass('点击展开面板')

  const count = await s.evaluate(`document.querySelectorAll('.settings__param').length`)
  pass('渲染出参数行', `${count} 项`)
  chk('分组标题按 section 出现', await s.evaluate(
    `[...document.querySelectorAll('.settings__params-group-title')].map(h=>h.textContent.trim()).join(',')`),
    '屏幕投屏,浏览器播放器')

  // 3. 三类控件都在
  chk('number 控件', await s.evaluate(
    `!!document.querySelector('.settings__param input[type=number]')`), true)
  chk('enum 控件（编码速度档）', await s.evaluate(
    `[...document.querySelectorAll('.settings__param .settings__label span:first-child, .settings__param .settings__label')]
      .some(el => el.textContent.includes('编码速度档'))`), true)
  chk('bool 控件', await s.evaluate(
    `!!document.querySelector('.settings__param .settings__toggle input[type=checkbox]')`), true)
  chk('1 秒默认值上屏', await s.evaluate(
    `[...document.querySelectorAll('.settings__param input[type=number]')].some(i => i.value === '1')`), true)

  // 4. 改数字 → 出现「N 项待保存」→ 保存按钮解禁
  await s.evaluate(`(() => {
    const box = [...document.querySelectorAll('.settings__param input[type=number]')][0]
    box.value = '4'
    box.dispatchEvent(new Event('change', { bubbles: true }))
  })()`)
  chk('待保存计数', await s.evaluate(
    `document.querySelector('.settings__params-count')?.textContent.trim()`), '1 项待保存')
  chk('保存按钮可点', await s.evaluate(
    `document.querySelector('.settings__params-save').disabled`), false)
  chk('恢复默认入口出现', await s.evaluate(
    `[...document.querySelectorAll('.settings__param-reset')].length`), 1)

  // 5. 保存后回落：值留在新值，「已改」标记出现，待保存清零
  await s.evaluate(`document.querySelector('.settings__params-save').click()`)
  // 保存过程中按钮同样是 disabled，必须等文案离开「保存中」再断言
  const saved = `(() => {
      const b = document.querySelector('.settings__params-save')
      return b && b.disabled && !/保存中/.test(b.textContent)
    })()`
  if (!(await s.settle(saved))) {
    fail('保存请求没有结束（按钮一直停在「保存中」）'); return
  }
  chk('保存后新值回读', await s.evaluate(
    `[...document.querySelectorAll('.settings__param input[type=number]')][0].value`), '4')
  chk('保存后无待保存项', await s.evaluate(
    `/待保存/.test(document.querySelector('.settings__params-count')?.textContent ?? '')`), false)
  chk('「已改」标记', await s.evaluate(
    `!!document.querySelector('.settings__badge--changed')`), true)
  chk('保存成功提示', await s.evaluate(
    `document.querySelector('.settings__params-count')?.textContent.includes('已保存')`), true)

  // 6. 恢复默认 → 覆盖项清除
  await s.evaluate(`(() => {
    const b = [...document.querySelectorAll('.settings__param-reset')]
      .find(x => x.textContent.trim() === '恢复默认')
    b && b.click()
  })()`)
  if (!(await s.settle(`[...document.querySelectorAll('.settings__param-reset')].length === 0`))) {
    fail('恢复默认没有清除覆盖标记'); return
  }
  chk('恢复默认后回到 1 秒', await s.evaluate(
    `[...document.querySelectorAll('.settings__param input[type=number]')][0].value`), '1')
  chk('「已改」标记消失', await s.evaluate(
    `!!document.querySelector('.settings__badge--changed')`), false)

  // 7. 越界值：mock 层不校验，改验 UI 把 min/max 交给了原生 input（浏览器会拦）
  chk('number 输入带上限', await s.evaluate(
    `[...document.querySelectorAll('.settings__param input[type=number]')][0].max`), '10')

  await s.evaluate(`document.querySelector('.settings__params')
    .scrollIntoView({ block: 'start' })`)
  await new Promise((r) => setTimeout(r, 400))
  await s.send('Emulation.clearDeviceMetricsOverride')
  const shot = await s.send('Page.captureScreenshot', { format: 'png' })
  const out = path.join('/tmp', 'hancast-params-ui.png')
  const { writeFileSync } = await import('node:fs')
  writeFileSync(out, Buffer.from(shot.data, 'base64'))
  console.log(`  截图: ${out}`)

  console.log(`\nRESULT: ${fails === 0 ? 'ALL PASS' : `${fails} FAIL`}`)
}

try {
  await main()
} finally {
  child.kill('SIGKILL')
  // Chrome 进程刚被杀时目录还在落盘，带重试删
  rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 })
}
process.exit(fails === 0 ? 0 : 1)
