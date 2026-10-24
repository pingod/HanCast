#!/usr/bin/env node
// Run: node tools/verify-app-lifecycle.cjs
// Compile the REAL App.vue and HomeView.vue (script + template) in memory.
// Real Vue renderer/Pinia/stores; only leaf UI, Settings route and platform I/O
// are faked. No generated files, network, sidecar or user configuration access.
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')
const ts = require('typescript')
const { parse, compileScript } = require('@vue/compiler-sfc')
const vue = require('vue')
const pinia = require('pinia')
const { createI18n } = require('vue-i18n')

const root = path.resolve(__dirname, '..')
const appFile = path.join(root, 'src/App.vue')
const homeFile = path.join(root, 'src/views/HomeView.vue')
const targetEvents = ['cast_state_changed', 'screen_cast_state_changed']
const scopedEvents = [...targetEvents, 'cast_error', 'device_found', 'device_lost']
const leafNames = new Set([
  'GuardConfirmModal', 'UpdateModal', 'LogsPanel', 'AddDeviceModal',
  'TitleBar', 'MediaInput', 'DeviceList', 'CastController', 'ScreenCastPanel',
  'StopWarningBanner', 'Footer', 'AutostartPromptModal',
])
const compiled = new Map()

function compile(filename) {
  if (compiled.has(filename)) return compiled.get(filename)
  try {
    let source = readFileSync(filename, 'utf8')
    if (filename.endsWith('.vue')) {
      const { descriptor, errors } = parse(source, { filename })
      if (errors.length) throw new Error(errors.map(String).join('\n'))
      assert.ok(descriptor.scriptSetup, `Missing real script setup: ${filename}`)
      assert.ok(descriptor.template, `Missing real template: ${filename}`)
      source = compileScript(descriptor, {
        id: path.relative(root, filename),
        inlineTemplate: true,
      }).content
    }
    const result = ts.transpileModule(source, {
      fileName: filename.endsWith('.vue') ? `${filename}.ts` : filename,
      reportDiagnostics: true,
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
      // Supply Vite's environment, without changing any setup/lifecycle code.
      transformers: { before: [(context) => {
        const visit = (node) => {
          if (ts.isMetaProperty(node) && node.keywordToken === ts.SyntaxKind.ImportKeyword) {
            return ts.factory.createObjectLiteralExpression([
              ts.factory.createPropertyAssignment('env', ts.factory.createObjectLiteralExpression([
                ts.factory.createPropertyAssignment('DEV', ts.factory.createFalse()),
              ])),
            ])
          }
          return ts.visitEachChild(node, visit, context)
        }
        return (sourceFile) => ts.visitNode(sourceFile, visit)
      }] },
    })
    const errors = (result.diagnostics ?? []).filter(d => d.category === ts.DiagnosticCategory.Error)
    if (errors.length) {
      throw new Error(errors.map(d => ts.flattenDiagnosticMessageText(d.messageText, '\n')).join('\n'))
    }
    compiled.set(filename, result.outputText)
    return result.outputText
  } catch (error) {
    throw new Error(`COMPILE FAILED: ${filename}\n${error.stack ?? error}`)
  }
}

function memoryRenderer() {
  const node = (type, text = '') => ({ type, text, props: {}, children: [], parent: null })
  const remove = (child) => {
    if (!child.parent) return
    const siblings = child.parent.children
    siblings.splice(siblings.indexOf(child), 1)
    child.parent = null
  }
  const insert = (child, parent, anchor = null) => {
    remove(child)
    const index = anchor ? parent.children.indexOf(anchor) : parent.children.length
    assert.ok(index >= 0, 'Renderer anchor must belong to its parent')
    parent.children.splice(index, 0, child)
    child.parent = parent
  }
  const renderer = vue.createRenderer({
    createElement: type => node(type),
    createText: text => node('#text', text),
    createComment: text => node('#comment', text),
    insert, remove,
    setText: (target, text) => { target.text = text },
    setElementText(target, text) {
      for (const child of target.children) child.parent = null
      target.children = []
      target.text = text
    },
    parentNode: target => target.parent,
    nextSibling: target => target.parent?.children[target.parent.children.indexOf(target) + 1] ?? null,
    patchProp: (target, key, previous, value) => { target.props[key] = value },
    insertStaticContent(content, parent, anchor) {
      const target = node('#static', content)
      insert(target, parent, anchor)
      return [target, target]
    },
  })
  return { renderer, container: node('root') }
}

