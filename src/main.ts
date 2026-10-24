import { createApp } from 'vue'
import { createPinia } from 'pinia'
import router from './router'
import App from './App.vue'
import { i18n } from './i18n'
import './styles/global.css'

// Apply theme before first paint to avoid flash
;(() => {
  const mode = localStorage.getItem('hancast-theme') || 'system'
  const isDark = mode === 'dark' || (mode === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light')
})()

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(i18n)

app.mount('#app')
