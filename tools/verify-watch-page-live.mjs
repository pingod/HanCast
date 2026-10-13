#!/usr/bin/env node
/**
 * 浏览器通道观看页真机验收（1 秒分片）
 *
 * 改动点:
 * - 直接驱动 CI 打进 .app 的 sidecar 起真实屏幕投屏，再用真实 Chromium（headless
 *   shell + CDP）打开观看页，只看现场证据：画面是否真的在动、有没有卡死、
 *   端到端延迟实测多少
 * - 分片时长、GOP、hls.js 低延迟参数这些「配置对不对」由 pytest 与
 *   tools/verify-browser-segment-live.py 负责；这里负责「播得起来吗」
 *
 * 用法: node tools/verify-watch-page-live.mjs [segmentSeconds]
 *   默认 1（出厂契约）；传 4 会先 save_params 覆盖再测，用来对照
 * 前置: 脚本会整体退出 HanCast.app 抢端口，结束后自动打开回来
 * 退出码 0 = 全部断言通过；截图落在 /tmp/hancast-watch-live.png
 */

import { spawn, execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path, { join } from 'node:path'

const SEG = Number(process.argv[2] ?? 1)
const SIDECAR = '/Applications/HanCast.app/Contents/MacOS/hancast-sidecar'
const CDP_PORT = 9338
const WATCH_SECONDS = 25

let fails = 0
const pass = (n, d = '') => console.log(`  PASS  ${n}${d ? `  [${d}]` : ''}`)
const fail = (n, d = '') => { fails++; console.log(`  FAIL  ${n}${d ? ` — ${d}` : ''}`) }
const chk = (n, got, want) => (got === want ? pass(n, String(got)) : fail(n, `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`))
const assert = (n, cond, detail = '') => (cond ? pass(n, detail) : fail(n, detail))
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function findShell() {
  const root = path.join(process.env.HOME ?? tmpdir(), '.cache/puppeteer/chrome-headless-shell')
  for (const v of readdirSync(root).filter((d) => d.startsWith('mac_arm-')).sort().reverse()) {
    const dir = path.join(root, v, 'chrome-headless-shell-mac-arm64')
    try { if (readdirSync(dir).includes('chrome-headless-shell')) return path.join(dir, 'chrome-headless-shell') } catch { /* 下一个版本 */ }
  }
  throw new Error('找不到 chrome-headless-shell')
}

// ── sidecar JSON-line 协议 ──
const responses = new Map()
function call(id, cmd, params = {}, timeoutMs = 60000) {
  responses.delete(id)
  proc.stdin.write(JSON.stringify({ id, cmd, params }) + '\n')
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve) => {
    const tick = () => {
      if (responses.has(id)) return resolve(responses.get(id))
      if (Date.now() > deadline) return resolve({ id, timeout: true })
      setTimeout(tick, 150)
    }
    tick()
  })
}

let proc
let chrome
let profile

