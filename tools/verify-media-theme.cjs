#!/usr/bin/env node
// Run: node tools/verify-media-theme.cjs
// Real Vue/Pinia; only commands, browser globals and timers are in-memory fakes.
// TypeScript is transpiled in memory. No sidecar, network, user settings or build output.
const assert = require('node:assert/strict')
const { File } = require('node:buffer')
const { readFileSync } = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const { test } = require('node:test')
const ts = require('typescript')
const vue = require('vue')
const pinia = require('pinia')

const root = path.resolve(__dirname, '..')
const compiled = new Map()

function loadStore(name, globals = {}, commands) {
  const filename = path.join(root, 'src', 'stores', `${name}.ts`)
  if (!compiled.has(filename)) {
    compiled.set(filename, ts.transpileModule(readFileSync(filename, 'utf8'), {
      fileName: filename,
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
    }).outputText)
  }
  const exports = {}
  vm.runInNewContext(compiled.get(filename), {
    exports,
    require(id) {
      if (id === 'vue') return vue
      if (id === 'pinia') return pinia
      if (id === '@/api/commands' && commands) return commands
      throw new Error(`Unexpected import: ${id}`)
    },
    ...globals,
  }, { filename })
  return exports
}

class MemoryTimers {
  now = 0
  nextId = 0
  pending = new Map()

  setTimeout = (callback, delay = 0) => {
    const id = ++this.nextId
    this.pending.set(id, { callback, time: this.now + delay })
    return id
  }

  clearTimeout = (id) => this.pending.delete(id)

  advance(ms) {
    const until = this.now + ms
    while (this.pending.size) {
      const [id, timer] = [...this.pending].sort((a, b) => a[1].time - b[1].time)[0]
      if (timer.time > until) break
      this.now = timer.time
      this.pending.delete(id)
      timer.callback()
    }
    this.now = until
  }
}

function mediaInfo(label, kind = 'url') {
  return {
    media_type: kind,
    uri: kind === 'file' ? `/media/${label}.mp4` : `https://cdn.example/${label}.mp4`,
    title: label,
    mime_type: 'video/mp4',
    file_size: 42,
    duration: 12,
    thumbnail: null,
    ...(kind === 'url' ? { source_url: `https://site.example/${label}` } : {}),
  }
}

function mediaFixture(t) {
  const timers = new MemoryTimers()
  const plans = { file: [], url: [] }
  const calls = []
  const invoke = (kind, ...args) => {
    calls.push({ kind, args })
    const plan = plans[kind].shift()
    assert.ok(plan, `Unplanned ${kind} API call`)
    return new Promise((resolve, reject) => timers.setTimeout(() => {
      if (plan.error) reject(new Error(plan.error))
      else resolve(plan.info)
    }, plan.delay))
  }
  const { useMediaStore } = loadStore('media', {
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
  }, {
    parseMediaFile: (...args) => invoke('file', ...args),
    resolveShare: (...args) => invoke('url', ...args),
  })
  const store = useMediaStore(pinia.createPinia())
  t.after(() => {
    store.$dispose()
    assert.equal(timers.pending.size, 0, 'All fake API timers must settle')
    assert.equal(plans.file.length + plans.url.length, 0, 'All planned calls must run')
  })
  return {
    store, timers, calls,
    plan(kind, delay, info, error) { plans[kind].push({ delay, info, error }) },
  }
}

function snapshot(store) {
  return {
    state: store.state,
    mediaInfo: JSON.parse(JSON.stringify(store.mediaInfo)),
    localFile: store.localFile,
    urlInput: store.urlInput,
    error: store.error,
    ledgerHint: store.ledgerHint,
    isReady: store.isReady,
    isParsing: store.isParsing,
  }
}

const oldRequests = [
  { name: 'URL success', kind: 'url', input: 'https://cdn.example/old.mp4' },
  { name: 'URL direct-link failure', kind: 'url', input: 'https://cdn.example/old.mp4', error: 'old failure' },
  { name: 'URL webpage failure', kind: 'url', input: 'https://site.example/watch/old', error: 'old failure' },
  { name: 'URL text failure', kind: 'url', input: 'old share text', error: 'old failure' },
  { name: 'file success', kind: 'file', input: '/media/old.mp4' },
  { name: 'file failure', kind: 'file', input: '/media/old.mp4', error: 'old failure' },
]

function startOld(f, request) {
  f.plan(request.kind, 30, mediaInfo('old', request.kind), request.error)
  return request.kind === 'url' ? f.store.parseUrl(request.input) : f.store.parseFile(request.input)
}

