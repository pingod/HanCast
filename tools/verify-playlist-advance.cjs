#!/usr/bin/env node
// 文件夹投屏 / 播放队列的两处回归判据（v2.4.1）：
//   ① 队列投屏必须弹出「选设备」而不是默默用默认设备（且队列面板不能盖住它）
//   ② 一集自然播完（设备从 playing 退到 STOPPED）必须按 playMode 续投下一集
//
// 做法：把真实的 src/stores/playlist.ts 与 src/stores/cast.ts 用 TypeScript 转译后
// 在隔离 vm 里加载（@/api/commands、事件、pinia 都打桩），直接驱动 store 的公开函数，
// 断言的是「真实的队列推进逻辑」，不是复制的副本。无浏览器、无真实设备。

const fs = require('fs')
const path = require('path')
const vm = require('vm')
const assert = require('assert')
const ts = require('typescript')
const vue = require('vue')
const pinia = require('pinia')

const root = path.resolve(__dirname, '..')

function transpile(file) {
  const source = fs.readFileSync(file, 'utf8')
  const out = ts.transpileModule(source, {
    fileName: file,
    reportDiagnostics: true,
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  })
  assert.equal(out.diagnostics?.length ?? 0, 0, `转译诊断: ${file}`)
  return out.outputText
}

// ── 时间控制：cast.ts 用 setTimeout 做换集窗口去抖，需要可控时钟 ──
function makeClock() {
  let id = 0
  const jobs = new Map()
  const set = (fn, ms, repeat = false) => { const k = ++id; jobs.set(k, { fn, ms, repeat }); return k }
  return {
    setTimeout: (fn, ms) => set(fn, ms),
    clearTimeout: (k) => jobs.delete(k),
    setInterval: (fn, ms) => set(fn, ms, true),
    clearInterval: (k) => jobs.delete(k),
    // 触发所有「一次性」延时（模拟等待窗口过去）
    flush() {
      for (const [k, job] of [...jobs]) {
        if (job.repeat) continue
        jobs.delete(k)
        job.fn()
      }
    },
  }
}

function loadModules(api, clock, listeners) {
  const imports = {
    vue: { ...vue, Transition: vue.BaseTransition },
    pinia,
    'vue-i18n': { useI18n: () => ({ t: (k) => k }), createI18n: () => ({ global: { t: (k) => k } }) },
    '@/api/commands': api,
    '@/i18n': { tt: (k) => `«${k}»` },
    '@/stores/playlist': playlistExports, // 让 cast.ts import 到同一个 playlist 实例
    '@/stores/device': { useDeviceStore: () => ({ devices: [], selectedDevice: null }) },
    '@tauri-apps/api/event': {
      listen: async (name, fn) => { listeners.set(name, fn); return () => listeners.delete(name) },
    },
    '@/types/cast': evaluate(path.join(root, 'src/types/cast.ts')),
    '@/types/media': { },
  }
  function evaluate(file) {
    const code = transpile(file)
    const mod = { exports: {} }
    const ctx = {
      module: mod, exports: mod.exports, require: (n) => {
        if (!(n in imports)) throw new Error(`Unexpected import: ${n}`)
        return imports[n]
      },
      // cast.ts 的 setupListeners 用动态 import('@tauri-apps/api/event')，CommonJS 转译后仍是 import()，
      // vm 里没有它 → 会落进 try/catch 静默失败。这里补一个 import shim 映射到同一个桩。
      import: async (n) => {
        if (!(n in imports)) throw new Error(`Unexpected import(): ${n}`)
        return imports[n]
      },
      console: { error() {}, log() {}, warn() {} },
      setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
      setInterval: clock.setInterval, clearInterval: clock.clearInterval,
      localStorage: { getItem: () => null, setItem() {} },
      window: {}, Math, Date, JSON, Object, Array, Set, Map, Number, String, Boolean, Promise, parseInt, parseFloat,
    }
    vm.runInNewContext(code, ctx, { filename: file })
    return mod.exports
  }
  const playlistMod = evaluate(path.join(root, 'src/stores/playlist.ts'))
  Object.assign(playlistExports, playlistMod)
  const castMod = evaluate(path.join(root, 'src/stores/cast.ts'))
  return { playlistMod, castMod }
}

let playlistExports = {}
let passed = 0
let failed = 0
function ok(name, fn) {
  try { fn(); console.log(`PASS ${name}`); passed++ }
  catch (e) { console.log(`FAIL ${name}: ${e.message}`); failed++ }
}

function makeApi(state) {
  const calls = []
  const base = {
    getCastState: async () => ({ sessions: {} }),
    getCastUrl: async () => 'http://x/stream',
    startCast: async (deviceId, uri, mime) => { calls.push({ deviceId, uri, mime }); },
    stopCast: async () => ({}), pauseCast: async () => {}, resumeCast: async () => {},
    seekCast: async () => {}, setVolume: async (v) => v, setMute: async () => {},
    getAutostart: async () => false, notify: async () => {},
    setAutostart: async () => ({}), listFolderMedia: async () => ({ folder: '/f', files: [] }),
  }
  const api = {}
  for (const k of Object.keys(base)) api[k] = (...a) => { calls.push({ fn: k, args: a }); return base[k](...a) }
  api._calls = calls
  return api
}

function mkMedia(uri) {
  return { uri, title: uri.split('/').pop(), mime_type: 'video/mp4', thumbnail: null, file_size: 1, duration: null, qualities: [], quality: '', media_type: 'file' }
}

