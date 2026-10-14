<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import Modal from '@/components/Modal.vue'
import CustomSelect from '@/components/CustomSelect.vue'
import StopWarningBanner from '@/components/StopWarningBanner.vue'
import { useScreenCastStore } from '@/stores/screencast'
import { useDeviceStore } from '@/stores/device'
import { writeClipboardText } from '@/api/commands'

const { t } = useI18n()
const sc = useScreenCastStore()
const deviceStore = useDeviceStore()

// ── 表单状态 ──
const target = ref<'dlna' | 'browser'>('browser')
const sourceType = ref<'screen' | 'window'>('screen')
const screenId = ref('desktop')
const windowId = ref('')
const deviceId = ref('')
const micOn = ref(false)
const micDevice = ref('')
const systemOn = ref(false)
const systemDevice = ref('')
const width = ref(1280)
const height = ref(720)
const fps = ref(15)
const bitrate = ref('2M')
const ffmpegPathInput = ref('')

// ── 打开面板时刷新 ──
function onOpen() {
  sc.openPanel()
  deviceStore.fetchDevices()
  // 默认选第一个在线设备
  if (!deviceId.value && deviceStore.devices.length > 0) {
    const online = deviceStore.devices.find(d => d.status === 'online')
    if (online) deviceId.value = online.id
  }
}

watch(() => sc.showPanel, (v) => { if (v) onOpen() })

// ── 音频设备默认值 ──
watch(() => sc.sources?.audio_inputs, (devices) => {
  if (!devices) return
  if (!micDevice.value && sc.micDevices.length > 0) micDevice.value = sc.micDevices[0].name
  if (!systemDevice.value && sc.systemDevices.length > 0) systemDevice.value = sc.systemDevices[0].name
})

const canStart = computed(() => {
  if (!sc.ffmpegOk || sc.isRunning) return false
  if (sourceType.value === 'window' && !selectedWindow.value) return false
  if (target.value === 'dlna' && !deviceId.value) return false
  return true
})

const deviceOptions = computed(() =>
  deviceStore.devices
    .filter(d => d.status === 'online')
    .map(d => ({ value: d.id, label: d.name }))
)

const screenOptions = computed(() =>
  (sc.sources?.screens ?? []).map(s => ({ value: s.id, label: s.title }))
)

// 窗口列表来自系统枚举：id 是后端认得的标识（Windows 带标题、macOS 带窗口号），
// 界面只负责让用户挑一个，不再让他手打标题
const windowOptions = computed(() =>
  sc.windows.map(w => ({ value: w.id, label: w.title }))
)

const selectedWindow = computed(() =>
  sc.windows.find(w => w.id === windowId.value)
)

// Windows 上「系统声音」是一个环回镜像列表：每块喇叭（含虚拟声卡、HDMI、蓝牙耳机）
// 各有一条，只有对应当前默认输出的那条真能读到声音。列表里不标出来，用户挑中闲置
// 的那块就只能看到「投屏正常但没声音」。
const hasLoopbackMirror = computed(() =>
  sc.systemDevices.some(d => d.loopback)
)
const systemDeviceOptions = computed(() =>
  sc.systemDevices.map(d => ({
    value: d.name,
    label: d.default ? `${d.name}${t('screencast.defaultOutputSuffix')}` : d.name,
  }))
)

watch(() => sc.sources?.windows, (wins) => {
  if (!wins?.length) return
  if (wins.some(w => w.id === windowId.value)) return
  windowId.value = wins[0].id
})

// 多屏时默认投主屏：「整个屏幕」抓的是所有显示器的并集，
// 画面被压扁、尺寸可能大到电视解不动（现场 4000x2571）
watch(() => sc.sources?.screens, (screens) => {
  if (!screens?.length) return
  if (screens.some(s => s.id === screenId.value)) return
  const primary = screens.find(s => s.primary)
  screenId.value = screens.length > 1 && primary ? primary.id : 'desktop'
}, { immediate: true })

// macOS 的麦克风授权是按「负责进程的 bundle」判定的：HanCast.app 有声明、
// 也授权了，它拉起的 sidecar/ffmpeg 才采得到声音；没授权时系统既不报错也不
// 弹窗，ffmpeg 只会采回一串 0——设备照样在列表里，投屏照样流畅，就是没声音。
// ffmpeg 自己不会触发那个弹窗，所以勾选麦克风时先用品客（webview）走一次
// getUserMedia，把弹窗和 TCC 记录落在 HanCast.app 身上，再让 ffmpeg 接手。
const micAccess = ref<'idle' | 'asking' | 'granted' | 'denied'>('idle')
const needMicConsent = computed(
  () => micOn.value && sc.sources?.platform === 'darwin')

