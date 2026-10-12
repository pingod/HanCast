<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import Modal from './Modal.vue'
import { useDeviceStore } from '@/stores/device'

const { t } = useI18n()
const deviceStore = useDeviceStore()

const address = ref('')
const addedName = ref('')

async function submit() {
  const value = address.value.trim()
  if (!value || deviceStore.adding) return
  addedName.value = ''
  const name = await deviceStore.addDevice(value)
  if (name) {
    address.value = ''
    addedName.value = name
    // 关得太快用户看不到「已添加」，留 900ms 再收起
    setTimeout(() => deviceStore.closeAddModal(), 900)
  }
}
</script>

<template>
  <Modal
    :visible="deviceStore.showAddModal"
    :title="t('devices.addDevice')"
    :show-icon="false"
    @close="deviceStore.closeAddModal()"
  >
    <p class="adm-hint">{{ t('devices.addHint') }}</p>

    <div class="adm-row">
      <input
        v-model="address"
        class="adm-input"
        :placeholder="t('devices.addressPlaceholder')"
        :disabled="deviceStore.adding"
        @keyup.enter="submit"
      />
      <button class="btn-confirm adm-add" :disabled="deviceStore.adding || !address.trim()" @click="submit">
        {{ deviceStore.adding ? t('devices.adding') : t('devices.add') }}
      </button>
    </div>

    <div v-if="deviceStore.addError" class="adm-error">
      {{ t('devices.addFailed') }}
      <div class="adm-error-detail">{{ deviceStore.addError }}</div>
    </div>
    <div v-if="addedName" class="adm-ok">{{ t('devices.addOk', { name: addedName }) }}</div>

    <div class="adm-saved">
      <div class="adm-saved-title">{{ t('devices.manualSaved') }}</div>
      <div v-if="deviceStore.manualDevices.length" class="adm-saved-list">
        <div v-for="addr in deviceStore.manualDevices" :key="addr" class="adm-saved-item">{{ addr }}</div>
      </div>
      <div v-else class="adm-saved-empty">{{ t('devices.manualEmpty') }}</div>
    </div>

    <template #actions>
      <button class="btn-cancel" @click="deviceStore.closeAddModal()">{{ t('common.close') }}</button>
    </template>
  </Modal>
</template>

<style scoped>
.adm-hint,
.adm-error,
.adm-ok,
.adm-saved {
  text-align: left;
}

.adm-hint {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  margin-bottom: var(--sp-sm);
}

.adm-row {
  display: flex;
  gap: 6px;
}

.adm-input {
  flex: 1;
  min-width: 0;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--text-primary);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  outline: none;
  font-family: var(--font);
}

.adm-input:focus {
  border-color: var(--border-focus);
}

.adm-add {
  padding: 6px 12px;
  font-size: 12px;
  color: white;
  background: var(--primary);
  border-radius: var(--r-md);
  white-space: nowrap;
}

.adm-add:hover:not(:disabled) {
  background: var(--primary-hover);
}

.adm-add:disabled {
  opacity: 0.55;
  cursor: default;
}

.adm-error {
  margin-top: 8px;
  font-size: 12px;
  color: #DC2626;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.25);
  border-radius: var(--r-md);
  padding: 6px 8px;
}

.adm-error-detail {
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-secondary);
  word-break: break-all;
}

.adm-ok {
  margin-top: 8px;
  font-size: 12px;
  color: #16A34A;
}

.adm-saved {
  margin-top: 12px;
}

.adm-saved-title {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-bottom: 4px;
}

.adm-saved-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 92px;
  overflow-y: auto;
}

.adm-saved-item {
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--text-primary);
}

.adm-saved-empty {
  font-size: 11px;
  color: var(--text-tertiary);
}
</style>