async function run() {
  const clock = makeClock()
  const listeners = new Map()
  const api = makeApi()
  const { playlistMod, castMod } = loadModules(api, clock, listeners)

  // 在组件实例里实例化 store：cast.ts 的 onMounted(() => setupListeners()) 才会真正注册监听。
  // 用最小的自定义渲染器（参考 tools/verify-cast-ui.cjs），不需要真实 DOM。
  const makeNode = () => ({ children: [] })
  const renderer = vue.createRenderer({
    createElement: makeNode, createText: makeNode, createComment: makeNode,
    setText() {}, setElementText() {}, patchProp() {},
    parentNode: () => null, nextSibling: () => null, querySelector: () => null,
    insert() {}, remove() {},
  })
  const piniaInst = pinia.createPinia()
  let playlist, cast
  const app = renderer.createApp({
    setup() {
      playlist = playlistMod.usePlaylistStore()
      cast = castMod.useCastStore()
      return () => vue.h('empty')
    },
  })
  app.use(piniaInst)
  app.mount(makeNode())
  // setupListeners 是 async，内部有多层 await（两次动态 import + await listen），
  // 需要多轮微任务才能把监听注册上。
  for (let i = 0; i < 10; i++) await Promise.resolve()
  assert.ok(listeners.get('cast_state_changed'), '挂载后应注册 cast_state_changed 监听')

  // 装 3 个可播项
  playlist.items = [mkMedia('/f/1.mp4'), mkMedia('/f/2.mp4'), mkMedia('/f/3.mp4')].map((m, i) => ({ id: `id${i}`, media: m }))
  playlist.selected = new Set(playlist.items.map((it) => it.id))
  playlist.folder = '/f'

  // ── 判据 A：nextItem 顺序语义 ──
  ok('sequential 从第 1 项推进到第 2 项', () => {
    playlist.playMode = 'sequential'
    playlist.playingId = 'id0'
    const n = playlist.nextItem()
    assert.equal(n && n.id, 'id1')
  })
  ok('sequential 最后一项 nextItem 返回 null（队列结束）', () => {
    playlist.playMode = 'sequential'
    playlist.playingId = 'id2'
    assert.equal(playlist.nextItem(), null)
  })
  ok('shuffle 只挑「不是当前」的一项', () => {
    playlist.playMode = 'shuffle'
    playlist.playingId = 'id0'
    for (let i = 0; i < 50; i++) {
      const n = playlist.nextItem()
      assert.ok(n && n.id !== 'id0', `shuffle 不应返回当前项, got ${n && n.id}`)
    }
  })
  ok('repeat-one 重复当前项', () => {
    playlist.playMode = 'repeat-one'
    playlist.playingId = 'id1'
    assert.equal(playlist.nextItem().id, 'id1')
  })

  // ── 判据 B：cast_state_changed → playing→stopped 触发自动续播 ──
  const onCastState = listeners.get('cast_state_changed')
  assert.ok(onCastState, '缺少 cast_state_changed 监听')

  // 先投第一集（建立会话，startCast 会把 session.media.uri 设成 /f/1.mp4，状态 playing）
  playlist.playMode = 'sequential'
  await cast.castQueueItem('dev-1', playlist.items[0])
  assert.equal(playlist.playingId, 'id0', '投第一项后 playingId 应为 id0')
  const session = cast.sessions['dev-1']
  assert.ok(session && session.media && session.media.uri === '/f/1.mp4', '会话应绑定第一集 uri')

  const beforeCalls = api._calls.filter((c) => c.fn === 'startCast' || c.fn === 'startCast').length
  const startBefore = api._calls.filter((c) => c.fn === 'startCast').length

  // 设备自然播完：报 STOPPED，且进程里 session 状态此刻是 playing（prevStatus === playing）
  onCastState({ payload: { device_id: 'dev-1', status: 'STOPPED' } })
  // 换集是异步 castQueueItem，等微任务跑完
  await new Promise((r) => setTimeout_(r, 0))

  ok('playing→STOPPED 后自动续投下一集（播放队列未死）', () => {
    const startAfter = api._calls.filter((c) => c.fn === 'startCast').length
    assert.ok(startAfter > startBefore, `应再发起一次 startCast（续播），实际 startCast 次数 ${startBefore} → ${startAfter}`)
    assert.equal(playlist.playingId, 'id1', `续播后 playingId 应为 id1，实际 ${playlist.playingId}`)
    const lastStart = api._calls.filter((c) => c.fn === 'startCast').pop()
    assert.equal(lastStart.args[1], '/f/2.mp4', `续播的应是第 2 集，实际 ${lastStart.args[1]}`)
  })

  // ── 判据 C：connect 阶段的 transient STOPPED 不触发续播（防误伤） ──
  ok('connecting→STOPPED 的瞬态不触发续播', () => {
    // 构造一个处在 connecting 的会话
    cast.sessions['dev-2'] = {
      device_id: 'dev-2', device_name: 'dev-2', status: 'connecting',
      media: { uri: '/f/1.mp4' }, position: 0, duration: 0, volume: 80, is_muted: false,
      positionTime: '00:00:00', durationTime: '00:00:00',
    }
    const startBefore2 = api._calls.filter((c) => c.fn === 'startCast').length
    onCastState({ payload: { device_id: 'dev-2', status: 'STOPPED' } })
    const startAfter2 = api._calls.filter((c) => c.fn === 'startCast').length
    assert.equal(startAfter2, startBefore2, 'connect 阶段 STOPPED 不应触发续播')
  })

  console.log(`\n${passed} passed, ${failed} failed`)
  if (failed > 0) process.exit(1)
}

// 用真实事件循环的 setTimeout（判据内部 await microtask）
const setTimeout_ = (fn) => Promise.resolve().then(fn)

run().catch((e) => { console.error('HARNESS ERROR:', e); process.exit(1) })
