<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Modal from '@/components/Modal.vue'
import { useScreenCastStore } from '@/stores/screencast'
import { writeClipboardText } from '@/api/commands'

const { t } = useI18n()
const sc = useScreenCastStore()

const report = computed(() => sc.ffmpegReport)
const task = computed(() => sc.downloadTask)
const missing = computed(() => report.value?.missing ?? [])
// 下载失败时把后端原样交来的异常链摆在明面上，别压成一句「下载失败」
const failure = computed(() =>
  (!sc.downloading && task.value?.status === 'error') ? (task.value.error ?? '') : '')
const canDownload = computed(() =>
  missing.value.length > 0 && report.value?.download?.available === true)

const manualPath = ref('')
const copied = ref('')

// 下载完成后直接收起弹窗：「下载并继续」的「继续」就是回到投屏面板
watch(() => task.value?.status, (s) => { if (s === 'done') sc.closeDepsModal() })

function stageText(stage?: string) {
  return stage ? t(`screencast.deps.downloadStage.${stage}`) : ''
}

function fmtBytes(n?: number): string {
  if (!n || n < 0) return '0'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

const progressText = computed(() => {
  const v = task.value
  if (!v) return ''
  return t('screencast.deps.downloadedSize', {
    done: fmtBytes(v.received),
    total: v.total ? fmtBytes(v.total) : '?',
    speed: fmtBytes(v.speed),
  })
})

const percent = computed(() => {
  const p = task.value?.percent
  return typeof p === 'number' ? Math.max(0, Math.min(100, p)) : null
})

async function copyText(key: string, text: string) {
  try {
    await writeClipboardText(text)
  } catch {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      return
    }
  }
  copied.value = key
  setTimeout(() => { if (copied.value === key) copied.value = '' }, 1500)
}

function sourceLabel(item: { source: string | null }) {
  return item.source ? t(`screencast.deps.source.${item.source}`) : ''
}

async function onRecheck() {
  await sc.refreshFfmpegReport(true)
}

async function onSetManual() {
  const path = manualPath.value.trim()
  if (!path) return
  if (await sc.setFfmpegPath(path)) manualPath.value = ''
}
</script>

