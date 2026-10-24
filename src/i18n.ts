import { createI18n } from 'vue-i18n'
import zhCN from './locales/zh-CN.json'
import enUS from './locales/en-US.json'

/** 独立成模块而不是留在 main.ts：store 层（如投屏失败要发系统通知）也要取文案，
 *  从 main.ts 反向 import 会把 App→store→main 绕成循环依赖。 */
export const i18n = createI18n({
  legacy: false,
  locale: 'zh-CN',
  fallbackLocale: 'en-US',
  messages: {
    'zh-CN': zhCN,
    'en-US': enUS,
  },
})

/** 非组件环境（store）里取文案 */
export const tt = (key: string, values?: Record<string, unknown>) =>
  values === undefined ? i18n.global.t(key) : i18n.global.t(key, values)
