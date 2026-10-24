<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useSettingsStore } from '@/stores/settings'
import { useThemeStore, type ThemeMode } from '@/stores/theme'
import { useGuardStore } from '@/stores/guard'
import { useUpdateStore } from '@/stores/update'
import { useLogsStore } from '@/stores/logs'
import GuardDeviceList from '@/components/GuardDeviceList.vue'
import CustomSelect from '@/components/CustomSelect.vue'
import PluginSettings from '@/components/PluginSettings.vue'
import { exportLogs, getParams, listNetworkInterfaces, saveParams as apiSaveParams } from '@/api/commands'
import type { NetworkInterface, ParamSpec } from '@/types/settings'

const router = useRouter()
const { t, locale } = useI18n()
const settingsStore = useSettingsStore()
const themeStore = useThemeStore()
const guardStore = useGuardStore()
const updateStore = useUpdateStore()
const logsStore = useLogsStore()

const friendlyName = ref('')
const showGuardDevices = ref(false)

const networkInterface = ref('')
const interfaces = ref<NetworkInterface[]>([])

const interfaceOptions = computed(() => [
  { label: t('settings.interfaceAuto'), value: '' },
  ...interfaces.value.map((i) => ({
    label: `${i.name} · ${i.ip}${i.is_default ? ` · ${t('settings.interfaceDefault')}` : ''}`,
    value: i.ip,
  })),
])

onMounted(async () => {
  await settingsStore.fetchSettings()
  friendlyName.value = settingsStore.settings.friendly_name
  networkInterface.value = settingsStore.settings.network_interface || ''
  try {
    const info = await listNetworkInterfaces()
    interfaces.value = info.interfaces
    if (info.selected) networkInterface.value = info.selected
  } catch {
    // 接口枚举失败时仅保留"自动"选项
  }
})

function goBack() {
  router.push('/')
}

function onLanguageChange(lang: string) {
  locale.value = lang
}

function onThemeChange(mode: ThemeMode) {
  themeStore.apply(mode)
}

async function saveFriendlyName() {
  await settingsStore.saveSettings({ friendly_name: friendlyName.value })
}

async function onNetworkInterfaceChange(value: string | number) {
  const ip = String(value)
  if (ip === networkInterface.value) return
  networkInterface.value = ip
  await settingsStore.saveSettings({ network_interface: ip })
}

function onGuardEnabledChange(e: Event) {
  const enabled = (e.target as HTMLInputElement).checked
  guardStore.saveSettings({ enabled })
}

function onGuardTimeoutChange(timeout: number | string) {
  guardStore.saveSettings({ confirm_timeout: Number(timeout) })
}

function toggleGuardDevices() {
  showGuardDevices.value = !showGuardDevices.value
  if (showGuardDevices.value) {
    guardStore.fetchDevices()
  }
}

// ── 后台可调参数（改参数不用重新编译）──

const showParams = ref(false)
const params = ref<ParamSpec[]>([])
const paramDraft = ref<Record<string, number | boolean | string>>({})
const paramErrors = ref<string[]>([])
const paramsLoading = ref(false)
const paramsSaving = ref(false)
const paramsSaved = ref(false)

const isEnglish = computed(() => locale.value !== 'zh-CN')

function paramLabel(spec: ParamSpec) {
  return isEnglish.value ? spec.label_en : spec.label_zh
}

function paramHelp(spec: ParamSpec) {
  return isEnglish.value ? spec.help_en ?? '' : spec.help_zh ?? ''
}

const paramGroups = computed(() => {
  const groups: { section: string; items: ParamSpec[] }[] = []
  for (const spec of params.value) {
    let group = groups.find((g) => g.section === spec.section)
    if (!group) {
      group = { section: spec.section, items: [] }
      groups.push(group)
    }
    group.items.push(spec)
  }
  return groups
})

const paramOverriddenCount = computed(() => params.value.filter((s) => s.overridden).length)

const paramPendingCount = computed(
  () => params.value.filter((s) => paramDraft.value[s.key] !== s.value).length,
)

function paramOptions(spec: ParamSpec) {
  return (spec.options ?? []).map((o) => ({ label: o, value: o }))
}

function draftOf(key: string) {
  return paramDraft.value[key]
}

function enumDraft(spec: ParamSpec) {
  const value = paramDraft.value[spec.key]
  return value === undefined ? String(spec.default) : String(value)
}

