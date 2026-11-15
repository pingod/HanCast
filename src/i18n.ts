import { createI18n } from 'vue-i18n'
import zhCN from './locales/zh-CN.json'
import enUS from './locales/en-US.json'

/** 语言选择的持久化键。theme/队列/历史都各自落盘，语言此前是唯一没落盘的一项：
 *  用户在设置里切到 English，重启后静默回到中文。 */
const LOCALE_KEY = 'hancast-locale'
type AppLocale = 'zh-CN' | 'en-US'
const SUPPORTED_LOCALES: AppLocale[] = ['zh-CN', 'en-US']

function initialLocale(): AppLocale {
  try {
    const saved = localStorage.getItem(LOCALE_KEY)
    if (saved && (SUPPORTED_LOCALES as string[]).includes(saved)) return saved as AppLocale
  } catch {
    // 受限 WebView 里 storage 可能抛错：退回默认语言即可
  }
  return 'zh-CN'
}

/** 独立成模块而不是留在 main.ts：store 层（如投屏失败要发系统通知）也要取文案，
 *  从 main.ts 反向 import 会把 App→store→main 绕成循环依赖。 */
export const i18n = createI18n({
  legacy: false,
  locale: initialLocale(),
  fallbackLocale: 'en-US',
  messages: {
    'zh-CN': zhCN,
    'en-US': enUS,
  },
})

/** 切换语言并落盘（设置页调用；下次启动从 localStorage 恢复） */
export function setLocale(lang: string): void {
  const next: AppLocale = (SUPPORTED_LOCALES as string[]).includes(lang)
    ? (lang as AppLocale)
    : 'zh-CN'
  i18n.global.locale.value = next
  try {
    localStorage.setItem(LOCALE_KEY, next)
  } catch {
    // 存不下就只在本次运行生效，至少不影响切换
  }
}

/** 非组件环境（store）里取文案 */
export const tt = (key: string, values?: Record<string, unknown>) =>
  values === undefined ? i18n.global.t(key) : i18n.global.t(key, values)
