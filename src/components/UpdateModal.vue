<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useUpdateStore } from '@/stores/update'
import { writeClipboardText } from '@/api/commands'
import Modal from './Modal.vue'
import CustomSelect from './CustomSelect.vue'
import type { UpdateAsset } from '@/types/update'

const { t } = useI18n()
const updateStore = useUpdateStore()

const isVisible = computed(() => updateStore.showModal && !!updateStore.updateInfo?.has_update)
const info = computed(() => updateStore.updateInfo)
const task = computed(() => updateStore.downloadTask)
const assets = computed<UpdateAsset[]>(() => info.value?.assets ?? [])

/** 当前选中的产物名（Linux 上 deb / rpm / AppImage / Arch 四选一） */
const pickedName = ref('')
const selected = computed<UpdateAsset | null>(
  () => assets.value.find((a) => a.name === pickedName.value) || assets.value[0] || null,
)

watch(
  () => [info.value?.latest, assets.value.length] as const,
  () => {
    // 只在新版本 / 新清单到来时对齐一次；用户手动选过就别抢
    if (!pickedName.value || !assets.value.some((a) => a.name === pickedName.value)) {
      pickedName.value = info.value?.selected?.name || assets.value[0]?.name || ''
    }
  },
  { immediate: true },
)

const KINDS = ['dmg', 'nsis', 'msi', 'deb', 'rpm', 'arch', 'appimage']
function kindLabel(kind?: string): string {
  if (!kind) return ''
  return KINDS.includes(kind) ? t(`update.kind.${kind}`) : kind
}

const assetOptions = computed(() =>
  assets.value.map((a) => ({
    value: a.name,
    label: [kindLabel(a.kind), fmtBytes(a.size), a.arch_match ? '' : t('update.archForeign')]
      .filter(Boolean)
      .join(' · '),
  })),
)