for (const old of oldRequests) {
  for (const newer of ['url', 'file', 'browser-file']) {
    test(`media: slow ${old.name} cannot overwrite fast ${newer}`, async (t) => {
      const f = mediaFixture(t)
      const pendingOld = startOld(f, old)
      let pendingNew
      if (newer === 'browser-file') {
        pendingNew = f.store.parseFile(new File(['new'], 'new.webm', { type: 'video/webm' }))
      } else {
        f.plan(newer, 10, mediaInfo('new', newer))
        pendingNew = newer === 'url'
          ? f.store.parseUrl('https://site.example/new', '1080p')
          : f.store.parseFile('/media/new.mp4')
      }
      f.timers.advance(10)
      await pendingNew
      assert.equal(f.store.isReady, true)
      assert.equal(f.store.mediaInfo.title, newer === 'browser-file' ? 'new.webm' : 'new')
      if (newer === 'url') {
        assert.equal(f.store.mediaInfo.quality, '1080p')
        assert.equal(f.store.urlInput, 'https://site.example/new')
        assert.deepEqual(f.calls[1].args, ['https://site.example/new', '1080p'])
      }
      const expected = snapshot(f.store)
      f.timers.advance(20)
      await pendingOld
      assert.deepEqual(snapshot(f.store), expected)
    })
  }

  for (const edit of ['reset', 'new-input', 'empty-input', 'empty-parse']) {
    test(`media: ${edit} invalidates pending ${old.name}`, async (t) => {
      const f = mediaFixture(t)
      const pendingOld = startOld(f, old)
      if (edit === 'reset') f.store.reset()
      else if (edit === 'empty-parse') await f.store.parseUrl('  ')
      else f.store.setInputChange(edit === 'new-input' ? 'new input' : '')
      assert.equal(f.store.state, edit === 'new-input' ? 'input' : 'idle')
      assert.equal(f.store.mediaInfo, null)
      assert.equal(f.store.isReady, false)
      assert.equal(f.store.isParsing, false)
      const expected = snapshot(f.store)
      f.timers.advance(30)
      await pendingOld
      assert.deepEqual(snapshot(f.store), expected)
    })
  }
}

test('media: repeated identical URL/quality requests use generation, not input equality', async (t) => {
  const f = mediaFixture(t)
  f.plan('url', 30, mediaInfo('old'))
  f.plan('url', 10, mediaInfo('new'))
  const old = f.store.parseUrl('https://site.example/same', '720p')
  const latest = f.store.parseUrl('https://site.example/same', '720p')
  f.timers.advance(10)
  await latest
  const expected = snapshot(f.store)
  f.timers.advance(20)
  await old
  assert.equal(f.store.mediaInfo.title, 'new')
  assert.deepEqual(snapshot(f.store), expected)
})

test('media: stale completion leaves a newer request parsing', async (t) => {
  const f = mediaFixture(t)
  f.plan('url', 10, mediaInfo('old'))
  f.plan('file', 30, mediaInfo('new', 'file'))
  const old = f.store.parseUrl('https://site.example/old')
  const latest = f.store.parseFile('/media/new.mp4')
  const expected = snapshot(f.store)
  f.timers.advance(10)
  await old
  assert.equal(f.store.isParsing, true)
  assert.deepEqual(snapshot(f.store), expected)
  f.timers.advance(20)
  await latest
  assert.equal(f.store.mediaInfo.title, 'new')
})

test('media: URL start clears browser preview and old info even on failure', async (t) => {
  const f = mediaFixture(t)
  await f.store.parseFile(new File(['old'], 'old.mp4', { type: 'video/mp4' }))
  assert.ok(f.store.localFile)
  f.plan('url', 10, null, 'cannot resolve')
  const pending = f.store.parseUrl('https://site.example/watch')
  assert.equal(f.store.state, 'parsing')
  assert.equal(f.store.localFile, null)
  assert.equal(f.store.mediaInfo, null)
  f.timers.advance(10)
  await pending
  assert.equal(f.store.state, 'idle')
  assert.equal(f.store.ledgerHint, true)
  assert.match(f.store.error, /cannot resolve/)
  f.store.setInputChange('replacement')
  assert.equal(f.store.error, null)
  assert.equal(f.store.ledgerHint, false)
})

test('media: latest file failure clears old info and is reported', async (t) => {
  const f = mediaFixture(t)
  await f.store.parseFile(new File(['old'], 'old.mp4'))
  f.plan('file', 10, null, 'file missing')
  const pending = f.store.parseFile('/media/missing.mp4')
  assert.equal(f.store.mediaInfo, null)
  assert.equal(f.store.localFile, null)
  f.timers.advance(10)
  await pending
  assert.equal(f.store.state, 'idle')
  assert.match(f.store.error, /file missing/)
})

for (const [name, title, mime] of [
  ['bad%name.mp4', 'bad%name.mp4', 'video/mp4'],
  ['bad%E0%A4.mp4', 'bad%E0%A4.mp4', 'video/mp4'],
  ['%E4%B8%AD%E6%96%87.mp4', '中文.mp4', 'video/mp4'],
  ['LIVE.M3U8', 'LIVE.M3U8', 'application/vnd.apple.mpegurl'],
]) {
  test(`media: safe direct-link fallback for ${name}`, async (t) => {
    const f = mediaFixture(t)
    const url = `https://cdn.example/${name}?token=abc#video`
    f.plan('url', 10, null, 'offline')
    const pending = f.store.parseUrl(`分享 ${url}。`)
    f.timers.advance(10)
    await pending
    assert.equal(f.store.isReady, true)
    assert.equal(f.store.isParsing, false)
    assert.equal(f.store.error, null)
    assert.equal(f.store.ledgerHint, false)
    assert.equal(f.store.mediaInfo.uri, url)
    assert.equal(f.store.mediaInfo.title, title)
    assert.equal(f.store.mediaInfo.mime_type, mime)
  })
}

