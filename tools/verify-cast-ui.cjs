#!/usr/bin/env node
// Isolated regressions: real Vue/Pinia + compiled production store/SFC.
// No network, device, filesystem writes, browser, or additional dependencies.
// Run: node tools/verify-cast-ui.cjs
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')
const pinia = require('pinia')
const { createI18n } = require('vue-i18n')
const { parse, compileScript, compileTemplate, compileStyle } = require('@vue/compiler-sfc')

const root = path.resolve(__dirname, '..')
const storeFile = path.join(root, 'src/stores/cast.ts')
const screenStoreFile = path.join(root, 'src/stores/screencast.ts')
const controllerFile = path.join(root, 'src/components/CastController.vue')
const storeSource = fs.readFileSync(storeFile, 'utf8')
const screenStoreSource = fs.readFileSync(screenStoreFile, 'utf8')
const controllerSource = fs.readFileSync(controllerFile, 'utf8')
const { descriptor, errors: parseErrors } = parse(controllerSource, { filename: controllerFile })
assert.deepEqual(parseErrors, [])
const script = compileScript(descriptor, { id: 'cast-ui-test' })
const template = compileTemplate({
  source: descriptor.template.content,
  filename: controllerFile,
  id: 'cast-ui-test',
  compilerOptions: { bindingMetadata: script.bindings },
})
assert.deepEqual(template.errors, [])
const styles = compileStyle({ source: descriptor.styles[0].content, filename: controllerFile, id: 'cast-ui-test', scoped: true })
assert.deepEqual(styles.errors, [])
const messages = Object.fromEntries(['zh-CN', 'en-US'].map(locale => [locale,
  JSON.parse(fs.readFileSync(path.join(root, 'src/locales', `${locale}.json`), 'utf8')),
]))

async function flush() {
  for (let i = 0; i < 20; i++) await Promise.resolve()
  await vue.nextTick()
}

class Clock {
  now = 0
  sequence = 0
  jobs = new Map()
  set(fn, delay, repeat = false) {
    const id = ++this.sequence
    this.jobs.set(id, { fn, at: this.now + delay, delay, repeat })
    return id
  }
  async tick(ms) {
    const end = this.now + ms
    let iterations = 0
    while (true) {
      const next = Math.min(...[...this.jobs.values()].map(job => job.at))
      if (next > end) break
      assert.ok(++iterations < 10000, 'timer runaway')
      this.now = next
      
for (const [id, job] of [...this.jobs]) {
        if (job.at !== next || !this.jobs.has(id)) continue
        if (job.repeat) job.at += job.delay
        else this.jobs.delete(id)
        job.fn()
      }
      await flush()
    }
    this.now = end
    await flush()
  }
}

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const event = value => ({ target: { value: String(value) } })
const info = (position = '00:00:00', duration = '00:00:00') => ({ status: 'playing', position, duration })
const media = {
  title: 'Sample', mime_type: 'video/mp4', quality: 'low', source_url: 'https://invalid.test/source',
  qualities: [{ id: 'low', label: 'Low' }, { id: 'high', label: 'High' }],
}

// Minimal host tree, not a DOM emulator. Vue renders the actual SFC template;
// only CSS transitions use BaseTransition because this test has no browser.
function node(type, text = '') { return { type, text, props: {}, children: [], parent: null } }
function makeRenderer(body) {
  return vue.createRenderer({
    createElement: type => node(type), createText: text => node('#text', text),
    createComment: text => node('#comment', text),
    setText: (el, text) => { el.text = text },
    setElementText: (el, text) => { el.text = text; el.children = [] },
    patchProp: (el, key, prev, next) => { el.props[key] = next },
    parentNode: el => el.parent,
    nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
    querySelector: selector => selector === 'body' ? body : null,
    insert(el, parent, anchor = null) {
      if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1)
      const index = anchor ? parent.children.indexOf(anchor) : -1
      parent.children.splice(index < 0 ? parent.children.length : index, 0, el)
      el.parent = parent
    },
    remove(el) {
      if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1)
      el.parent = null
    },
  })
}
function allNodes(el) { return [el, ...el.children.flatMap(allNodes)] }
function textOf(el) { return el.text + el.children.map(textOf).join('') }

function evaluate(source, filename, imports, clock, logs, globals = {}) {
  const result = ts.transpileModule(source, {
    fileName: filename,
    reportDiagnostics: true,
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  })
  assert.equal(result.diagnostics?.length ?? 0, 0, filename)
  const exports = {}
  vm.runInNewContext(result.outputText, {
    exports,
    require(name) {
      if (!(name in imports)) throw new Error(`Unexpected import (must stay isolated): ${name}`)
      return imports[name]
    },
    console: { ...console, error: (...args) => logs.push(args) },
    setTimeout: (fn, ms) => clock.set(fn, ms),
    clearTimeout: id => clock.jobs.delete(id),
    setInterval: (fn, ms) => clock.set(fn, ms, true),
    clearInterval: id => clock.jobs.delete(id),
    localStorage: { getItem: () => 'true', setItem() {} },
    window: {},
    ...globals,
  }, { filename })
  return exports
}

