<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Modal from './Modal.vue'
import { useDeviceStore } from '@/stores/device'

const { t } = useI18n()
const deviceStore = useDeviceStore()

// 恢复时后端要重新发现一次，条目上给个 loading 态，不然点了没反应像没生效
const restoring = ref<string | null>(null)

async function restore(udn: string) {
  if (restoring.value) return
  restoring.value = udn
  try {
    await deviceStore.unhide(udn)
  } catch (err) {
    console.error('Failed to unhide device:', err)
  } finally {
    restoring.value = null
  }
}
</script>

<template>
  <Modal
    :visible="deviceStore.showHiddenModal"
    :title="t('devices.hiddenTitle')"
    :show-icon="false"
    @close="deviceStore.closeHiddenModal()"
  >
    <p class="hdm-hint">{{ t('devices.hiddenHint') }}</p>

    <div v-if="deviceStore.hiddenDevices.length" class="hdm-list">
      <div v-for="item in deviceStore.hiddenDevices" :key="item.udn" class="hdm-item">
        <div class="hdm-item__main">
          <div class="hdm-item__name">
            {{ item.name }}
            <span v-if="item.is_self" class="hdm-item__badge">{{ t('devices.hiddenSelf') }}</span>
          </div>
          <div class="hdm-item__meta">
            <span v-if="item.ip">{{ item.ip }}</span>
            <span :class="['hdm-item__state', item.online ? 'is-online' : 'is-offline']">
              {{ item.online ? t('devices.hiddenOnline') : t('devices.hiddenOffline') }}
            </span>
          </div>
        </div>
        <button
          class="btn-confirm hdm-item__btn"
          :disabled="restoring === item.udn"
          @click="restore(item.udn)"
        >
          {{ restoring === item.udn ? t('devices.hiddenRestoring') : t('devices.hiddenRestore') }}
        </button>
      </div>
    </div>
    <div v-else class="hdm-empty">{{ t('devices.hiddenEmpty') }}</div>

    <template #actions>
      <button class="btn-cancel" @click="deviceStore.closeHiddenModal()">{{ t('common.close') }}</button>
    </template>
  </Modal>
</template>

<style scoped>
.hdm-hint,
.hdm-item__main {
  text-align: left;
}

.hdm-hint {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  margin-bottom: var(--sp-sm);
}

.hdm-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 260px;
  overflow-y: auto;
}

.hdm-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
}

.hdm-item__main {
  flex: 1;
  min-width: 0;
}

.hdm-item__name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.hdm-item__badge {
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 400;
  color: var(--text-secondary);
  background: var(--bg-secondary);
  border-radius: var(--r-sm);
  padding: 1px 5px;
}

.hdm-item__meta {
  display: flex;
  gap: 8px;
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.hdm-item__state.is-online {
  color: #16A34A;
}

.hdm-item__state.is-offline {
  color: var(--text-tertiary);
}

.hdm-item__btn {
  flex-shrink: 0;
  padding: 4px 10px;
  font-size: 11px;
  color: var(--on-primary);
  background: var(--primary);
  border-radius: var(--r-md);
  white-space: nowrap;
}

.hdm-item__btn:hover:not(:disabled) {
  background: var(--primary-hover);
}

.hdm-item__btn:disabled {
  opacity: 0.55;
  cursor: default;
}

.hdm-empty {
  padding: var(--sp-lg) 0;
  text-align: center;
  font-size: 12px;
  color: var(--text-tertiary);
}
</style>
