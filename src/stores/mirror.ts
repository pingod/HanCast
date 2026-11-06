import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { defineStore } from 'pinia'
import type { UnlistenFn } from '@tauri-apps/api/event'
import type {
  MirrorStatus,
  MirrorState,
  MirrorDevice,
  MirrorPrefs,
} from '@/types/mirror'
import {
  getMirrorStatus,
  listMirrorDevices,
  connectMirrorDevice,
  startMirror as apiStartMirror,
  stopMirror as apiStopMirror,
  setMirrorToolPath as apiSetMirrorToolPath,
  notify,
} from '@/api/commands'
import { tt } from '@/i18n'

const PREFS_KEY = 'hancast.mirror.settings'

const DEFAULT_PREFS: MirrorPrefs = {
  max_size: 0,
  max_fps: 0,
  video_bitrate: 8,
  audio: true,
  control: true,
  turn_screen_off: false,
  stay_awake: true,
  always_on_top: false,
}

function loadPrefs(): MirrorPrefs {
  try {
    const raw = localStorage.getItem(PREFS_KEY)
    if (!raw) return { ...DEFAULT_PREFS }
    const parsed = JSON.parse(raw) as Partial<MirrorPrefs>
    // 逐字段兜底：旧版本存过的字段可能缺，也可能类型不对
    return {
      max_size: Number(parsed.max_size) || 0,
      max_fps: Number(parsed.max_fps) || 0,
      video_bitrate: Number(parsed.video_bitrate) || DEFAULT_PREFS.video_bitrate,
      audio: parsed.audio !== false,
      control: parsed.control !== false,
      turn_screen_off: parsed.turn_screen_off === true,
      stay_awake: parsed.stay_awake !== false,
      always_on_top: parsed.always_on_top === true,
    }
  } catch {
    return { ...DEFAULT_PREFS }
  }
}

