import { ref, computed, onMounted, onUnmounted } from 'vue'
import { defineStore } from 'pinia'
import type { UnlistenFn } from '@tauri-apps/api/event'
import type {
  ScreenSources,
  ScreenCastState,
  ScreenCastStartParams,
} from '@/types/screencast'
import {
  listScreenSources,
  startScreenCast as apiStartScreenCast,
  stopScreenCast as apiStopScreenCast,
  getScreenCastStatus as apiGetScreenCastStatus,
  setFfmpegPath as apiSetFfmpegPath,
} from '@/api/commands'

export const useScreenCastStore = defineStore('screencast', () => {
  // ── State ──
  const sources = ref<ScreenSources | null>(null)
  const sourcesLoading = ref(false)
  const state = ref<ScreenCastState>({ running: false })
  const starting = ref(false)
  const stopping = ref(false)
  const showPanel = ref(false)
  const lastError = ref<string | null>(null)

  // ── Computed ──
  const isRunning = computed(() => state.value.running)
  const ffmpegOk = computed(() => !!sources.value?.ffmpeg?.ok)
  const micDevices = computed(() =>
    (sources.value?.audio_inputs ?? []).filter(d => d.kind === 'mic')
  )
  const systemDevices = computed(() =>
    (sources.value?.audio_inputs ?? []).filter(d => d.kind === 'system')
  )
  const windows = computed(() => sources.value?.windows ?? [])

  // ── Actions ──
  async function fetchSources() {
    sourcesLoading.value = true
    try {
      sources.value = await listScreenSources()
      lastError.value = sources.value.ffmpeg?.ok
        ? null
        : (sources.value.ffmpeg?.error ?? 'FFmpeg 不可用')
    } catch (err) {
      console.error('Failed to list screen sources:', err)
      lastError.value = '无法获取投屏源'
    } finally {
      sourcesLoading.value = false
    }
  }

  async function fetchStatus() {
    try {
      state.value = await apiGetScreenCastStatus()
    } catch (err) {
      console.error('Failed to get screencast status:', err)
    }
  }

  async function start(params: ScreenCastStartParams) {
    starting.value = true
    lastError.value = null
    try {
      const result = await apiStartScreenCast(params)
      if (!result.ok) {
        lastError.value = result.error ?? '启动失败'
        return false
      }
      await fetchStatus()
      return true
    } catch (err) {
      console.error('Failed to start screencast:', err)
      lastError.value = '启动失败'
      return false
    } finally {
      starting.value = false
    }
  }

  async function stop() {
    stopping.value = true
    try {
      await apiStopScreenCast()
      await fetchStatus()
    } catch (err) {
      console.error('Failed to stop screencast:', err)
    } finally {
      stopping.value = false
    }
  }

  async function setFfmpegPath(path: string): Promise<boolean> {
    try {
      const result = await apiSetFfmpegPath(path)
      if (result.ok) {
        await fetchSources()
        return true
      }
      lastError.value = result.error ?? '设置失败'
      return false
    } catch (err) {
      console.error('Failed to set ffmpeg path:', err)
      lastError.value = '设置失败'
      return false
    }
  }

  function openPanel() {
    showPanel.value = true
    fetchSources()
    fetchStatus()
  }

  function closePanel() {
    showPanel.value = false
  }

  // ── Event listeners ──
  const unlistenFns: UnlistenFn[] = []

  async function setupListeners() {
    try {
      const { listen } = await import('@tauri-apps/api/event')
      unlistenFns.push(
        await listen<ScreenCastState>('screen_cast_state_changed', (event) => {
          state.value = event.payload
          if (event.payload.error) {
            lastError.value = event.payload.error
          }
        })
      )
    } catch {
      // Running outside Tauri, skip listeners
    }
  }

  onMounted(() => {
    fetchStatus()
    setupListeners()
  })

  onUnmounted(() => {
    unlistenFns.forEach((fn) => fn())
  })

  return {
    sources,
    sourcesLoading,
    state,
    starting,
    stopping,
    showPanel,
    lastError,
    isRunning,
    ffmpegOk,
    micDevices,
    systemDevices,
    windows,
    fetchSources,
    fetchStatus,
    start,
    stop,
    setFfmpegPath,
    openPanel,
    closePanel,
  }
})