function onParamNumberChange(key: string, e: Event) {
  const raw = (e.target as HTMLInputElement).value
  paramDraft.value[key] = raw === '' ? '' : Number(raw)
}

function onParamTextChange(key: string, e: Event) {
  paramDraft.value[key] = (e.target as HTMLInputElement).value.trim()
}

async function loadParams() {
  paramsLoading.value = true
  try {
    params.value = await getParams()
    paramDraft.value = Object.fromEntries(params.value.map((s) => [s.key, s.value]))
    paramErrors.value = []
  } catch {
    paramErrors.value = [t('settings.paramsLoadFailed')]
  } finally {
    paramsLoading.value = false
  }
}

function toggleParams() {
  showParams.value = !showParams.value
  if (showParams.value && !params.value.length) {
    loadParams()
  }
}

async function submitParams(values: Record<string, number | boolean | string>) {
  if (!Object.keys(values).length) return
  paramsSaving.value = true
  try {
    const res = await apiSaveParams(values)
    params.value = res.specs ?? params.value
    paramDraft.value = Object.fromEntries(params.value.map((s) => [s.key, s.value]))
    paramErrors.value = res.errors ?? []
    if (res.ok) {
      paramsSaved.value = true
      setTimeout(() => { paramsSaved.value = false }, 2000)
    }
  } catch {
    paramErrors.value = [t('settings.paramsSaveFailed')]
  } finally {
    paramsSaving.value = false
  }
}

function saveChangedParams() {
  const values: Record<string, number | boolean | string> = {}
  for (const spec of params.value) {
    const next = paramDraft.value[spec.key]
    if (next !== spec.value) values[spec.key] = next
  }
  submitParams(values)
}

function resetParam(spec: ParamSpec) {
  submitParams({ [spec.key]: spec.default })
}

// ── 解析插件 ──

const showPlugins = ref(false)
const pluginCount = ref(0)

function togglePlugins() {
  showPlugins.value = !showPlugins.value
  if (showPlugins.value && !pluginCount.value) {
    pluginListCount()
  }
}

async function pluginListCount() {
  try {
    const { pluginList } = await import('@/api/commands')
    pluginCount.value = (await pluginList()).length
  } catch {
    // 徽标计数失败不影响展开
  }
}

const checkUpdateStatus = ref<'idle' | 'checking' | 'no_update' | 'error'>('idle')
const exportLogsStatus = ref<'idle' | 'done' | 'error'>('idle')

async function onExportLogs() {
  try {
    const ok = await exportLogs()
    if (ok) {
      exportLogsStatus.value = 'done'
      setTimeout(() => { exportLogsStatus.value = 'idle' }, 2000)
    }
  } catch {
    exportLogsStatus.value = 'error'
    setTimeout(() => { exportLogsStatus.value = 'idle' }, 2000)
  }
}

async function onCheckUpdate() {
  checkUpdateStatus.value = 'checking'
  const result = await updateStore.check(settingsStore.settings.version, true)
  if (result?.has_update) {
    checkUpdateStatus.value = 'idle' // modal will show
  } else if (result && !result.has_update) {
    checkUpdateStatus.value = 'no_update'
    setTimeout(() => { checkUpdateStatus.value = 'idle' }, 2000)
  } else {
    checkUpdateStatus.value = 'error'
    setTimeout(() => { checkUpdateStatus.value = 'idle' }, 2000)
  }
}
</script>

