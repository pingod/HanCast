#!/usr/bin/env node
// 文件夹投屏 / 播放队列的两处回归判据（v2.4.1）：
//   ① 队列投屏必须弹出「选设备」而不是默默用默认设备（且队列面板不能盖住它）
//   ② 一集自然播完（设备从 playing 退到 STOPPED）必须按 playMode 续投下一集
//   ③ 用户手动点「停止投屏」后，设备那条 STOPPED 不能被当成「播完」再续投
//   ④ 投单个文件时必须把上一个文件夹队列作废（否则点投屏还在投旧列表）
//   ⑤ 投屏历史按设备记账：队列只记一条、单文件另记一条
//   ⑥～⑨ 队列里的**图片项**（v2.4.12）：图片没有「播完」那一刻（后端对图片跳过进度
//      轮询，models/session.py:88-91），续播只能由发送端计时。现场是 Windows 投
//      「JOJO S05」39 集 + Cover.jpg：自然序把封面图排在第 1 项，投出去就再也动不了。
//      ⑥ 图片项到点必须自动投下一项（不等 STOPPED）
//      ⑦ 视频项不许被计时提前跳走（只有图片计时）
//      ⑧ 点停止后在途计时作废；暂停时不跳走、恢复后照样续播
//      ⑨ 停留档位可切换并落盘；repeat-one 下不反复重推同一张图
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
    // 按延时长度筛一次性延时：判据⑧用它证明「停止真的撤掉了停留计时器」，
    // 而不是只靠代次/用户停止标记兜住（那样断言没有牙齿）。
    // 队列里别处也用 setTimeout（换集去抖 1500ms、遥控器镜像去抖等），
    // 所以只能按「这条定时器是停留时长那个 ms」来认，不能数总量。
    pendingMs(ms) { return [...jobs.values()].filter((j) => !j.repeat && j.ms === ms).length },
  }
}

// 可控的内存 localStorage：判据⑨要看「档位到底落盘了没」
function makeStorage() {
  const data = new Map()
  return {
    data,
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => { data.set(k, String(v)) },
    removeItem: (k) => { data.delete(k) },
  }
}

function loadModules(api, clock, listeners, storage) {
  const imports = {
    vue: { ...vue, Transition: vue.BaseTransition },
    pinia,
    'vue-i18n': { useI18n: () => ({ t: (k) => k }), createI18n: () => ({ global: { t: (k) => k } }) },
    '@/api/commands': api,
    '@/i18n': { tt: (k) => `«${k}»` },
    '@/stores/playlist': playlistExports, // 让 cast.ts import 到同一个 playlist 实例
    '@/stores/device': { useDeviceStore: () => ({ devices: [], selectedDevice: null }) },
    '@/stores/history': historyExports, // 同上：用真实实现，判据⑤才有意义
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
      localStorage: storage,
      window: {}, Math, Date, JSON, Object, Array, Set, Map, Number, String, Boolean, Promise, parseInt, parseFloat,
    }
    vm.runInNewContext(code, ctx, { filename: file })
    return mod.exports
  }
  const historyMod = evaluate(path.join(root, 'src/stores/history.ts'))
  Object.assign(historyExports, historyMod)
  const playlistMod = evaluate(path.join(root, 'src/stores/playlist.ts'))
  Object.assign(playlistExports, playlistMod)
  const castMod = evaluate(path.join(root, 'src/stores/cast.ts'))
  return { playlistMod, castMod }
}

let playlistExports = {}
let historyExports = {}
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
    // cast.ts 的遥控器 UI 镜像去抖与分享解析：桩必须跟住 src/stores/cast.ts 的 import 清单，
    // 否则 clock.flush() 一放到那个去抖定时器就会炸在这里。
    remoteUpdateUi: async () => ({}), resolveShare: async () => ({ ok: false, error: 'stub' }),
  }
  const api = {}
  for (const k of Object.keys(base)) api[k] = (...a) => { calls.push({ fn: k, args: a }); return base[k](...a) }
  api._calls = calls
  return api
}

function mkMedia(uri) {
  return { uri, title: uri.split('/').pop(), mime_type: 'video/mp4', thumbnail: null, file_size: 1, duration: null, qualities: [], quality: '', media_type: 'file' }
}

/** 队列里的图片项：后端 list_folder 对 .jpg 给的就是 image/jpeg */
function mkImageMedia(uri) {
  return { ...mkMedia(uri), mime_type: 'image/jpeg' }
}

/** 项 id 与 src/stores/playlist.ts 的 makeId 无关，测试里只要稳定即可 */
function mkItems(list) {
  return list.map((m, i) => ({ id: `t${i}`, media: m }))
}

