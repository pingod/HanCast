<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Modal from '@/components/Modal.vue'
import { remoteStatus, startRemoteControl, writeClipboardText } from '@/api/commands'

const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const { t } = useI18n()

const url = ref('')
const port = ref(0)
const running = ref(false)
const loading = ref(false)
const error = ref('')
const copied = ref(false)

/**
 * 每次打开都问后端要一次地址，而不是缓存上次的：
 * 端口被占用时服务会退到随机端口，缓存值会把用户指到一个死地址。
 */
async function refresh() {
  error.value = ''
  copied.value = false
  loading.value = true
  try {
    let r = await remoteStatus()
    // 没在跑就顺手起一次（幂等：已在跑时后端返回同一份地址）
    if (!r.ok) r = await startRemoteControl()
    running.value = r.ok
    url.value = r.url || ''
    port.value = r.port || 0
    if (!r.ok || !r.url) error.value = t('cast.remoteFailed')
  } catch (err: any) {
    running.value = false
    url.value = ''
    error.value = String(err?.message ?? err) || t('cast.remoteFailed')
  } finally {
    loading.value = false
  }
}

watch(() => props.visible, (v) => { if (v) refresh() }, { immediate: true })

const displayUrl = computed(() => url.value.replace(/^https?:\/\//, ''))

async function copyUrl() {
  if (!url.value) return
  try {
    await writeClipboardText(url.value)
    copied.value = true
  } catch {
    /* 复制失败就让用户自己选中文本 */
  }
}

function close() {
  emit('close')
}
</script>

<template>
  <Modal :visible="visible" :title="t('cast.remoteTitle')" size="default" @close="close">
    <template #icon>
      <svg viewBox="0 0 24 24" width="36" height="36" fill="none" stroke="#5B9BF5" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
        <rect x="7" y="2" width="10" height="20" rx="2" />
        <path d="M11 5h2M11 18h2" />
      </svg>
    </template>

    <p class="remote__hint">{{ t('cast.remoteHint') }}</p>

    <p v-if="loading" class="remote__hint">{{ t('cast.remoteStarting') }}</p>
    <p v-else-if="error" class="remote__error">{{ error }}</p>
    <template v-else>
      <button class="remote__url" type="button" :title="t('cast.remoteCopy')" @click="copyUrl">
        {{ displayUrl }}
      </button>
      <p class="remote__hint">{{ copied ? t('cast.remoteCopied') : t('cast.remoteCopy') }}</p>
      <p class="remote__hint remote__hint--dim">
        {{ running ? t('cast.remoteAlwaysOn') : t('cast.remoteNotRunning') }}
      </p>
    </template>

    <template #actions>
      <button class="btn-cancel" type="button" @click="close">{{ t('common.close') }}</button>
      <button v-if="url" class="btn-confirm" type="button" @click="copyUrl">
        {{ copied ? t('cast.remoteCopied') : t('cast.remoteCopy') }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.remote__hint {
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.remote__hint--dim {
  color: var(--text-tertiary, var(--text-secondary));
  opacity: 0.8;
  margin-bottom: 0;
}

/* 地址是这一屏的主角：等宽、可选、整块可点复制 */
.remote__url {
  display: block;
  width: 100%;
  margin: 2px 0 8px;
  padding: 10px 12px;
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 13px;
  line-height: 1.5;
  color: var(--text-primary);
  background: var(--bg-input);
  border: 1px solid var(--border-focus);
  border-radius: var(--r-md);
  cursor: pointer;
  overflow-wrap: anywhere;
  text-align: center;
  transition: background var(--transition-fast), border-color var(--transition-fast);
}

.remote__url:hover {
  background: var(--bg-secondary);
}

.remote__error {
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--danger);
  overflow-wrap: anywhere;
}
</style>
