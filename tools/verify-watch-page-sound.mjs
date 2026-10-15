#!/usr/bin/env node
/**
 * 观看页出声真机验收（真实 Chromium + 真实 HLS + 真实点击）
 *
 * 验两条路径（对应观看页 index.html 的 tryStart 分支）：
 *   pass A  --autoplay-policy=document-user-activation-required（浏览器默认的最严形态）:
 *           非静音 play() 被拒 → 静音兜底自动起播 → 「点击开启声音」按钮出现
 *           → CDP 合成真实点击（真实用户手势）→ muted=false 且画面持续推进
 *   pass B  --autoplay-policy=no-user-gesture-required（部分浏览器/配置全放行）:
 *           直接带声起播、不出按钮
 *
 * 为什么必须「有头」：实测（2026-10-15，chrome-headless-shell / Chrome for Testing 154）
 * headless 系不给 muted 自动播放豁免（连 muted 属性起播都 NotAllowedError），
 * 只有有头窗口才有「allowed-muted」真实行为，所以窗口会在屏幕上闪 ~40 秒，跑完自动关。
 *
 * 素材：ffmpeg 现生成 1s 分片 HLS（testsrc2 1280x720@25 + 440Hz 低音量正弦音轨）；
 * 页面直接加载源码 www/index.html（替换 __SESSION_ID__ / __PLAYER_CONFIG__），
 * hls.min.js 也取源码目录 —— 验的是产品代码本身，不是替身。
 *
 * 用法: node tools/verify-watch-page-sound.mjs
 * 退出码 0 = 两 pass 全部断言通过；截图落 /tmp/hancast-watch-sound-{A,B}.png
 */

import { spawn, execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync, readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import http from 'node:http'
import path, { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const WWW = join(path.resolve(HERE, '..'), 'hancast-backend/hancast_sidecar/screencast/www')
const SID = 'vws' + Math.random().toString(16).slice(2, 10)
const CLIP_SECONDS = 24

let fails = 0
const pass = (n, d = '') => console.log(`  PASS  ${n}${d ? `  [${d}]` : ''}`)
const fail = (n, d = '') => { fails++; console.log(`  FAIL  ${n}${d ? ` — ${d}` : ''}`) }
const chk = (n, got, want) => (got === want ? pass(n, String(got)) : fail(n, `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`))
const assert = (n, cond, detail = '') => (cond ? pass(n, detail) : fail(n, detail))
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function waitFor(fn, timeoutMs, stepMs = 400) {
  const deadline = Date.now() + timeoutMs
  let last = null
  while (Date.now() < deadline) {
    last = await fn()
    if (last?.done) return last.value
    await sleep(stepMs)
  }
  return last?.value ?? null
}

function findChrome() {
  if (process.env.HANCAST_VERIFY_CHROME) return process.env.HANCAST_VERIFY_CHROME
  const root = join(process.env.HOME ?? tmpdir(), '.cache/puppeteer/chrome')
  for (const v of readdirSync(root).filter((d) => d.startsWith('mac_arm-')).sort().reverse()) {
    const bin = join(root, v, 'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing')
    if (existsSync(bin)) return bin
  }
  throw new Error('找不到 Chrome for Testing（~/.cache/puppeteer/chrome），可用 HANCAST_VERIFY_CHROME 指定')
}

function generateHls(dir) {
  execFileSync('ffmpeg', [
    '-hide_banner', '-loglevel', 'error', '-y',
    '-f', 'lavfi', '-i', `testsrc2=size=1280x720:rate=25`,
    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
    '-t', String(CLIP_SECONDS),
    '-c:v', 'libx264', '-preset', 'veryfast', '-profile:v', 'main', '-level', '4.2',
    '-pix_fmt', 'yuv420p', '-g', '25', '-keyint_min', '25', '-sc_threshold', '0', '-b:v', '2M',
    '-c:a', 'aac', '-b:a', '128k', '-af', 'volume=-20dB',
    '-f', 'hls', '-hls_time', '1', '-hls_list_size', '0',
    '-hls_segment_filename', join(dir, 'seg%05d.ts'), join(dir, 'live.m3u8'),
  ], { stdio: ['ignore', 'ignore', 'inherit'] })
}

function startServer(hlsDir, watchUrlRef) {
  // 与 httpd.player_config() 的默认形状一致（params 默认：sync=1 / max=4 / lowLatency=on）
  const cfg = JSON.stringify({ lowLatencyMode: true, liveSyncDurationCount: 1, liveMaxLatencyDurationCount: 4 })
  const html = readFileSync(join(WWW, 'index.html'), 'utf8')
    .replace('__SESSION_ID__', SID)
    .replace('__PLAYER_CONFIG__', cfg)
  const hlsJs = readFileSync(join(WWW, 'hls.min.js'))
  const types = { '.m3u8': 'application/vnd.apple.mpegurl', '.ts': 'video/mp2t' }
  const server = http.createServer((req, res) => {
    const p = decodeURIComponent(new URL(req.url, 'http://x').pathname)
    if (p === `/r/${SID}`) {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
      return res.end(html)
    }
    if (p.startsWith(`/hls/${SID}/`)) {
      const name = path.basename(p)
      const file = join(hlsDir, name)
      if (!existsSync(file)) { res.writeHead(404); return res.end() }
      res.writeHead(200, { 'Content-Type': types[path.extname(name)] ?? 'application/octet-stream' })
      return res.end(readFileSync(file))
    }
    if (p === '/static/hls.min.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript' })
      return res.end(hlsJs)
    }
    res.writeHead(404)
    res.end()
  })
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => {
    watchUrlRef.value = `http://127.0.0.1:${server.address().port}/r/${SID}`
    resolve(server)
  }))
}