/** 将 markdown changelog 简单转为纯文本摘要 */
const changelogPreview = computed(() => {
  if (!info.value?.body) return ''
  return info.value.body
    .replace(/^#+\s*/gm, '')
    .replace(/\*\*/g, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .trim()
    .slice(0, 300)
})

function fmtBytes(n?: number): string {
  if (!n || n < 0) return '0'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}

const percent = computed(() => {
  const p = task.value?.percent
  return typeof p === 'number' ? Math.max(0, Math.min(100, p)) : null
})

const progressText = computed(() => {
  const v = task.value
  if (!v) return ''
  return t('update.downloadedSize', {
    done: fmtBytes(v.received),
    total: v.total ? fmtBytes(v.total) : '?',
    speed: fmtBytes(v.speed),
  })
})

const STAGES = ['connecting', 'downloading', 'verifying', 'done', 'failed', 'cancelling', 'cancelled']
const showProgress = computed(() => {
  const s = task.value?.status
  return Boolean(s) && s !== 'idle' && (updateStore.downloading || s === 'done' || s === 'error')
})
const stageKnown = computed(() =>
  task.value?.stage && STAGES.includes(task.value.stage))

const failure = computed(() =>
  (!updateStore.downloading && task.value?.status === 'error') ? (task.value.error ?? '') : '')

const done = computed(() => task.value?.status === 'done')
const verified = computed(() => Boolean(task.value?.verified))
const plan = computed(() => updateStore.plan)
/** 需要提权的产物（deb/rpm/Arch）：主按钮变成「复制安装命令」 */
const manualCommand = computed(() =>
  (plan.value && !plan.value.auto ? plan.value.command || '' : ''))

const verifyText = computed(() => {
  const v = task.value
  if (!v || v.status !== 'done') return ''
  return v.verified
    ? t('update.verifyOk', { sha: (v.sha256_actual || '').slice(0, 12) })
    : t('update.verifyNone')
})

const resumedText = computed(() => {
  const v = task.value
  return v && v.resumed_from ? t('update.resumed', { from: fmtBytes(v.resumed_from) }) : ''
})

const viaText = computed(() => {
  const via = task.value?.via || info.value?.via
  return via ? t('update.via', { via }) : ''
})

// ── 镜像开关：后台参数 github.mirror_* 的影子，改这里等于改设置页那两项 ──
const mirrorDraft = ref(false)
const prefixDraft = ref('')
const mirrorError = ref('')

watch(
  () => [updateStore.mirrorEnabled, updateStore.mirrorPrefix] as const,
  ([on, pre]) => {
    mirrorDraft.value = on
    prefixDraft.value = pre
  },
  { immediate: true },
)

async function onToggleMirror(on: boolean) {
  mirrorDraft.value = on
  // 只提交开关：前缀此时可能还没从参数表读回来，一起提交会清掉用户配好的地址
  mirrorError.value = await updateStore.saveMirrorSettings(on, null)
}

async function onSavePrefix() {
  const prefix = prefixDraft.value.trim()
  if (!prefix) {
    mirrorError.value = t('update.mirrorPrefixEmpty')
    return
  }
  mirrorError.value = await updateStore.saveMirrorSettings(mirrorDraft.value, prefix)
}

// 弹窗每次打开都与后端对齐一次：下载在 store 里跑，界面重开不能停在旧状态
watch(isVisible, (open) => {
  if (!open) return
  void updateStore.resumeDownload()
  void updateStore.loadMirrorSettings()
  // 启动自检那条事件带的载荷可能没有产物清单（旧壳层、或检查时镜像回退失败），
  // 那就补取一次；不补的话下载区只有一行产物名可选的都没有
  if (!assets.value.length && info.value?.latest) void updateStore.loadAssets(info.value.latest)
})

function onIgnore() {
  updateStore.ignoreVersion()
}

function onClose() {
  updateStore.closeModal()
}

const copied = ref(false)
async function copyCommand() {
  const cmd = manualCommand.value
  if (!cmd) return
  try {
    await writeClipboardText(cmd)
  } catch {
    try {
      await navigator.clipboard.writeText(cmd)
    } catch {
      mirrorError.value = t('update.copyFailed')
      return
    }
  }
  copied.value = true
  setTimeout(() => { copied.value = false }, 1500)
}

const applying = computed(() => updateStore.applying)

async function onPrimary() {
  if (updateStore.downloading) {
    await updateStore.cancelDownload()
    return
  }
  if (done.value) {
    if (manualCommand.value) {
      await copyCommand()
      return
    }
    const res = await updateStore.applyUpdate()
    // manual 分支：壳层没代跑（要提权），把命令放上剪贴板
    if (res.manual) await copyCommand()
    return
  }
  await updateStore.startDownload(selected.value?.name || '')
}

const primaryLabel = computed(() => {
  if (applying.value) return t('update.installing')
  if (updateStore.downloading) return t('update.cancel')
  if (done.value) return manualCommand.value ? t('update.copyCommand') : t('update.installNow')
  return t('update.downloadAndInstall', { size: fmtBytes(selected.value?.size) })
})

const primaryDisabled = computed(() => {
  if (applying.value) return true
  if (updateStore.downloading) return task.value?.stage === 'cancelling'
  if (done.value) return !plan.value
  return !selected.value || Boolean(info.value?.no_asset_for_platform)
})

const canPickAsset = computed(() =>
  assetOptions.value.length > 1 && !updateStore.downloading && !done.value)
</script>

<template>
  <Modal :visible="isVisible" size="wide" @close="onClose">
    <div class="upd">
      <div class="upd-version">
        <span class="upd-current">{{ info?.current }}</span>
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M5 12h14M12 5l7 7-7 7" />
        </svg>
        <span class="upd-latest">{{ info?.latest }}</span>
        <span v-if="viaText" class="upd-via">{{ viaText }}</span>
      </div>

      <div v-if="changelogPreview" class="upd-changelog">
        <div class="upd-changelog-label">{{ t('update.changelog') }}</div>
        <div class="upd-changelog-text">{{ changelogPreview }}</div>
      </div>

      <!-- 这版没有本平台产物：直说，并只留网页出口，不给一个必然失败的下载按钮 -->
      <p v-if="info?.no_asset_for_platform" class="upd-warn">
        {{ t('update.noAsset', { os: info.no_asset_for_platform, ver: info.latest }) }}
      </p>

      <div v-else class="upd-block">
        <div class="upd-asset">
          <span class="upd-asset-label">{{ t('update.asset') }}</span>
          <CustomSelect
            v-if="assetOptions.length > 1"
            v-model="pickedName"
            :options="assetOptions"
            :aria-label="t('update.asset')"
          />
          <span v-else class="upd-asset-name" :title="selected?.name">{{ selected?.name }}</span>
          <span v-if="updateStore.assetsLoading" class="upd-via">{{ t('update.checking') }}</span>
        </div>
        <p v-if="selected && !selected.arch_match" class="upd-hint">
          {{ t('update.archForeignHint', { arch: info?.platform?.arch || '?', pkg: selected.arch }) }}
        </p>

        <div class="upd-mirror">
          <label class="upd-mirror-row">
            <input
              type="checkbox"
              :checked="mirrorDraft"
              :disabled="updateStore.downloading || updateStore.mirrorSaving"
              @change="onToggleMirror(($event.target as HTMLInputElement).checked)"
            />
            <span>{{ t('update.mirrorToggle') }}</span>
          </label>
          <div v-if="mirrorDraft" class="upd-mirror-prefix">
            <input
              v-model="prefixDraft"
              class="upd-input"
              :placeholder="t('update.mirrorPrefixPlaceholder')"
              :disabled="updateStore.downloading || updateStore.mirrorSaving"
              @keyup.enter="onSavePrefix"
            />
            <button class="upd-btn" :disabled="!prefixDraft.trim()" @click="onSavePrefix">
              {{ t('update.save') }}
            </button>
          </div>
          <p class="upd-hint">{{ t('update.mirrorHint') }}</p>
          <p v-if="mirrorError" class="upd-hint upd-hint--bad">{{ mirrorError }}</p>
        </div>

        <div v-if="showProgress" class="upd-progress">
          <div class="upd-stage">
            <span v-if="stageKnown">{{ t(`update.stage.${task?.stage}`) }}</span>
            <span v-else>{{ task?.stage }}</span>
            <span v-if="resumedText" class="upd-resumed">{{ resumedText }}</span>
          </div>
          <div class="upd-bar">
            <i v-if="percent !== null" :style="{ width: percent + '%' }"></i>
            <i v-else class="upd-bar--unknown"></i>
          </div>
          <div class="upd-size">{{ progressText }}</div>
          <div
            v-if="done"
            class="upd-verify"
            :class="verified ? 'upd-verify--ok' : 'upd-verify--warn'"
          >{{ verifyText }}</div>
        </div>

        <div v-if="failure" class="upd-error">
          <span class="upd-error-text">{{ failure }}</span>
        </div>

        <p v-if="manualCommand" class="upd-cmd-wrap">
          <span class="upd-hint">{{ t('update.manualHint') }}</span>
          <code class="upd-cmd" @click="copyCommand">
            {{ copied ? t('update.copied') : manualCommand }}
          </code>
        </p>
        <p v-if="applying" class="upd-applying">{{ t('update.installHint') }}</p>
        <p v-if="updateStore.error && !failure" class="upd-hint upd-hint--bad">
          {{ updateStore.error }}
        </p>
      </div>
    </div>

    <template #actions>
      <button class="btn-cancel" @click="onIgnore">{{ t('update.ignoreThisVersion') }}</button>
      <button class="btn-cancel" :disabled="updateStore.downloading" @click="updateStore.revealDownload()">
        {{ t('update.openDir') }}
      </button>
      <button class="btn-cancel" @click="updateStore.openReleasePage()">
        {{ t('update.releasePage') }}
      </button>
      <button class="btn-confirm" :disabled="primaryDisabled" @click="onPrimary">
        {{ primaryLabel }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.upd {
  text-align: center;
}

.upd-version {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-sm);
  margin-bottom: var(--sp-md);
}

.upd-current {
  font-size: 14px;
  color: var(--text-secondary);
  padding: 4px 10px;
  background: var(--bg-secondary);
  border-radius: var(--r-sm);
}

.upd-latest {
  font-size: 14px;
  font-weight: 600;
  color: var(--primary);
  padding: 4px 10px;
  background: var(--primary-light);
  border-radius: var(--r-sm);
}

.upd-via {
  font-size: 11px;
  color: var(--text-tertiary);
}

.upd-version svg {
  color: var(--text-tertiary);
  flex-shrink: 0;
}

.upd-changelog {
  text-align: left;
  margin-bottom: var(--sp-sm);
}

.upd-changelog-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: var(--sp-xs);
}

.upd-changelog-text {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
  max-height: 110px;
  overflow-y: auto;
  padding: var(--sp-sm);
  background: var(--bg-secondary);
  border-radius: var(--r-sm);
  white-space: pre-wrap;
}

.upd-block {
  text-align: left;
  display: flex;
  flex-direction: column;
  gap: var(--sp-sm);
}

.upd-asset {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
}

.upd-asset-label {
  font-size: 12px;
  color: var(--text-tertiary);
  flex-shrink: 0;
}

.upd-asset-name {
  font-size: 12px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.upd-mirror {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: var(--sp-sm);
  background: var(--bg-secondary);
  border-radius: var(--r-sm);
}

.upd-mirror-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-primary);
  cursor: pointer;
}