async function run() {
  const clock = makeClock()
  const listeners = new Map()
  const api = makeApi()
  const storage = makeStorage()
  const { playlistMod, castMod } = loadModules(api, clock, listeners, storage)

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
  let playlist, cast, history
  const app = renderer.createApp({
    setup() {
      playlist = playlistMod.usePlaylistStore()
      cast = castMod.useCastStore()
      history = historyExports.useHistoryStore()
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
  await cast.castQueueItem('dev-1', playlist.items[0], { startsQueue: true })
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

  // ── 判据 D：手动停止后，设备那条 STOPPED 不许再触发续投 ──
  // 真实时序：用户点停止 → stopCast 发出 Stop → 设备自报 STOPPED。
  // 那条 STOPPED 与「一集播完」完全同形，修之前会立刻续投下一集，
  // 用户看到的就是「点了停止又自己投起来，得点好几次」。
  await cast.stopCast('dev-1')
  // 停止后会话可能已被清掉；把会话摆回来模拟「STOPPED 晚到、此刻会话还在」
  cast.sessions['dev-1'] = {
    device_id: 'dev-1', device_name: 'dev-1', status: 'playing',
    media: { uri: '/f/2.mp4' }, position: 0, duration: 0, volume: 80, is_muted: false,
    positionTime: '00:00:00', durationTime: '00:00:00',
  }
  const startBeforeStop = api._calls.filter((c) => c.fn === 'startCast').length
  onCastState({ payload: { device_id: 'dev-1', status: 'STOPPED' } })
  await setTimeout_(0)
  ok('手动停止后 STOPPED 不再自动续投（点一次就停得住）', () => {
    const startAfterStop = api._calls.filter((c) => c.fn === 'startCast').length
    assert.equal(startAfterStop, startBeforeStop,
      `手动停止后不应再 startCast，实际 ${startBeforeStop} → ${startAfterStop}`)
  })

  // ── 判据 E：投单个文件要把上一个文件夹队列作废 ──
  assert.ok(playlist.items.length > 0, '前置：此刻队列应还有内容')
  await cast.startCast('dev-1', 'file:///g/other.mp4', { title: 'other.mp4' })
  ok('投单个文件后旧文件夹队列被清空（不会又投回旧列表）', () => {
    assert.equal(playlist.items.length, 0, `队列应被清空，实际还有 ${playlist.items.length} 项`)
    assert.equal(playlist.playingId, null)
    const lastStart = api._calls.filter((c) => c.fn === 'startCast').pop()
    assert.equal(lastStart.args[1], 'file:///g/other.mp4')
  })

  // ── 判据 F：投屏历史按设备记账，队列只记一条 ──
  ok('队列投屏只记一条历史（自动续集不重复记账）', () => {
    const queueEntries = history.forDevice('dev-1').filter((e) => e.kind === 'queue')
    assert.equal(queueEntries.length, 1, `队列历史应为 1 条，实际 ${queueEntries.length}`)
    assert.equal(queueEntries[0].folder, '/f')
    assert.equal(queueEntries[0].count, 3)
  })
  ok('单文件投屏另记一条历史，且能按设备查回', () => {
    const fileEntries = history.forDevice('dev-1').filter((e) => e.kind === 'file')
    assert.equal(fileEntries.length, 1, `单文件历史应为 1 条，实际 ${fileEntries.length}`)
    assert.equal(fileEntries[0].uri, 'file:///g/other.mp4')
    assert.ok(history.deviceIdsByRecency().includes('dev-1'))
  })

  // ── 判据 G～K：队列里的**图片项**（2026-11-06 现场回归）──
  // 现场：Windows 投 `JOJO S05 黄金之风`（39 集 mp4 + Cover.jpg）到 macOS，
  // 自然序把封面图排在第 1 项；图片不会让设备自报 STOPPED，于是 39 集一集都不会续。
  const startsFor = (id) => api._calls.filter((c) => c.fn === 'startCast' && c.args[0] === id).length
  const lastStartFor = (id) => api._calls.filter((c) => c.fn === 'startCast' && c.args[0] === id).pop()

  async function setupImageQueue(mode = 'sequential') {
    playlist.playMode = mode
    playlist.stopAfterCurrent = false
    playlist.items = mkItems([mkImageMedia('/f/Cover.jpg'), mkMedia('/f/1.mp4'), mkMedia('/f/2.mp4')])
    playlist.selected = new Set(playlist.items.map((it) => it.id))
    playlist.folder = '/f'
    playlist.playingId = null
  }

  await setupImageQueue()
  ok('前置：图片停留默认 8 秒', () => assert.equal(playlist.imageDwellSeconds, 8))

  const gBefore = startsFor('dev-img')
  assert.equal(await cast.castQueueItem('dev-img', playlist.items[0], { startsQueue: true }), true)
  ok('图片项刚投出去时没有任何续播（设备不会报 STOPPED，判据要的是计时之后）', () => {
    assert.equal(startsFor('dev-img'), gBefore + 1, `只应有 Cover.jpg 那一次起播，实际 ${gBefore} → ${startsFor('dev-img')}`)
    assert.equal(playlist.playingId, 't0')
  })
  clock.flush()
  await setTimeout_(0)
  ok('图片项：停留计时到点自动投下一项（不等设备报 STOPPED）', () => {
    assert.equal(startsFor('dev-img'), gBefore + 2, `应再发起一次 startCast，实际 ${gBefore} → ${startsFor('dev-img')}`)
    assert.equal(lastStartFor('dev-img').args[1], '/f/1.mp4', `续播的应是第 1 集，实际 ${lastStartFor('dev-img').args[1]}`)
    assert.equal(playlist.playingId, 't1')
  })

  await setupImageQueue()
  const hBefore = startsFor('dev-vid')
  await cast.castQueueItem('dev-vid', playlist.items[1], { startsQueue: true })
  clock.flush()
  await setTimeout_(0)
  ok('视频项不会被停留计时提前跳走（只有图片才计时）', () => {
    assert.equal(startsFor('dev-vid'), hBefore + 1, `视频项只应起播一次，实际 ${hBefore} → ${startsFor('dev-vid')}`)
    assert.equal(playlist.playingId, 't1')
  })

  await setupImageQueue()
  const iBefore = startsFor('dev-stop')
  const dwellMs = () => playlist.imageDwellSeconds * 1000
  await cast.castQueueItem('dev-stop', playlist.items[0], { startsQueue: true })
  ok('前置：图片项投出去后确实挂着一个停留计时', () => {
    assert.equal(clock.pendingMs(dwellMs()), 1, `应有一个 ${dwellMs()}ms 的停留计时，实际 ${clock.pendingMs(dwellMs())}`)
  })
  await cast.stopCast('dev-stop')
  ok('点停止必须把在途的停留计时撤掉（不许留个定时器在那儿到点拉起设备）', () => {
    assert.equal(clock.pendingMs(dwellMs()), 0, `停止后停留计时应清零，实际还剩 ${clock.pendingMs(dwellMs())}`)
  })
  clock.flush()
  await setTimeout_(0)
  ok('停止后不会再投下一项', () => {
    assert.equal(startsFor('dev-stop'), iBefore + 1, `停止后不该再 startCast，实际 ${iBefore} → ${startsFor('dev-stop')}`)
  })

  await setupImageQueue()
  const jBefore = startsFor('dev-pause')
  await cast.castQueueItem('dev-pause', playlist.items[0], { startsQueue: true })
  cast.sessions['dev-pause'].status = 'paused'
  clock.flush()
  await setTimeout_(0)
  ok('暂停时图片不跳走（计时重新挂起，尊重用户按下的暂停）', () => {
    assert.equal(startsFor('dev-pause'), jBefore + 1, `暂停期间不该续播，实际 ${jBefore} → ${startsFor('dev-pause')}`)
  })
  cast.sessions['dev-pause'].status = 'playing'
  clock.flush()
  await setTimeout_(0)
  ok('恢复播放后照样续下一项', () => {
    assert.equal(startsFor('dev-pause'), jBefore + 2, `恢复后应续播，实际 ${jBefore} → ${startsFor('dev-pause')}`)
    assert.equal(lastStartFor('dev-pause').args[1], '/f/1.mp4')
  })

  await setupImageQueue('repeat-one')
  const kBefore = startsFor('dev-repeat')
  await cast.castQueueItem('dev-repeat', playlist.items[0], { startsQueue: true })
  clock.flush()
  await setTimeout_(0)
  ok('单曲循环下图片不反复重推同一张（每 N 秒闪一次）', () => {
    assert.equal(startsFor('dev-repeat'), kBefore + 1, `不该重推，实际 ${kBefore} → ${startsFor('dev-repeat')}`)
  })

  // ── 判据 L：停留档位可切、可落盘、非法值回默认 ──
  ok('停留档位可切换并落盘（关掉程序再开还在）', () => {
    assert.equal(playlist.imageDwellSeconds, 8)
    playlist.cycleImageDwell()
    assert.equal(playlist.imageDwellSeconds, 15, `8 的下一档应是 15，实际 ${playlist.imageDwellSeconds}`)
    const raw = storage.data.get('hancast.playlist.queue')
    assert.ok(raw, '队列应已写盘')
    assert.equal(JSON.parse(raw).imageDwellSeconds, 15)
    playlist.setImageDwellSeconds(30)
    assert.equal(JSON.parse(storage.data.get('hancast.playlist.queue')).imageDwellSeconds, 30)
    playlist.setImageDwellSeconds(0)
    assert.equal(playlist.imageDwellSeconds, 8, '非法值应回默认 8')
  })

  ok('队列含图片项时界面才该露出档位（纯视频队列不该看见）', () => {
    playlist.items = mkItems([mkImageMedia('/f/Cover.jpg'), mkMedia('/f/1.mp4')])
    playlist.selected = new Set(playlist.items.map((it) => it.id))
    assert.equal(playlist.hasImageItems, true)
    playlist.items = mkItems([mkMedia('/f/1.mp4'), mkMedia('/f/2.mp4')])
    playlist.selected = new Set(playlist.items.map((it) => it.id))
    assert.equal(playlist.hasImageItems, false, '纯视频队列不该显示图片停留档位')
  })

  console.log(`\n${passed} passed, ${failed} failed`)
  if (failed > 0) process.exit(1)
}

// 用真实事件循环的 setTimeout（判据内部 await microtask）
const setTimeout_ = (fn) => Promise.resolve().then(fn)

run().catch((e) => { console.error('HARNESS ERROR:', e); process.exit(1) })