for (const [input, hint] of [
  ['https://site.example/watch', true],
  ['https://site.example/watch?file=movie.mp4', true],
  ['https://cdn.example/picture.jpg', true],
  ['share text without a URL', false],
]) {
  test(`media: backend failure does not pass through ${input}`, async (t) => {
    const f = mediaFixture(t)
    f.plan('url', 10, null, 'unresolved')
    const pending = f.store.parseUrl(input)
    f.timers.advance(10)
    await pending
    assert.equal(f.store.state, 'idle')
    assert.equal(f.store.mediaInfo, null)
    assert.equal(f.store.ledgerHint, hint)
    assert.match(f.store.error, /unresolved/)
  })
}

function themeFixture(t, savedMode, dark = false) {
  const storage = new Map(savedMode == null ? [] : [['hancast-theme', savedMode]])
  const attrs = new Map()
  const listeners = new Set()
  const mq = {
    matches: dark,
    addEventListener(event, listener) {
      assert.equal(event, 'change')
      listeners.add(listener)
    },
    removeEventListener(event, listener) {
      assert.equal(event, 'change')
      assert.equal(listeners.delete(listener), true)
    },
  }
  const { useThemeStore } = loadStore('theme', {
    window: { matchMedia(query) {
      assert.equal(query, '(prefers-color-scheme: dark)')
      return mq
    } },
    document: { documentElement: { setAttribute: (key, value) => attrs.set(key, value) } },
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem(key, value) {
        assert.equal(key, 'hancast-theme', 'No new setting keys')
        storage.set(key, value)
      },
    },
  })
  const store = useThemeStore(pinia.createPinia())
  t.after(() => {
    store.$dispose()
    assert.equal(listeners.size, 0)
  })
  return {
    store, storage, attrs, listeners,
    change(matches) {
      mq.matches = matches
      for (const listener of listeners) listener({ matches })
    },
  }
}

for (const savedMode of [undefined, '', 'invalid', 'LIGHT', 'system', 'light', 'dark']) {
  for (const dark of [false, true]) {
    test(`theme: initial mode ${JSON.stringify(savedMode)}, system dark=${dark}`, (t) => {
      const f = themeFixture(t, savedMode, dark)
      const mode = ['system', 'light', 'dark'].includes(savedMode) ? savedMode : 'system'
      const applied = mode === 'system' ? (dark ? 'dark' : 'light') : mode
      assert.equal(f.store.mode, mode)
      assert.equal(f.store.applied, applied)
      assert.equal(f.attrs.get('data-theme'), applied)
      assert.equal(f.storage.get('hancast-theme'), mode)
      assert.equal(f.listeners.size, 1)
    })
  }
}

for (const dark of [false, true]) {
  test(`theme: system change updates computed and first toggle (initial dark=${dark})`, async (t) => {
    const f = themeFixture(t, 'system', dark)
    const initial = dark ? 'dark' : 'light'
    const changed = dark ? 'light' : 'dark'
    const seen = []
    const stop = vue.watch(() => f.store.applied, (value) => seen.push(value))
    t.after(stop)
    assert.equal(f.store.applied, initial) // Prime the computed cache before change.
    f.change(!dark)
    await vue.nextTick()
    assert.deepEqual(seen, [changed])
    assert.equal(f.store.applied, changed)
    assert.equal(f.attrs.get('data-theme'), changed)
    assert.equal(f.storage.get('hancast-theme'), 'system')
    f.store.toggle()
    assert.equal(f.store.mode, initial)
    assert.equal(f.store.applied, initial)
    assert.equal(f.attrs.get('data-theme'), initial)
    assert.equal(f.storage.get('hancast-theme'), initial)
  })
}

for (const explicit of ['light', 'dark']) {
  test(`theme: explicit ${explicit} ignores system visually but tracks it for re-entry`, (t) => {
    const f = themeFixture(t, explicit, false)
    f.change(true)
    assert.equal(f.store.applied, explicit)
    assert.equal(f.attrs.get('data-theme'), explicit)
    assert.equal(f.storage.get('hancast-theme'), explicit)
    f.store.apply('system')
    assert.equal(f.store.applied, 'dark')
    assert.equal(f.attrs.get('data-theme'), 'dark')
    f.change(false)
    assert.equal(f.store.applied, 'light')
    assert.equal(f.attrs.get('data-theme'), 'light')
  })
}

test('theme: disposal removes system subscription', (t) => {
  const f = themeFixture(t, 'system')
  f.store.$dispose()
  assert.equal(f.listeners.size, 0)
  f.change(true)
  assert.equal(f.attrs.get('data-theme'), 'light')
})