async function fixture(locale = 'en-US') {
  const clock = new Clock()
  const logs = []
  const calls = []
  // 系统通知记录：断言「失败必须落到 OS 通知」，而不是只写进界面红条
  const notified = []
  // 设备表：投屏失败的通知要带设备名，测试里按需塞进去
  const deviceList = vue.ref([])
  const handlers = {
    getCastState: async () => ({ sessions: {} }), getCastUrl: async () => info(),
    startCast: async () => {}, stopCast: async () => ({}), pauseCast: async () => {},
    resumeCast: async () => {}, seekCast: async () => {}, setVolume: async v => v,
    setMute: async () => {}, getAutostart: async () => true,
    notify: async (title, body) => { notified.push({ title, body }) },
    resolveShare: async () => ({ ...media, uri: 'https://invalid.test/high.mp4', quality: 'high' }),
    // 屏幕投屏 store 的壳层命令：只为让真实 screencast.ts 能装起来，默认全走成功路径
    listScreenSources: async () => ({ screens: [], windows: [], audio_inputs: [], ffmpeg: { ok: true } }),
    startScreenCast: async () => ({ ok: true }),
    stopScreenCast: async () => ({}),
    getScreenCastStatus: async () => ({ running: false }),
    setFfmpegPath: async () => ({ ok: true }),
    ffmpegStatus: async () => ({ ok: true }),
    ffmpegDownloadStart: async () => ({}),
    ffmpegDownloadStatus: async () => null,
    openFfmpegDir: async () => ({ ok: true }),
  }
  const api = Object.fromEntries(Object.keys(handlers).map(name => [name, (...args) => {
    calls.push({ name, args })
    return handlers[name](...args)
  }]))
  const listeners = new Map()
  const imports = {
    vue: { ...vue, Transition: vue.BaseTransition }, pinia,
    'vue-i18n': require('vue-i18n'),
    '@/api/commands': api,
    '@/i18n': { tt: (key) => `«${key}»` },
    '@tauri-apps/api/event': { listen: async (name, fn) => {
      listeners.set(name, fn)
      return () => listeners.delete(name)
    } },
  }
  imports['@/types/cast'] = evaluate(fs.readFileSync(path.join(root, 'src/types/cast.ts'), 'utf8'),
    path.join(root, 'src/types/cast.ts'), imports, clock, logs)
  imports['@/stores/device'] = { useDeviceStore: pinia.defineStore('device', () => ({ devices: deviceList })) }
  // cast.ts 会反查播放队列（自动续播/切集）。桩必须可变：v2.4.2 的两处停止缺陷
  // （停止后又被自动续投 / 停止后投新东西仍沿用旧文件夹队列）只能靠改队列状态复现。
  // 注意两点：① pinia 会把 setup 返回值**深拷贝**成自己的 state，所以状态只能通过
  // store 代理读写（拿原始对象改是改不到的）；② 方法里的 `this` 绑在 store 上，
  // 计数器必须写成 state 属性再用 `this.x += 1`，否则测试读不到。
  imports['@/stores/playlist'] = { usePlaylistStore: pinia.defineStore('playlist', () => ({
    hasQueue: false, items: [], playingId: null, playingIndex: 0, total: 0,
    nextCalls: 0, resetCalls: 0, nextValue: null, stopAfterCurrent: false,
    folder: '', playMode: 'order',
    nextItem() { this.nextCalls += 1; return this.nextValue },
    manualNext: () => null,
    manualPrev: () => null,
    setPlayingId(id) { this.playingId = id },
    setStopAfterCurrent(v) { this.stopAfterCurrent = v },
    setPlayMode(m) { this.playMode = m },
    appendFolder() { return Promise.resolve(0) },
    appendItems() { return 0 },
    reset() { this.resetCalls += 1; this.hasQueue = false; this.items = [] },
  })) }
  // cast.ts 投屏成功后会写「投屏历史」（按设备记账）。这里用记调用的桩：
  // 历史本身的行为由下面「history store」那组用例直接测真实现，两边不重复。
  const historyRecords = []
  imports['@/stores/history'] = { useHistoryStore: pinia.defineStore('history', () => ({
    entries: [], forDevice: () => [], deviceIdsByRecency: () => [],
    record(entry) { historyRecords.push(entry) },
    removeEntry() {}, clear() {},
  })) }
  const storeModule = evaluate(storeSource, storeFile, imports, clock, logs)
  imports['@/stores/cast'] = storeModule
  const screenModule = evaluate(screenStoreSource, screenStoreFile, imports, clock, logs)
  const Controller = evaluate(script.content, controllerFile, imports, clock, logs).default
  Controller.render = evaluate(template.code, `${controllerFile}.render.js`, imports, clock, logs).render
  const body = node('body')
  const container = node('root')
  const renderer = makeRenderer(body)
  const storeRoot = pinia.createPinia()
  const showChild = vue.ref(true)
  let playlistStore = null
  let store, screen, instance
  const app = renderer.createApp({
    setup() {
      instance = vue.getCurrentInstance()
      store = storeModule.useCastStore()
      screen = screenModule.useScreenCastStore()
      // 队列桩在 setup 里先实化，测试在起播前就能改队列状态（模拟「上一个文件夹还在队列里」）
      playlistStore = imports['@/stores/playlist'].usePlaylistStore()
      return () => showChild.value ? vue.h(Controller) : vue.h('empty')
    },
  })
  app.use(storeRoot)
  app.use(createI18n({ legacy: false, locale, messages, missingWarn: false, fallbackWarn: false }))
  app.mount(container)
  await flush()
  return {
    store, screen, handlers, calls, clock, logs, listeners, body, showChild, notified, deviceList, historyRecords,
    get playlist() { return playlistStore },
    get ui() { return instance.subTree.component.setupState },
    count(name) { return calls.filter(call => call.name === name).length },
    find(className) { return allNodes(body).find(el => el.props.class?.split(' ').includes(className)) },
    async start(id = 'a') { await store.startCast(id, 'https://invalid.test/low.mp4', media); await flush() },
    async close() {
      app.unmount()
      await flush()
      assert.equal(clock.jobs.size, 0, 'all store/component timers must be cleaned up')
      assert.equal(listeners.size, 0, 'listeners must be cleaned up')
      storeRoot._s.forEach(s => s.$dispose())
    },
  }
}

