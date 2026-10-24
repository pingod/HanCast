import { ref, computed, onScopeDispose } from 'vue'
import { defineStore } from 'pinia'

export type ThemeMode = 'system' | 'light' | 'dark'

const STORAGE_KEY = 'hancast-theme'

export const useThemeStore = defineStore('theme', () => {
  const savedMode = localStorage.getItem(STORAGE_KEY)
  const mode = ref<ThemeMode>(
    savedMode === 'light' || savedMode === 'dark' || savedMode === 'system' ? savedMode : 'system',
  )
  const mq = window.matchMedia('(prefers-color-scheme: dark)')
  const systemDark = ref(mq.matches)

  /** Actual visual theme (resolves 'system' to light/dark) */
  const applied = computed(() => mode.value === 'system' ? (systemDark.value ? 'dark' : 'light') : mode.value)

  function persist() {
    document.documentElement.setAttribute('data-theme', applied.value)
    localStorage.setItem(STORAGE_KEY, mode.value)
  }

  function apply(m: ThemeMode) {
    mode.value = m
    persist()
  }

  /** Title bar: flip current visual, set mode to explicit light/dark */
  function toggle() {
    apply(applied.value === 'dark' ? 'light' : 'dark')
  }

  // Apply on init
  persist()

  // Keep tracking the system even in explicit mode, so switching back is current.
  function onSystemChange(event: MediaQueryListEvent) {
    systemDark.value = event.matches
    if (mode.value === 'system') {
      document.documentElement.setAttribute('data-theme', applied.value)
    }
  }
  mq.addEventListener('change', onSystemChange)
  onScopeDispose(() => mq.removeEventListener('change', onSystemChange))

  return { mode, applied, apply, toggle }
})