.upd-mirror-prefix {
  display: flex;
  gap: 6px;
}

.upd-input {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  padding: 5px 8px;
  border: 1px solid var(--border-color);
  border-radius: var(--r-sm);
  background: var(--bg-primary);
  color: var(--text-primary);
}

.upd-btn {
  font-size: 12px;
  padding: 5px 10px;
  border-radius: var(--r-sm);
  border: 1px solid var(--border-color);
  background: var(--bg-primary);
  color: var(--text-primary);
  cursor: pointer;
}

.upd-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.upd-hint {
  font-size: 11px;
  color: var(--text-tertiary);
  line-height: 1.5;
}

.upd-hint--bad {
  color: var(--danger, #d9534f);
}

.upd-warn {
  font-size: 12px;
  color: var(--text-secondary);
  text-align: left;
  padding: var(--sp-sm);
  background: var(--bg-secondary);
  border-radius: var(--r-sm);
}

.upd-progress {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.upd-stage {
  font-size: 12px;
  color: var(--text-secondary);
  display: flex;
  gap: 6px;
}

.upd-resumed {
  color: var(--text-tertiary);
  font-size: 11px;
}

.upd-bar {
  height: 6px;
  border-radius: 3px;
  background: var(--bg-secondary);
  overflow: hidden;
}

.upd-bar i {
  display: block;
  height: 100%;
  background: var(--primary);
  transition: width 0.3s ease;
}

.upd-bar--unknown {
  width: 40%;
  animation: upd-slide 1.2s infinite ease-in-out;
}

@keyframes upd-slide {
  0% { margin-left: -40%; }
  100% { margin-left: 100%; }
}

.upd-size {
  font-size: 11px;
  color: var(--text-tertiary);
}

.upd-verify {
  font-size: 11px;
}

.upd-verify--ok {
  color: var(--success, #2e9e5b);
}

.upd-verify--warn {
  color: var(--warning, #d9822b);
}

.upd-error {
  padding: var(--sp-sm);
  background: rgba(217, 83, 79, 0.08);
  border: 1px solid rgba(217, 83, 79, 0.3);
  border-radius: var(--r-sm);
}

.upd-error-text {
  font-size: 11px;
  color: var(--danger, #d9534f);
  line-height: 1.5;
  word-break: break-all;
  white-space: pre-wrap;
}

.upd-cmd-wrap {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.upd-cmd {
  display: block;
  font-size: 11px;
  padding: 6px 8px;
  background: var(--bg-secondary);
  border-radius: var(--r-sm);
  cursor: pointer;
  word-break: break-all;
  white-space: pre-wrap;
}

.upd-applying {
  font-size: 12px;
  color: var(--primary);
}
</style>
