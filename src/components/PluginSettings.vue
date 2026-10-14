<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  ruleInventory,
  pluginCheckUpdates,
  pluginPull,
  pluginImportFile,
  pluginImportData,
  pluginImportUrl,
  pluginRemove,
  pluginSetRuleEnabled,
  pluginTest,
  type PackIndexEntry,
  type PackUpdateEntry,
  type PluginInfo,
  type RuleInventory,
} from '@/api/commands'

const { t } = useI18n()

const inv = ref<RuleInventory>({ rules_dir: '', script_packs: [], declarative: [] })
const plugins = computed(() => inv.value.script_packs)
const loading = ref(false)
const importing = ref(false)
const actionError = ref('')

async function reload() {
  loading.value = true
  try {
    inv.value = await ruleInventory()
    actionError.value = ''
  } catch (err: any) {
    actionError.value = String(err?.message ?? err)
  } finally {
    loading.value = false
  }
}

onMounted(reload)

// ── 导入：本地文件（Tauri 对话框，浏览器回退到 <input type=file>）──

const fileInput = ref<HTMLInputElement>()

async function onImportFile() {
  try {
    const { open } = await import('@tauri-apps/plugin-dialog')
    const selected = await open({
      multiple: false,
      filters: [{ name: 'Plugin', extensions: ['zip'] }],
    })
    if (selected && typeof selected === 'string') {
      await doImport(() => pluginImportFile(selected))
    }
  } catch {
    fileInput.value?.click()
  }
}

function fileToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf)
  let bin = ''
  const chunk = 0x8000
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode(...bytes.subarray(i, i + chunk))
  }
  return btoa(bin)
}

async function onFileSelected(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  const buf = await file.arrayBuffer()
  await doImport(() => pluginImportData(fileToBase64(buf)))
}

// ── 导入：远程 URL ──

const importUrl = ref('')
const showUrlImport = ref(false)

async function onImportUrl() {
  const url = importUrl.value.trim()
  if (!url) return
  await doImport(() => pluginImportUrl(url))
  importUrl.value = ''
  showUrlImport.value = false
}

async function doImport(fn: () => Promise<PluginInfo[]>) {
  importing.value = true
  try {
    inv.value = { ...inv.value, script_packs: await fn() }
    actionError.value = ''
  } catch (err: any) {
    actionError.value = String(err?.message ?? err)
  } finally {
    importing.value = false
  }
}

// ── 规则开关 / 删除 ──

async function onToggleRule(plugin: string, rule: string, e: Event) {
  const enabled = (e.target as HTMLInputElement).checked
  try {
    inv.value = { ...inv.value, script_packs: await pluginSetRuleEnabled(plugin, rule, enabled) }
  } catch (err: any) {
    actionError.value = String(err?.message ?? err)
    await reload()
  }
}

// ── 检查规则包更新（官方索引 → 可更新 / 可安装两堆）──

const checking = ref(false)
const checked = ref(false)
const packUpdates = ref<PackUpdateEntry[]>([])
const packAvailable = ref<PackIndexEntry[]>([])
const indexUrl = ref('')
const pulling = ref('')
const pulled = ref('')

async function onCheckUpdates() {
  if (checking.value) return
  checking.value = true
  pulled.value = ''
  try {
    const r = await pluginCheckUpdates()
    packUpdates.value = r.updates
    packAvailable.value = r.available
    indexUrl.value = r.index_url
    checked.value = true
    actionError.value = ''
  } catch (err: any) {
    actionError.value = String(err?.message ?? err)
    packUpdates.value = []
    packAvailable.value = []
  } finally {
    checking.value = false
  }
}

async function onPull(entry: PackIndexEntry) {
  if (pulling.value) return
  pulling.value = entry.name
  try {
    inv.value = { ...inv.value, script_packs: await pluginPull(entry.name) }
    packUpdates.value = packUpdates.value.filter((u) => u.name !== entry.name)
    packAvailable.value = packAvailable.value.filter((a) => a.name !== entry.name)
    pulled.value = t('settings.pluginPulled', { name: entry.name, version: entry.version })
    actionError.value = ''
  } catch (err: any) {
    actionError.value = String(err?.message ?? err)
  } finally {
    pulling.value = ''
  }
}

const confirmRemove = ref<Record<string, boolean>>({})

