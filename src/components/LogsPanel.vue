<script setup lang="ts">
import { ref, computed, watch, nextTick, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import Modal from '@/components/Modal.vue'
import CustomSelect from '@/components/CustomSelect.vue'
import { useLogsStore } from '@/stores/logs'
import { exportLogs } from '@/api/commands'
import type { LogModule } from '@/types/logs'

const { t, locale } = useI18n()
const logs = useLogsStore()

const autoTail = ref(true)
const exported = ref(false)
const listEl = ref<HTMLElement | null>(null)

const moduleOptions = computed(() => {
  const opts: { value: string; label: string }[] = [{ value: '', label: t('logs.moduleAll') }]
  // 「全部模块」由前端补，后端只返回真实模块
  for (const m of logs.modules) {
    if (!m.id) continue
    opts.push({ value: m.id, label: modLabel(m) })
  }
  return opts
})

const levelOptions = computed(() => [
  { value: '', label: t('logs.levelAll') },
  ...['DEBUG', 'INFO', 'WARNING', 'ERROR'].map((l) => ({ value: l, label: l })),
])

const limitOptions = computed(() =>
  [200, 400, 1000, 3000].map((n) => ({ value: String(n), label: t('logs.limit', { n }) }))
)

const modLabelCache = new Map<string, string>()

/**
 * 后端日志模块名带点，不能直接当 i18n key（点号是 vue-i18n 的路径分隔符，
 * 未收录的模块名会走路径解析并报缺 key），所以按 id 缓存查询结果。
 */
function modLabel(m: LogModule): string {
  const cacheKey = `${locale.value}|${m.id}`
  const cached = modLabelCache.get(cacheKey)
  if (cached !== undefined) return cached
  const key = 'logs.mod.' + m.id.replace(/\./g, '_')
  const translated = t(key)
  const label = translated === key ? m.label : translated
  modLabelCache.set(cacheKey, label)
  return label
}

/** 每行都前缀 hancast. 没有信息量，只留模块段 */
function shortModule(m: string): string {
  return m.startsWith('hancast.') ? m.slice('hancast.'.length) : m
}

function levelClass(level: string): string {  if (level === 'ERROR' || level === 'CRITICAL') return 'lg-row--error'
  if (level === 'WARNING') return 'lg-row--warn'
  if (level === 'DEBUG') return 'lg-row--debug'
  return ''
}

watch(autoTail, (on) => {
  if (on && logs.showPanel) logs.startTail()
  else logs.stopTail()
})

// 过滤条件一改立刻重取，不等 3 秒轮询的下一班
watch([() => logs.module, () => logs.filterLevel, () => logs.limit, () => logs.includeOlder],
  () => logs.refresh())

// 关键字逐字输入：停手 450ms 后再查，避免每个字符打一次后端
let kwTimer: number | undefined
watch(() => logs.keyword, () => {
  window.clearTimeout(kwTimer)
  kwTimer = window.setTimeout(() => logs.refresh(), 450)
})

// 追日志时 newest 在底部，不自动滚的话用户看不到刚发生的行
watch(() => logs.lines, async () => {
  if (!autoTail.value) return
  await nextTick()
  const el = listEl.value
  if (el) el.scrollTop = el.scrollHeight
})

watch(() => logs.showPanel, (open) => {
  if (!open) logs.stopTail()
  else if (autoTail.value) logs.startTail()
})

onUnmounted(() => {
  logs.stopTail()
  window.clearTimeout(kwTimer)
})

async function onExport() {
  try {
    await exportLogs()
    exported.value = true
    setTimeout(() => { exported.value = false }, 2000)
  } catch {
    exported.value = false
  }
}
</script>

<template>
  <Modal
    :visible="logs.showPanel"
    :title="t('logs.title')"
    size="wide"
    @close="logs.close()"
  >
    <div class="lg">
      <p class="lg-hint">{{ t('logs.hint') }}</p>

      <div class="lg-filters">
        <CustomSelect
          class="lg-filter"
          :options="moduleOptions"
          :model-value="logs.module"
          @update:model-value="logs.module = $event as string"
        />
        <CustomSelect
          class="lg-filter"
          :options="levelOptions"
          :model-value="logs.filterLevel"
          @update:model-value="logs.filterLevel = $event as string"
        />
        <CustomSelect
          class="lg-filter lg-filter--narrow"
          :options="limitOptions"
          :model-value="String(logs.limit)"
          @update:model-value="logs.limit = Number($event)"
        />
        <input
          v-model="logs.keyword"
          class="lg-keyword"
          :placeholder="t('logs.keywordPlaceholder')"
          @keyup.enter="logs.refresh()"
        />
      </div>

      <div class="lg-toggles">
        <label class="lg-check">
          <input type="checkbox"
                 :checked="logs.debugOn"
                 @change="logs.toggleDebug(($event.target as HTMLInputElement).checked)" />
          <span>{{ t('logs.debug') }}</span>
        </label>
        <label class="lg-check">
          <input v-model="logs.includeOlder" type="checkbox" />
          <span>{{ t('logs.older') }}</span>
        </label>
        <label class="lg-check">
          <input v-model="autoTail" type="checkbox" />
          <span>{{ t('logs.autoTail') }}</span>
        </label>
        <button class="lg-btn" :disabled="logs.loading" @click="logs.refresh()">
          {{ logs.loading ? t('logs.loading') : t('logs.refresh') }}
        </button>
        <button class="lg-btn" @click="onExport">
          {{ exported ? '✓' : t('logs.export') }}
        </button>
      </div>

      <div v-if="logs.error" class="lg-error">{{ logs.error }}</div>

      <div class="lg-list" ref="listEl">
        <div v-if="!logs.lines.length && !logs.loading" class="lg-empty">
          {{ t('logs.empty') }}
        </div>
        <div
          v-for="(row, i) in logs.lines"
          :key="i"
          class="lg-row"
          :class="levelClass(row.level)"
        >
          <span class="lg-ts">{{ row.ts }}</span>
          <span class="lg-level">{{ row.level }}</span>
          <span class="lg-mod">{{ shortModule(row.module) }}</span>
          <span class="lg-msg">{{ row.msg }}</span>
        </div>
      </div>

      <div class="lg-footer">
        <span>{{ t('logs.showing', { shown: logs.lines.length, total: logs.total }) }}</span>
        <span v-if="logs.truncated" class="lg-muted">{{ t('logs.truncated') }}</span>
        <span class="lg-path" :title="logs.path">{{ logs.path }}</span>
      </div>
    </div>

    <template #actions>
      <button class="btn-cancel" @click="logs.close()">{{ t('common.close') }}</button>
    </template>
  </Modal>
</template>

<style scoped>
.lg {
  display: flex;
  flex-direction: column;
  gap: 10px;
  text-align: left;
}

.lg-hint {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.lg-filters {
  display: grid;
  grid-template-columns: 1.4fr 1fr 0.8fr 1.6fr;
  gap: 8px;
}

.lg-keyword {
  padding: 6px 10px;
  font-size: 12px;
  color: var(--text-primary);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  outline: none;
  font-family: var(--font);
}

.lg-keyword:focus {
  border-color: var(--border-focus);
}

.lg-toggles {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.lg-check {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-primary);
  cursor: pointer;
}

.lg-btn {
  padding: 5px 12px;
  font-size: 12px;
  color: var(--text-primary);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  cursor: pointer;
  font-family: var(--font);
}

.lg-btn:hover {
  background: var(--border);
}

.lg-btn:disabled {
  opacity: 0.6;
  cursor: default;
}

.lg-error {
  font-size: 12px;
  color: #DC2626;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.25);
  border-radius: var(--r-md);
  padding: 6px 10px;
}

.lg-list {
  max-height: 46vh;
  min-height: 160px;
  overflow: auto;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: 8px 10px;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.55;
}

.lg-empty {
  color: var(--text-tertiary);
  font-size: 12px;
}

.lg-row {
  display: grid;
  grid-template-columns: 132px 58px 104px 1fr;
  gap: 8px;
  color: var(--text-primary);
}

.lg-row .lg-msg {
  white-space: pre-wrap;
  word-break: break-all;
}

.lg-ts {
  color: var(--text-tertiary);
}

.lg-level {
  color: var(--text-secondary);
}

.lg-mod {
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.lg-row--debug {
  color: var(--text-tertiary);
}

.lg-row--warn .lg-level,
.lg-row--warn .lg-msg {
  color: #B45309;
}

.lg-row--error .lg-level,
.lg-row--error .lg-msg {
  color: #DC2626;
}

.lg-footer {
  display: flex;
  gap: 10px;
  align-items: center;
  font-size: 11px;
  color: var(--text-secondary);
}

.lg-muted {
  color: var(--text-tertiary);
}

.lg-path {
  margin-left: auto;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-tertiary);
}
</style>
