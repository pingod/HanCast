import { createApp } from 'vue'
import { createPinia } from 'pinia'
import router from './router'
import App from './App.vue'
import { i18n } from './i18n'
import { frontendLog } from './api/commands'
import './styles/global.css'

// Apply theme before first paint to avoid flash
;(() => {
  const mode = localStorage.getItem('hancast-theme') || 'system'
  const isDark = mode === 'dark' || (mode === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light')
})()

/**
 * 前端报错兜底：把 WebView 里的异常送进 hancast.log。
 *
 * 为什么需要：2026-11-03 用户报「首页中间的媒体输入区自己消失了」，界面上少了一整块，
 * 但日志里只有 DLNA 的 SOAP 记录——前端异常在打包版里是彻底沉默的，既看不到也查不到。
 * 现在 error / unhandledrejection / 组件错误三类都记一条，后台日志面板直接能翻。
 *
 * 两条纪律：
 * - 同一条消息只报一次，且总量封顶（渲染循环报错会把日志刷爆）；
 * - 上报本身失败就丢弃，绝不在错误处理里再造一个错误。
 */
const reportedKeys = new Set<string>()
let reportedCount = 0
const REPORT_LIMIT = 30

function reportError(
  level: 'error' | 'warn' | 'info',
  message: string,
  stack?: string,
  source = 'webview',
) {
  const text = String(message ?? '').slice(0, 500)
  if (!text) return
  if (reportedCount >= REPORT_LIMIT || reportedKeys.has(text)) return
  reportedKeys.add(text)
  reportedCount += 1
  void frontendLog({ level, message: text, stack, source })
}

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(i18n)

app.config.errorHandler = (err, _instance, info) => {
  const e = err as { message?: string; stack?: string } | undefined
  reportError('error', `${info || '组件错误'}：${e?.message ?? String(err)}`, e?.stack, 'vue')
}

window.addEventListener('error', (ev) => {
  const e = (ev as ErrorEvent).error as { stack?: string } | undefined
  reportError('error', (ev as ErrorEvent).message || String(e), e?.stack, 'window')
})

window.addEventListener('unhandledrejection', (ev) => {
  const reason = (ev as PromiseRejectionEvent).reason as { message?: string; stack?: string } | undefined
  reportError('error', `未处理的 Promise 拒绝：${reason?.message ?? String(reason)}`, reason?.stack, 'promise')
})

app.mount('#app')
