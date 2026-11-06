<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Modal from '@/components/Modal.vue'
import CustomSelect from '@/components/CustomSelect.vue'
import { useMirrorStore } from '@/stores/mirror'
import { writeClipboardText } from '@/api/commands'

const { t } = useI18n()
const mirror = useMirrorStore()

const wirelessOpen = ref(false)
const wirelessAddress = ref('')
const connecting = ref(false)
const connectNote = ref('')
const copied = ref('')
const savingTool = ref('')
const pathDraft = ref<Record<string, string>>({ adb: '', scrcpy: '' })

const tools = computed(() => mirror.status?.tools ?? [])
const missingTools = computed(() => tools.value.filter(tool => !tool.found).map(tool => tool.key))
const installCommands = computed(() => mirror.status?.install_commands ?? [])
const unauthorized = computed(() => mirror.devices.filter(d => d.state === 'unauthorized'))
const offline = computed(() => mirror.devices.filter(d => d.state === 'offline'))

const deviceOptions = computed(() =>
  mirror.onlineDevices.map(d => ({
    value: d.serial,
    label: d.model ? `${d.model} · ${d.serial}` : d.serial,
  })),
)

const sizeOptions = computed(() => [
  { value: 0, label: t('mirror.native') },
  { value: 1080, label: '1080p' },
  { value: 720, label: '720p' },
  { value: 480, label: '480p' },
])

const fpsOptions = computed(() => [
  { value: 0, label: t('mirror.native') },
  { value: 60, label: '60 fps' },
  { value: 30, label: '30 fps' },
  { value: 15, label: '15 fps' },
])

const bitrateOptions = computed(() =>
  [2, 4, 8, 16, 32].map(n => ({ value: n, label: `${n} Mbps` })),
)

// 依赖齐、有在线设备才能按开始：缺 scrcpy 时按下去只会得到一句报错
const canStart = computed(
  () => mirror.toolsOk && !mirror.isRunning && !!mirror.selectedSerial && !mirror.starting,
)

watch(() => mirror.showPanel, (visible) => {
  if (!visible) {
    wirelessOpen.value = false
    connectNote.value = ''
    copied.value = ''
    return
  }
  void mirror.refreshStatus(false)
  void mirror.refreshDevices()
})

function close() {
  mirror.closePanel()
}

async function copyCommand(cmd: string) {
  try {
    await writeClipboardText(cmd)
    copied.value = cmd
    window.setTimeout(() => {
      if (copied.value === cmd) copied.value = ''
    }, 1600)
  } catch (err) {
    console.error('Failed to copy install command:', err)
  }
}

async function saveToolPath(tool: string) {
  if (savingTool.value) return
  const path = (pathDraft.value[tool] ?? '').trim()
  savingTool.value = tool
  try {
    const result = await mirror.setToolPath(tool as 'adb' | 'scrcpy', path)
    if (result.ok) pathDraft.value[tool] = ''
  } finally {
    savingTool.value = ''
  }
}

async function connectWireless() {
  const address = wirelessAddress.value.trim()
  if (!address || connecting.value) return
  connecting.value = true
  connectNote.value = ''
  try {
    const result = await mirror.connect(address)
    if (result.ok) {
      connectNote.value = t('mirror.connectOk', { address })
      wirelessAddress.value = ''
    }
  } finally {
    connecting.value = false
  }
}
</script>

