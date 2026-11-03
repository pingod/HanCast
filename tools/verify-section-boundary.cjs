#!/usr/bin/env node
// SectionBoundary 回归（v2.4.11）：
// 钉死三件事——① 子组件 setup 抛错时，界面**不能整块消失**，必须原地出现可见的
// 失败提示；② 错误要经 frontendLog 落进 hancast.log（这是「查不到」那个问题的解药）；
// ③ 点「重试」要发出 retry，由父组件换 key 重新挂载这一块。
// 另加一条静态断言：HomeView 真的把 MediaInput 包进了错误边界里。
// 真实 Vue + 真实 SFC 模板，无浏览器/网络/文件写入。用法：node tools/verify-section-boundary.cjs
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const vue = require('vue')
const { createI18n } = require('vue-i18n')
const { parse, compileScript, compileTemplate } = require('@vue/compiler-sfc')

const root = path.resolve(__dirname, '..')
const boundaryFile = path.join(root, 'src/components/SectionBoundary.vue')
const homeFile = path.join(root, 'src/views/HomeView.vue')
const messages = Object.fromEntries(['zh-CN', 'en-US'].map(locale => [locale,
  JSON.parse(fs.readFileSync(path.join(root, 'src/locales', `${locale}.json`), 'utf8')),
]))

async function flush() { for (let i = 0; i < 20; i++) await Promise.resolve(); await vue.nextTick() }

// 极简宿主树：只为让 Vue 渲染真实模板，不是 DOM 模拟器
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
function hasClass(el, cls) { return String(el.props.class || '').split(' ').includes(cls) }
function textOf(el) { return allNodes(el).map(n => n.text || '').join('') }

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
    console, setTimeout, clearTimeout, setInterval, clearInterval,
  }, { filename })
  return exports
}

function compileSfc(file) {
  const source = fs.readFileSync(file, 'utf8')
  const { descriptor, errors } = parse(source, { filename: file })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: `sfc-${path.basename(file)}` })
  const template = compileTemplate({
    source: descriptor.template.content,
    filename: file,
    id: `tpl-${path.basename(file)}`,
    compilerOptions: { bindingMetadata: script.bindings },
  })
  assert.deepEqual(template.errors, [])
  return { script, template }
}

const boundarySfc = compileSfc(boundaryFile)

/** 组装一次挂载：child 由用例决定（正常 / setup 抛错 / 可切换） */
function mountBoundary(child, { label = '媒体输入区', locale = 'zh-CN' } = {}) {
  const logged = []
  const retries = []
  const imports = {
    vue: { ...vue },
    'vue-i18n': require('vue-i18n'),
    '@/api/commands': { frontendLog: async (entry) => { logged.push(entry) } },
  }
  const Boundary = evaluate(boundarySfc.script.content, boundaryFile, imports).default
  Boundary.render = evaluate(boundarySfc.template.code, `${boundaryFile}.render.js`, imports).render
  const Host = {
    components: { Boundary, Child: child },
    setup() { return { label, onRetry: () => retries.push(1) } },
    render() {
      return vue.h(Boundary, { label: this.label, onRetry: this.onRetry }, {
        default: () => vue.h(child),
      })
    },
  }
  const container = node('root')
  const renderer = makeRenderer()
  const app = renderer.createApp(Host)
  app.use(createI18n({ legacy: false, locale, messages, missingWarn: false, fallbackWarn: false }))
  app.config.warnHandler = () => {}
  app.mount(container)
  return {
    container, app, logged, retries,
    find: cls => allNodes(container).find(el => hasClass(el, cls)),
  }
}

const tests = []
function test(name, run) { tests.push({ name, run }) }

test('子组件 setup 抛错 → 原地出现失败提示，不外抛、整块不消失', async () => {
  const Broken = { name: 'Broken', setup() { throw new Error('store 炸了') }, render: () => null }
  const h = mountBoundary(Broken)
  await flush()
  const banner = h.find('section-fail')
  assert.ok(banner, '失败提示必须渲染出来（以前是什么都不剩）')
  const text = textOf(banner)
  assert.ok(text.includes('媒体输入区'), `提示里要写明是哪一块，实际=${text}`)
  assert.ok(text.includes('store 炸了'), `提示里要带上原始错误，实际=${text}`)
  h.app.unmount(); await flush()
})