async function flush() {
  // Only drain microtasks (including transpiled dynamic imports); never run real
  // timers or sleep. All command promises except explicitly held listen calls settle.
  for (let i = 0; i < 30; i++) await Promise.resolve()
  await vue.nextTick()
}

function fixture(t, delayed = false) {
  const failures = []
  const records = []
  const pending = new Map()
  const timers = new Map()
  const storage = new Map()
  const domListeners = new Map()
  const apiCalls = []
  const moduleCache = new Map()
  let nextTimer = 0
  let holding = delayed
  let mounted = false
  const fail = (message) => {
    const error = new Error(message)
    failures.push(error.stack)
    throw error
  }
  const timer = kind => (callback, delay) => {
    const id = ++nextTimer
    timers.set(id, { kind, callback, delay })
    return id
  }
  const timerApi = {
    setInterval: timer('interval'), setTimeout: timer('timeout'),
    clearInterval: id => timers.delete(id), clearTimeout: id => timers.delete(id),
  }
  const events = {
    listen(name, callback) {
      const record = { name, callback, active: true, unlistens: 0 }
      records.push(record)
      const unlisten = () => {
        record.active = false
        record.unlistens++
      }
      // Model native registration already being active, while its async
      // UnlistenFn has not yet reached the awaiting setupListeners continuation.
      if (holding && targetEvents.includes(name)) {
        return new Promise(resolve => pending.set(record, () => resolve(unlisten)))
      }
      return Promise.resolve(unlisten)
    },
  }
  const release = () => {
    holding = false
    for (const resolve of pending.values()) resolve()
    pending.clear()
  }
  const responses = {
    getDevices: () => [],
    getCastState: () => ({ sessions: { tv: {
      device_name: 'Memory TV', status: 'playing', position: '00:00:01', duration: '00:01:00',
    } } }),
    getScreenCastStatus: () => ({ running: false }),
    getSettings: () => ({ friendly_name: 'Memory only', version: 'test', settings: {} }),
    getAutostart: () => false,
    getGuardDevices: () => ({ trusted: [], blacklisted: [] }),
    getGuardSettings: () => ({ enabled: true, confirm_timeout: 15 }),
    isStoreVersion: () => false,
  }
  const commands = new Proxy({}, {
    get(_target, name) {
      return (...args) => {
        apiCalls.push({ name, args })
        if (!Object.hasOwn(responses, name)) return fail(`Unplanned API call: ${String(name)}`)
        return Promise.resolve(responses[name]())
      }
    },
  })
  const globals = {
    ...timerApi,
    console: {
      log() {},
      warn: (...args) => failures.push(`Store warning: ${args.join(' ')}`),
      error: (...args) => failures.push(`Store error: ${args.join(' ')}`),
    },
    window: {
      ...timerApi,
      matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    },
    document: {
      documentElement: { setAttribute() {} },
      addEventListener: (name, callback) => domListeners.set(name, callback),
      removeEventListener: (name, callback) => {
        if (domListeners.get(name) === callback) domListeners.delete(name)
      },
    },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
    },
  }

  function load(filename) {
    if (moduleCache.has(filename)) return moduleCache.get(filename)
    try {
      const exports = {}
      moduleCache.set(filename, exports)
      vm.runInNewContext(compile(filename), {
        ...globals, exports,
        require(id) {
          if (id === 'vue') return vue
          if (id === 'pinia') return pinia
          if (id === 'vue-i18n') return require('vue-i18n')
          if (id === '@/api/commands') return commands
          // 本用例测的是「监听随根组件存续」，文案层给最小替身即可
          if (id === '@/i18n') return { tt: (key) => key, i18n: { global: { t: (k) => k } } }
          if (id === '@tauri-apps/api/event') return events
          const leaf = /^@\/components\/(\w+)\.vue$/.exec(id)
          if (leaf && leafNames.has(leaf[1])) {
            return { default: vue.defineComponent({ name: `${leaf[1]}Stub`, inheritAttrs: false, render: () => null }) }
          }
          if (id.startsWith('@/stores/') || id.startsWith('@/types/')) {
            return load(path.join(root, 'src', `${id.slice(2)}.ts`))
          }
          // No fallback to Node/Tauri/browser APIs: unexpected imports fail even
          // if application code catches the rejected dynamic import itself.
          return fail(`Unexpected import ${id} in ${filename}`)
        },
      }, { filename })
      return exports
    } catch (error) {
      moduleCache.delete(filename)
      return fail(`LOAD FAILED: ${filename}\n${error.stack ?? error}`)
    }
  }

  const App = load(appFile).default
  const Home = load(homeFile).default
  const Settings = vue.defineComponent({
    name: 'SettingsRouteStub', render: () => vue.h('main', { 'data-route': 'settings' }, 'Settings'),
  })
  const route = vue.shallowRef(Home)
  const { renderer, container } = memoryRenderer()
  const storeContainer = pinia.createPinia()
  // Crucially, App itself is the root, not a test Root that copies its setup.
  const app = renderer.createApp(App)
  const homes = { mounted: [], unmounted: [] }
  app.mixin({
    mounted() { if (this.$.type === Home) homes.mounted.push(this.$.uid) },
    unmounted() { if (this.$.type === Home) homes.unmounted.push(this.$.uid) },
  })
  app.config.errorHandler = (error, instance, info) => failures.push(`Vue ${info}: ${error.stack ?? error}`)
  app.config.warnHandler = message => failures.push(`Vue warning: ${message}`)
  app.use(storeContainer)
  app.use(createI18n({ legacy: false, locale: 'en', messages: { en: {} }, missingWarn: false, fallbackWarn: false }))
  app.component('RouterView', vue.defineComponent({
    name: 'MemoryRouterView', setup: () => () => vue.h(route.value),
  }))
  const unmount = () => {
    if (mounted) {
      app.unmount()
      mounted = false
    }
  }
  t.after(async () => {
    unmount()
    release()
    await flush()
    // Test isolation only; application cleanup assertions run BEFORE disposal.
    for (const store of storeContainer._s.values()) store.$dispose()
    pinia.setActivePinia(undefined)
    assert.deepEqual(failures, [], 'Compilation/loading/runtime errors must not be silently skipped')
    records.length = 0
    timers.clear()
  })

  const count = name => records.filter(r => r.name === name && r.active).length
  const total = name => records.filter(r => r.name === name).length
  const emit = (name, payload) => {
    for (const record of records.filter(r => r.active && r.name === name)) record.callback({ event: name, payload })
  }
  return {
    homes, timers, apiCalls, domListeners, records, pending,
    count, total, emit, release, unmount,
    async mount() {
      mounted = true
      const instance = app.mount(container)
      // Vue createApp shallow-copies root options; its setup function retains
      // identity. Comparing the options object itself incorrectly rejects App.
      assert.equal(instance.$.type.setup, App.setup, 'The root must execute the compiled actual App.vue setup')
      await flush()
      assert.deepEqual(failures, [], 'Actual App/Home/stores must compile, load and mount without errors')
      assert.equal(homes.mounted.length, 1, 'Actual HomeView must mount through App router-view')
    },
    store(name) {
      const store = storeContainer._s.get(name)
      assert.ok(store, `${name} must already be created by the real component tree, not by the test`)
      return store
    },
    async navigate(name) {
      route.value = name === 'home' ? Home : Settings
      await flush()
      assert.deepEqual(failures, [], 'Route rendering must not fail silently')
    },
  }
}