async function ensureMicAccess() {
  if (sc.sources?.platform !== 'darwin') return
  if (micAccess.value === 'granted' || micAccess.value === 'asking') return
  micAccess.value = 'asking'
  try {
    // 只申请音频：这条流纯粹是为了触发授权，拿到就立刻放开，真采集交给 ffmpeg
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    stream.getTracks().forEach(tr => tr.stop())
    micAccess.value = 'granted'
  } catch {
    micAccess.value = 'denied'
  }
}

watch(micOn, (on) => { if (on) void ensureMicAccess() })

async function onStart() {
  if (micOn.value) await ensureMicAccess()
  const ok = await sc.start({
    target: target.value,
    ...(target.value === 'dlna' ? { device_id: deviceId.value } : {}),
    source: sourceType.value === 'window'
      ? { type: 'window', id: selectedWindow.value!.id, title: selectedWindow.value!.title }
      : { type: 'screen', id: screenId.value },
    audio: {
      mic: micOn.value,
      mic_device: micOn.value ? micDevice.value : undefined,
      system: systemOn.value,
      system_device: systemOn.value ? systemDevice.value : undefined,
    },
    video: {
      width: width.value,
      height: height.value,
      fps: fps.value,
      bitrate: bitrate.value,
    },
  })
  if (ok) {
    // 成功开始后保持面板打开展示邀请链接，用户可手动关闭
  }
}

async function onStop() {
  await sc.stop()
}

async function onSetFfmpeg() {
  if (!ffmpegPathInput.value) return
  await sc.setFfmpegPath(ffmpegPathInput.value)
  ffmpegPathInput.value = ''
}

async function copyText(text: string) {
  try {
    await writeClipboardText(text)
    return
  } catch {
    /* Rust 侧不可用（浏览器 dev），走 Web API */
  }
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const el = document.createElement('textarea')
    el.value = text
    document.body.appendChild(el)
    el.select()
    document.execCommand('copy')
    document.body.removeChild(el)
  }
}
</script>

