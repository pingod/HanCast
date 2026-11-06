#!/usr/bin/env node
// 手机遥控入口回归：真实 Vue + 真实 SFC 模板，Modal 与 stores/api 打桩。
// 钉死两件事（v2.4.10）：
//   ① 首页**不依赖是否正在投屏**就有一个「手机遥控」入口——旧实现只把按钮藏在
//      CastController 的 `v-if="castStore.showController"` 里，不投屏就完全看不到地址，
//      用户只能来问「手机怎么连」；
//   ② 面板显示的是**后端当下报的地址**（先 remote_status，没在跑才 start），
//      不是缓存的旧地址——端口被占用时会退到随机端口，缓存值会把手机指到死地址。
// 无需浏览器、网络、文件写入、额外依赖。用法：node tools/verify-remote-entry.cjs
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')
const pinia = require('pinia')
const { createI18n } = require('vue-i18n')
const { parse, compileScript, compileTemplate } = require('@vue/compiler-sfc')

const root = path.resolve(__dirname, '..')

async function flush() { for (let i = 0; i < 30; i++) await Promise.resolve(); await vue.nextTick() }

// 极简宿主树，只用于让 Vue 渲染真实模板（不是 DOM 模拟器）
function node(type, text = '') { return { type, text, props: {}, children: [], parent: null } }
function makeRenderer() {
  return vue.createRenderer({
    createElement: type => node(type), createText: text => node('#text', text),
    createComment: text => node('#comment', text),
    setText: (el, text) => { el.text = text },
    setElementText: (el, text) => { el.text = text; el.children = [] },
    patchProp: (el, key, prev, next) => { el.props[key] = next },
    parentNode: el => el.parent,
    nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
    querySelector: () => null,
    insert(el, parent, anchor = null) {
      if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1)
      const index = anchor ? parent.children.indexOf(anchor) : -1
      parent.children.splice(index < 0 ? parent.children.length : index, 0, el)
      el.parent = parent
    },
    remove(el) { if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1); el.parent = null },
  })
}

function allNodes(el) { return [el, ...el.children.flatMap(allNodes)] }
function textOf(el) { return allNodes(el).map(n => n.text || '').join('') }
function hasClass(el, cls) { return String(el.props.class || '').split(' ').includes(cls) }

function evaluate(source, filename, imports) {
  const result = ts.transpileModule(source, {
    fileName: filename, reportDiagnostics: true,
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  })
  assert.equal(result.diagnostics?.length ?? 0, 0, filename)
  const exports = {}
  vm.runInNewContext(result.outputText, {
    exports,
    require(name) { if (!(name in imports)) throw new Error(`Unexpected import: ${name}`); return imports[name] },
    console, setTimeout, clearTimeout, setInterval, clearInterval, window: {},
  }, { filename })
  return exports
}

/** 编译一个 SFC，返回组件定义（render 用真实模板） */
function loadSfc(file, imports, id) {
  const source = fs.readFileSync(file, 'utf8')
  const { descriptor, errors } = parse(source, { filename: file })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id })
  const template = compileTemplate({
    source: descriptor.template.content,
    filename: file,
    id,
    compilerOptions: { bindingMetadata: script.bindings },
  })
  assert.deepEqual(template.errors, [])
  const component = evaluate(script.content, file, imports).default
  component.render = evaluate(template.code, `${file}.render.js`, imports).render
  return component
}

const messages = Object.fromEntries(['zh-CN', 'en-US'].map(locale => [locale,
  JSON.parse(fs.readFileSync(path.join(root, 'src/locales', `${locale}.json`), 'utf8')),
]))

function makeI18n() {
  return createI18n({ legacy: false, locale: 'zh-CN', fallbackLocale: 'zh-CN', messages })
}