function onRemove(name: string) {
  if (!confirmRemove.value[name]) {
    confirmRemove.value[name] = true
    setTimeout(() => { confirmRemove.value[name] = false }, 4000)
    return
  }
  confirmRemove.value[name] = false
  doImport(async () => pluginRemove(name))
}

// ── 试跑：粘贴一段分享文本，走完整解析管线 ──

const testDraft = ref<Record<string, string>>({})
const testBusy = ref<Record<string, boolean>>({})
const testResult = ref<Record<string, { ok: boolean; text: string }>>({})

async function runTest(p: PluginInfo) {
  const text = (testDraft.value[p.name] || '').trim()
  if (!text || testBusy.value[p.name]) return
  testBusy.value[p.name] = true
  testResult.value[p.name] = { ok: true, text: '' }
  try {
    const r = await pluginTest(p.name, text)
    const lines = [r.title, r.uri].filter(Boolean)
    if (r.via) lines.push(t('settings.pluginTestVia', { via: r.via }))
    testResult.value[p.name] = { ok: true, text: lines.join('\n') }
  } catch (err: any) {
    testResult.value[p.name] = { ok: false, text: String(err?.message ?? err) }
  } finally {
    testBusy.value[p.name] = false
  }
}
</script>

<template>
  <div class="plugins">
    <p class="plugins__hint">{{ t('settings.pluginsHint') }}</p>

    <div v-if="loading" class="plugins__empty">{{ t('settings.pluginsLoading') }}</div>

    <template v-else>
      <h4 class="plugins__section">{{ t('settings.rulesSectionPacks') }}</h4>
      <!-- 插件卡片 -->
      <div v-for="p in plugins" :key="p.name" class="plugins__card">
        <div class="plugins__head">
          <div class="plugins__title">
            <span class="plugins__name">{{ p.name }}</span>
            <span v-if="p.version" class="plugins__ver">v{{ p.version }}</span>
            <span v-if="p.broken" class="plugins__broken">{{ t('settings.pluginBroken') }}</span>
          </div>
          <button
            class="plugins__remove"
            :class="{ 'plugins__remove--confirm': confirmRemove[p.name] }"
            @click="onRemove(p.name)"
          >
            {{ confirmRemove[p.name] ? t('settings.pluginRemoveConfirm') : t('settings.pluginRemove') }}
          </button>
        </div>

        <p v-if="p.author || p.description" class="plugins__desc">
          <span v-if="p.author">{{ p.author }}</span>
          <span v-if="p.author && p.description"> · </span>
          <span>{{ p.description }}</span>
        </p>
        <p v-if="p.error" class="plugins__error">{{ p.error }}</p>

        <!-- 规则列表 -->
        <div v-for="r in p.rules" :key="r.id" class="plugins__rule">
          <div class="plugins__rule-info">
            <span class="plugins__rule-name">{{ r.name }}</span>
            <span class="plugins__rule-meta">
              {{ r.stage }}
              <template v-if="r.hosts.length"> · {{ r.hosts.join(' ') }}</template>
            </span>
          </div>
          <label class="plugins__toggle">
            <input
              type="checkbox"
              :checked="r.enabled"
              @change="onToggleRule(p.name, r.id, $event)"
            />
            <span class="plugins__toggle-slider" />
          </label>
        </div>

        <!-- 试跑 -->
        <div class="plugins__test">
          <input
            class="plugins__input"
            type="text"
            v-model="testDraft[p.name]"
            :placeholder="t('settings.pluginTestPlaceholder')"
            @keydown.enter="runTest(p)"
          />
          <button class="plugins__btn" :disabled="testBusy[p.name]" @click="runTest(p)">
            {{ testBusy[p.name] ? t('settings.pluginTesting') : t('settings.pluginTest') }}
          </button>
        </div>
        <pre v-if="testResult[p.name]?.text" class="plugins__result" :class="{ 'plugins__result--error': !testResult[p.name]?.ok }">{{ testResult[p.name].text }}</pre>
      </div>

      <div v-if="!plugins.length" class="plugins__empty">
        {{ t('settings.pluginsEmpty') }}
      </div>

      <!-- 声明式 JSON 规则：改文件即生效，写坏的那条也要露面 -->
      <h4 class="plugins__section">{{ t('settings.rulesSectionJson') }}</h4>
      <p class="plugins__hint">
        {{ t('settings.rulesJsonHint') }}
        <code v-if="inv.rules_dir" class="plugins__dir">{{ inv.rules_dir }}</code>
      </p>
      <div v-if="inv.declarative.length" class="plugins__card">
        <div v-for="(d, i) in inv.declarative" :key="d.file + i" class="plugins__rule">
          <div class="plugins__rule-info">
            <span class="plugins__rule-name">
              {{ d.name }}
              <span v-if="d.error" class="plugins__broken">{{ t('settings.ruleBroken') }}</span>
              <span v-else-if="!d.enabled" class="plugins__tag">{{ t('settings.ruleDisabled') }}</span>
            </span>
            <span class="plugins__rule-meta">
              {{ d.stage }}
              <template v-if="d.hosts.length"> · {{ d.hosts.join(' ') }}</template>
              · {{ d.file.split('/').pop() }}
            </span>
          </div>
        </div>
      </div>
      <div v-else class="plugins__empty">{{ t('settings.rulesJsonEmpty') }}</div>
    </template>

    <!-- 导入区 -->
    <div class="plugins__import">
      <button class="plugins__btn" :disabled="importing" @click="onImportFile">
        {{ importing ? t('settings.pluginImporting') : t('settings.pluginImportFile') }}
      </button>
      <button class="plugins__btn" @click="showUrlImport = !showUrlImport">
        {{ t('settings.pluginImportUrl') }}
      </button>
      <div v-if="showUrlImport" class="plugins__url">
        <input
          class="plugins__input"
          type="text"
          v-model="importUrl"
          :placeholder="t('settings.pluginUrlPlaceholder')"
          @keydown.enter="onImportUrl"
        />
        <button class="plugins__btn" :disabled="importing || !importUrl.trim()" @click="onImportUrl">
          {{ t('common.confirm') }}
        </button>
      </div>
    </div>

    <div v-if="actionError" class="plugins__error">{{ actionError }}</div>

    <!-- 规则包更新：官方索引 → 可更新 / 可安装 -->
    <h4 class="plugins__section">{{ t('settings.rulesSectionUpdates') }}</h4>
    <p class="plugins__hint">{{ t('settings.rulesUpdatesHint') }}</p>
    <button class="plugins__btn" :disabled="checking" @click="onCheckUpdates">
      {{ checking ? t('settings.rulesChecking') : t('settings.rulesCheckUpdates') }}
    </button>
    <p v-if="indexUrl" class="plugins__hint">
      {{ t('settings.rulesIndexUsed') }} <code class="plugins__dir">{{ indexUrl }}</code>
    </p>
    <p v-if="pulled" class="plugins__pulled">{{ pulled }}</p>

    <div v-if="packUpdates.length" class="plugins__card">
      <div v-for="u in packUpdates" :key="u.name" class="plugins__pack-row">
        <div class="plugins__rule-info">
          <span class="plugins__rule-name">{{ u.name }}</span>
          <span class="plugins__rule-meta">
            {{ t('settings.rulesUpdateFrom', { from: u.current, to: u.version }) }}
            <template v-if="u.description"> · {{ u.description }}</template>
          </span>
        </div>
        <button class="plugins__btn" :disabled="pulling === u.name" @click="onPull(u)">
          {{ pulling === u.name ? t('settings.rulesPulling') : t('settings.rulesUpdate') }}
        </button>
      </div>
    </div>

    <div v-if="packAvailable.length" class="plugins__card">
      <div class="plugins__pack-title">{{ t('settings.rulesAvailableTitle') }}</div>
      <div v-for="a in packAvailable" :key="a.name" class="plugins__pack-row">
        <div class="plugins__rule-info">
          <span class="plugins__rule-name">{{ a.name }}</span>
          <span class="plugins__rule-meta">
            v{{ a.version }}<template v-if="a.description"> · {{ a.description }}</template>
          </span>
        </div>
        <button class="plugins__btn" :disabled="pulling === a.name" @click="onPull(a)">
          {{ pulling === a.name ? t('settings.rulesPulling') : t('settings.rulesInstall') }}
        </button>
      </div>
    </div>

    <div
      v-if="!checking && checked && !packUpdates.length && !packAvailable.length"
      class="plugins__empty"
    >{{ t('settings.rulesNoUpdates') }}</div>

    <input ref="fileInput" type="file" accept=".zip" hidden @change="onFileSelected" />
  </div>