<template>
  <Modal
    :visible="sc.showPanel"
    :title="t('screencast.title')"
    size="default"
    @close="sc.closePanel()"
  >
    <!-- FFmpeg 不可用提示 -->
    <div v-if="!sc.ffmpegOk" class="sc-error">
      <div class="sc-error-title">{{ t('screencast.ffmpegMissing') }}</div>
      <div class="sc-error-row">
        <input v-model="ffmpegPathInput" :placeholder="t('screencast.ffmpegPathPlaceholder')" />
        <button class="btn-mini" @click="onSetFfmpeg">{{ t('screencast.setPath') }}</button>
      </div>
    </div>

    <!-- 目标类型 -->
    <div v-if="!sc.isRunning" class="sc-section">
      <div class="sc-label">{{ t('screencast.target') }}</div>
      <div class="sc-targets">
        <button
          class="sc-target"
          :class="{ 'sc-target--active': target === 'browser' }"
          @click="target = 'browser'"
        >
          <span class="sc-target-icon">🌐</span>
          <span>{{ t('screencast.targetBrowser') }}</span>
        </button>
        <button
          class="sc-target"
          :class="{ 'sc-target--active': target === 'dlna' }"
          @click="target = 'dlna'"
        >
          <span class="sc-target-icon">📺</span>
          <span>{{ t('screencast.targetDlna') }}</span>
        </button>
      </div>

      <!-- DLNA 设备选择 -->
      <div v-if="target === 'dlna'" class="sc-field">
        <div class="sc-label">{{ t('screencast.device') }}</div>
        <CustomSelect
          v-if="deviceOptions.length > 0"
          :options="deviceOptions"
          :model-value="deviceId"
          @update:model-value="deviceId = $event as string"
        />
        <div v-else class="sc-empty sc-empty--device">
          <p>{{ t('screencast.noDevice') }}</p>
          <p class="sc-empty-hint">{{ t('screencast.noDeviceHint') }}</p>
          <button class="sc-empty-btn" @click="deviceStore.openAddModal()">
            {{ t('devices.addDevice') }}
          </button>
        </div>
      </div>

      <!-- 捕获源 -->
      <div class="sc-label">{{ t('screencast.source') }}</div>
      <div class="sc-targets">
        <button
          class="sc-target sc-target--small"
          :class="{ 'sc-target--active': sourceType === 'screen' }"
          @click="sourceType = 'screen'"
        >{{ t('screencast.fullScreen') }}</button>
        <button
          class="sc-target sc-target--small"
          :class="{ 'sc-target--active': sourceType === 'window' }"
          @click="sourceType = 'window'"
        >{{ t('screencast.window') }}</button>
      </div>
      <div v-if="sourceType === 'screen' && screenOptions.length > 1" class="sc-field">
        <CustomSelect
          :options="screenOptions"
          :model-value="screenId"
          @update:model-value="screenId = $event as string"
        />
        <div class="sc-empty-hint">{{ t('screencast.screenTip') }}</div>
      </div>
      <div v-if="sourceType === 'window'" class="sc-field">
        <CustomSelect
          v-if="windowOptions.length"
          :options="windowOptions"
          :model-value="windowId"
          @update:model-value="windowId = $event as string"
        />
        <div v-else class="sc-empty">{{ t('screencast.noWindows') }}</div>
        <div class="sc-window-tools">
          <button
            class="sc-empty-btn"
            :disabled="sc.sourcesLoading"
            @click="sc.fetchSources()"
          >{{ t('screencast.windowRefresh') }}</button>
          <span class="sc-empty-hint">{{ t('screencast.windowTip') }}</span>
        </div>
      </div>

      <!-- 音频 -->
      <div class="sc-label">{{ t('screencast.audio') }}</div>
      <div class="sc-audio-row">
        <label class="sc-check">
          <input v-model="micOn" type="checkbox" />
          <span>{{ t('screencast.mic') }}</span>
        </label>
        <label class="sc-check">
          <input v-model="systemOn" type="checkbox" />
          <span>{{ t('screencast.systemSound') }}</span>
        </label>
      </div>
      <div v-if="micOn" class="sc-field">
        <CustomSelect
          v-if="sc.micDevices.length > 0"
          :options="sc.micDevices.map(d => ({ value: d.name, label: d.name }))"
          :model-value="micDevice"
          @update:model-value="micDevice = $event as string"
        />
        <div v-else class="sc-empty">{{ t('screencast.noMic') }}</div>
      </div>
      <!-- macOS 的麦克风权限不给就是恒静音，而且不报错：把状态摆在明面上 -->
      <div v-if="needMicConsent && micAccess !== 'granted'" class="sc-empty">
        <span>{{ micAccess === 'asking' ? t('screencast.micAsking') : t('screencast.micDenied') }}</span>
        <button v-if="micAccess === 'denied'" class="sc-empty-btn" @click="ensureMicAccess">
          {{ t('screencast.micRetry') }}
        </button>
      </div>
      <div v-if="systemOn" class="sc-field">
        <CustomSelect
          v-if="sc.systemDevices.length > 0"
          :options="systemDeviceOptions"
          :model-value="systemDevice"
          @update:model-value="systemDevice = $event as string"
        />
        <div v-else class="sc-empty">{{ t('screencast.noSystemSound') }}</div>
        <!-- Windows 每个扬声器都有一条环回镜像，选到没在出声的那块就是「投出去没声音」，
             所以把「当前出声」标出来，让用户知道自己选的是哪一块 -->
        <div v-if="hasLoopbackMirror" class="sc-empty-hint">{{ t('screencast.loopbackTip') }}</div>
      </div>

      <!-- 视频设置 -->
      <div class="sc-label">{{ t('screencast.video') }}</div>
      <div class="sc-grid">
        <div class="sc-field">
          <div class="sc-label">{{ t('screencast.resolution') }}</div>
          <CustomSelect
            :options="[
              { value: '1920x1080', label: '1920×1080' },
              { value: '1280x720', label: '1280×720' },
              { value: '854x480', label: '854×480' },
            ]"
            :model-value="`${width}x${height}`"
            @update:model-value="(v: string | number) => { const [w, h] = String(v).split('x'); width = Number(w); height = Number(h) }"
          />
        </div>
        <div class="sc-field">
          <div class="sc-label">FPS</div>
          <CustomSelect
            :options="[
              { value: '30', label: '30' },
              { value: '15', label: '15' },
              { value: '10', label: '10' },
            ]"
            :model-value="String(fps)"
            @update:model-value="(v: string | number) => { fps = Number(v) }"
          />
        </div>
        <div class="sc-field">
          <div class="sc-label">{{ t('screencast.bitrate') }}</div>
          <CustomSelect
            :options="[
              { value: '5M', label: '5 Mbps' },
              { value: '2M', label: '2 Mbps' },
              { value: '1M', label: '1 Mbps' },
            ]"
            :model-value="bitrate"
            @update:model-value="(v: string | number) => { bitrate = String(v) }"
          />
        </div>
      </div>
    </div>

    <!-- 运行中状态 -->
    <div v-if="sc.isRunning" class="sc-running">
      <div class="sc-running-badge">
        <span class="sc-live-dot"></span>
        {{ t('screencast.running') }}
        <span v-if="sc.state.target === 'browser'"> · {{ sc.state.viewers ?? 0 }} {{ t('screencast.viewers') }}</span>
      </div>

      <template v-if="sc.state.target === 'browser' && sc.state.watch_url">
        <div class="sc-label">{{ t('screencast.inviteLink') }}</div>
        <div class="sc-link-row">
          <input class="sc-input" readonly :value="sc.state.watch_url" />
          <button class="btn-mini" @click="copyText(sc.state.watch_url!)">{{ t('screencast.copy') }}</button>
        </div>
        <div class="sc-tip">{{ t('screencast.inviteTip') }}</div>
      </template>

      <template v-if="sc.state.target === 'dlna' && sc.state.device_id">
        <div class="sc-tip">{{ t('screencast.dlnaTip') }}</div>
      </template>
    </div>

    <!-- 失败原因：停止态也要看得到，否则用户不知道为什么没画面 -->
    <div v-if="sc.lastError" class="sc-error">
      <div class="sc-error-title">{{ sc.lastError }}</div>
    </div>

    <!-- 电视没确认收到停止指令 -->
    <StopWarningBanner :warning="sc.stopWarning" @close="sc.dismissStopWarning()" />

    <template #actions>
      <button v-if="!sc.isRunning" class="btn-cancel" @click="sc.closePanel()">
        {{ t('common.cancel') }}
      </button>
      <button
        v-if="!sc.isRunning"
        class="btn-confirm"
        :disabled="!canStart || sc.starting"
        @click="onStart"
      >
        {{ sc.starting ? t('screencast.starting') : t('screencast.start') }}
      </button>
      <button
        v-if="sc.isRunning"
        class="btn-danger"
        :disabled="sc.stopping"
        @click="onStop"
      >
        {{ sc.stopping ? t('screencast.stopping') : t('screencast.stop') }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.sc-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
  text-align: left;
  max-height: 56vh;
  overflow-y: auto;
  padding-right: 4px;
}