// ── CDP 会话 ──
async function openCdp(port) {
  let base = null
  for (let i = 0; i < 60 && !base; i++) {
    try { base = await (await fetch(`http://127.0.0.1:${port}/json/version`, { signal: AbortSignal.timeout(800) })).json() } catch { await sleep(300) }
  }
  if (!base) throw new Error(`CDP 未就绪（:${port}）`)
  const page = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: 'PUT' })).json()
  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej) })
  let mid = 0
  const pending = new Map()
  const loadWaiters = []
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data)
    if (m.method === 'Page.loadEventFired') loadWaiters.splice(0).forEach((f) => f())
    if (m.id && pending.has(m.id)) {
      const { r, j } = pending.get(m.id)
      pending.delete(m.id)
      m.error ? j(new Error(m.error.message)) : r(m.result)
    }
  })
  const send = (method, params = {}) => new Promise((r, j) => {
    const id = ++mid
    pending.set(id, { r, j })
    ws.send(JSON.stringify({ id, method, params }))
  })
  const evalJs = async (expression) => {
    const out = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (out.exceptionDetails) throw new Error(out.exceptionDetails.exception?.description ?? out.exceptionDetails.text)
    return out.result.value
  }
  await send('Page.enable')
  return { send, evalJs, ws, loadFired: () => new Promise((r) => loadWaiters.push(r)) }
}

const PROBE = `(function(){
  var v = document.querySelector('video');
  if (!v) return null;
  var btn = document.getElementById('soundBtn');
  return { t: Date.now(), ct: v.currentTime, paused: v.paused, muted: v.muted,
           vw: v.videoWidth, vh: v.videoHeight,
           btn: btn ? getComputedStyle(btn).display : '?',
           status: (document.getElementById('statusText')||{}).textContent,
           err: (document.getElementById('errText')||{}).textContent };
})()`

async function clickCenter(cdp, selector) {
  const pt = await cdp.evalJs(`(function(){
    var b = document.querySelector(${JSON.stringify(selector)});
    if (!b) return null;
    var r = b.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  })()`)
  if (!pt) throw new Error(`找不到 ${selector}，无法点击`)
  const common = { x: Math.round(pt.x), y: Math.round(pt.y), button: 'left', clickCount: 1 }
  await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', ...common, buttons: 1 })
  await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...common, buttons: 0 })
}