async function main() {
  console.log(`=== 准备：退出 GUI，用打包 sidecar 起 ${SEG}s 分片的浏览器投屏 ===`)
  try { execFileSync('osascript', ['-e', 'tell application "HanCast" to quit'], { stdio: 'ignore' }) } catch { /* 没在跑 */ }
  await sleep(4000)
  try {
    execFileSync('pkill', ['-f', 'HanCast.app/Contents/MacOS/hancast-sidecar'], { stdio: 'ignore' })
  } catch { /* 没有残留 sidecar，pkill 非 0 是正常的 */ }
  await sleep(1000)

  proc = spawn(SIDECAR, [], { cwd: path.dirname(SIDECAR) })
  let buf = ''
  proc.stdout.on('data', (chunk) => {
    buf += chunk.toString()
    let i
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim()
      buf = buf.slice(i + 1)
      if (!line.startsWith('{')) continue
      try { const m = JSON.parse(line); if (m.id) responses.set(m.id, m) } catch { /* 事件行 */ }
    }
  })
  proc.stderr.on('data', () => { /* sidecar 日志走文件，忽略 */ })

  if (SEG !== 1) {
    const r = await call(1, 'save_params', { values: { 'screencast.browser_segment_seconds': SEG } })
    chk(`save_params 覆盖为 ${SEG}s`, r?.data?.ok, true)
  }
  const gp = await call(2, 'get_params')
  const specs = gp?.data?.specs ?? []
  const st0 = specs.find((s) => s.key === 'screencast.browser_segment_seconds')
  chk(`投屏前参数值 = ${SEG}`, st0?.value, SEG)
  if (st0?.value !== SEG) console.log(`  诊断: specs=${specs.length} ${JSON.stringify(gp).slice(0, 240)}`)

  const started = (await call(3, 'start_screen_cast', {
    target: 'browser',
    source: { type: 'screen', id: 'desktop' },
    audio: { mic: false, system: false },
    video: { width: 1280, height: 720, fps: 25, bitrate: '2M' },
  })).data
  chk('浏览器通道投屏已启动', started?.ok, true)
  if (!started?.ok) throw new Error(JSON.stringify(started))
  const watchUrl = started.watch_url
  const m3u8 = watchUrl.replace(/\/r\/[0-9a-f]+$/, `/hls/${started.session_id}/live.m3u8`)
  console.log(`  观看页 ${watchUrl}`)
  const startedAt = ((await call(4, 'screen_cast_status')).data || {}).started_at

  // 播放列表先看一眼，确认分片时长（浏览器与本机读的是同一份）
  await sleep(6000)
  let playlist = ''
  for (let i = 0; i < 15 && !playlist; i++) {
    try { playlist = await (await fetch(m3u8)).text() } catch { await sleep(1000) }
  }
  const td = /#EXT-X-TARGETDURATION:(\d+)/.exec(playlist)?.[1]
  chk(`播放列表 TARGETDURATION = ${SEG}`, Number(td), SEG)
  const extinf = [...playlist.matchAll(/#EXTINF:([\d.]+)/g)].map((m) => Number(m[1]))
  if (extinf.length) {
    const mean = extinf.reduce((x, y) => x + y, 0) / extinf.length
    assert(`EXTINF 实测均值 ≈${SEG}s`, Math.abs(mean - SEG) <= Math.max(0.6, SEG * 0.35),
           `avg=${mean.toFixed(2)} n=${extinf.length}`)
  } else {
    fail('播放列表里有分片', playlist.slice(0, 120))
  }

  // 观看页 HTML：低延迟配置确实注入到了前端（不是模板里的占位符）
  const html = await (await fetch(watchUrl)).text()
  chk('观看页 HTTP 200 且是 HTML', /<video id="video"/.test(html), true)
  assert('__PLAYER_CONFIG__ 占位符已被替换', !html.includes('__PLAYER_CONFIG__'))
  const cfg = /var playerCfg = (\{[^;]*\});/.exec(html)?.[1] ?? ''
  assert('注入的 hls.js 配置含 lowLatencyMode/liveSyncDurationCount',
         /lowLatencyMode/.test(cfg) && /liveSyncDurationCount/.test(cfg), cfg.slice(0, 120))

  // ffmpeg 侧证据：-hls_time 与关键帧间隔跟着分片时长走
  const log = readFileSync(join(process.env.HOME ?? tmpdir(),
    'Library/Application Support/HanCast/logs/hancast.log'), 'utf8')
  const cmdLine = [...log.matchAll(/HLS cmd: (.*)$/gm)].map((m) => m[1]).pop() ?? ''
  assert(`FFmpeg 用 -hls_time ${SEG}`, new RegExp(`-hls_time ${SEG}(\\s|$)`).test(cmdLine),
         cmdLine ? `-hls_time 匹配自本次 cmd` : '日志里没找到 HLS cmd 行')
  assert(`关键帧间隔 -g = fps×${SEG}（HLS 只能在关键帧切分片）`, /-g (25|100)\b/.test(cmdLine),
         /-g \d+/.exec(cmdLine)?.[0] ?? '无 -g')

  console.log('\n=== 真实浏览器打开观看页 ===')
  profile = mkdtempSync(path.join(tmpdir(), 'hancast-watch-'))
  chrome = spawn(findShell(), [
    `--remote-debugging-port=${CDP_PORT}`, `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check', '--mute-audio',
    '--autoplay-policy=no-user-gesture-required',
    '--window-size=1280,800', 'about:blank',
  ], { stdio: 'ignore' })

  let base
  for (let i = 0; i < 70 && !base; i++) {
    try { base = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`)).json() } catch { await sleep(300) }
  }
  if (!base) throw new Error('CDP 未就绪')

  const page = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/new?${encodeURIComponent(watchUrl)}`, { method: 'PUT' })).json()
  const ws = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej) })
  let mid = 0
  const pending = new Map()
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data)
    if (m.id && pending.has(m.id)) { const { r, j } = pending.get(m.id); pending.delete(m.id); m.error ? j(new Error(m.error.message)) : r(m.result) }
  })
  const send = (method, params = {}) => new Promise((r, j) => { const id = ++mid; pending.set(id, { r, j }); ws.send(JSON.stringify({ id, method, params })) })
  const evalJs = async (expression) => {
    const out = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (out.exceptionDetails) throw new Error(out.exceptionDetails.exception?.description ?? out.exceptionDetails.text)
    return out.result.value
  }
  await send('Page.enable')

  const probe = `(function(){
    var v = document.querySelector('video');
    if (!v) return null;
    var q = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : {};
    var b = v.buffered.length ? v.buffered.end(v.buffered.length - 1) : 0;
    return { t: Date.now(), ct: v.currentTime, rs: v.readyState, paused: v.paused,
             dropped: q.droppedVideoFrames || 0, painted: q.totalVideoFrames || 0,
             bufEnd: b, status: (document.getElementById('statusText')||{}).textContent,
             err: (document.getElementById('errText')||{}).textContent };
  })()`

  let first = null
  for (let i = 0; i < 60 && !first; i++) {
    first = await evalJs(probe)
    if (first?.ct > 0) break
    await sleep(500)
  }
  chk('观看页里有 <video> 并且开始取流', !!first, true)
  chk('状态文案切到「正在观看」', await evalJs(`(document.getElementById('statusText')||{}).textContent`), '正在观看')
  chk('没有报错文案', await evalJs(`(document.getElementById('errText')||{}).textContent`), '')

  const samples = []
  const until = Date.now() + WATCH_SECONDS * 1000
  while (Date.now() < until) { samples.push(await evalJs(probe)); await sleep(2000) }
  samples.push(await evalJs(probe))
  const a = samples[0], b = samples[samples.length - 1]
  const wall = (b.t - a.t) / 1000
  const advanced = b.ct - a.ct
  const gaps = samples.slice(1).map((s, i) => (s.t - samples[i].t) / 1000 - (s.ct - samples[i].ct))
  const maxGap = Math.max(...gaps)

  assert(`${WATCH_SECONDS}s 内画面持续推进（无卡死）`, advanced >= wall * 0.9, `${advanced.toFixed(1)}s / ${wall.toFixed(1)}s 墙钟`)
  assert('单次停顿 ≤2.5s', maxGap <= 2.5, `最大 ${maxGap.toFixed(2)}s`)
  assert('没有丢帧堆积', b.dropped - a.dropped <= 5, `新增 dropped=${b.dropped - a.dropped}`)
  assert('持续有新帧解码上屏', b.painted - a.painted >= wall * 15, `新增 frames=${b.painted - a.painted}`)

  const ahead = b.bufEnd - b.ct
  // 播放列表留 6 片、低延迟模式追到落后 2 片 ⇒ 缓冲领先上限 ≈ (6-2)×分片时长
  assert(`缓冲领先量在 ${SEG}s 分片应有的范围内（≤${4 * SEG + 1}s）`, ahead <= 4 * SEG + 1, `ahead=${ahead.toFixed(2)}s`)

  // 端到端延迟：会话已跑时长 - 播放器当前位置
  const latency = (Date.now() / 1000 - startedAt) - b.ct
  console.log(`  实测端到端延迟 ≈ ${latency.toFixed(2)}s（${SEG}s 分片）`)
  assert('延迟 < 8s（1s 分片的目标是低延迟，不能反而更差）', latency < 8, `${latency.toFixed(2)}s`)

  // 画面确实渲染出内容（画布取样，不看截图猜；屏幕静止时像素不变是正常的，
  // 「有没有画面」用亮度均值+方差判，「有没有持续解码」用上面的 painted 帧数判）
  const content = await evalJs(`(function(){
    var v=document.querySelector('video'); var c=document.createElement('canvas');
    c.width=160; c.height=90; var g=c.getContext('2d');
    g.drawImage(v,0,0,160,90);
    var d=g.getImageData(0,0,160,90).data, lum=[], i, s=0;
    for (i=0;i<d.length;i+=4){ var L=0.299*d[i]+0.587*d[i+1]+0.114*d[i+2]; lum.push(L); s+=L; }
    var m=s/lum.length, sd=Math.sqrt(lum.reduce(function(a,x){return a+(x-m)*(x-m);},0)/lum.length);
    return { mean: Math.round(m), sd: Math.round(sd*10)/10, w: v.videoWidth, h: v.videoHeight };
  })()`)
  assert('画面已出尺寸（不是 0×0）', content.w === 1280 && content.h === 720, `${content.w}×${content.h}`)
  assert('画面不是黑屏（亮度均值 >8）', content.mean > 8, `mean=${content.mean}`)
  assert('画面有真实内容（亮度标准差 >2）', content.sd > 2, `sd=${content.sd}`)

  const shot = await send('Page.captureScreenshot', { format: 'png' })
  writeFileSync('/tmp/hancast-watch-live.png', Buffer.from(shot.data, 'base64'))
  console.log('  截图 /tmp/hancast-watch-live.png')
}

main()
  .catch((e) => { fail('探针本身出错', e.message) })
  .finally(async () => {
    if (chrome) chrome.kill('SIGKILL')
    if (profile) try { rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }) } catch { /* 残留无所谓 */ }
    if (proc) {
      try {
        await call(90, 'stop_screen_cast', {}, 30000)
        if (SEG !== 1) await call(91, 'save_params', { values: { 'screencast.browser_segment_seconds': 1 } }, 20000)
        await call(99, 'exit', {}, 8000)
      } catch { /* 收尾失败不影响判据 */ }
      proc.kill('SIGKILL')
    }
    execFileSync('open', ['/Applications/HanCast.app'], { stdio: 'ignore' })
    console.log('\n已停掉投屏并把 HanCast.app 打开回来')
    console.log('RESULT:', fails === 0 ? 'ALL PASS' : `FAIL（${fails} 条）`)
    process.exit(fails === 0 ? 0 : 1)
  })