// ── 用例 1：首页有常驻「手机遥控」入口，点了就把面板打开 ──
async function testHomeHasPersistentRemoteEntry() {
  const { renderer, container } = { renderer: makeRenderer(), container: node('root') }
  const seen = { visibleProps: [] }
  const stubs = {}
  const imports = {
    vue: { ...vue, Transition: vue.BaseTransition, Teleport: vue.Teleport },
    pinia,
    'vue-i18n': require('vue-i18n'),
  }
  // 所有叶子组件打成空桩；只有 RemoteControlPanel 例外——它要把 visible 记下来
  const leaf = name => (stubs[name] ??= {
    __esModule: true,
    default: vue.defineComponent({ name: `${name}Stub`, inheritAttrs: false, render: () => null }),
  })
  const { default: RemoteControlPanelStub } = (imports['@/components/RemoteControlPanel.vue'] = {
    __esModule: true,
    default: vue.defineComponent({
      name: 'RemoteControlPanelStub',
      props: ['visible'],
      setup(props) {
        seen.visibleProps.push(props.visible)
        return () => vue.h('div', { class: 'remote-panel-stub', 'data-visible': String(props.visible) })
      },
    }),
  })
  assert.ok(RemoteControlPanelStub)
  imports['@/stores/cast'] = {
    useCastStore: pinia.defineStore('cast', () => ({
      showAutostartPrompt: vue.ref(false),
      lastError: vue.ref(null),
      stopWarning: vue.ref(null),
      dismissError() {}, dismissStopWarning() {}, closeAutostartPrompt() {},
    })),
  }
  imports['@/stores/screencast'] = {
    useScreenCastStore: pinia.defineStore('screencast', () => ({
      isRunning: vue.ref(false),
      state: vue.ref({ viewers: 0 }),
      openPanel() { seen.screenPanelOpened = (seen.screenPanelOpened || 0) + 1 },
    })),
  }
  // 手机镜像那轮在 HomeView 里加了 useMirrorStore + MirrorPanel（首页那颗
  // 「手机镜像」按钮与在跑时的小圆点）。不登记这个 store，脚本会以
  // 「Unexpected import: @/stores/mirror」直接加载失败。
  imports['@/stores/mirror'] = {
    useMirrorStore: pinia.defineStore('mirror', () => ({
      isRunning: vue.ref(false),
      status: vue.ref(null),
      showPanel: vue.ref(false),
      openPanel() { seen.mirrorPanelOpened = (seen.mirrorPanelOpened || 0) + 1 },
      closePanel() {},
    })),
  }
  const HomeView = loadSfc(path.join(root, 'src/views/HomeView.vue'), new Proxy(imports, {
    has() { return true },
    get(target, name) {
      if (name in target) return target[name]
      const m = /^@\/components\/(\w+)\.vue$/.exec(String(name))
      if (m) return leaf(m[1])
      throw new Error(`Unexpected import: ${String(name)}`)
    },
  }), 'home-view-remote-entry')

  const app = renderer.createApp(HomeView)
  app.use(pinia.createPinia())
  app.use(makeI18n())
  app.component('RouterLink', { render: () => null })
  app.mount(container)

  const label = messages['zh-CN']['home.remote']
  assert.ok(label, 'locale 缺 home.remote 文案')
  const buttons = allNodes(container).filter(n => hasClass(n, 'home__screencast-btn'))
  // 手机镜像那轮在首页加了第三颗常驻按钮，所以这里是 3 而不是 2。
  // 判据不放宽：三颗都点名，少一颗就红。
  assert.equal(buttons.length, 3, '首页应有三颗常驻按钮：屏幕投屏 + 手机镜像 + 手机遥控')
  const mirrorLabel = messages['zh-CN']['home.mirror']
  assert.ok(mirrorLabel, 'locale 缺 home.mirror 文案')
  assert.ok(buttons.find(b => textOf(b).includes(mirrorLabel)),
    `首页没有「${mirrorLabel}」入口（手机镜像）`)
  const remoteBtn = buttons.find(b => textOf(b).includes(label))
  assert.ok(remoteBtn, `首页没有「${label}」入口——不投屏时用户就看不到遥控器地址`)
  const stub = allNodes(container).find(n => n.props.class === 'remote-panel-stub')
  assert.ok(stub, '首页没有挂 RemoteControlPanel')
  assert.equal(stub.props['data-visible'], 'false', '面板默认收起')
  assert.equal(seen.screenPanelOpened, undefined, '渲染首页不该顺手开屏幕投屏面板')

  // 点一下入口：面板必须被打开（visible 变 true）
  remoteBtn.props.onClick()
  await flush()
  const opened = allNodes(container).find(n => n.props.class === 'remote-panel-stub')
  assert.equal(opened.props['data-visible'], 'true', '点「手机遥控」后面板没打开')

  app.unmount()
}