export const useMirrorStore = defineStore('mirror', () => {
  // ── State ──
  const status = ref<MirrorStatus | null>(null)
  const devices = ref<MirrorDevice[]>([])
  const selectedSerial = ref('')
  const showPanel = ref(false)
  const loading = ref(false)
  const devicesLoading = ref(false)
  const starting = ref(false)
  const stopping = ref(false)
  const lastError = ref<string | null>(null)
  const prefs = ref<MirrorPrefs>(loadPrefs())

  let pollTimer: number | null = null
  let unlisten: UnlistenFn | null = null
  let lastAlertedError: string | null = null

  // ── Computed ──
  const isRunning = computed(() => !!status.value?.running)
  const toolsOk = computed(() => !!status.value?.ok)
  const onlineDevices = computed(() => devices.value.filter(d => d.online))
  const selectedDevice = computed(
    () => devices.value.find(d => d.serial === selectedSerial.value) ?? null,
  )
  const audioSupported = computed(() => status.value?.scrcpy_audio_supported !== false)

  // ── 偏好持久化 ──
  watch(prefs, (value) => {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(value))
    } catch {
      // 隐私模式 / 配额满：记不住偏好不影响功能
    }
  }, { deep: true })

  function applyState(state: Partial<MirrorState>) {
    if (!status.value) return
    const wasRunning = status.value.running
    status.value = { ...status.value, ...state }
    if (wasRunning && !state.running) {
      // 会话结束（正常停止或手机拔掉）：设备状态可能变了，顺手刷一次
      void refreshDevices()
    }
  }

  // ── Actions ──

  /** 手机拔掉 / scrcpy 中途挂了：窗口收进托盘时界面没人看得到，必须发系统通知 */
  function alertError(reason: string) {
    if (!reason || reason === lastAlertedError) return
    lastAlertedError = reason
    void notify(tt('mirror.notifyTitle'), reason)
  }

  async function refreshStatus(refresh = false) {
    loading.value = true
    try {
      const report = await getMirrorStatus(refresh)
      status.value = report
      if (report.error) {
        lastError.value = report.error
        alertError(report.error)
      }
      return report
    } catch (err) {
      console.error('Failed to get mirror status:', err)
      lastError.value = String(err)
      return null
    } finally {
      loading.value = false
    }
  }

  async function refreshDevices() {
    devicesLoading.value = true
    try {
      const report = await listMirrorDevices()
      if (report.error) lastError.value = report.error
      devices.value = report.devices
      const stillThere = report.devices.some(d => d.serial === selectedSerial.value && d.online)
      if (!stillThere) {
        // 默认选第一台在线设备：绝大多数人只插一台手机
        selectedSerial.value = report.devices.find(d => d.online)?.serial ?? ''
      }
      return report
    } catch (err) {
      console.error('Failed to list mirror devices:', err)
      lastError.value = String(err)
      return null
    } finally {
      devicesLoading.value = false
    }
  }

  async function connect(address: string) {
    lastError.value = null
    try {
      const result = await connectMirrorDevice(address)
      if (!result.ok) {
        lastError.value = result.error ?? '连接失败'
        return result
      }
      await refreshDevices()
      if (result.device?.serial) selectedSerial.value = result.device.serial
      return result
    } catch (err) {
      lastError.value = String(err)
      return { ok: false, error: String(err) }
    }
  }

  async function start() {
    if (starting.value || isRunning.value) return
    const serial = selectedSerial.value || selectedDevice.value?.serial
    if (!serial) {
      lastError.value = tt('mirror.noDevice')
      return
    }
    starting.value = true
    lastError.value = null
    try {
      const result = await apiStartMirror({
        serial,
        window_title: tt('mirror.windowTitle'),
        ...prefs.value,
      })
      if (!result.ok) {
        lastError.value = result.error ?? '启动镜像失败'
        alertError(lastError.value)
        // 启动失败后端会回传最新状态（running=false）
        if (typeof result.running === 'boolean' && status.value) {
          applyState(result)
        }
        return
      }
      if (result.running !== undefined && status.value) applyState(result)
      else await refreshStatus(false)
      startPolling()
    } catch (err) {
      lastError.value = String(err)
      alertError(lastError.value)
    } finally {
      starting.value = false
    }
  }

  async function stop() {
    if (stopping.value) return
    stopping.value = true
    try {
      const result = await apiStopMirror()
      if (typeof result.running === 'boolean' && status.value) applyState(result)
      else await refreshStatus(false)
      stopPolling()
    } catch (err) {
      lastError.value = String(err)
    } finally {
      stopping.value = false
    }
  }

  async function setToolPath(tool: 'adb' | 'scrcpy', path: string) {
    lastError.value = null
    try {
      const result = await apiSetMirrorToolPath(tool, path)
      if (!result.ok) {
        lastError.value = result.error ?? '路径设置失败'
        return result
      }
      await refreshStatus(true)
      await refreshDevices()
      return result
    } catch (err) {
      lastError.value = String(err)
      return { ok: false, error: String(err) }
    }
  }

  function startPolling() {
    stopPolling()
    pollTimer = window.setInterval(() => {
      // 面板关着就不轮询：scrcpy 中途退出时由事件通知，不靠轮询兜底
      if (!showPanel.value) return
      void refreshStatus(false)
    }, 2000)
  }

  function stopPolling() {
    if (pollTimer !== null) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  function openPanel() {
    showPanel.value = true
    lastError.value = null
    void refreshStatus(false)
    void refreshDevices()
    startPolling()
  }

  function closePanel() {
    showPanel.value = false
    stopPolling()
  }

  onMounted(() => {
    // 全局只订阅一次：面板关着也要能收到「手机拔了」的通知
    void import('@tauri-apps/api/event').then(async ({ listen }) => {
      unlisten = await listen<MirrorState>('mirror_state_changed', (event) => {
        applyState(event.payload)
        if (event.payload.error) {
          lastError.value = event.payload.error
          alertError(event.payload.error)
        }
        if (!event.payload.running) stopPolling()
      })
    }).catch(() => {
      // 浏览器 dev：没有 Tauri 事件总线
    })
  })

  onUnmounted(() => {
    stopPolling()
    unlisten?.()
    unlisten = null
  })

  return {
    status,
    devices,
    selectedSerial,
    showPanel,
    loading,
    devicesLoading,
    starting,
    stopping,
    lastError,
    prefs,
    isRunning,
    toolsOk,
    onlineDevices,
    selectedDevice,
    audioSupported,
    refreshStatus,
    refreshDevices,
    connect,
    start,
    stop,
    setToolPath,
    openPanel,
    closePanel,
  }
})