.sc-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-top: 2px;
}

.sc-targets {
  display: flex;
  gap: 8px;
}

.sc-target {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 10px 8px;
  font-size: 13px;
  color: var(--text-secondary);
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  font-family: var(--font);
}

.sc-target--small {
  flex: 0 0 auto;
  padding: 6px 14px;
  font-size: 12px;
}

.sc-target--active {
  color: white;
  background: var(--primary);
  border-color: var(--primary);
}

.sc-target-icon {
  font-size: 15px;
}

.sc-input {
  width: 100%;
  padding: 8px 12px;
  font-size: 13px;
  color: var(--text-primary);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  outline: none;
  font-family: var(--font);
}

.sc-input:focus {
  border-color: var(--border-focus);
}

.sc-grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 8px;
}

.sc-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.sc-audio-row {
  display: flex;
  gap: 16px;
}

.sc-check {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text-primary);
  cursor: pointer;
}

.sc-empty {
  font-size: 12px;
  color: var(--text-secondary);
  padding: 6px 0;
}

.sc-empty--device {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 0;
}

.sc-window-tools {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  margin-top: 6px;
}

.sc-empty-hint {
  font-size: 11px;
  line-height: 1.6;
  color: var(--text-tertiary);
}

.sc-empty-btn {
  align-self: flex-start;
  padding: 5px 12px;
  font-size: 12px;
  color: var(--text-primary);
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  font-family: var(--font);
}

.sc-empty-btn:hover {
  border-color: var(--border-focus);
  color: var(--primary);
}

.sc-error {
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.25);
  border-radius: var(--r-md);
  padding: 10px 12px;
  margin-bottom: 10px;
  text-align: left;
}

.sc-error-title {
  font-size: 12px;
  color: #DC2626;
  margin-bottom: 6px;
}

.sc-error-row {
  display: flex;
  gap: 6px;
}

.sc-error-row .sc-input {
  flex: 1;
}

.btn-mini {
  padding: 6px 12px;
  font-size: 12px;
  color: white;
  background: var(--primary);
  border: none;
  border-radius: var(--r-md);
  cursor: pointer;
  font-family: var(--font);
}

.btn-mini:hover {
  background: var(--primary-hover);
}

/* ── 运行中 ── */
.sc-running {
  display: flex;
  flex-direction: column;
  gap: 10px;
  text-align: left;
}

.sc-running-badge {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.sc-live-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #3ecf8e;
  animation: sc-pulse 1.6s ease-in-out infinite;
}

@keyframes sc-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.8); }
}

.sc-link-row {
  display: flex;
  gap: 6px;
}

.sc-link-row .sc-input {
  flex: 1;
  font-size: 12px;
}

.sc-tip {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
}
</style>