</template>

<style scoped>
.plugins {
  display: flex;
  flex-direction: column;
  gap: var(--sp-md);
}

.plugins__hint {
  font-size: 12px;
  color: var(--text-tertiary);
  line-height: 1.5;
  margin: 0;
}

.plugins__empty {
  font-size: 13px;
  color: var(--text-tertiary);
  text-align: center;
  padding: var(--sp-sm) 0;
}

.plugins__section {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
  margin: var(--sp-sm) 0 0;
}

.plugins__tag {
  font-size: 11px;
  color: var(--text-tertiary);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--r-full);
  padding: 1px 6px;
  flex-shrink: 0;
}

.plugins__pack-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.plugins__pack-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-sm);
  padding: var(--sp-xs) 0;
  border-top: 1px solid var(--border);
}

.plugins__pulled {
  font-size: 12px;
  color: #22C55E;
  margin: 0;
}

.plugins__dir {
  font-size: 11px;
  word-break: break-all;
}

.plugins__card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  padding: var(--sp-md);
  display: flex;
  flex-direction: column;
  gap: var(--sp-sm);
}

.plugins__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-sm);
}

.plugins__title {
  display: flex;
  align-items: baseline;
  gap: var(--sp-sm);
  min-width: 0;
}

.plugins__name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.plugins__ver {
  font-size: 11px;
  color: var(--text-tertiary);
  flex-shrink: 0;
}

