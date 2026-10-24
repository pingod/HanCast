<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useThemeStore } from '@/stores/theme'
import { useGuardStore } from '@/stores/guard'
import { useUpdateStore } from '@/stores/update'
import { useDeviceStore } from '@/stores/device'
import { useCastStore } from '@/stores/cast'
import { useScreenCastStore } from '@/stores/screencast'
import GuardConfirmModal from '@/components/GuardConfirmModal.vue'
import UpdateModal from '@/components/UpdateModal.vue'
import LogsPanel from '@/components/LogsPanel.vue'
import AddDeviceModal from '@/components/AddDeviceModal.vue'

const settingsStore = useSettingsStore()
const themeStore = useThemeStore()
const guardStore = useGuardStore()
const updateStore = useUpdateStore()
// Pinia setup stores are singletons, but their component lifecycle hooks belong
// to the first caller. Own all persistent subscriptions here, not in a route
// that is destroyed when opening Settings (returning does not rerun setup).
useDeviceStore()
useCastStore()
useScreenCastStore()

// 非 dev 模式下禁用浏览器默认右键菜单
function onContextMenu(e: MouseEvent) {
  e.preventDefault()
}

onMounted(() => {
  settingsStore.fetchSettings()
  if (!import.meta.env.DEV) {
    document.addEventListener('contextmenu', onContextMenu)
  }
})

onUnmounted(() => {
  document.removeEventListener('contextmenu', onContextMenu)
})
</script>

<template>
  <router-view />
  <!-- Guard modal: always mounted, works on any page -->
  <GuardConfirmModal />
  <!-- Update modal: always mounted, listens for update-available event -->
  <UpdateModal />
  <!-- 日志面板 / 手动添加设备：设置页和设备列表都要能打开，挂全局 -->
  <LogsPanel />
  <AddDeviceModal />
</template>

<style>
/* Global styles are imported via main.ts */
</style>