function assertSingleListeners(f, phase) {
  for (const name of scopedEvents) {
    assert.equal(f.count(name), 1, `${phase}: ${name} must stay live`)
    assert.equal(f.total(name), 1, `${phase}: ${name} must not be registered twice`)
  }
}

function assertEventDelivery(f, sequence) {
  const cast = f.store('cast')
  const screen = f.store('screencast')
  const position = `00:00:${10 + sequence}`
  f.emit('cast_state_changed', {
    device_id: 'tv', status: sequence === 2 ? 'PLAYING' : 'PAUSED',
    position, duration: '00:02:00', volume: 20 + sequence, is_muted: true,
  })
  f.emit('screen_cast_state_changed', { running: true, viewers: sequence, watch_url: '/memory-only' })
  assert.equal(cast.sessions.tv.status, sequence === 2 ? 'playing' : 'paused')
  assert.equal(cast.sessions.tv.position, 10 + sequence)
  assert.equal(cast.sessions.tv.positionTime, position)
  assert.equal(cast.sessions.tv.durationTime, '00:02:00')
  assert.equal(cast.sessions.tv.volume, 20 + sequence)
  assert.equal(cast.sessions.tv.is_muted, true)
  assert.equal(screen.isRunning, true)
  assert.equal(screen.state.viewers, sequence)
}

