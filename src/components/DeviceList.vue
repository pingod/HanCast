<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDeviceStore } from '@/stores/device'
import { useLogsStore } from '@/stores/logs'
import DeviceCard from './DeviceCard.vue'

const { t } = useI18n()
const deviceStore = useDeviceStore()
const logsStore = useLogsStore()

// 控制刷新按钮旋转动画
const isSpinning = ref(false)

const handleRefresh = () => {
  if (isSpinning.value) return // 防止重复点击
  isSpinning.value = true
  deviceStore.refresh()
  // 10秒后停止动画
  setTimeout(() => {
    isSpinning.value = false
  }, 10000)
}
</script>

<template>
  <div class="device-list">
    <div class="device-list__header">
      <div class="device-list__title">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="2" y="3" width="20" height="14" rx="2" />
          <path d="M8 21h8M12 17v4" />
        </svg>
        <span>{{ t('devices.title') }}</span>
      </div>
      <div class="device-list__actions">
        <button
          class="device-list__icon-btn"
          :title="t('devices.addDevice')"
          :aria-label="t('devices.addDevice')"
          @click="deviceStore.openAddModal()"
        >
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="9" />
            <path d="M12 8v8M8 12h8" />
          </svg>
        </button>
        <button
          class="device-list__icon-btn"
          :title="t('settings.viewLogs')"
          :aria-label="t('settings.viewLogs')"
          @click="logsStore.open()"
        >
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M4 6h16M4 12h16M4 18h10" />
          </svg>
        </button>
        <button class="device-list__refresh" :title="t('devices.refresh')" @click="handleRefresh">
          <svg
            viewBox="0 0 24 24"
            width="16"
            height="16"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            :class="{ 'spinning': isSpinning }"
          >
            <path d="M23 4v6h-6M1 20v-6h6" />
            <path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" />
          </svg>
        </button>
      </div>
    </div>

    <div class="device-list__items" v-if="deviceStore.devices.length > 0">
      <DeviceCard
        v-for="device in deviceStore.devices"
        :key="device.id"
        :device="device"
      />
    </div>

    <div v-else class="device-list__empty">
      <p>{{ t('devices.offline') }}</p>
      <p class="device-list__empty-hint">{{ t('devices.emptyHint') }}</p>
      <button class="device-list__empty-btn" @click="deviceStore.openAddModal()">
        {{ t('devices.addDevice') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.device-list {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.device-list__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--sp-lg);
  margin-bottom: var(--sp-md);
}

.device-list__title {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.device-list__actions {
  display: flex;
  align-items: center;
  gap: 2px;
}

.device-list__refresh,
.device-list__icon-btn {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--r-sm);
  color: var(--text-secondary);
  transition: all var(--transition-fast);
}

.device-list__refresh:hover,
.device-list__icon-btn:hover {
  background: var(--bg-secondary);
  color: var(--text-primary);
}

.spinning {
  animation: spin-linear 10s linear;
}

@keyframes spin-linear {
  0% { transform: rotate(0deg); }
  100% { transform: rotate(1800deg); }
}

.device-list__items {
  display: flex;
  flex-direction: column;
  gap: var(--sp-sm);
  padding: 0 var(--sp-md);
  overflow-y: auto;
  flex: 1;
}

.device-list__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--sp-sm);
  padding: var(--sp-2xl);
  color: var(--text-tertiary);
  font-size: 13px;
}

.device-list__empty-hint {
  max-width: 300px;
  font-size: 12px;
  line-height: 1.6;
  text-align: center;
}

.device-list__empty-btn {
  padding: 5px 14px;
  font-size: 12px;
  color: var(--text-primary);
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  font-family: var(--font);
}

.device-list__empty-btn:hover {
  border-color: var(--border-focus);
  color: var(--primary);
}
</style>