const tests = []
function test(name, run) { tests.push({ name, run }) }

test('first start failure exposes raw error without a session; dismiss/retry clears it', async f => {
  const reason = new Error('device refused / 设备拒绝')
  f.handlers.startCast = async () => { throw reason }
  await assert.doesNotReject(() => f.store.startCast('a', 'https://invalid.test/1'))
  assert.equal(f.store.lastError, String(reason))
  assert.equal(Object.keys(f.store.sessions).length, 0)
  assert.equal(f.store.loading, false)
  f.store.dismissError()
  assert.equal(f.store.lastError, null)
  f.handlers.startCast = async () => { throw 'sidecar timeout' }
  await f.start()
  assert.equal(f.store.lastError, 'sidecar timeout')
  f.handlers.startCast = async () => {}
  await f.start()
  assert.equal(f.store.lastError, null)
  assert.equal(f.store.sessions.a.status, 'playing')
})

test('pause/resume/seek/volume/mute failures are visible and never reject', async f => {
  await f.start()
  for (const [api, action] of [
    ['pauseCast', () => f.store.pauseCast()], ['resumeCast', () => f.store.resumeCast()],
    ['seekCast', () => f.store.seek('00:00:11')], ['setVolume', () => f.store.setVolume(30)],
    ['setMute', () => f.store.setMute(true)],
  ]) {
    f.handlers[api] = async () => { throw `${api}: transport rejected` }
    await assert.doesNotReject(action)
    assert.equal(f.store.lastError, `${api}: transport rejected`)
    f.store.dismissError()
  }
  f.handlers.pauseCast = async () => {}
  await f.store.pauseCast()
  assert.equal(f.store.sessions.a.status, 'paused')
  f.handlers.resumeCast = async () => {}
  await f.store.resumeCast()
  assert.equal(f.store.sessions.a.status, 'playing')
  f.handlers.setVolume = async v => v - 1
  assert.equal(await f.store.setVolume(30), 29)
  assert.equal(f.store.sessions.a.volume, 29)
  f.handlers.setMute = async () => {}
  await f.store.setMute(true)
  assert.equal(f.store.sessions.a.is_muted, true)
})

test('single-flight poll drops pre-seek 120, accepts 11 and later legitimate backward/zero samples', async f => {
  await f.start()
  const slow = deferred()
  f.handlers.getCastUrl = () => slow.promise
  await f.clock.tick(4000)
  assert.equal(f.count('getCastUrl'), 1, 'slow requests must not overlap')
  await f.store.seek('00:00:11')
  slow.resolve(info('00:02:00', '00:10:00'))
  await flush()
  assert.equal(f.store.positionSeconds, 11)
  assert.equal(f.store.durationSeconds, 0, 'stale duration must be discarded too')
  await f.store.pauseCast()
  f.handlers.getCastUrl = async () => info('00:00:07', '00:10:00')
  await f.clock.tick(1000)
  assert.equal(f.store.positionSeconds, 7, 'do not use Math.max for fresh samples')
  f.handlers.getCastUrl = async () => info('00:00:00', '00:10:00')
  await f.clock.tick(1000)
  assert.equal(f.store.positionSeconds, 0)
})

test('poll does not sample a pending seek, and ignores old results before seek acknowledgement', async f => {
  await f.start()
  const poll = deferred(), seek = deferred()
  f.handlers.getCastUrl = () => poll.promise
  await f.clock.tick(1000)
  f.handlers.seekCast = () => seek.promise
  const pending = f.store.seek('00:00:11')
  poll.resolve(info('00:02:00', '00:10:00'))
  await flush()
  assert.notEqual(f.store.positionSeconds, 120)
  await f.clock.tick(3000)
  assert.equal(f.count('getCastUrl'), 1)
  seek.resolve()
  await pending
  assert.equal(f.store.positionSeconds, 11)
})

test('device operation tokens are independent within a single-flight sweep', async f => {
  await f.start('a')
  await f.start('b')
  const first = deferred()
  f.handlers.getCastUrl = id => id === 'a' ? first.promise : Promise.resolve(info('00:00:23', '00:10:00'))
  await f.clock.tick(1000)
  await f.store.seek('00:00:11', 'a')
  first.resolve(info('00:02:00', '00:10:00'))
  await flush()
  assert.equal(f.store.sessions.a.position, 11)
  assert.equal(f.store.sessions.b.position, 23)
  assert.equal(f.count('getCastUrl'), 2)
})