<template>
  <div class="settings">
    <header class="settings__header" data-tauri-drag-region>
      <button class="settings__back" :aria-label="t('settings.back')" @click="goBack">
        <svg aria-hidden="true" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M19 12H5M12 19l-7-7 7-7" />
        </svg>
      </button>
      <span class="settings__title" data-tauri-drag-region>{{ t('settings.title') }}</span>
    </header>

    <div class="settings__body">
      <!-- General -->
      <section class="settings__section">
        <h3 class="settings__section-title">{{ t('settings.general') }}</h3>

        <div class="settings__item settings__item--field">
          <span class="settings__label">{{ t('theme.title') }}</span>
          <CustomSelect
            :aria-label="t('theme.title')"
            :model-value="themeStore.mode"
            @update:model-value="onThemeChange($event as ThemeMode)"
            :options="[
              { label: t('theme.system'), value: 'system' },
              { label: t('theme.light'), value: 'light' },
              { label: t('theme.dark'), value: 'dark' },
            ]"
          />
        </div>

        <div class="settings__item settings__item--field">
          <span class="settings__label">{{ t('settings.language') }}</span>
          <CustomSelect
            :aria-label="t('settings.language')"
            :model-value="locale"
            @update:model-value="onLanguageChange($event as string)"
            :options="[
              { label: '简体中文', value: 'zh-CN' },
              { label: 'English', value: 'en-US' },
            ]"
          />
        </div>

        <div class="settings__item">
          <span class="settings__label">{{ t('settings.autoStart') }}</span>
          <label class="settings__toggle">
            <input
              type="checkbox"
              :aria-label="t('settings.autoStart')"
              :checked="settingsStore.autostart"
              @change="settingsStore.toggleAutostart()"
            />
            <span class="settings__toggle-slider" />
          </label>
        </div>
      </section>

      <!-- Cast -->
      <section class="settings__section">
        <h3 class="settings__section-title">{{ t('settings.cast') }}</h3>

        <div class="settings__item settings__item--field">
          <label class="settings__label" for="settings-friendly-name">{{ t('settings.dlnaName') }}</label>
          <input
            id="settings-friendly-name"
            class="settings__input"
            type="text"
            v-model="friendlyName"
            @blur="saveFriendlyName"
          />
        </div>

        <div class="settings__item settings__item--field">
          <span class="settings__label">{{ t('settings.interfaces') }}</span>
          <CustomSelect
            :aria-label="t('settings.interfaces')"
            :model-value="networkInterface"
            @update:model-value="onNetworkInterfaceChange($event as string)"
            :options="interfaceOptions"
          />
        </div>
        <p class="settings__hint">{{ t('settings.interfaceHint') }}</p>

      </section>

      <!-- Cast Security (Device Guard) -->
      <section class="settings__section">
        <h3 class="settings__section-title">{{ t('guard.title') }}</h3>

        <div class="settings__item">
          <span class="settings__label">{{ t('guard.enable') }}</span>
          <label class="settings__toggle">
            <input
              type="checkbox"
              :aria-label="t('guard.enable')"
              :checked="guardStore.settings.enabled"
              @change="onGuardEnabledChange"
            />
            <span class="settings__toggle-slider" />
          </label>
        </div>

        <div class="settings__item settings__item--field">
          <span class="settings__label">{{ t('guard.timeout') }}</span>
          <CustomSelect
            :aria-label="t('guard.timeout')"
            :model-value="guardStore.settings.confirm_timeout"
            @update:model-value="onGuardTimeoutChange"
            :options="[
              { label: t('guard.seconds', { n: 10 }), value: 10 },
              { label: t('guard.seconds', { n: 15 }), value: 15 },
              { label: t('guard.seconds', { n: 30 }), value: 30 },
              { label: t('guard.seconds', { n: 60 }), value: 60 },
            ]"
          />
        </div>

        <button class="settings__item settings__item--clickable" :aria-expanded="showGuardDevices" :aria-controls="showGuardDevices ? 'settings-guard-devices' : undefined" @click="toggleGuardDevices">
          <span class="settings__label">
            {{ t('guard.trusted') }}
            <span v-if="guardStore.trustedDevices.length" class="settings__badge">
              {{ guardStore.trustedDevices.length }}
            </span>
          </span>
          <svg
            class="settings__chevron"
            :class="{ 'settings__chevron--open': showGuardDevices }"
            viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>

        <Transition name="settings-expand">
          <div v-if="showGuardDevices" id="settings-guard-devices" class="settings__guard-list">
            <GuardDeviceList />
          </div>
        </Transition>
      </section>

      <!-- Advanced parameters (no rebuild needed) -->
      <section class="settings__section">
        <h3 class="settings__section-title">{{ t('settings.params') }}</h3>

        <button class="settings__item settings__item--clickable" :aria-expanded="showParams" :aria-controls="showParams ? 'settings-params' : undefined" @click="toggleParams">
          <span class="settings__label">
            {{ t('settings.paramsToggle') }}
            <span v-if="paramOverriddenCount" class="settings__badge">{{ paramOverriddenCount }}</span>
          </span>
          <svg
            class="settings__chevron"
            :class="{ 'settings__chevron--open': showParams }"
            viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>

        <Transition name="settings-expand-lg">
          <div v-if="showParams" id="settings-params" class="settings__params">
            <p class="settings__hint">{{ t('settings.paramsHint') }}</p>

            <div v-if="paramsLoading" class="settings__params-empty">{{ t('settings.paramsLoading') }}</div>

            <template v-else>
              <div v-for="group in paramGroups" :key="group.section" class="settings__params-group">
                <h4 class="settings__params-group-title">
                  {{ t('settings.paramsGroup.' + group.section) }}
                </h4>

                <div v-for="spec in group.items" :key="spec.key" class="settings__param">
                  <div class="settings__param-head" :class="{ 'settings__param-head--text': spec.type === 'text' || spec.type === 'string' }">
                    <span :id="`settings-param-${spec.key}`" class="settings__label">
                      {{ paramLabel(spec) }}
                      <span v-if="spec.overridden" class="settings__badge settings__badge--changed">
                        {{ t('settings.paramsChangedFlag') }}
                      </span>
                    </span>

                    <div class="settings__param-control">
                      <input
                        v-if="spec.type === 'text' || spec.type === 'string'"
                        class="settings__input settings__input--text"
                        type="text"
                        spellcheck="false"
                        :aria-labelledby="`settings-param-${spec.key}`"
                        :value="draftOf(spec.key)"
                        @change="onParamTextChange(spec.key, $event)"
                      />

                      <input
                        v-else-if="spec.type === 'number'"
                        class="settings__input settings__input--small"
                        type="number"
                        :min="spec.min"
                        :max="spec.max"
                        :aria-labelledby="`settings-param-${spec.key}`"
                        :value="draftOf(spec.key)"
                        @change="onParamNumberChange(spec.key, $event)"
                      />
                      <span v-if="spec.type === 'number' && spec.unit" class="settings__param-unit">{{ spec.unit }}</span>

                      <CustomSelect
                        v-if="spec.type === 'enum'"
                        :aria-labelledby="`settings-param-${spec.key}`"
                        :model-value="enumDraft(spec)"
                        :options="paramOptions(spec)"
                        @update:model-value="paramDraft[spec.key] = $event as string"
                      />

                      <label v-if="spec.type === 'bool'" class="settings__toggle">
                        <input
                          type="checkbox"
                          :aria-labelledby="`settings-param-${spec.key}`"
                          :checked="draftOf(spec.key) === true"
                          @change="paramDraft[spec.key] = ($event.target as HTMLInputElement).checked"
                        />
                        <span class="settings__toggle-slider" />
                      </label>

                      <button
                        v-if="paramDraft[spec.key] !== spec.value"
                        class="settings__link settings__param-reset"
                        @click="paramDraft[spec.key] = spec.default"
                      >{{ t('settings.paramsReset') }}</button>
                      <button
                        v-else-if="spec.overridden"
                        class="settings__link settings__param-reset"
                        @click="resetParam(spec)"
                      >{{ t('settings.paramsReset') }}</button>
                    </div>
                  </div>
                  <p v-if="paramHelp(spec)" class="settings__param-help">{{ paramHelp(spec) }}</p>
                </div>
              </div>

              <div v-if="paramErrors.length" class="settings__params-errors">
                <div v-for="err in paramErrors" :key="err" class="settings__params-error">{{ err }}</div>
              </div>

              <div class="settings__params-actions">
                <span class="settings__params-count">
                  <template v-if="paramPendingCount">{{ t('settings.paramsPending', { n: paramPendingCount }) }}</template>
                  <template v-else-if="paramsSaved">✓ {{ t('settings.paramsSaved') }}</template>
                </span>
                <button
                  class="settings__params-save"
                  :disabled="!paramPendingCount || paramsSaving"
                  @click="saveChangedParams"
                >
                  {{ paramsSaving ? t('settings.paramsSaving') : t('settings.paramsSave') }}
                </button>
              </div>
            </template>
          </div>
        </Transition>
      </section>

      <!-- 解析插件（JS 规则包） -->
      <section class="settings__section">
        <h3 class="settings__section-title">{{ t('settings.plugins') }}</h3>

        <button class="settings__item settings__item--clickable" :aria-expanded="showPlugins" :aria-controls="showPlugins ? 'settings-plugins' : undefined" @click="togglePlugins">
          <span class="settings__label">
            {{ t('settings.pluginsToggle') }}
            <span v-if="pluginCount" class="settings__badge">{{ pluginCount }}</span>
          </span>
          <svg
            class="settings__chevron"
            :class="{ 'settings__chevron--open': showPlugins }"
            viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>

        <Transition name="settings-expand-lg">
          <div v-if="showPlugins" id="settings-plugins" class="settings__params">
            <PluginSettings />
          </div>
        </Transition>
      </section>

      <!-- About -->
      <section class="settings__section">
        <h3 class="settings__section-title">{{ t('settings.about') }}</h3>

        <div class="settings__item">
          <span class="settings__label">{{ t('settings.version') }}</span>
          <span class="settings__value">{{ settingsStore.settings.version }}</span>
        </div>

        <div class="settings__item">
          <span class="settings__label">{{ t('settings.viewLogs') }}</span>
          <button class="settings__link" @click="logsStore.open()">{{ t('settings.openPanel') }}</button>
        </div>

        <div class="settings__item">
          <span class="settings__label">{{ t('settings.exportLogs') }}</span>
          <button class="settings__link" :aria-label="t('settings.exportLogs')" @click="onExportLogs">
            <span v-if="exportLogsStatus === 'done'" class="settings__check-result">✓</span>
            <span v-else-if="exportLogsStatus === 'error'" class="settings__check-result settings__check-result--error">✕</span>
            <span v-else>{{ t('common.saveAs') }}</span>
          </button>
        </div>

        <div class="settings__item">
          <template v-if="updateStore.storeVersion">
            <a
              class="settings__link"
              href="https://apps.microsoft.com/detail/9nk1xwpg6hd5?launch=true&mode=mini"
              target="_blank"
            >
              {{ t('update.storeHint') }}
            </a>
          </template>
          <template v-else>
            <button
              class="settings__link"
              :disabled="checkUpdateStatus === 'checking'"
              @click="onCheckUpdate"
            >
              <span v-if="checkUpdateStatus === 'checking'">{{ t('update.checking') }}</span>
              <span v-else-if="checkUpdateStatus === 'no_update'" class="settings__check-result">{{ t('update.latestAlready') }}</span>
              <span v-else-if="checkUpdateStatus === 'error'" class="settings__check-result settings__check-result--error">{{ t('update.checkFailed') }}</span>
              <span v-else>{{ t('settings.checkUpdate') }}</span>
            </button>
          </template>
        </div>

        <div class="settings__item">
          <a class="settings__link" href="https://github.com/pingod/HanCast" target="_blank">
            {{ t('settings.repository') }}
          </a>
        </div>

        <div class="settings__item">
          <a class="settings__link" href="https://qm.qq.com/q/ihWd29FR0A?group=821473246" target="_blank">
            {{ t('settings.qqGroup') }}
          </a>
        </div>

        <div class="settings__item">
          <a class="settings__link" href="https://github.com/pingod/HanCast?tab=GPL-3.0-1-ov-file#" target="_blank">
            {{ t('settings.license') }}
          </a>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.settings {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  height: 100%;
  background: var(--bg-primary);
}

