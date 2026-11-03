#!/usr/bin/env node
// DeviceList 空状态回归：真实 Vue + 真实 SFC 模板，DeviceCard 打桩、store 打桩。
// 钉死一件事（v2.4.9 修的 bug）：**有可见设备但无隐藏设备时，空状态块绝不能出现**。
// 旧实现把空状态的 v-else 挂到了「隐藏设备按钮」的 v-if 上（v2.4.4 引入），
// 于是「有设备 + 无隐藏设备」这个组合下，设备列表与空状态会同时渲染。
// 无需浏览器、网络、文件写入、额外依赖。用法：node tools/verify-device-list-empty.cjs
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
const listFile = path.join(root, 'src/components/DeviceList.vue')
const listSource = fs.readFileSync(listFile, 'utf8')
const { descriptor, errors: parseErrors } = parse(listSource, { filename: listFile })
assert.deepEqual(parseErrors, [])
const script = compileScript(descriptor, { id: 'device-list-test' })
const template = compileTemplate({
  source: descriptor.template.content,
  filename: listFile,
  id: 'device-list-test',
  compilerOptions: { bindingMetadata: script.bindings },
})
assert.deepEqual(template.errors, [])
const messages = Object.fromEntries(['zh-CN', 'en-US'].map(locale => [locale,
  JSON.parse(fs.readFileSync(path.join(root, 'src/locales', `${locale}.json`), 'utf8')),
]))

async function flush() { for (let i = 0; i < 20; i++) await Promise.resolve(); await vue.nextTick() }

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
function hasClass(el, cls) { return (el.props.class || '').split(' ').includes(cls) }

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

// 用真实 device store 的形状做一个可变桩（pinia setup store，状态走 store 代理读写）
function makeHarness({ devices, hidden }, locale = 'zh-CN') {
  const DeviceCardStub = { name: 'DeviceCard', props: ['device'], render() { return vue.h('div', { class: 'device-card-stub' }, this.device?.name || '') } }
  const imports = {
    vue: { ...vue, Transition: vue.BaseTransition }, pinia,
    'vue-i18n': require('vue-i18n'),
    '@/stores/device': { useDeviceStore: pinia.defineStore('device', () => ({
      devices: vue.ref(devices),
      hiddenDevices: vue.ref(hidden),
      refresh: () => Promise.resolve(),
      openAddModal() {}, openHiddenModal() {},
    })) },
    '@/stores/logs': { useLogsStore: pinia.defineStore('logs', () => ({})) },
    // 注意：compileScript 把 `import DeviceCard from './DeviceCard.vue'` 转成 CommonJS 后
    // 取的是 `.default`，所以这里必须包一层 { default }，直接塞桩对象会得到 undefined，
    // 表现为「Invalid vnode type when creating vnode: undefined」，看起来像模板写错了。
    './DeviceCard.vue': { __esModule: true, default: DeviceCardStub },
  }
  const List = evaluate(script.content, listFile, imports).default
  List.render = evaluate(template.code, `${listFile}.render.js`, imports).render
  const body = node('body')
  const container = node('root')
  const renderer = makeRenderer()
  const storeRoot = pinia.createPinia()
  const app = renderer.createApp({ render: () => vue.h(List) })
  app.use(storeRoot)
  app.use(createI18n({ legacy: false, locale, messages, missingWarn: false, fallbackWarn: false }))
  app.mount(container)
  // 挂在 container 上、也从 container 里找：body 只有在组件 teleport 到 body 时才会有东西
  return { container, app, storeRoot, find: cls => allNodes(container).find(el => hasClass(el, cls)) }
}

const tests = []
function test(name, run) { tests.push({ name, run }) }

// 核心回归：有可见设备 + 无隐藏设备 → 设备列表在、空状态不在（正是用户截图那幕）
test('有可见设备 + 无隐藏设备 → 不出现空状态块', async () => {
  const h = makeHarness({ devices: [{ id: 'a', name: '小度添添闺蜜机 Pro 4K Max' }, { id: 'b', name: 'cl32' }], hidden: [] })
  await flush()
  assert.ok(h.find('device-list__items'), '设备列表容器必须渲染')
  assert.equal(h.find('device-list__empty'), undefined, '空状态块绝不能与设备列表同时出现')
  h.app.unmount(); await flush(); h.storeRoot._s.forEach(s => s.$dispose())
})

// 无设备 → 空状态在、设备列表不在
test('无设备 → 出现空状态块、无设备列表', async () => {
  const h = makeHarness({ devices: [], hidden: [] })
  await flush()
  assert.equal(h.find('device-list__items'), undefined, '无设备时不该有列表容器')
  assert.ok(h.find('device-list__empty'), '无设备时必须显示空状态')
  h.app.unmount(); await flush(); h.storeRoot._s.forEach(s => s.$dispose())
})

// 有设备 + 有隐藏设备 → 列表 + 隐藏按钮都在、空状态不在
test('有设备 + 有隐藏设备 → 列表与隐藏按钮都在、无空状态', async () => {
  const h = makeHarness({ devices: [{ id: 'a', name: 'A' }], hidden: [{ id: 'h', name: 'H' }] })
  await flush()
  assert.ok(h.find('device-list__items'), '设备列表容器必须渲染')
  assert.ok(h.find('device-list__hidden'), '有隐藏设备时隐藏按钮必须出现')
  assert.equal(h.find('device-list__empty'), undefined, '有设备时空状态绝不出现')
  h.app.unmount(); await flush(); h.storeRoot._s.forEach(s => s.$dispose())
})

// 无设备 + 有隐藏设备 → 空状态在、隐藏按钮也在（两者独立，不互斥）
test('无设备 + 有隐藏设备 → 空状态与隐藏按钮都在', async () => {
  const h = makeHarness({ devices: [], hidden: [{ id: 'h', name: 'H' }] })
  await flush()
  assert.ok(h.find('device-list__empty'), '无设备时显示空状态')
  assert.ok(h.find('device-list__hidden'), '有隐藏设备时隐藏按钮出现')
  h.app.unmount(); await flush(); h.storeRoot._s.forEach(s => s.$dispose())
})

;(async () => {
  let failed = 0
  for (const t of tests) {
    try { await t.run(); console.log(`PASS ${t.name}`) }
    catch (e) { failed += 1; console.error(`FAIL ${t.name}\n  ${e.message}`) }
  }
  if (failed) { console.error(`\n${failed}/${tests.length} failed`); process.exit(1) }
  console.log(`\nALL PASS (${tests.length}/${tests.length})`)
})()