.plugins__broken {
  font-size: 11px;
  color: #EF4444;
  background: rgba(239, 68, 68, 0.1);
  border-radius: var(--r-full);
  padding: 1px 6px;
  flex-shrink: 0;
}

.plugins__remove {
  font-size: 12px;
  color: var(--text-tertiary);
  background: none;
  border: none;
  cursor: pointer;
  padding: 2px 4px;
  flex-shrink: 0;
  transition: color var(--transition-fast);
}

.plugins__remove:hover {
  color: #EF4444;
}

.plugins__remove--confirm {
  color: #EF4444;
  font-weight: 600;
}

.plugins__desc {
  font-size: 12px;
  color: var(--text-tertiary);
  margin: 0;
  line-height: 1.5;
}

.plugins__error {
  font-size: 12px;
  color: #EF4444;
  margin: 0;
  line-height: 1.5;
  word-break: break-all;
}

.plugins__rule {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-sm);
  padding: var(--sp-xs) 0;
  border-top: 1px solid var(--border);
}

.plugins__rule-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.plugins__rule-name {
  font-size: 13px;
  color: var(--text-primary);
  font-weight: 500;
}

.plugins__rule-meta {
  font-size: 11px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.plugins__toggle {
  position: relative;
  display: inline-block;
  width: 40px;
  height: 22px;
  cursor: pointer;
  flex-shrink: 0;
}

.plugins__toggle input {
  opacity: 0;
  width: 0;
  height: 0;
}

.plugins__toggle-slider {
  position: absolute;
  inset: 0;
  background: var(--border);
  border-radius: var(--r-full);
  transition: background var(--transition-fast);
}

.plugins__toggle-slider::before {
  content: '';
  position: absolute;
  width: 16px;
  height: 16px;
  left: 3px;
  top: 3px;
  background: #fff;
  border-radius: 50%;
  transition: transform var(--transition-fast);
}

.plugins__toggle input:checked + .plugins__toggle-slider {
  background: var(--primary);
}

.plugins__toggle input:checked + .plugins__toggle-slider::before {
  transform: translateX(18px);
}

.plugins__test {
  display: flex;
  gap: var(--sp-sm);
}

.plugins__input {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  background: var(--bg-secondary);
  color: var(--text-primary);
  outline: none;
}

.plugins__input:focus {
  border-color: var(--primary);
}

.plugins__btn {
  font-size: 13px;
  padding: 6px 12px;
  border-radius: var(--r-sm);
  border: 1px solid var(--border);
  background: var(--bg-card);
  color: var(--text-secondary);
  cursor: pointer;
  flex-shrink: 0;
  transition: all var(--transition-fast);
}

.plugins__btn:hover:not(:disabled) {
  border-color: var(--primary);
  color: var(--primary);
}

.plugins__btn:disabled {
  opacity: 0.5;
  cursor: default;
}

.plugins__result {
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--bg-secondary);
  border-radius: var(--r-sm);
  padding: var(--sp-sm);
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.5;
}

.plugins__result--error {
  color: #EF4444;
}

.plugins__import {
  display: flex;
  gap: var(--sp-sm);
  flex-wrap: wrap;
}

.plugins__url {
  display: flex;
  gap: var(--sp-sm);
  width: 100%;
}
</style>