async function runPass({ bin, watchUrl, policy, port, label }) {
  console.log(`\n=== ${label}（--autoplay-policy=${policy}）===`)
  const profile = mkdtempSync(join(tmpdir(), 'hancast-sound-prof-'))
  const chrome = spawn(bin, [
    `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--noerrdialogs', '--disable-infobars',
    '--window-size=1100,760', '--window-position=48,48',
    `--autoplay-policy=${policy}`, 'about:blank',
  ], { stdio: 'ignore' })
  try {
    const cdp = await openCdp(port)
    const loaded = cdp.loadFired()
    await cdp.send('Page.navigate', { url: watchUrl })
    await Promise.race([loaded, sleep(10000)])

    const wantStarted = policy === 'no-user-gesture-required'
      ? (st) => st && st.paused === false && st.muted === false && st.vw > 0
      : (st) => st && st.paused === false && st.muted === true && st.btn === 'block' && st.vw > 0
    const started = await waitFor(async () => {
      const st = await cdp.evalJs(PROBE)
      return { done: !!wantStarted(st), value: st }
    }, 25000, 500)

    assert('视频元素出现', !!started, started ? '' : '25s 内 video 都没出现')
    if (!started) return
    chk('画面已解码（1280×720）', `${started.vw}x${started.vh}`, '1280x720')
    chk('状态文案切到「正在观看」', started.status, '正在观看')
    chk('没有报错文案', started.err, '')

    if (policy !== 'no-user-gesture-required') {
      chk('起播是静音状态（非静音被拒后走了静音兜底）', started.muted, true)
      chk('「点击开启声音」按钮已亮出', started.btn, 'block')
    } else {
      chk('直接带声起播（muted=false）', started.muted, false)
      assert('未出现「点击开启声音」按钮', started.btn !== 'block', `btn display=${started.btn}`)
    }

    await sleep(2500)
    let st = await cdp.evalJs(PROBE)
    const adv1 = st.ct - started.ct
    assert('2.5s 内画面持续推进（无卡死）', st.paused === false && adv1 >= 1.5,
      `${adv1.toFixed(2)}s / 2.5s`)

    if (policy !== 'no-user-gesture-required') {
      await clickCenter(cdp, '#soundBtn')
      // muted 置 false 与 volumechange 回调（收按钮）不在同一拍，两个条件都要等到
      st = await waitFor(async () => {
        const s = await cdp.evalJs(PROBE)
        return { done: s && s.muted === false && s.btn !== 'block', value: s }
      }, 4000, 300)
      chk('CDP 真实点击后已解除静音（muted=false）', st?.muted, false)
      assert('「点击开启声音」按钮已收起', st?.btn !== 'block', `btn display=${st?.btn}`)
      const t0 = st?.ct ?? 0
      await sleep(3000)
      st = await cdp.evalJs(PROBE)
      const adv2 = st.ct - t0
      assert('解除静音后画面持续推进', st.paused === false && adv2 >= 2.0,
        `${adv2.toFixed(2)}s / 3s`)
      assert('全程无报错文案（解除静音后）', st.err === '', st.err || '')
    }

    const shot = await cdp.send('Page.captureScreenshot', { format: 'png' })
    const shotPath = `/tmp/hancast-watch-sound-${label.includes('A') ? 'A' : 'B'}.png`
    writeFileSync(shotPath, Buffer.from(shot.data, 'base64'))
    console.log(`  截图 ${shotPath}`)
  } finally {
    try { chrome.kill('SIGKILL') } catch { /* 已退出 */ }
    try { rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }) } catch { /* 残留无所谓 */ }
  }
}

async function main() {
  const bin = findChrome()
  console.log(`Chrome: ${bin}`)
  const hlsDir = mkdtempSync(join(tmpdir(), 'hancast-sound-hls-'))
  let server
  try {
    console.log(`生成 ${CLIP_SECONDS}s 测试 HLS（1s 分片，含 440Hz 音轨）…`)
    generateHls(hlsDir)
    const watchUrlRef = { value: '' }
    server = await startServer(hlsDir, watchUrlRef)
    console.log(`观看页 ${watchUrlRef.value}`)

    await runPass({ bin, watchUrl: watchUrlRef.value, policy: 'document-user-activation-required', port: 9339, label: 'pass A 被拒→静音兜底→点击出声' })
    await runPass({ bin, watchUrl: watchUrlRef.value, policy: 'no-user-gesture-required', port: 9340, label: 'pass B 直接带声起播' })
  } finally {
    if (server) server.close()
    try { rmSync(hlsDir, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }) } catch { /* 残留无所谓 */ }
  }
}

main()
  .catch((e) => fail('探针本身出错', e.message))
  .finally(() => {
    console.log('\nRESULT:', fails === 0 ? 'ALL PASS' : `FAIL（${fails} 条）`)
    process.exit(fails === 0 ? 0 : 1)
  })
