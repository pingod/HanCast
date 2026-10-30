import { ref, onMounted, onUnmounted } from 'vue'
import { defineStore } from 'pinia'
import type { Device } from '@/types/device'
import type { UnlistenFn } from '@tauri-apps/api/event'
import {
  getDevices,
  refreshDevices,
  setDefaultDevice as apiSetDefault,
  renameDevice as apiRename,
  hideDevice as apiHide,
  unhideDevice as apiUnhide,
  getHiddenDevices,
  addDevice as apiAddDevice,
  getManualDevices,
  removeManualDevice as apiRemoveManual,
  type HiddenDevice,
} from '@/api/commands'

export const useDeviceStore = defineStore('device', () => {
  const devices = ref<Device[]>([])
  const selectedDevice = ref<Device | null>(null)
  const loading = ref(false)
  const showAddModal = ref(false)
  const manualDevices = ref<string[]>([])
  const adding = ref(false)
  const addError = ref<string | null>(null)

  let refreshTimer: number | undefined
  const unlistenFns: UnlistenFn[] = []

  async function fetchDevices() {
    loading.value = true
    try {
      devices.value = await getDevices()
    } catch (err) {
      console.error('Failed to fetch devices:', err)
    } finally {
      loading.value = false
    }
  }

  async function refresh() {
    loading.value = true
    try {
      devices.value = await refreshDevices()
    } catch (err) {
      console.error('Failed to refresh devices:', err)
    } finally {
      loading.value = false
    }
  }

  async function setDefault(id: string) {
    await apiSetDefault(id)
    devices.value.forEach((d) => (d.is_default = d.id === id))
  }

  async function rename(id: string, name: string) {
    await apiRename(id, name)
    const device = devices.value.find((d) => d.id === id)
    if (device) device.name = name
  }

  async function hide(id: string) {
    await apiHide(id)
    devices.value = devices.value.filter((d) => d.id !== id)
    if (selectedDevice.value?.id === id) {
      selectedDevice.value = null
    }
    // 隐藏后同步隐藏列表，用户想反悔时能立刻在「已隐藏」里找回
    fetchHiddenDevices()
  }

  // ── 已隐藏设备（此前"隐藏"是单向门：界面上没有任何入口能恢复）──

  const hiddenDevices = ref<HiddenDevice[]>([])
  const showHiddenModal = ref(false)

  async function fetchHiddenDevices() {
    try {
      hiddenDevices.value = await getHiddenDevices()
    } catch (err) {
      console.error('Failed to fetch hidden devices:', err)
    }
  }

  function openHiddenModal() {
    showHiddenModal.value = true
    fetchHiddenDevices()
  }

  function closeHiddenModal() {
    showHiddenModal.value = false
  }

  /** 取消隐藏：从隐藏列表移除，再把设备重新拉回列表 */
  async function unhide(id: string) {
    await apiUnhide(id)
    hiddenDevices.value = hiddenDevices.value.filter((d) => d.udn !== id)
    await fetchDevices()
  }

  function selectDevice(device: Device | null) {
    selectedDevice.value = device
  }

  // ── 手动添加设备（组播被防火墙/安全软件拦截时的兜底通道）──

  async function fetchManualDevices() {
    try {
      manualDevices.value = await getManualDevices()
    } catch (err) {
      console.error('Failed to fetch manual devices:', err)
    }
  }

  /** 删掉一条填错/不再需要的地址（只摘持久化记录，不动已发现的设备） */
  async function removeManualAddress(address: string) {
    await apiRemoveManual(address)
    await fetchManualDevices()
  }

  function openAddModal() {
    showAddModal.value = true
    addError.value = null
    fetchManualDevices()
  }

  function closeAddModal() {
    showAddModal.value = false
    addError.value = null
  }

  /** 成功返回设备名，失败写入 addError 并返回 null */
  async function addDevice(address: string): Promise<string | null> {
    adding.value = true
    addError.value = null
    try {
      const device = await apiAddDevice(address)
      // device_found 事件对已存在设备可能不再触发，直接并入列表
      const exists = devices.value.some((d) => d.id === device.id)
      devices.value = exists
        ? devices.value.map((d) => (d.id === device.id ? { ...d, ...device } : d))
        : [...devices.value, device]
      await fetchManualDevices()
      return device.name
    } catch (err) {
      addError.value = err instanceof Error ? err.message : String(err)
      return null
    } finally {
      adding.value = false
    }
  }

  // Set up event listeners
  async function setupListeners() {
    try {
      const { listen } = await import('@tauri-apps/api/event')

      // Device found（upsert：新设备加入，已有设备就地更新，
      // 保证状态变化如 offline→online 能实时反映，而不是等 30s 自动刷新）
      unlistenFns.push(
        await listen<Device>('device_found', (event) => {
          const device = event.payload
          const idx = devices.value.findIndex((d) => d.id === device.id)
          if (idx === -1) {
            devices.value.push(device)
          } else {
            // 保留用户自定义名称（后端 custom_name 已并入 name，
            // 直接整体替换即可；is_default 标志以当前列表为准）
            devices.value = devices.value.map((d) =>
              d.id === device.id ? { ...d, ...device, is_default: d.is_default } : d
            )
          }
        })
      )

      // Device lost
      unlistenFns.push(
        await listen<{ id: string }>('device_lost', (event) => {
          devices.value = devices.value.filter((d) => d.id !== event.payload.id)
        })
      )
    } catch {
      // Running outside Tauri, skip listeners
    }
  }

  // Auto-refresh every 30s
  onMounted(() => {
    fetchDevices()
    fetchHiddenDevices()
    setupListeners()
    refreshTimer = window.setInterval(fetchDevices, 30000)
  })

  onUnmounted(() => {
    if (refreshTimer) clearInterval(refreshTimer)
    unlistenFns.forEach((fn) => fn())
  })

  return {
    devices,
    selectedDevice,
    loading,
    showAddModal,
    manualDevices,
    adding,
    addError,
    hiddenDevices,
    showHiddenModal,
    fetchDevices,
    refresh,
    setDefault,
    rename,
    hide,
    unhide,
    fetchHiddenDevices,
    openHiddenModal,
    closeHiddenModal,
    selectDevice,
    fetchManualDevices,
    removeManualAddress,
    openAddModal,
    closeAddModal,
    addDevice,
  }
})