test('old poll cannot mutate a pending replacement cast or its new session', async f => {
  await f.start()
  const poll = deferred(), start = deferred()
  f.handlers.getCastUrl = () => poll.promise
  await f.clock.tick(1000)
  f.handlers.startCast = () => start.promise
  const pending = f.store.startCast('a', 'https://invalid.test/new.mp4', media)
  await f.clock.tick(2000)
  assert.equal(f.count('getCastUrl'), 1)
  start.resolve()
  await pending
  poll.resolve(info('00:02:00', '00:10:00'))
  await flush()
  assert.equal(f.store.sessions.a.position, 0)
  assert.equal(f.store.sessions.a.media.uri, 'https://invalid.test/new.mp4')
  assert.equal(f.store.durationSeconds, 0)
})

test('Stop invalidates polls immediately; a late Stop cannot remove a newer cast', async f => {
  await f.start()
  const poll = deferred(), stop = deferred()
  f.handlers.getCastUrl = () => poll.promise
  await f.clock.tick(1000)
  f.handlers.stopCast = () => stop.promise
  const stopping = f.store.stopCast('a')
  poll.resolve(info('00:02:00', '00:10:00'))
  await flush()
  assert.notEqual(f.store.positionSeconds, 120)
  await f.store.startCast('a', 'https://invalid.test/new.mp4', media)
  stop.resolve({})
  await stopping
  assert.equal(f.store.sessions.a.media.uri, 'https://invalid.test/new.mp4')
  assert.equal(f.store.sessions.a.status, 'playing')
})

test('stop/restart preserves single-flight and ignores the old poll across polling epochs', async f => {
  await f.start()
  const poll = deferred()
  f.handlers.getCastUrl = () => poll.promise
  await f.clock.tick(1000)
  await f.store.stopCast('a')
  assert.equal(f.store.sessions.a, undefined)
  await f.start()
  await f.clock.tick(3000)
  assert.equal(f.count('getCastUrl'), 1)
  const before = f.store.positionSeconds
  poll.resolve(info('00:02:00', '00:10:00'))
  await flush()
  assert.equal(f.store.positionSeconds, before)
  f.handlers.getCastUrl = async () => { throw new Error('offline') }
  await f.clock.tick(1000)
  f.handlers.getCastUrl = async () => info('00:00:09', '00:10:00')
  await f.store.pauseCast()
  await f.clock.tick(1000)
  assert.equal(f.store.positionSeconds, 9, 'poll failure must release guard')
})

test('stop-all also cancels a pending first start; failure still preserves Stop warning', async f => {
  const start = deferred()
  f.handlers.startCast = () => start.promise
  const starting = f.store.startCast('a', 'https://invalid.test/1')
  f.handlers.stopCast = async () => { throw 'Stop timeout' }
  await f.store.stopCast()
  assert.equal(f.store.stopWarning.reasons[0], 'Stop timeout')
  start.resolve()
  await starting
  assert.equal(Object.keys(f.store.sessions).length, 0)
  assert.equal(f.store.showController, false)
})

test('out-of-order seeks and mount-state fetch cannot overwrite a newer operation', async f => {
  await f.start()
  const fetch = deferred(), oldSeek = deferred()
  f.handlers.getCastState = () => fetch.promise
  const fetching = f.store.fetchState()
  f.handlers.seekCast = () => oldSeek.promise
  const seeking = f.store.seek('00:02:00')
  f.handlers.seekCast = async () => {}
  await f.store.seek('00:00:11')
  oldSeek.resolve()
  await seeking
  fetch.resolve({ sessions: { a: { status: 'playing', position: '00:02:00', duration: '00:10:00' } } })
  await fetching
  assert.equal(f.store.positionSeconds, 11)
})

test('quality resolve/start failure restores simulation for playing, not paused sessions', async f => {
  await f.start()
  f.store.updateSession('a', { position: 10, positionTime: '00:00:10' })
  const resolve = f.handlers.resolveShare
  f.handlers.resolveShare = async () => { throw 'resolve failed' }
  await f.store.switchQuality('high')
  assert.equal(f.store.lastError, 'resolve failed')
  assert.equal(f.store.qualitySwitching, false)
  await f.clock.tick(1000)
  assert.equal(f.store.positionSeconds, 11)
  f.handlers.resolveShare = resolve
  f.handlers.startCast = async () => { throw 'quality start failed' }
  await f.store.switchQuality('high')
  assert.equal(f.store.lastError, 'quality start failed')
  assert.equal(f.store.castState.media.quality, 'low')
  await f.clock.tick(1000)
  assert.equal(f.store.positionSeconds, 12)
  await f.store.pauseCast()
  await f.store.switchQuality('high')
  await f.clock.tick(1000)
  assert.equal(f.store.positionSeconds, 12)
  assert.equal(f.store.castState.status, 'paused')
})

test('exhausted quality restore retries expose original error and restart simulation', async f => {
  await f.start()
  f.store.updateSession('a', { position: 10, positionTime: '00:00:10' })
  f.handlers.seekCast = async () => { throw 'Seek unsupported' }
  const switching = f.store.switchQuality('high')
  await flush()
  await f.clock.tick(4000)
  await switching
  assert.equal(f.count('seekCast'), 4)
  assert.equal(f.store.lastError, 'Seek unsupported')
  assert.equal(f.store.qualitySwitching, false)
  assert.equal(f.store.castState.media.quality, 'high')
  assert.equal(f.store.positionSeconds, 0)
  await f.clock.tick(1000)
  assert.equal(f.store.positionSeconds, 1)
})