// ── 用例 2：面板展示后端当下报的地址，且已在跑时不再重复 start ──
async function testPanelShowsLiveAddress({ running }) {
  const { renderer, container } = { renderer: makeRenderer(), container: node('root') }
  const calls = []
  const LIVE = 'http://192.168.1.9:8899/r/abc123/'
  const ModalStub = {
    __esModule: true,
    default: vue.defineComponent({
      name: 'ModalStub',
      props: ['visible', 'title', 'size', 'showIcon'],
      setup(props, { slots }) {
        return () => (props.visible
          ? vue.h('div', { class: 'modal-stub', 'data-title': props.title }, [
            slots.default ? slots.default() : null,
            slots.actions ? slots.actions() : null,
          ])
          : null)
      },
    }),
  }
  const imports = {
    vue: { ...vue, Transition: vue.BaseTransition },
    pinia,
    'vue-i18n': require('vue-i18n'),
    '@/components/Modal.vue': ModalStub,
    '@/api/commands': {
      remoteStatus: async () => {
        calls.push('remoteStatus')
        return running
          ? { ok: true, url: LIVE, port: 8899 }
          : { ok: false, url: '', port: 0 }
      },
      startRemoteControl: async () => {
        calls.push('startRemoteControl')
        return { ok: true, url: LIVE, port: 8899 }
      },
      writeClipboardText: async (text) => { calls.push(`writeClipboardText:${text}`) },
    },
  }
  const Panel = loadSfc(path.join(root, 'src/components/RemoteControlPanel.vue'), imports, 'remote-panel')
  const host = vue.defineComponent({
    components: { Panel },
    render() { return vue.h(Panel, { visible: true }) },
  })
  const app = renderer.createApp(host)
  app.use(makeI18n())
  app.mount(container)
  await flush()

  assert.equal(calls[0], 'remoteStatus', '打开面板必须先问后端要当下地址')
  if (running) {
    assert.ok(!calls.includes('startRemoteControl'), '服务已在跑时不该再 start 一次')
  } else {
    assert.equal(calls[1], 'startRemoteControl', '服务没在跑时应顺手起一次')
  }

  const shown = textOf(container)
  assert.ok(shown.includes('192.168.1.9:8899/r/abc123'), `面板没显示后端给的地址，实际文案：${shown.slice(0, 200)}`)
  assert.ok(!shown.includes('http://'), '展示地址时应去掉 http:// 前缀（省一行宽度）')

  const urlBtn = allNodes(container).find(n => hasClass(n, 'remote__url'))
  assert.ok(urlBtn, '面板没有可点复制的地址块')
  urlBtn.props.onClick()
  await flush()
  assert.ok(calls.includes(`writeClipboardText:${LIVE}`), '点地址块没把链接写进剪贴板')

  app.unmount()
}

async function main() {
  const cases = [
    ['首页常驻「手机遥控」入口（不投屏也能看到地址）', () => testHomeHasPersistentRemoteEntry()],
    ['面板先 remote_status 拿当下地址（服务已在跑）', () => testPanelShowsLiveAddress({ running: true })],
    ['面板在服务未跑时自动 start 一次', () => testPanelShowsLiveAddress({ running: false })],
  ]
  let failed = 0
  for (const [name, fn] of cases) {
    try {
      await fn()
      console.log(`PASS ${name}`)
    } catch (error) {
      failed++
      console.log(`FAIL ${name}\n     ${(error && (error.stack || error.message)) || error}`)
    }
  }
  console.log(`\n${cases.length - failed}/${cases.length} passed`)
  if (failed) process.exitCode = 1
}

main()