<template>
  <Modal :visible="sc.showDepsModal" @close="sc.closeDepsModal()">
    <div class="deps">
      <div class="deps-head">
        <span class="deps-title">{{ t('screencast.deps.title') }}</span>
        <button class="deps-x" :aria-label="t('common.close')" @click="sc.closeDepsModal()">✕</button>
      </div>

      <p class="deps-statement">
        {{ t('screencast.deps.statement') }}
        <span v-if="missing.length" class="deps-missing">
          {{ t('screencast.deps.missing', { list: missing.join('、') }) }}
        </span>
        <span v-else class="deps-ready">{{ t('screencast.deps.allSet') }}</span>
      </p>

      <ul v-if="report" class="deps-items">
        <li v-for="item in report.items" :key="item.key" class="deps-item">
          <span class="deps-item-name">{{ item.label }}</span>
          <span class="deps-badge" :class="item.found ? 'deps-badge--ok' : 'deps-badge--bad'">
            {{ item.found ? t('screencast.deps.found') : t('screencast.deps.missingItem') }}
          </span>
          <span v-if="!item.required" class="deps-badge deps-badge--opt">
            {{ t('screencast.deps.optional') }}
          </span>
          <span v-if="item.found" class="deps-item-path" :title="item.path ?? ''">
            {{ sourceLabel(item) }} · {{ item.path }}<template v-if="item.version"> · {{ item.version }}</template>
          </span>
        </li>
      </ul>

      <div v-if="failure" class="deps-error">
        <span class="deps-error-text">
          {{ t('screencast.deps.failedLabel', { dep: 'FFmpeg' }) }}{{ failure }}
        </span>
      </div>

      <div v-if="sc.downloading" class="deps-progress">
        <div class="deps-stage">{{ stageText(task?.stage) }}</div>
        <div class="deps-bar">
          <i v-if="percent !== null" :style="{ width: percent + '%' }"></i>
          <i v-else class="deps-bar--unknown"></i>
        </div>
        <div class="deps-size">{{ progressText }}</div>
      </div>

      <p v-if="report?.install_commands?.length" class="deps-hint">
        <span>{{ t('screencast.deps.installHint') }}</span>
        <code
          v-for="cmd in report.install_commands"
          :key="cmd"
          class="deps-cmd"
          @click="copyText(cmd, cmd)"
        >{{ copied === cmd ? t('screencast.deps.copied') : cmd }}</code>
      </p>
      <p v-else-if="missing.length && report?.download?.available !== true" class="deps-hint">
        {{ t('screencast.deps.unavailable') }}
      </p>

      <p v-if="report?.managed_dir" class="deps-dir" :title="report.managed_dir">
        {{ t('screencast.deps.dirHint', { dir: report.managed_dir }) }}
      </p>

      <div class="deps-manual">
        <span class="deps-manual-label">{{ t('screencast.deps.manual') }}</span>
        <div class="deps-manual-row">
          <input
            v-model="manualPath"
            class="deps-input"
            :placeholder="t('screencast.ffmpegPathPlaceholder')"
            @keyup.enter="onSetManual"
          />
          <button class="deps-btn" :disabled="!manualPath.trim()" @click="onSetManual">
            {{ t('screencast.setPath') }}
          </button>
        </div>
      </div>
    </div>

    <template #actions>
      <button class="btn-cancel" @click="sc.openFfmpegDir()">
        {{ t('screencast.deps.openDir') }}
      </button>
      <button class="btn-cancel" :disabled="sc.reportLoading" @click="onRecheck">
        {{ sc.reportLoading ? t('screencast.deps.checking') : t('screencast.deps.recheck') }}
      </button>
      <button
        v-if="canDownload"
        class="btn-confirm"
        :disabled="sc.downloading"
        @click="sc.startFfmpegDownload()"
      >
        {{ sc.downloading ? t('screencast.deps.downloading') : t('screencast.deps.download') }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.deps {
  display: flex;
  flex-direction: column;
  gap: 10px;
  text-align: left;
}

.deps-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.deps-title {
  flex: 1;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.deps-x {
  padding: 2px 6px;
  font-size: 13px;
  line-height: 1;
  color: var(--text-tertiary);
  background: none;
  border: none;
  cursor: pointer;
  font-family: var(--font);
}

.deps-x:hover {
  color: var(--text-primary);
}

.deps-statement {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--text-secondary);
}

.deps-missing {
  color: #DC2626;
  font-weight: 600;
}

.deps-ready {
  color: #16A34A;
}

.deps-items {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.deps-item {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 12px;
}

.deps-item-name {
  font-weight: 600;
  color: var(--text-primary);
}

.deps-badge {
  padding: 1px 6px;
  font-size: 11px;
  border-radius: 999px;
  border: 1px solid var(--border);
  color: var(--text-secondary);
}

.deps-badge--ok {
  color: #16A34A;
  border-color: rgba(22, 163, 74, 0.35);
  background: rgba(22, 163, 74, 0.08);
}

.deps-badge--bad {
  color: #DC2626;
  border-color: rgba(220, 38, 38, 0.35);
  background: rgba(220, 38, 38, 0.08);
}

.deps-badge--opt {
  color: var(--text-tertiary);
}

.deps-item-path {
  flex: 1 1 100%;
  font-size: 11px;
  color: var(--text-tertiary);
  overflow-wrap: anywhere;
}

.deps-error {
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.25);
  border-radius: var(--r-md);
  padding: 8px 10px;
}

.deps-error-text {
  font-size: 11px;
  line-height: 1.7;
  color: #DC2626;
  overflow-wrap: anywhere;
}

.deps-progress {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.deps-stage,
.deps-size {
  font-size: 11px;
  color: var(--text-secondary);
}

.deps-bar {
  height: 6px;
  border-radius: 999px;
  background: var(--bg-secondary);
  overflow: hidden;
}

.deps-bar i {
  display: block;
  height: 100%;
  background: var(--primary);
  transition: width var(--transition-fast);
}

.deps-bar--unknown {
  width: 35%;
  animation: deps-slide 1.2s linear infinite;
}

@keyframes deps-slide {
  0% { transform: translateX(-100%); }
  100% { transform: translateX(300%); }
}

.deps-hint {
  margin: 0;
  font-size: 11px;
  line-height: 1.8;
  color: var(--text-secondary);
}

.deps-cmd {
  display: block;
  padding: 3px 6px;
  margin-top: 2px;
  font-size: 11px;
  color: var(--text-primary);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  cursor: pointer;
  overflow-wrap: anywhere;
}

.deps-cmd:hover {
  border-color: var(--border-focus);
}

.deps-dir {
  margin: 0;
  font-size: 11px;
  color: var(--text-tertiary);
  overflow-wrap: anywhere;
}

.deps-manual {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.deps-manual-label {
  font-size: 11px;
  color: var(--text-tertiary);
}

.deps-manual-row {
  display: flex;
  gap: 6px;
}

.deps-input {
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

.deps-input:focus {
  border-color: var(--border-focus);
}

.deps-btn {
  padding: 6px 12px;
  font-size: 12px;
  color: var(--text-primary);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  cursor: pointer;
  font-family: var(--font);
}

.deps-btn:hover:not(:disabled) {
  border-color: var(--border-focus);
  color: var(--primary);
}

.deps-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
