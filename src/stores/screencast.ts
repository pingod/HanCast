import { ref, computed, onMounted, onUnmounted } from 'vue'
import { defineStore } from 'pinia'
import type { UnlistenFn } from '@tauri-apps/api/event'
import type { StopWarning } from '@/api/commands'
import { useDeviceStore } from '@/stores/device'
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
  // 电视没确认收到 Stop 时的告警（分片已删，但电视可能还在播缓冲）
  const stopWarning = ref<StopWarning | null>(null)

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
    stopWarning.value = null
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
    stopWarning.value = null
    // fetchStatus 之后 state 已被重置，设备身份要先摘下来
    const deviceId = state.value.device_id
    const isDlna = state.value.target === 'dlna'
    let failed: string[] = []
    let reasons: string[] = []
    try {
      const res = await apiStopScreenCast()
      failed = res?.control_failed ?? []
      const map = res?.reasons ?? {}
      reasons = failed.map((id) => map[id] ?? '')
    } catch (err) {
      console.error('Failed to stop screencast:', err)
      // 命令层本身失败时无法确认电视收到 Stop
      if (isDlna && deviceId) {
        failed = [deviceId]
        reasons = [String(err)]
      }
    } finally {
      await fetchStatus()
      if (failed.length) {
        const deviceStore = useDeviceStore()
        stopWarning.value = {
          devices: failed.map(
            (id) => deviceStore.devices.find((d) => d.id === id)?.name ?? id
          ),
          reasons: reasons.filter(Boolean),
        }
      }
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
    stopWarning.value = null
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
    stopWarning,
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
    dismissStopWarning: () => { stopWarning.value = null },
  }
})