.settings__header {
  height: var(--titlebar-height, 44px);
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  padding: 0 var(--sp-md);
  border-bottom: 1px solid var(--border);
  -webkit-app-region: drag;
  flex-shrink: 0;
}

.settings__back {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  border-radius: var(--r-md);
  color: var(--text-secondary);
  -webkit-app-region: no-drag;
  transition: background var(--transition-fast), color var(--transition-fast);
}

.settings__back:hover {
  background: var(--bg-secondary);
  color: var(--text-primary);
}

.settings__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  -webkit-app-region: drag;
}

.settings__body {
  flex: 1;
  min-height: 0;
  min-width: 0;
  overflow-y: auto;
  padding: var(--sp-md) var(--sp-lg) var(--sp-xl);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--sp-md);
}

.settings__section {
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  width: 100%;
  max-width: 720px;
  min-width: 0;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
}

.settings__section-title {
  margin: 0;
  padding: 10px var(--sp-md);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.settings__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-md);
  min-width: 0;
  min-height: 44px;
  padding: 9px var(--sp-md);
  border-top: 1px solid var(--border);
  text-align: left;
}

.settings__item:last-child {
  border-radius: 0 0 var(--r-lg) var(--r-lg);
}

.settings__item > .settings__label {
  flex: 1;
}

