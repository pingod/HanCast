<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { StopWarning } from '@/api/commands'

// 「本机这一侧已经停了，但电视没确认收到 Stop」的全局提示。
// 投屏条和控制器弹窗都会在停止后关闭/清空会话，所以提示必须挂在页面上，
// 否则用户只会看到「点了停止、界面没反应」，而真相要靠这行字说明。
defineProps<{ warning: StopWarning | null }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const { t } = useI18n()
</script>

<template>
  <div v-if="warning" class="stop-warn" role="alert">
    <span class="stop-warn__icon">!</span>
    <div class="stop-warn__body">
      <div class="stop-warn__text">
        {{ t('cast.stopNotDelivered', { devices: warning.devices.join('、') }) }}
      </div>
      <ul v-if="warning.reasons.length" class="stop-warn__reasons">
        <li v-for="(reason, i) in warning.reasons" :key="i">{{ reason }}</li>
      </ul>
    </div>
    <button class="stop-warn__close" :title="t('common.close')" @click="emit('close')">×</button>
  </div>
</template>

<style scoped>
.stop-warn {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-sm);
  padding: var(--sp-sm) var(--sp-md);
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-primary);
  background: rgba(245, 158, 11, 0.1);
  border: 1px solid rgba(245, 158, 11, 0.4);
  border-radius: var(--r-md);
}

.stop-warn__icon {
  flex: none;
  width: 16px;
  height: 16px;
  margin-top: 1px;
  font-weight: 700;
  line-height: 16px;
  text-align: center;
  color: #fff;
  background: var(--status-busy);
  border-radius: var(--r-full);
}

.stop-warn__body {
  flex: 1;
  min-width: 0;
}

.stop-warn__reasons {
  margin: 4px 0 0;
  padding-left: 16px;
  color: var(--text-secondary);
}

.stop-warn__close {
  flex: none;
  padding: 0 4px;
  font-size: 16px;
  line-height: 1;
  color: var(--text-tertiary);
  background: none;
  border: none;
  cursor: pointer;
}

.stop-warn__close:hover {
  color: var(--text-primary);
}
</style>