test('错误经 frontendLog 落进日志（source=section，带区块名与原文）', async () => {
  const Broken = { name: 'Broken', setup() { throw new Error('boom') }, render: () => null }
  const h = mountBoundary(Broken)
  await flush()
  await flush()
  assert.equal(h.logged.length, 1, `应当只上报一次，实际 ${h.logged.length} 次`)
  const entry = h.logged[0]
  assert.equal(entry.level, 'error')
  assert.equal(entry.source, 'section')
  assert.ok(entry.message.includes('媒体输入区'), entry.message)
  assert.ok(entry.message.includes('boom'), entry.message)
  h.app.unmount(); await flush()
})

test('点「重试」→ 发出 retry，父组件换 key 重新挂载后内容恢复', async () => {
  const state = { broken: true }
  const Toggle = {
    name: 'Toggle',
    setup() {
      if (state.broken) throw new Error('第一次挂了')
      return () => vue.h('div', { class: 'child-ok' }, '媒体输入区内容')
    },
  }
  // 父组件：Keyed 的边界，retry 时换 key 重新挂载
  const logged = []
  const imports = {
    vue: { ...vue },
    'vue-i18n': require('vue-i18n'),
    '@/api/commands': { frontendLog: async (entry) => { logged.push(entry) } },
  }
  const Boundary = evaluate(boundarySfc.script.content, boundaryFile, imports).default
  Boundary.render = evaluate(boundarySfc.template.code, `${boundaryFile}.render.js`, imports).render
  const Host = {
    components: { Boundary, Toggle },
    setup() { return { k: vue.ref(0), label: '媒体输入区' } },
    render() {
      return vue.h(Boundary, { key: this.k, label: this.label, onRetry: () => { state.broken = false; this.k += 1 } }, {
        default: () => vue.h(Toggle),
      })
    },
  }
  const container = node('root')
  const renderer = makeRenderer()
  const app = renderer.createApp(Host)
  app.use(createI18n({ legacy: false, locale: 'zh-CN', messages, missingWarn: false, fallbackWarn: false }))
  app.config.warnHandler = () => {}
  app.mount(container)
  await flush()
  const find = cls => allNodes(container).find(el => hasClass(el, cls))
  const banner = find('section-fail')
  assert.ok(banner, '先要看到失败提示')
  const retryButton = allNodes(banner).find(el => el.type === 'button')
  assert.ok(retryButton, '失败提示里必须有重试按钮')
  retryButton.props.onClick()
  await flush()
  await flush()
  assert.equal(find('section-fail'), undefined, '重试之后失败提示应当消失')
  assert.ok(find('child-ok'), '重试之后子内容必须真的渲染出来')
  app.unmount(); await flush()
})

test('子组件正常时：不显示失败提示，原样渲染内容，不上报', async () => {
  const Fine = { name: 'Fine', setup: () => () => vue.h('div', { class: 'child-ok' }, 'ok') }
  const h = mountBoundary(Fine)
  await flush()
  assert.equal(h.find('section-fail'), undefined, '没出错时不该出现失败提示')
  assert.ok(h.find('child-ok'), '子内容必须正常渲染')
  assert.equal(h.logged.length, 0, '没出错时不该上报')
  h.app.unmount(); await flush()
})

test('HomeView 确实把 MediaInput 包在错误边界里', async () => {
  const source = fs.readFileSync(homeFile, 'utf8')
  assert.ok(/import SectionBoundary from '@\/components\/SectionBoundary\.vue'/.test(source),
    'HomeView 必须 import SectionBoundary')
  assert.ok(/<SectionBoundary[\s\S]{0,200}?<MediaInput \/>[\s\S]{0,40}?<\/SectionBoundary>/.test(source),
    'MediaInput 必须被 SectionBoundary 包住（否则抛错时整块还是会被卸掉）')
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