<template>
  <Modal :visible="mirror.showPanel" :title="t('mirror.title')" size="default" @close="close">
    <div class="mp-section">
      <div class="mp-subtitle">{{ t('mirror.subtitle') }}</div>

      <!-- 运行中：只留状态和「停止」，改配置要等下一次 -->
      <div v-if="mirror.isRunning" class="mp-running">
        <div class="mp-running-badge">
          <span class="mp-live-dot"></span>
          <span>{{ t('mirror.running', { model: mirror.status?.model || mirror.status?.serial || '' }) }}</span>
        </div>
        <div class="mp-chip">{{ t('mirror.externalWindow') }}</div>
        <div class="mp-tip">{{ t('mirror.runningHint') }}</div>
      </div>

      <template v-else>
        <!-- 设备 -->
        <div class="mp-field">
          <div class="mp-row">
            <div class="mp-label">{{ t('mirror.sectionDevice') }}</div>
            <button
              class="mp-link"
              type="button"
              :disabled="mirror.devicesLoading"
              @click="mirror.refreshDevices()"
            >
              {{ t('mirror.refresh') }}
            </button>
          </div>
          <CustomSelect
            v-if="deviceOptions.length"
            v-model="mirror.selectedSerial"
            :options="deviceOptions"
            :aria-label="t('mirror.sectionDevice')"
          />
          <div v-else class="mp-empty">{{ t('mirror.noDevice') }}</div>
          <div v-if="unauthorized.length" class="mp-hint mp-hint--warn">{{ t('mirror.unauthorized') }}</div>
          <div v-else-if="offline.length" class="mp-hint">{{ t('mirror.offline') }}</div>
        </div>

        <!-- 画质 -->
        <div v-if="mirror.toolsOk" class="mp-field">
          <div class="mp-label">{{ t('mirror.sectionQuality') }}</div>
          <div class="mp-grid">
            <div class="mp-field">
              <div class="mp-label">{{ t('mirror.size') }}</div>
              <CustomSelect v-model="mirror.prefs.max_size" :options="sizeOptions" :aria-label="t('mirror.size')" />
            </div>
            <div class="mp-field">
              <div class="mp-label">{{ t('mirror.fps') }}</div>
              <CustomSelect v-model="mirror.prefs.max_fps" :options="fpsOptions" :aria-label="t('mirror.fps')" />
            </div>
            <div class="mp-field">
              <div class="mp-label">{{ t('mirror.bitrate') }}</div>
              <CustomSelect v-model="mirror.prefs.video_bitrate" :options="bitrateOptions" :aria-label="t('mirror.bitrate')" />
            </div>
          </div>

          <!-- 选项 -->
          <div class="mp-label">{{ t('mirror.sectionOptions') }}</div>
          <div class="mp-checks">
            <label class="mp-check" :class="{ 'mp-check--off': !mirror.audioSupported }">
              <input v-model="mirror.prefs.audio" type="checkbox" :disabled="!mirror.audioSupported" />
              <span>{{ t('mirror.audio') }}</span>
            </label>
            <label class="mp-check">
              <input v-model="mirror.prefs.control" type="checkbox" />
              <span>{{ t('mirror.control') }}</span>
            </label>
            <label class="mp-check">
              <input v-model="mirror.prefs.turn_screen_off" type="checkbox" />
              <span>{{ t('mirror.turnScreenOff') }}</span>
            </label>
            <label class="mp-check">
              <input v-model="mirror.prefs.stay_awake" type="checkbox" />
              <span>{{ t('mirror.stayAwake') }}</span>
            </label>
            <label class="mp-check">
              <input v-model="mirror.prefs.always_on_top" type="checkbox" />
              <span>{{ t('mirror.alwaysOnTop') }}</span>
            </label>
          </div>
          <div v-if="!mirror.audioSupported" class="mp-hint">
            {{ t('mirror.audioUnsupported', { version: mirror.status?.scrcpy_audio_min_version || '2.0.0' }) }}
          </div>
        </div>

        <!-- 无线调试 -->
        <div class="mp-field">
          <button class="mp-link mp-link--section" type="button" @click="wirelessOpen = !wirelessOpen">
            {{ t('mirror.sectionWireless') }}
            <span class="mp-caret" :class="{ 'mp-caret--open': wirelessOpen }">▸</span>
          </button>
          <template v-if="wirelessOpen">
            <div class="mp-tip">{{ t('mirror.wirelessHint') }}</div>
            <div class="mp-row">
              <input
                v-model="wirelessAddress"
                class="mp-input"
                :placeholder="t('mirror.wirelessPlaceholder')"
                spellcheck="false"
                @keyup.enter="connectWireless"
              />
              <button
                class="mp-mini"
                type="button"
                :disabled="!wirelessAddress.trim() || connecting"
                @click="connectWireless"
              >
                {{ connecting ? t('mirror.connecting') : t('mirror.connect') }}
              </button>
            </div>
            <div v-if="connectNote" class="mp-hint mp-hint--ok">{{ connectNote }}</div>
            <div class="mp-tip">{{ t('mirror.wirelessPairHint') }}</div>
          </template>
        </div>

        <div class="mp-tip">{{ t('mirror.qualityTip') }}</div>
      </template>

      <!-- 依赖 -->
      <div v-if="!mirror.isRunning" class="mp-field mp-tools">
        <div class="mp-label">{{ t('mirror.sectionTools') }}</div>
        <div v-if="mirror.toolsOk" class="mp-hint mp-hint--ok">{{ t('mirror.toolsHint') }}</div>
        <template v-else>
          <div class="mp-hint mp-hint--warn">{{ t('mirror.toolsMissing', { list: missingTools.join(', ') }) }}</div>
          <div class="mp-label">{{ t('mirror.installTitle') }}</div>
          <div v-for="cmd in installCommands" :key="cmd" class="mp-cmd">
            <code class="mp-cmd-text">{{ cmd }}</code>
            <button class="mp-mini mp-mini--ghost" type="button" @click="copyCommand(cmd)">
              {{ copied === cmd ? t('mirror.copied') : t('mirror.copy') }}
            </button>
          </div>
        </template>
        <div class="mp-label">{{ t('mirror.specifyPath') }}</div>
        <div v-for="tool in tools" :key="tool.key" class="mp-path-row">
          <span class="mp-path-name">{{ tool.key }}</span>
          <input
            v-model="pathDraft[tool.key]"
            class="mp-input"
            :placeholder="tool.path || t('mirror.pathPlaceholder')"
            spellcheck="false"
          />
          <button
            class="mp-mini mp-mini--ghost"
            type="button"
            :disabled="savingTool === tool.key"
            @click="saveToolPath(tool.key)"
          >
            {{ savingTool === tool.key ? t('mirror.saving') : t('mirror.save') }}
          </button>
        </div>
        <div class="mp-tip">{{ t('mirror.clearPath') }}</div>
      </div>

      <div v-if="mirror.lastError" class="mp-error">
        <div class="mp-error-title">{{ mirror.lastError }}</div>
      </div>
    </div>

    <template #actions>
      <button class="btn-cancel" type="button" @click="close">{{ t('mirror.close') }}</button>
      <button
        v-if="mirror.isRunning"
        class="btn-confirm"
        type="button"
        :disabled="mirror.stopping"
        @click="mirror.stop()"
      >
        {{ mirror.stopping ? t('mirror.stopping') : t('mirror.stop') }}
      </button>
      <button v-else class="btn-confirm" type="button" :disabled="!canStart" @click="mirror.start()">
        {{ mirror.starting ? t('mirror.starting') : t('mirror.start') }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.mp-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
  text-align: left;
  max-height: 56vh;
  overflow-y: auto;
  padding-right: 4px;
}