test('real App: Home → Settings → Home retains one live subscription and releases on unmount', async (t) => {
  const f = fixture(t)
  await f.mount()
  const cast = f.store('cast')
  const screen = f.store('screencast')
  const device = f.store('device')
  assertSingleListeners(f, 'initial mount')
  assertEventDelivery(f, 1)
  assert.equal(f.domListeners.size, 1, 'Real App production contextmenu hook must run')

  await f.navigate('settings')
  assert.deepEqual(f.homes.unmounted, [f.homes.mounted[0]], 'Home really unmounted, not merely hidden')
  assertSingleListeners(f, 'Settings')
  assertEventDelivery(f, 2)

  await f.navigate('home')
  assert.equal(f.homes.mounted.length, 2, 'A fresh Home instance must mount on return')
  assert.notEqual(f.homes.mounted[0], f.homes.mounted[1])
  assert.equal(f.store('cast'), cast, 'Pinia must reuse cast across route instances')
  assert.equal(f.store('screencast'), screen)
  assert.equal(f.store('device'), device)
  assertSingleListeners(f, 'returned Home')
  assertEventDelivery(f, 3)
  for (const name of ['getCastState', 'getScreenCastStatus', 'getDevices']) {
    assert.equal(f.apiCalls.filter(call => call.name === name).length, 1, `${name}: mount initialization must not repeat`)
  }
  assert.ok(f.timers.size > 0, 'In-memory polling timers must actually have been created')

  f.unmount()
  await flush()
  for (const name of scopedEvents) {
    assert.equal(f.count(name), 0, `App unmount must release ${name}`)
    assert.equal(f.records.find(r => r.name === name).unlistens, 1, `${name}: cleanup exactly once`)
  }
  assert.equal(f.timers.size, 0, 'App unmount must clear scoped polling/simulation timers')
  assert.equal(f.domListeners.size, 0, 'Real App contextmenu cleanup must run')
  const snapshot = JSON.stringify({ sessions: cast.sessions, screen: screen.state, devices: device.devices })
  f.emit('cast_state_changed', { device_id: 'tv', status: 'PLAYING', position: '00:00:59' })
  f.emit('screen_cast_state_changed', { running: false })
  f.emit('device_found', { id: 'late', name: 'Must not arrive' })
  assert.equal(JSON.stringify({ sessions: cast.sessions, screen: screen.state, devices: device.devices }), snapshot)
  // Unrelated settings/guard/update lifecycle is not asserted by this focused test.
})

test('real App: unmount before async listen resolves must release late UnlistenFn handles', async (t) => {
  const f = fixture(t, true)
  await f.mount()
  assert.deepEqual([...f.pending.keys()].map(r => r.name).sort(), [...targetEvents].sort(),
    'Both actual stores must be awaiting their Tauri listen promise')
  for (const name of targetEvents) assert.equal(f.count(name), 1)
  f.unmount()
  assert.equal(f.timers.size, 0, 'Unmount must run before resolving delayed registrations')
  f.release()
  await flush()
  const leaked = scopedEvents.filter(name => f.count(name) !== 0)
  assert.deepEqual(leaked, [],
    `Late registration leaked after actual App unmount: ${leaked.map(name => `${name}=${f.count(name)}`).join(', ')}; ` +
    'onUnmounted must invalidate pending registration and immediately call any late UnlistenFn')
})