.settings__item--field > .settings__input,
.settings__item--field > .custom-select {
  flex: 0 1 56%;
  width: 56%;
  max-width: 280px;
  min-width: 0;
}

.settings__label {
  min-width: 0;
  font-size: 13px;
  line-height: 1.5;
  color: var(--text-primary);
  overflow-wrap: anywhere;
}

.settings__value {
  min-width: 0;
  font-size: 13px;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.settings__value--mono {
  font-family: 'SF Mono', 'Consolas', monospace;
  font-size: 12px;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* select styles moved to CustomSelect.vue */

.settings__input {
  width: 100%;
  min-width: 0;
  min-height: 32px;
  padding: 5px 9px;
  font-size: 13px;
  line-height: 1.5;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  color: var(--text-primary);
  transition: border-color var(--transition-fast);
}

.settings__input--small {
  width: 88px;
  max-width: 100%;
}

/* 地址类参数独占一行；单行 input 保留原有编辑行为。 */
.settings__input--text {
  flex: 1 1 160px;
  font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.settings__input:focus {
  border-color: var(--border-focus);
  outline: 2px solid var(--border-focus);
  outline-offset: 2px;
}

.settings__hint {
  margin: 0;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.settings__section > .settings__hint {
  padding: 0 var(--sp-md) var(--sp-md);
}

.settings__link {
  min-width: 0;
  max-width: 100%;
  padding: 3px 0;
  font-size: 13px;
  line-height: 1.5;
  text-align: left;
  overflow-wrap: anywhere;
  color: var(--text-link);
  background: none;
  border: none;
  cursor: pointer;
}

.settings__item > .settings__label + .settings__link {
  max-width: 55%;
  text-align: right;
}

.settings__link:hover {
  text-decoration: underline;
}

.settings__link:disabled {
  opacity: 0.6;
  cursor: not-allowed;
  text-decoration: none;
}

.settings__check-result {
  font-size: 12px;
  color: var(--text-tertiary);
}

.settings__check-result--error {
  color: var(--danger);
}

/* ── Toggle switch ── */
.settings__toggle {
  position: relative;
  display: inline-block;
  flex-shrink: 0;
  width: 40px;
  height: 22px;
  cursor: pointer;
}

.settings__toggle input {
  position: absolute;
  inset: 0;
  opacity: 0;
  width: 100%;
  height: 100%;
  cursor: pointer;
}

.settings__toggle input:focus-visible + .settings__toggle-slider {
  outline: 2px solid var(--border-focus);
  outline-offset: 3px;
}

.settings__toggle-slider {
  position: absolute;
  inset: 0;
  background: var(--border);
  border-radius: var(--r-full);
  transition: background var(--transition-fast);
}

.settings__toggle-slider::before {
  content: '';
  position: absolute;
  width: 18px;
  height: 18px;
  left: 2px;
  bottom: 2px;
  background: white;
  border-radius: 50%;
  transition: transform var(--transition-fast);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.15);
}

.settings__toggle input:checked + .settings__toggle-slider {
  background: var(--primary);
}

.settings__toggle input:checked + .settings__toggle-slider::before {
  transform: translateX(18px);
}

/* ── Clickable item ── */
.settings__item--clickable {
  cursor: pointer;
  transition: background var(--transition-fast);
}

.settings__item--clickable:hover {
  background: var(--bg-input);
}

.settings__item--clickable:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: -2px;
}

/* ── Badge ── */
.settings__badge {
  display: inline-block;
  vertical-align: baseline;
  font-size: 11px;
  padding: 1px 6px;
  background: var(--primary-light);
  color: var(--primary);
  border-radius: var(--r-full);
  font-weight: 600;
  margin-left: var(--sp-xs);
}

/* ── Chevron ── */
.settings__chevron {
  color: var(--text-tertiary);
  transition: transform var(--transition-fast);
  flex-shrink: 0;
}

.settings__chevron--open {
  transform: rotate(180deg);
}

/* ── Guard list expand ── */
.settings__guard-list {
  min-width: 0;
  padding: var(--sp-md);
  border-top: 1px solid var(--border);
}

.settings-expand-enter-active,
.settings-expand-leave-active {
  overflow: hidden;
  transition: opacity var(--transition-normal), max-height var(--transition-normal);
  max-height: 500px;
}

.settings-expand-enter-from,
.settings-expand-leave-to {
  opacity: 0;
  max-height: 0;
}

/* ── Advanced parameters ── */
.settings__params {
  display: flex;
  flex-direction: column;
  gap: var(--sp-md);
  min-width: 0;
  padding: var(--sp-md);
  border-top: 1px solid var(--border);
}

.settings-expand-lg-enter-active,
.settings-expand-lg-leave-active {
  overflow: hidden;
  transition: opacity var(--transition-normal), max-height var(--transition-normal);
  max-height: 4000px;
}

.settings-expand-lg-enter-from,
.settings-expand-lg-leave-to {
  opacity: 0;
  max-height: 0;
}

.settings__params-group {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.settings__params-group-title {
  margin: 0;
  padding: 8px 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  overflow-wrap: anywhere;
}

.settings__param {
  min-width: 0;
  padding: 10px 0;
  border-top: 1px solid var(--border);
}

.settings__param-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-sm) var(--sp-md);
}

.settings__param-head > .settings__label {
  flex: 1 1 150px;
}

.settings__param-control {
  display: flex;
  flex: 1 1 160px;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: var(--sp-sm);
  min-width: 0;
  max-width: 100%;
}

.settings__param-control > .custom-select {
  flex: 1 1 100px;
  max-width: 240px;
}

.settings__param-head--text {
  flex-direction: column;
  align-items: stretch;
}

.settings__param-head--text > .settings__label,
.settings__param-head--text > .settings__param-control {
  flex: none;
}

.settings__param-unit {
  font-size: 12px;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.settings__param-reset {
  font-size: 12px;
  white-space: nowrap;
}

.settings__param-help {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
  margin-top: 6px;
  overflow-wrap: anywhere;
}

.settings__params-empty {
  font-size: 13px;
  color: var(--text-secondary);
  padding: var(--sp-sm) 0;
}

.settings__params-errors {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--sp-sm) var(--sp-md);
  background: var(--danger-light);
  border-radius: var(--r-md);
}

.settings__params-error {
  font-size: 12px;
  color: var(--danger);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.settings__params-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-sm);
  padding-top: var(--sp-md);
  border-top: 1px solid var(--border);
}