.mp-subtitle {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
}

.mp-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.mp-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-top: 2px;
}

.mp-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.mp-row .mp-input {
  flex: 1;
}

.mp-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.mp-input {
  width: 100%;
  padding: 8px 12px;
  font-size: 13px;
  color: var(--text-primary);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  outline: none;
  font-family: var(--font);
  min-width: 0;
}

.mp-input:focus {
  border-color: var(--border-focus);
}

.mp-checks {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.mp-check {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text-primary);
  cursor: pointer;
}

.mp-check--off {
  color: var(--text-secondary);
  cursor: not-allowed;
}

.mp-hint {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.mp-hint--warn {
  color: var(--warning, #d97706);
}

.mp-hint--ok {
  color: #3ecf8e;
}

.mp-tip {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
}

.mp-empty {
  font-size: 12px;
  color: var(--text-secondary);
  padding: 6px 0;
  line-height: 1.6;
}

.mp-link {
  align-self: flex-start;
  padding: 0;
  font-size: 12px;
  color: var(--primary);
  background: none;
  border: none;
  cursor: pointer;
  font-family: var(--font);
}

.mp-link:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.mp-link--section {
  display: flex;
  align-items: center;
  gap: 4px;
  font-weight: 600;
  color: var(--text-secondary);
}

.mp-caret {
  display: inline-block;
  transition: transform var(--transition-fast);
}

.mp-caret--open {
  transform: rotate(90deg);
}

.mp-mini {
  flex: 0 0 auto;
  padding: 6px 12px;
  font-size: 12px;
  color: var(--on-primary);
  background: var(--primary);
  border: none;
  border-radius: var(--r-md);
  cursor: pointer;
  font-family: var(--font);
}

.mp-mini:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.mp-mini--ghost {
  color: var(--text-secondary);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
}

.mp-cmd {
  display: flex;
  align-items: center;
  gap: 8px;
}

.mp-cmd-text {
  flex: 1;
  padding: 6px 8px;
  font-size: 12px;
  color: var(--text-primary);
  background: var(--bg-secondary);
  border-radius: var(--r-md);
  overflow-wrap: anywhere;
}

.mp-path-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.mp-path-name {
  flex: 0 0 auto;
  width: 48px;
  font-size: 12px;
  color: var(--text-secondary);
}

.mp-path-row .mp-input {
  flex: 1;
}

.mp-error {
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.25);
  border-radius: var(--r-md);
  padding: 10px 12px;
}

.mp-error-title {
  font-size: 12px;
  color: var(--danger);
  overflow-wrap: anywhere;
}

/* ── 运行中 ── */
.mp-running {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.mp-running-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.mp-live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #3ecf8e;
  animation: mp-pulse 1.6s ease-in-out infinite;
}

@keyframes mp-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.8); }
}

.mp-chip {
  align-self: flex-start;
  padding: 2px 8px;
  font-size: 11px;
  color: var(--text-secondary);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 999px;
}
</style>
