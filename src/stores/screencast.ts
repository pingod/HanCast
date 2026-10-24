import { ref, computed, onMounted, onUnmounted } from 'vue'
import { defineStore } from 'pinia'
import type { UnlistenFn } from '@tauri-apps/api/event'
import type { StopWarning } from '@/api/commands'
import { useDeviceStore } from '@/stores/device'
import type {
  ScreenSources,
  ScreenCastState,
  ScreenCastStartParams,
  FfmpegStatus,
  FfmpegDownloadTask,
} from '@/types/screencast'
import {
  listScreenSources,
  startScreenCast as apiStartScreenCast,
  stopScreenCast as apiStopScreenCast,
  getScreenCastStatus as apiGetScreenCastStatus,
  setFfmpegPath as apiSetFfmpegPath,
  ffmpegStatus as apiFfmpegStatus,
  ffmpegDownloadStart as apiFfmpegDownloadStart,
  ffmpegDownloadStatus as apiFfmpegDownloadStatus,
  openFfmpegDir as apiOpenFfmpegDir,
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
  // 「需要下载运行依赖」弹窗：状态由后端 ffmpeg_status 供，下载进度轮询驱动
  const showDepsModal = ref(false)
  const ffmpegReport = ref<FfmpegStatus | null>(null)
  const downloadTask = ref<FfmpegDownloadTask | null>(null)
  const downloading = ref(false)
  const reportLoading = ref(false)
  let downloadTimer: number | null = null

  // ── Computed ──
  const isRunning = computed(() => state.value.running)
  const ffmpegOk = computed(() => !!(sources.value?.ffmpeg?.ok ?? ffmpegReport.value?.ok))
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
      ffmpegReport.value = sources.value.ffmpeg
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

  /** 重新扫描本机（refresh=false 时用后端 30s 缓存，供弹窗打开时秒出） */
  async function refreshFfmpegReport(refresh = true) {
    reportLoading.value = true
    try {
      const report = await apiFfmpegStatus(refresh)
      ffmpegReport.value = report
      if (sources.value) sources.value.ffmpeg = report
      lastError.value = report.ok ? null : (report.error ?? 'FFmpeg 不可用')
      return report
    } catch (err) {
      console.error('Failed to check ffmpeg:', err)
      lastError.value = '检测 FFmpeg 失败'
      return null
    } finally {
      reportLoading.value = false
    }
  }

  function stopDownloadPolling() {
    if (downloadTimer !== null) {
      clearInterval(downloadTimer)
      downloadTimer = null
    }
  }

  async function pollDownload() {
    try {
      const task = await apiFfmpegDownloadStatus()
      downloadTask.value = task
      if (task.status !== 'pending') {
        stopDownloadPolling()
        downloading.value = false
        if (task.status === 'done') await refreshFfmpegReport(true)
      }
    } catch (err) {
      stopDownloadPolling()
      downloading.value = false
      downloadTask.value = { status: 'error', stage: 'failed', error: String(err) }
    }
  }

  /** 让程序自己下载 FFmpeg —— 大文件走后台任务，命令循环 60s 超时扛不住 */
  async function startFfmpegDownload() {
    if (downloading.value) return
    downloading.value = true
    lastError.value = null
    downloadTask.value = { status: 'pending', stage: 'connecting' }
    try {
      downloadTask.value = await apiFfmpegDownloadStart()
      stopDownloadPolling()
      downloadTimer = window.setInterval(pollDownload, 1000)
    } catch (err) {
      downloading.value = false
      downloadTask.value = { status: 'error', stage: 'failed', error: String(err) }
    }
  }

  async function openFfmpegDir() {
    try {
      const res = await apiOpenFfmpegDir()
      if (!res?.ok) lastError.value = res?.error ?? '无法打开目录'
      return res
    } catch (err) {
      console.error('Failed to open ffmpeg dir:', err)
      lastError.value = '无法打开目录'
      return null
    }
  }

  /** 轮询定时器挂在组件生命周期上，关面板就会断；重新打开时必须向后端对齐一次，
   *  否则界面永远停在「下载中...」，而按钮是禁用的——后端其实早下完了。 */
  async function resumeDownloadPolling() {
    try {
      const task = await apiFfmpegDownloadStatus()
      downloadTask.value = task
      if (task.status === 'pending') {
        downloading.value = true
        if (downloadTimer === null) downloadTimer = window.setInterval(pollDownload, 1000)
        return
      }
      downloading.value = false
      if (task.status === 'done') await refreshFfmpegReport(true)
    } catch {
      downloading.value = false
    }
  }

  function openDepsModal() {
    showDepsModal.value = true
    if (!ffmpegReport.value) refreshFfmpegReport(false)
    resumeDownloadPolling()
  }

  function closeDepsModal() {
    showDepsModal.value = false
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
  let disposed = false

  async function setupListeners() {
    try {
      const { listen } = await import('@tauri-apps/api/event')
      if (disposed) return
      const unlisten = await listen<ScreenCastState>('screen_cast_state_changed', (event) => {
        if (disposed) return
        state.value = event.payload
        if (event.payload.error) {
          lastError.value = event.payload.error
        }
      })
      if (disposed) unlisten()
      else unlistenFns.push(unlisten)
    } catch {
      // Running outside Tauri, skip listeners
    }
  }

  onMounted(() => {
    fetchStatus()
    setupListeners()
  })

  onUnmounted(() => {
    disposed = true
    stopDownloadPolling()
    unlistenFns.splice(0).forEach((fn) => fn())
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
    showDepsModal,
    ffmpegReport,
    downloadTask,
    downloading,
    reportLoading,
    isRunning,
    ffmpegOk,
    micDevices,
    systemDevices,
    windows,
    fetchSources,
    fetchStatus,
    refreshFfmpegReport,
    startFfmpegDownload,
    openFfmpegDir,
    openDepsModal,
    closeDepsModal,
    start,
    stop,
    setFfmpegPath,
    openPanel,
    closePanel,
    dismissStopWarning: () => { stopWarning.value = null },
  }
})