test('quality success restores progress; Stop cancels delayed resolve before re-casting', async f => {
  await f.start()
  f.store.updateSession('a', { position: 10, positionTime: '00:00:10' })
  const switching = f.store.switchQuality('high')
  await flush()
  await f.clock.tick(1000)
  await switching
  assert.equal(f.store.positionSeconds, 10)
  assert.equal(f.store.lastError, null)
  const resolve = deferred()
  f.handlers.resolveShare = () => resolve.promise
  const cancelled = f.store.switchQuality('low')
  await f.store.stopCast('a')
  const count = f.count('startCast')
  resolve.resolve({ ...media, uri: 'https://invalid.test/cancelled' })
  await cancelled
  assert.equal(f.count('startCast'), count)
  assert.equal(f.store.sessions.a, undefined)
})

test('late quality restore acknowledgement cannot undo a user seek', async f => {
  await f.start()
  f.store.updateSession('a', { position: 120, positionTime: '00:02:00' })
  const oldSeek = deferred()
  f.handlers.seekCast = () => oldSeek.promise
  const switching = f.store.switchQuality('high')
  await flush()
  await f.clock.tick(1000)
  f.handlers.seekCast = async () => {}
  await f.store.seek('00:00:11')
  oldSeek.resolve()
  await switching
  assert.equal(f.store.positionSeconds, 11)
  assert.equal(f.store.lastError, null)
})

test('controller drag previews; successful seek unlocks and follows subsequent progress', async f => {
  await f.start()
  f.store.updateSession('a', { durationTime: '00:03:20', position: 20, positionTime: '00:00:20' })
  f.ui.onSeekInput(event(50))
  await f.clock.tick(1000)
  assert.equal(f.ui.displayProgress, 50)
  assert.equal(f.ui.displayPosition, '01:40')
  await f.ui.onSeekChange(event(50))
  await flush()
  assert.equal(f.ui.seekDragging, false)
  assert.equal(f.ui.seekLocked, false)
  f.store.updateSession('a', { position: 105 })
  await flush()
  assert.equal(f.ui.displayProgress, 52.5)
  assert.equal(f.ui.displayPosition, '01:45')
})

test('active seek watcher unlocks without any consumer; failure and timeout also release lock', async f => {
  await f.start()
  f.store.updateSession('a', { durationTime: '00:03:20', position: 20 })
  const seek = deferred()
  f.handlers.seekCast = () => seek.promise
  const seeking = f.ui.onSeekChange(event(50))
  await flush()
  assert.equal(f.ui.seekLocked, true)
  f.store.updateSession('a', { position: 100 })
  await flush()
  assert.equal(f.ui.seekLocked, false)
  seek.resolve()
  await seeking
  f.handlers.seekCast = async () => { throw 'seek rejected' }
  await f.ui.onSeekChange(event(10))
  assert.equal(f.ui.seekLocked, false)
  assert.equal(f.store.lastError, 'seek rejected')
  const hung = deferred()
  f.handlers.seekCast = () => hung.promise
  const hanging = f.ui.onSeekChange(event(80))
  await f.clock.tick(5000)
  assert.equal(f.ui.seekLocked, false, 'nonresponsive device must not leave a permanent lock')
  hung.resolve()
  await hanging
})

test('old seek completion cannot unlock the next seek; focus/minimize clears interaction timers', async f => {
  await f.start()
  f.store.updateSession('a', { durationTime: '00:03:20', position: 20 })
  const first = deferred(), second = deferred()
  f.handlers.seekCast = () => first.promise
  const firstChange = f.ui.onSeekChange(event(40))
  f.handlers.seekCast = () => second.promise
  const secondChange = f.ui.onSeekChange(event(80))
  first.resolve()
  await firstChange
  await flush()
  assert.equal(f.ui.seekLocked, true)
  assert.equal(f.ui.displayProgress, 80)
  f.ui.onVolumeChange(event(35))
  f.store.focusedDeviceId = 'b'
  await flush()
  assert.equal(f.ui.seekLocked, false)
  await f.clock.tick(50)
  assert.equal(f.count('setVolume'), 0, 'queued volume must not be sent to another device')
  second.resolve()
  await secondChange
  f.store.focusedDeviceId = 'a'
  await flush()
  f.ui.onSeekInput(event(40))
  f.store.minimizeController()
  await flush()
  assert.equal(f.ui.seekDragging, false)
})

test('unmount cleans seek fallback and volume debounce while root store stays alive', async f => {
  await f.start()
  f.store.updateSession('a', { durationTime: '00:03:20', position: 20 })
  const seek = deferred()
  f.handlers.seekCast = () => seek.promise
  const pending = f.ui.onSeekChange(event(70))
  f.ui.onVolumeChange(event(40))
  f.showChild.value = false
  await flush()
  assert.ok([...f.clock.jobs.values()].every(job => job.repeat), 'no component timeouts remain')
  await f.clock.tick(50)
  assert.equal(f.count('setVolume'), 0)
  seek.resolve()
  await pending
})

test('rendered alert retains escaped raw reason and dismiss button; native quality selector rolls back', async f => {
  await f.start()
  f.handlers.resolveShare = async () => { throw '<script>not HTML</script>\n' + 'x'.repeat(600) }
  const changing = f.ui.onQualityChange(event('high'))
  assert.equal(f.ui.selectedQuality, 'high')
  await changing
  await flush()
  assert.equal(f.ui.selectedQuality, 'low')
  assert.equal(f.find('ctrl__quality-select').props.value, 'low')
  const alert = f.find('ctrl__error')
  assert.equal(alert.props.role, 'alert')
  assert.ok(textOf(alert).includes(f.store.lastError))
  assert.equal(allNodes(alert).some(el => el.type === 'script'), false)
  allNodes(alert).find(el => el.type === 'button').props.onClick()
  await flush()
  assert.equal(f.find('ctrl__error'), undefined)
  assert.ok(f.find('ctrl__thumb').props['aria-label'])
})