.settings__params-count {
  flex: 1 1 120px;
  font-size: 12px;
  color: var(--text-secondary);
  overflow-wrap: anywhere;
}

.settings__params-save {
  min-height: 32px;
  padding: 6px 16px;
  font-size: 13px;
  font-weight: 600;
  color: var(--on-primary, #fff);
  background: var(--primary);
  border-radius: var(--r-md);
  transition: background var(--transition-fast), opacity var(--transition-fast);
}

.settings__params-save:hover:not(:disabled) {
  background: var(--primary-hover);
}

.settings__params-save:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.settings__badge--changed {
  background: var(--warning-light, var(--primary-light));
}

/* Expanded plugin content must wrap inside the same narrow card. */
.settings__params :deep(.plugins) {
  min-width: 0;
  overflow-wrap: anywhere;
}

.settings__params :deep(.plugins__pack-row),
.settings__params :deep(.plugins__row),
.settings__params :deep(.plugins__head) {
  flex-wrap: wrap;
}

.settings__params :deep(.plugins__btn) {
  max-width: 100%;
  white-space: normal;
}

@media (max-width: 480px) {
  .settings__param-head {
    flex-direction: column;
    align-items: stretch;
  }

  .settings__param-head > .settings__label,
  .settings__param-head > .settings__param-control {
    flex: none;
  }

  .settings__param-control {
    justify-content: flex-start;
  }

  .settings__param-control > .custom-select {
    max-width: none;
  }
}

@media (max-width: 380px) {
  .settings__body {
    padding-right: var(--sp-md);
    padding-left: var(--sp-md);
  }

  .settings__item--field {
    flex-direction: column;
    align-items: stretch;
    gap: 6px;
  }

  .settings__item--field > .settings__input,
  .settings__item--field > .custom-select {
    flex: none;
    width: 100%;
    max-width: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .settings-expand-enter-active,
  .settings-expand-leave-active,
  .settings-expand-lg-enter-active,
  .settings-expand-lg-leave-active {
    transition: none;
  }
}
</style>