test('scoped style and both locale contracts cover theme contrast and long/narrow content', async f => {
  const css = descriptor.styles[0].content
  for (const selector of ['ctrl__stop', 'ctrl__thumb-play']) {
    const block = css.match(new RegExp(`\\.${selector}\\s*\\{([^}]+)\\}`))[1]
    assert.match(block, /color: var\(--on-primary\)/)
    assert.match(block, /background: var\(--primary\)/)
  }
  assert.doesNotMatch(css, /linear-gradient|#[0-9a-f]{3,8}\b/i)
  assert.match(css, /max-width: calc\(100% - 24px\)/)
  assert.match(css, /max-height: calc\(100% - 24px\)/)
  assert.match(css, /overflow-y: auto/)
  assert.match(css, /overflow-wrap: anywhere/)
  assert.match(css, /\.ctrl__slider-wrap\s*\{[^}]*min-width: 0/)
  for (const locale of Object.values(messages)) {
    for (const key of ['cast.error', 'common.confirm', 'cast.play', 'cast.pause']) assert.ok(locale[key], key)
  }
  await f.start()
})

test('cast failure and undelivered stop both raise a system notification', async f => {
  const reason = 'unknown tcl 的 DLNA 控制服务返回 HTTP 500：请在电视上打开「多屏互动」'
  f.handlers.startCast = async () => { throw new Error(reason) }
  await f.store.startCast('tv9', 'https://invalid.test/a.mp4')
  assert.equal(f.notified.length, 1, '投屏失败要发一条系统通知')
  assert.equal(f.notified[0].title, '«notify.castFailed»')
  assert.ok(f.notified[0].body.includes(reason), f.notified[0].body)
  assert.ok(f.notified[0].body.includes('tv9'), '设备名/ id 要在正文里')

  f.notified.length = 0
  f.handlers.startCast = async () => {}          // 恢复正常起播，别把上一段的抛错带进来
  await f.start()
  assert.deepEqual(f.notified, [], '起播成功不该发通知')
  f.handlers.stopCast = async () => ({ control_failed: ['a'], reasons: { a: '控制接口不可达' } })
  await f.store.stopCast('a')
  assert.equal(f.notified.length, 1, '电视没确认 Stop 也要通知')
  assert.equal(f.notified[0].title, '«notify.stopNotDelivered»')
  assert.match(f.notified[0].body, /控制接口不可达/)

  // 成功路径不许发通知
  f.notified.length = 0
  await f.start()
  f.handlers.stopCast = async () => ({})
  await f.start()
  await f.store.stopCast('a')
  assert.deepEqual(f.notified, [], '正常停止不该打扰用户')
})

test('screen cast refusal, undelivered stop and mid-session error each raise one system notification', async f => {
  const reason = '电视返回 HTTP 500：请在电视上打开「多屏互动/无线投屏/DLNA」'
  f.deviceList.value = [{ id: 'tv1', name: '客厅 TCL' }]
  f.handlers.startScreenCast = async () => ({ ok: false, error: reason })
  assert.equal(await f.screen.start({ target: 'dlna', device_id: 'tv1', source: { type: 'screen' } }), false)
  await flush()
  assert.equal(f.notified.length, 1, '屏幕投屏被电视拒绝也要发系统通知')
  assert.equal(f.notified[0].title, '«notify.castFailed»')
  assert.ok(f.notified[0].body.startsWith('客厅 TCL：'), f.notified[0].body)
  assert.ok(f.notified[0].body.includes(reason), '正文要留 Sidecar 原文')

  // 只投给浏览器时没有设备名可带，正文就是原因本身
  f.notified.length = 0
  f.handlers.startScreenCast = async () => { throw new Error('FFmpeg 起不来') }
  assert.equal(await f.screen.start({ target: 'browser', source: { type: 'screen' } }), false)
  await flush()
  assert.equal(f.notified.length, 1)
  assert.ok(f.notified[0].body.includes('FFmpeg 起不来'), f.notified[0].body)
  assert.ok(!f.notified[0].body.startsWith('：'), '不该硬凑一个空设备名前缀')

  f.notified.length = 0
  f.handlers.startScreenCast = async () => ({ ok: true })
  assert.equal(await f.screen.start({ target: 'dlna', device_id: 'tv1', source: { type: 'screen' } }), true)
  await flush()
  assert.deepEqual(f.notified, [], '成功起播不该打扰用户')

  f.handlers.stopScreenCast = async () => ({ control_failed: ['tv1'], reasons: { tv1: '控制接口不可达' } })
  await f.screen.stop()
  await flush()
  assert.equal(f.notified.length, 1, '停止没送达也要通知')
  assert.equal(f.notified[0].title, '«notify.stopNotDelivered»')
  assert.match(f.notified[0].body, /客厅 TCL.*控制接口不可达/)

  // 会话中途自己死了（状态事件带 error）：同一句话连递两次也只弹一次
  f.notified.length = 0
  f.handlers.stopScreenCast = async () => ({})
  await f.screen.stop()
  const dying = { running: true, target: 'dlna', device_id: 'tv1', error: '采集进程退出：wlgrab 连不上合成器' }
  const emit = e => f.listeners.get('screen_cast_state_changed')({ payload: e })
  await emit(dying)
  await emit(dying)
  assert.equal(f.notified.length, 1, '重复的终止事件不该连弹两条')
  assert.match(f.notified[0].body, /wlgrab/)
  assert.equal(f.screen.lastError, '采集进程退出：wlgrab 连不上合成器', '面板里仍要留着这句话')
})

test('preview never loads a video as an image and recovers after a broken cover', async f => {
  await f.start()
  assert.equal(f.ui.previewUrl, null)
  assert.equal(f.find('ctrl__thumb-img'), undefined)
  f.store.updateSession('a', { media: { ...f.store.sessions.a.media, thumbnail: 'https://invalid.test/cover.jpg' } })
  await flush()
  assert.equal(f.find('ctrl__thumb-img').props.src, 'https://invalid.test/cover.jpg')
  f.find('ctrl__thumb-img').props.onError()
  await flush()
  assert.equal(f.find('ctrl__thumb-img'), undefined)
  f.store.updateSession('a', { media: { ...f.store.sessions.a.media, thumbnail: null, mime_type: '', uri: 'https://invalid.test/photo.png?token=x' } })
  await flush()
  assert.equal(f.ui.isImage, true)
  assert.equal(f.ui.previewFailed, false)
  assert.equal(f.find('ctrl__thumb-img').props.src, 'https://invalid.test/photo.png?token=x')
})

test('manual stop suppresses the queue auto-advance', async f => {
  await f.start('tv1')
  const session = f.store.sessions.tv1
  assert.ok(session, '起播应建立会话')
  const currentUri = session.media?.uri ?? 'https://invalid.test/low.mp4'
  // 造出「正在放队列里某一项」的现场
  f.playlist.hasQueue = true
  f.playlist.items = [{ id: 'i1', media: { uri: currentUri } }]
  f.playlist.nextValue = { id: 'i2', media: { uri: 'https://invalid.test/next.mp4' } }
  const castsBefore = f.count('startCast')

  // 关键时序：设备收到 Stop 后自报的 STOPPED，完全可能早于 stop_cast 的响应回来，
  // 那时本地会话还在——这条事件与「一集播完」逐字相同，正是用户报的 bug 的现场。
  f.handlers.stopCast = async () => {
    f.listeners.get('cast_state_changed')({ payload: { device_id: 'tv1', status: 'STOPPED' } })
    await flush()
    return {}
  }
  await f.store.stopCast('tv1')
  await flush()

  assert.equal(f.playlist.nextCalls, 0, '用户手动停止后的 STOPPED 不许当成自然播完')
  assert.equal(f.count('startCast'), castsBefore, '停止后又自动投下一集，就是用户点的「一次停不掉」')
})

test('casting a new item drops the stale folder queue', async f => {
  f.playlist.hasQueue = true
  f.playlist.items = [{ id: 'i1', media: { uri: 'https://invalid.test/old-folder-ep1.mp4' } }]
  await f.store.startCast('tv1', 'https://invalid.test/other.mp4')
  await flush()
  assert.equal(f.playlist.resetCalls, 1, '投单个文件必须清掉上一个文件夹的队列')
  assert.equal(f.playlist.hasQueue, false, '旧队列不能继续挂着，否则界面和续投都还在用旧列表')

  // 队列自己发起的起播不能反过来把队列清掉（否则自动续播会把队列打断）
  f.playlist.hasQueue = true
  await f.store.startCast('tv1', 'https://invalid.test/queue-ep.mp4', undefined, { fromQueue: true })
  await flush()
  assert.equal(f.playlist.resetCalls, 1, '队列发起的起播不许 reset')
  assert.equal(f.playlist.hasQueue, true)
})

/** 用真实现装一份 history store（内存 localStorage），历史语义直接在这里验 */
function loadRealHistoryStore() {
  const mem = new Map()
  const localStorage = {
    getItem: key => (mem.has(key) ? mem.get(key) : null),
    setItem: (key, value) => { mem.set(key, String(value)) },
    removeItem: key => { mem.delete(key) },
  }
  const file = path.join(root, 'src/stores/history.ts')
  const clock = new Clock()
  const logs = []
  const mod = evaluate(fs.readFileSync(file, 'utf8'), file, { vue, pinia }, clock, logs, { localStorage })
  const previous = pinia.getActivePinia?.()
  try {
    pinia.setActivePinia(pinia.createPinia())
    return { store: mod.useHistoryStore(), mem }
  } finally {
    if (previous) pinia.setActivePinia(previous)
  }
}

test('a local file is remembered by its own path, never by the temporary media URL', async f => {
  // 后端把本地文件挂到自己那个 HTTP 服务上再投给电视，所以 uri 是「本机临时地址」；
  // 重启后那套注册就没了。历史里只记 uri 的话，用户点它就是一条点不动的死链。
  const path = '/Users/pavia/Movies/纪录片/第1集.mp4'
  await f.store.startCast('tv1', 'http://192.168.1.5:51999/media/deadbeef.mp4', {
    media_type: 'file', uri: 'http://192.168.1.5:51999/media/deadbeef.mp4', title: '第1集.mp4',
    mime_type: 'video/mp4', file_size: 1, duration: null, thumbnail: null, file_path: path,
  })
  await flush()
  const rec = f.historyRecords.at(-1)
  assert.ok(rec, '投屏成功要落一条历史')
  assert.equal(rec.kind, 'file')
  assert.equal(rec.filePath, path, '本地文件必须记住原始路径，否则历史条目无法重新解析')
  assert.equal(rec.sourceUrl, undefined)
})

test('a resolved share link is remembered with its source page so it can be re-resolved', async f => {
  // 分享链解析出来的直链带签名，几小时就过期；重投要回到来源页重新解一次
  await f.store.startCast('tv1', 'https://v6-cold.douyinvod.com/xyz/video.mp4', {
    media_type: 'url', uri: 'https://v6-cold.douyinvod.com/xyz/video.mp4', title: '豆包',
    mime_type: 'video/mp4', file_size: null, duration: null, thumbnail: null,
    source_url: 'https://v.douyin.com/cAS0o-smMyw/',
  })
  await flush()
  const rec = f.historyRecords.at(-1)
  assert.equal(rec.kind, 'url')
  assert.equal(rec.sourceUrl, 'https://v.douyin.com/cAS0o-smMyw/', '链接类历史要留来源页')
  assert.equal(rec.filePath, undefined)
})

test('a folder cast is remembered as a queue with its folder and item count', async f => {
  f.playlist.hasQueue = true
  f.playlist.folder = '/movies/season1'
  f.playlist.total = 3
  await f.store.castQueueItem('tv1', { id: 'i1', media: { uri: 'https://invalid.test/e1.mp4', title: 'e1' } }, { startsQueue: true })
  await flush()
  const rec = f.historyRecords.at(-1)
  assert.equal(rec.kind, 'queue')
  assert.equal(rec.folder, '/movies/season1', '文件夹投屏要记住文件夹，重投时才能把整队重新读出来')
  assert.equal(rec.count, 3)
})

test('history store: re-casting the same item bumps a count instead of piling up rows', async () => {
  const { store, mem } = loadRealHistoryStore()
  const base = { deviceId: 'tv1', deviceName: 'TCL', uri: 'https://x/a.mp4', title: 'A', mime_type: 'video/mp4', kind: 'url', at: 1000 }
  store.record(base)
  store.record({ ...base, at: 2000 })
  assert.equal(store.entries.length, 1, '同一设备同一内容只留一行')
  assert.equal(store.entries[0].castCount, 2, '重复投同一内容要累加次数（界面上显示「投过 N 次」）')
  assert.equal(store.entries[0].at, 2000, '最近一次投放要顶到最前')

  // 持久化：重启后历史还在（localStorage 里是 JSON）
  const saved = JSON.parse(mem.get('hancast.cast.history'))
  assert.equal(saved.length, 1)
  assert.equal(saved[0].castCount, 2)
})

test('history store: per-device cap keeps the newest entries and clearing stays scoped', async () => {
  const { store } = loadRealHistoryStore()
  for (let i = 0; i < 25; i++) {
    store.record({ deviceId: 'tv1', deviceName: 'TCL', uri: `https://x/${i}.mp4`, title: `E${i}`, mime_type: '', kind: 'url', at: 1000 + i })
  }
  assert.equal(store.forDevice('tv1').length, 20, '单设备上限 20 条，超了要淘汰最旧的')
  assert.equal(store.forDevice('tv1')[0].title, 'E24', '留下的是最新的那批')

  store.record({ deviceId: 'tv2', deviceName: 'Linux', uri: 'https://x/other.mp4', title: 'other', mime_type: '', kind: 'url', at: 9000 })
  store.clear('tv1')
  assert.equal(store.forDevice('tv1').length, 0, '清空要按设备隔离')
  assert.equal(store.forDevice('tv2').length, 1, '别把别的设备的历史一起清掉')
})

;(async () => {
  // Optional test-name substring for focused follow-up checks, e.g. "preview".
  const filter = process.argv.slice(2).join(' ')
  const selected = tests.filter(({ name }) => name.includes(filter))
  assert.ok(selected.length, `No tests match: ${filter}`)
  const unhandled = []
  const onUnhandled = reason => { unhandled.push(reason) }
  process.on('unhandledRejection', onUnhandled)
  let failed = 0
  for (const { name, run } of selected) {
    let f, watchdog
    try {
      // A forgotten deferred promise must fail, not let Node silently exit 0.
      await Promise.race([
        (async () => {
          f = await fixture()
          await run(f)
          await f.close()
          f = null
          await new Promise(resolve => setImmediate(resolve))
          assert.deepEqual(unhandled, [], 'unhandled rejection')
        })(),
        new Promise((_, reject) => {
          watchdog = setTimeout(() => reject(new Error('Test did not settle within 5 seconds')), 5000)
        }),
      ])
      console.log(`PASS ${name}`)
    } catch (err) {
      failed++
      console.error(`FAIL ${name}\n`, err)
      if (f) {
        try { await f.close() } catch (cleanup) { console.error('Cleanup failure:', cleanup) }
      }
    } finally {
      clearTimeout(watchdog)
    }
  }
  process.removeListener('unhandledRejection', onUnhandled)
  console.log(`${selected.length - failed}/${selected.length} targeted tests passed; no real API/device calls.`)
  if (failed) process.exitCode = 1
})().catch(err => { console.error(err); process.exitCode = 1 })
