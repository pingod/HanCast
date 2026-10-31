<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useCastStore } from '@/stores/cast'
import { useDeviceStore } from '@/stores/device'
import { usePlaylistStore } from '@/stores/playlist'
import { startRemoteControl, writeClipboardText, subtitleApply, subtitleStatus } from '@/api/commands'

const { t } = useI18n()
const castStore = useCastStore()
const deviceStore = useDeviceStore()
const playlist = usePlaylistStore()

// ── Device ──
const currentDevice = computed(() => {
  const id = castStore.castState.device_id
  if (!id) return null
  return deviceStore.devices.find(d => d.id === id) ?? null
})
const deviceName = computed(() => currentDevice.value?.name ?? castStore.castState.device_id ?? '—')

// ── Media type ──
const mediaMimeType = computed(() => {
  let mime = castStore.castState.media?.mime_type ?? ''
  if (!mime) {
    const uri = castStore.castState.media?.uri ?? ''
    const ext = uri.split(/[?#]/)[0].split('.').pop()?.toLowerCase() ?? ''
    const map: Record<string, string> = {
      mp4: 'video/mp4', mkv: 'video/x-matroska', avi: 'video/x-msvideo',
      mov: 'video/quicktime', webm: 'video/webm', flv: 'video/x-flv',
      mp3: 'audio/mpeg', flac: 'audio/flac', wav: 'audio/wav',
      aac: 'audio/aac', ogg: 'audio/ogg', m4a: 'audio/mp4',
      jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
      gif: 'image/gif', webp: 'image/webp', bmp: 'image/bmp',
    }
    mime = map[ext] ?? ''
  }
  return mime
})
// ── 播放选项：播完即停 / 睡眠定时 ──
const nowTick = ref(Date.now())
let sleepTickTimer: number | null = null
onMounted(() => {
  sleepTickTimer = setInterval(() => { nowTick.value = Date.now() }, 1000) as unknown as number
})
onUnmounted(() => {
  if (sleepTickTimer) { clearInterval(sleepTickTimer); sleepTickTimer = null }
  if (subTimer) { clearInterval(subTimer); subTimer = null }
})
function onToggleStopAfter(e: Event) {
  playlist.setStopAfterCurrent((e.target as HTMLInputElement).checked)
}
function onSleepChange(e: Event) {
  castStore.setSleepTimer(Number((e.target as HTMLSelectElement).value))
}
/** 睡眠倒计时文案（没设定时器时为空，整行不渲染） */
const sleepText = computed(() => {
  const deadline = castStore.sleepDeadline
  if (!deadline) return ''
  const s = Math.max(0, Math.round((deadline - nowTick.value) / 1000))
  const mm = Math.floor(s / 60)
  const ss = s % 60
  return t('cast.sleepRemaining', { time: `${mm}:${String(ss).padStart(2, '0')}` })
})

// ── 外挂字幕内封 ──
const subOpen = ref(false)
const subBusy = ref(false)
const subProgress = ref(0)
const subError = ref('')
const subMsg = ref('')
let subTimer: number | null = null
function currentDeviceId(): string {
  return castStore.focusedDeviceId ?? castStore.castState.device_id ?? ''
}
async function openSubtitle() {
  subOpen.value = true
  subError.value = ''
  subMsg.value = ''
  subProgress.value = 0
  await refreshSubStatus()
}
async function pickSubtitle() {
  try {
    const { open } = await import('@tauri-apps/plugin-dialog')
    const f = await open({
      multiple: false,
      filters: [{ name: '字幕文件', extensions: ['srt', 'ass', 'ssa', 'vtt'] }],
    })
    if (typeof f === 'string' && f) await applySubtitle(f)
  } catch (err: any) {
    subError.value = String(err?.message ?? err)
  }
}
async function applySubtitle(path: string) {
  subError.value = ''
  subBusy.value = true
  try {
    const id = currentDeviceId()
    if (!id) throw new Error(t('cast.subtitleFailed'))
    await subtitleApply(id, path)
    startSubPoll()
  } catch (err: any) {
    subBusy.value = false
    subError.value = String(err?.message ?? err) || t('cast.subtitleFailed')
  }
}
function startSubPoll() {
  if (subTimer) clearInterval(subTimer)
  void refreshSubStatus()
  subTimer = window.setInterval(() => { void refreshSubStatus() }, 800)
}
async function refreshSubStatus() {
  const id = currentDeviceId()
  if (!id) return
  try {
    const s = await subtitleStatus(id)
    subProgress.value = s.progress ?? 0
    if (s.status === 'running') {
      subBusy.value = true
      subMsg.value = t('cast.subtitleWorking', { p: Math.round(s.progress || 0) })
    } else {
      subBusy.value = false
      if (subTimer) { clearInterval(subTimer); subTimer = null }
      if (s.status === 'done') subMsg.value = t('cast.subtitleDone')
      else if (s.status === 'error') subError.value = s.message || t('cast.subtitleFailed')
    }
  } catch {
    /* 轮询失败就下次再试 */
  }
}
function closeSubtitle() {
  subOpen.value = false
  if (subTimer) { clearInterval(subTimer); subTimer = null }
}

// ── 手机遥控器 ──
const remoteOpen = ref(false)
const remoteUrl = ref('')
const remoteLoading = ref(false)
const remoteError = ref('')
const remoteCopied = ref(false)
async function openRemote() {
  remoteError.value = ''
  remoteCopied.value = false
  remoteLoading.value = true
  remoteOpen.value = true
  try {
    const r = await startRemoteControl()
    if (r.ok && r.url) remoteUrl.value = r.url
    else remoteError.value = t('cast.remoteFailed')
  } catch (err: any) {
    remoteError.value = String(err?.message ?? err) || t('cast.remoteFailed')
  } finally {
    remoteLoading.value = false
  }
}
async function copyRemoteUrl() {
  if (!remoteUrl.value) return
  try {
    await writeClipboardText(remoteUrl.value)
    remoteCopied.value = true
  } catch {
    /* 复制失败就让用户手动选文本 */
  }
}
function closeRemote() {
  remoteOpen.value = false
}

const isImage = computed(() => mediaMimeType.value.startsWith('image/'))
const isPlayable = computed(() => mediaMimeType.value.startsWith('video/') || mediaMimeType.value.startsWith('audio/'))

// ── Media title ──
const mediaTitle = computed(() => castStore.castState.media?.title ?? '')

// ── Quality ──
// 规则包给了 2 档以上才允许切换；档位不是缓存直链，切换要按档位重新解析
const qualityOptions = computed(() => {
  const qs = castStore.castState.media?.qualities ?? []
  return qs.length >= 2 ? qs : null
})
const selectedQuality = ref('')
watch(() => castStore.castState.media?.quality, (q) => {
  selectedQuality.value = q ?? ''
}, { immediate: true })

async function onQualityChange(e: Event) {
  const qid = (e.target as HTMLSelectElement).value
  selectedQuality.value = qid
  await castStore.switchQuality(qid)
  // 跟随当前会话；失败时也必须触发 value 更新，复位原生 select。
  selectedQuality.value = castStore.castState.media?.quality ?? ''
}

// ── Preview ──
const convertFileSrc = ref<((path: string, protocol?: string) => string) | null>(null)
const previewUrl = computed(() => {
  const info = castStore.castState.media
  if (!info) return null

  // Prefer thumbnail (e.g. Bilibili cover image, already proxied)
  if (info.thumbnail) return info.thumbnail
  // Video/audio streams are not images; never hand their URI to an <img>.
  if (!isImage.value) return null

  // Local file → asset:// URL
  if (info.uri && convertFileSrc.value && !info.uri.startsWith('http')) {
    return convertFileSrc.value(info.uri)
  }

  // Remote URL (direct image links)
  if (info.uri?.startsWith('http')) return info.uri

  return null
})

const previewFailed = ref(false)
watch(previewUrl, () => { previewFailed.value = false })

// ── Playback ──
const isPlaying = computed(() => castStore.castState.status === 'playing')

async function togglePlayPause() {
  if (isPlaying.value) await castStore.pauseCast()
  else await castStore.resumeCast()
}

// ── Volume ──
const volumePercent = computed(() => castStore.castState.is_muted ? 0 : castStore.castState.volume)
const volumeIcon = computed(() => {
  if (castStore.castState.is_muted || castStore.castState.volume === 0) return 'muted'
  if (castStore.castState.volume < 50) return 'low'
  return 'high'
})

function toggleMute() {
  castStore.setMute(!castStore.castState.is_muted)
}

function onVolumeInput(e: Event) {
  const v = Number((e.target as HTMLInputElement).value)
  castStore.setLocalVolume(v)
}

let _debounce: ReturnType<typeof setTimeout> | null = null
function onVolumeChange(e: Event) {
  const v = Number((e.target as HTMLInputElement).value)
  const id = castStore.focusedDeviceId
  if (_debounce) clearTimeout(_debounce)
  _debounce = setTimeout(() => {
    _debounce = null
    if (id) void castStore.setVolume(v, id)
  }, 50)
}

// ── Progress bar ──
const progress = computed(() => {
  const dur = castStore.durationSeconds
  if (dur <= 0) return 0
  return Math.min(100, (castStore.positionSeconds / dur) * 100)
})

// Seek 锁定：用户拖动后锁定进度条，直到后端位置追上
const seekLocked = ref(false)      // 是否锁定中
const seekLockedPct = ref(0)       // 锁定的百分比位置
const seekDragging = ref(false)    // 是否正在拖动
const seekPreview = ref(0)         // 拖动中的预览值

let seekUnlockTimer: ReturnType<typeof setTimeout> | null = null
let seekRequest = 0

function unlockSeek() {
  seekLocked.value = false
  if (seekUnlockTimer) clearTimeout(seekUnlockTimer)
  seekUnlockTimer = null
}

// computed is lazy: unlocking must be an active watcher, not an unused getter.
watch([() => castStore.positionSeconds, () => castStore.durationSeconds, seekLocked, seekLockedPct], ([pos, dur]) => {
  if (!seekLocked.value || dur <= 0) return
  if (Math.abs(pos - (seekLockedPct.value / 100) * dur) < 3) unlockSeek()
})

function resetInteractions() {
  ++seekRequest
  seekDragging.value = false
  unlockSeek()
  if (_debounce) clearTimeout(_debounce)
  _debounce = null
}

watch(() => castStore.focusedDeviceId, resetInteractions)
watch(() => castStore.showController, (visible) => {
  if (!visible) resetInteractions()
})
onUnmounted(resetInteractions)

const displayProgress = computed(() => {
  if (seekDragging.value) return seekPreview.value
  if (seekLocked.value) return seekLockedPct.value
  return progress.value
})

function formatTime(seconds: number): string {
  if (seconds <= 0) return '00:00'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  const mm = String(m).padStart(2, '0')
  const ss = String(s).padStart(2, '0')
  if (h > 0) return `${h}:${mm}:${ss}`
  return `${mm}:${ss}`
}

const displayPosition = computed(() => {
  if (seekDragging.value) {
    return formatTime((seekPreview.value / 100) * castStore.durationSeconds)
  }
  if (seekLocked.value) {
    return formatTime((seekLockedPct.value / 100) * castStore.durationSeconds)
  }
  return formatTime(castStore.positionSeconds)
})
const displayDuration = computed(() => formatTime(castStore.durationSeconds))
const canSeek = computed(() => castStore.isCasting)

function onSeekInput(e: Event) {
  seekDragging.value = true
  seekPreview.value = Number((e.target as HTMLInputElement).value)
}

async function onSeekChange(e: Event) {
  const val = Number((e.target as HTMLInputElement).value)
  const request = ++seekRequest
  seekDragging.value = false
  unlockSeek()
  if (castStore.durationSeconds > 0) {
    // 锁定到目标，位置追上、请求结束或 5 秒兜底均会解锁。
    seekLocked.value = true
    seekLockedPct.value = val
    seekUnlockTimer = setTimeout(unlockSeek, 5000)

    const targetSeconds = (val / 100) * castStore.durationSeconds
    const h = Math.floor(targetSeconds / 3600)
    const m = Math.floor((targetSeconds % 3600) / 60)
    const s = Math.floor(targetSeconds % 60)
    const hh = String(h).padStart(2, '0')
    const mm = String(m).padStart(2, '0')
    const ss = String(s).padStart(2, '0')
    await castStore.seek(`${hh}:${mm}:${ss}`)
    // An older seek finishing must not release the next drag's lock.
    if (request === seekRequest) unlockSeek()
  }
}

onMounted(async () => {
  if (window.__TAURI_INTERNALS__) {
    try { convertFileSrc.value = (await import('@tauri-apps/api/core')).convertFileSrc } catch {}
  }
})
</script>

<template>
  <Teleport to="body">
    <Transition name="ctrl-fade">
      <div v-if="castStore.showController" class="ctrl-overlay" @click.self="castStore.minimizeController()">
        <div class="ctrl-modal">

          <!-- Header -->
          <div class="ctrl__head">
            <div class="ctrl__title">{{ deviceName }}</div>
            <button class="ctrl__min" @click="castStore.minimizeController()" :title="t('controller.minimize')">
              <svg viewBox="0 0 12 12" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5"><line x1="2" y1="6" x2="10" y2="6"/></svg>
            </button>
          </div>

          <div v-if="castStore.lastError !== null" class="ctrl__error" role="alert">
            <strong>{{ t('cast.error') }}</strong>
            <span>{{ castStore.lastError }}</span>
            <button type="button" @click="castStore.dismissError()">{{ t('common.confirm') }}</button>
          </div>

          <!-- Image mode: preview + stop -->
          <template v-if="isImage">
            <!-- Image preview -->
            <div class="ctrl__thumb">
              <img v-if="previewUrl && !previewFailed" :src="previewUrl" class="ctrl__thumb-img" :alt="mediaTitle" @error="previewFailed = true" />
              <div v-else class="ctrl__thumb-ph">
                <svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.5" opacity=".2">
                  <rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>
                </svg>
              </div>
            </div>
            <div class="ctrl__name">{{ mediaTitle }}</div>
            <button class="ctrl__stop" @click="castStore.stopCast(castStore.focusedDeviceId ?? undefined)" :disabled="castStore.loading">
              {{ t('cast.stop') }}
            </button>
          </template>

          <!-- Video/audio mode: full controls -->
          <template v-else>
            <!-- Thumbnail + play/pause overlay -->
            <button type="button" class="ctrl__thumb" @click="togglePlayPause"
              :aria-label="isPlaying ? t('cast.pause') : t('cast.play')">
              <img v-if="previewUrl && !previewFailed" :src="previewUrl" class="ctrl__thumb-img" :alt="mediaTitle" @error="previewFailed = true" />
              <div v-else class="ctrl__thumb-ph">
                <svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.5" opacity=".2">
                  <rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>
                </svg>
              </div>
              <!-- Play/Pause overlay button -->
              <div class="ctrl__thumb-overlay" :class="{ 'ctrl__thumb-overlay--paused': !isPlaying }">
                <div class="ctrl__thumb-play">
                  <svg v-if="isPlaying" viewBox="0 0 24 24" width="28" height="28" fill="currentColor">
                    <rect x="6" y="4" width="4" height="16" rx="1"/>
                    <rect x="14" y="4" width="4" height="16" rx="1"/>
                  </svg>
                  <svg v-else viewBox="0 0 24 24" width="28" height="28" fill="currentColor">
                    <polygon points="6 3 20 12 6 21 6 3"/>
                  </svg>
                </div>
              </div>
            </button>

            <!-- Filename -->
            <div class="ctrl__name">{{ mediaTitle }}</div>

            <!-- 播放队列：上一集 / 位置 / 下一集（仅队列投屏时出现） -->
            <div v-if="playlist.hasQueue" class="ctrl__queue">
              <button type="button" class="ctrl__queue-btn" :title="t('media.prev')" @click="castStore.skipToPrev()">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M6 6h2v12H6zM20 6v12l-9-6z"/></svg>
              </button>
              <span class="ctrl__queue-pos">{{ t('media.playingOf', { i: playlist.playingIndex, n: playlist.total }) }}</span>
              <button type="button" class="ctrl__queue-btn" :title="t('media.next')" @click="castStore.skipToNext()">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M16 6h2v12h-2zM4 6v12l9-6z"/></svg>
              </button>
            </div>

            <!-- 播放选项：播完即停 + 睡眠定时 -->
            <div class="ctrl__opts">
              <label v-if="playlist.hasQueue" class="ctrl__opt-check" :title="t('cast.stopAfterCurrent')">
                <input type="checkbox" :checked="playlist.stopAfterCurrent" @change="onToggleStopAfter" />
                <span>{{ t('cast.stopAfterCurrent') }}</span>
              </label>
              <label class="ctrl__opt-sleep">
                <span class="ctrl__opt-label">{{ t('cast.sleepTimer') }}</span>
                <select class="ctrl__opt-select" :value="String(castStore.sleepMinutes)" @change="onSleepChange">
                  <option value="0">{{ t('cast.sleepOff') }}</option>
                  <option value="15">{{ t('cast.sleepMin', { n: 15 }) }}</option>
                  <option value="30">{{ t('cast.sleepMin', { n: 30 }) }}</option>
                  <option value="60">{{ t('cast.sleepMin', { n: 60 }) }}</option>
                  <option value="90">{{ t('cast.sleepMin', { n: 90 }) }}</option>
                </select>
              </label>
            </div>
            <div v-if="sleepText" class="ctrl__opt-hint">{{ sleepText }}</div>

            <!-- Quality (shown only when the rule pack offers 2+ tiers) -->
            <div v-if="qualityOptions" class="ctrl__quality">
              <span class="ctrl__quality-label">{{ t('cast.quality') }}</span>
              <select class="ctrl__quality-select" :value="selectedQuality"
                :disabled="castStore.qualitySwitching" @change="onQualityChange">
                <option v-for="q in qualityOptions" :key="q.id" :value="q.id">{{ q.label }}</option>
              </select>
            </div>
            <div v-if="castStore.qualitySwitching" class="ctrl__quality-hint">
              {{ t('cast.qualitySwitching') }}
            </div>

            <!-- Progress bar -->
            <div v-if="canSeek" class="ctrl__progress">
              <span class="ctrl__time">{{ displayPosition }}</span>
              <div class="ctrl__slider-wrap">
                <div class="ctrl__slider-track">
                  <div class="ctrl__slider-fill" :style="{ width: displayProgress + '%' }" />
                </div>
                <input type="range" class="ctrl__slider-input" min="0" max="100" step="0.5"
                  :value="displayProgress"
                  @input.stop="onSeekInput"
                  @change.stop="onSeekChange" />
              </div>
              <span class="ctrl__time">{{ displayDuration }}</span>
            </div>

            <!-- Volume -->
            <div class="ctrl__vol">
              <!-- Volume icon — click to toggle mute -->
              <button class="ctrl__vol-icon" @click="toggleMute" :title="castStore.castState.is_muted ? t('cast.unmute') : t('cast.mute')">
                <svg v-if="volumeIcon === 'muted'" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                  <line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/>
                </svg>
                <svg v-else-if="volumeIcon === 'low'" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                  <path d="M15.54 8.46a5 5 0 010 7.07"/>
                </svg>
                <svg v-else viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>
                  <path d="M15.54 8.46a5 5 0 010 7.07M19.07 4.93a10 10 0 010 14.14"/>
                </svg>
              </button>
              <div class="ctrl__slider-wrap ctrl__slider-wrap--vol">
                <div class="ctrl__slider-track">
                  <div class="ctrl__slider-fill" :style="{ width: volumePercent + '%' }" />
                </div>
                <input type="range" class="ctrl__slider-input" min="0" max="100"
                  :value="volumePercent"
                  @input.stop="onVolumeInput"
                  @change.stop="onVolumeChange" />
              </div>
            </div>

            <!-- Stop -->
            <button class="ctrl__stop" @click="castStore.stopCast(castStore.focusedDeviceId ?? undefined)" :disabled="castStore.loading">
              {{ t('cast.stop') }}
            </button>

            <!-- 字幕 / 手机遥控器 -->
            <div class="ctrl__extra-row">
              <button class="ctrl__remote-btn" type="button" @click="openSubtitle">
                💬 {{ t('cast.subtitle') }}
              </button>
              <button class="ctrl__remote-btn" type="button" @click="openRemote">
                📱 {{ t('cast.remoteTitle') }}
              </button>
            </div>
          </template>

        </div>
      </div>
    </Transition>

    <!-- 字幕内封弹窗 -->
    <Transition name="fade">
      <div v-if="subOpen" class="ctrl-overlay" @click.self="closeSubtitle">
        <div class="ctrl-modal ctrl-remote">
          <div class="ctrl-remote__title">💬 {{ t('cast.subtitle') }}</div>
          <p class="ctrl-remote__hint">{{ t('cast.subtitleHint') }}</p>
          <div v-if="subBusy" class="ctrl-sub__bar"><i :style="{ width: subProgress + '%' }"></i></div>
          <p v-if="subMsg" class="ctrl-remote__hint">{{ subMsg }}</p>
          <div v-if="subError" class="ctrl__error">{{ subError }}</div>
          <button class="ctrl__stop" :disabled="subBusy" @click="pickSubtitle">{{ t('cast.subtitlePick') }}</button>
          <button class="ctrl__remote-btn" type="button" @click="closeSubtitle">{{ t('common.close') }}</button>
        </div>
      </div>
    </Transition>

    <!-- 手机遥控器弹窗 -->
    <Transition name="fade">
      <div v-if="remoteOpen" class="ctrl-overlay" @click.self="closeRemote">
        <div class="ctrl-modal ctrl-remote">
          <div class="ctrl-remote__title">📱 {{ t('cast.remoteTitle') }}</div>
          <p class="ctrl-remote__hint">{{ t('cast.remoteHint') }}</p>
          <p v-if="remoteLoading" class="ctrl-remote__hint">{{ t('cast.remoteStarting') }}</p>
          <div v-else-if="remoteError" class="ctrl__error">{{ remoteError }}</div>
          <template v-else>
            <div class="ctrl-remote__url" @click="copyRemoteUrl" :title="t('cast.remoteCopy')">{{ remoteUrl }}</div>
            <p class="ctrl-remote__hint">{{ remoteCopied ? t('cast.remoteCopied') : t('cast.remoteCopy') }}</p>
          </template>
          <button class="ctrl__stop ctrl__wide" @click="closeRemote">{{ t('common.close') }}</button>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* ── Overlay ── */
.ctrl-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.3);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

/* ── Modal ── */
.ctrl-modal {
  width: 240px;
  max-width: calc(100% - 24px);
  max-height: calc(100% - 24px);
  min-width: 0;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--r-xl);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 0;
  overflow-x: hidden;
  overflow-y: auto;
  font-family: var(--font);
  user-select: none;
}
.ctrl-modal > * { flex-shrink: 0; }

.ctrl__error {
  width: calc(100% - 24px);
  min-width: 0;
  margin: 4px 12px;
  padding: 8px;
  border-radius: var(--r-sm);
  background: var(--danger-light);
  color: var(--danger);
  font-size: 12px;
  overflow-wrap: anywhere;
  word-break: break-word;
  white-space: pre-wrap;
  user-select: text;
}
.ctrl__error strong, .ctrl__error span { display: block; }
.ctrl__error button {
  display: block;
  margin-top: 6px;
  max-width: 100%;
  color: var(--text-primary);
  text-decoration: underline;
  overflow-wrap: anywhere;
}

/* ── Header ── */
.ctrl__head {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 12px 6px;
}

.ctrl__title {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  flex: 1;
}

.ctrl__min {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-tertiary);
  cursor: pointer;
  transition: background 0.15s;
}
.ctrl__min:hover { background: var(--bg-secondary); }

/* ── Thumbnail ── */
.ctrl__thumb {
  width: calc(100% - 24px);
  aspect-ratio: 16 / 10;
  border: none;
  padding: 0;
  border-radius: 10px;
  overflow: hidden;
  background: var(--bg-input);
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 4px 12px;
  position: relative;
  cursor: pointer;
}
.ctrl__thumb-img { width: 100%; height: 100%; object-fit: cover; }
.ctrl__thumb-ph { display: flex; align-items: center; justify-content: center; }

/* Play/Pause overlay on thumbnail */
.ctrl__thumb-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.25);
  opacity: 0;
  transition: opacity 0.2s ease;
}
.ctrl__thumb:hover .ctrl__thumb-overlay,
.ctrl__thumb:focus-visible .ctrl__thumb-overlay {
  opacity: 1;
}
.ctrl__thumb-overlay--paused {
  opacity: 1;
}
.ctrl__thumb-play {
  width: 44px;
  height: 44px;
  border-radius: 50%;
  background: var(--primary);
  color: var(--on-primary);
  display: flex;
  align-items: center;
  justify-content: center;
  transition: transform 0.15s ease, background 0.15s ease;
}
.ctrl__thumb-play:hover {
  transform: scale(1.04);
  background: var(--primary-hover);
}

/* ── Filename ── */
.ctrl__name {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
  padding: 2px 12px;
  text-align: center;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  width: 100%;
}

/* ── 播放队列控制（上一集/下一集） ── */
.ctrl__queue {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 2px 12px 4px;
}
.ctrl__queue-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
  color: var(--text-secondary);
  border-radius: 6px;
  cursor: pointer;
}
.ctrl__queue-btn:hover {
  color: var(--accent);
  border-color: var(--accent);
}
.ctrl__queue-pos {
  font-size: 12px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
  min-width: 56px;
  text-align: center;
}

/* ── 字幕 / 遥控器按钮行 ── */
.ctrl__extra-row {
  display: flex;
  gap: 8px;
  width: calc(100% - 24px);
  margin: 4px 12px 10px;
}
.ctrl__extra-row .ctrl__remote-btn {
  margin: 0;
  width: auto;
  flex: 1;
}
.ctrl-sub__bar {
  width: 100%;
  height: 6px;
  border-radius: 3px;
  background: var(--bg-input);
  overflow: hidden;
}
.ctrl-sub__bar > i {
  display: block;
  height: 100%;
  width: 0;
  background: var(--accent);
  transition: width .2s ease;
}

/* ── 手机遥控器 ── */
.ctrl__remote-btn {
  margin: 4px 12px 10px;
  width: calc(100% - 24px);
  padding: 8px 0;
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
  color: var(--text-secondary);
  border-radius: 8px;
  font-size: 12px;
  font-family: var(--font);
  cursor: pointer;
}
.ctrl__remote-btn:hover { color: var(--accent); border-color: var(--accent); }
.ctrl-remote { padding: 16px 14px; gap: 8px; }
.ctrl-remote__title { font-size: 14px; font-weight: 600; color: var(--text-primary); }
.ctrl-remote__hint { margin: 0; font-size: 12px; color: var(--text-secondary); text-align: center; line-height: 1.5; }
.ctrl-remote__url {
  width: 100%;
  padding: 10px 8px;
  border: 1px dashed var(--accent);
  border-radius: 8px;
  font-size: 12px;
  font-family: ui-monospace, Menlo, monospace;
  color: var(--accent);
  text-align: center;
  overflow-wrap: anywhere;
  cursor: pointer;
  user-select: all;
}
.ctrl-remote .ctrl__stop.ctrl__wide { margin: 6px 0 0; width: 100%; }

/* ── 播放选项（播完即停 / 睡眠定时） ── */
.ctrl__opts {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 4px 12px 2px;
  width: 100%;
  flex-wrap: wrap;
}
.ctrl__opt-check {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
  user-select: none;
}
.ctrl__opt-check input {
  margin: 0;
  cursor: pointer;
}
.ctrl__opt-sleep {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}
.ctrl__opt-label {
  white-space: nowrap;
}
.ctrl__opt-select {
  font-size: 12px;
  font-family: var(--font);
  color: var(--text-primary);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 2px 6px;
  cursor: pointer;
  outline: none;
}
.ctrl__opt-select:focus-visible {
  outline: 2px solid var(--border-focus);
}
.ctrl__opt-hint {
  padding: 0 12px 4px;
  font-size: 11px;
  color: var(--accent);
  font-variant-numeric: tabular-nums;
}

/* ── Quality (rule-pack supplied tiers) ── */
.ctrl__quality {
  display: flex;  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 2px 12px;
  width: 100%;
}

.ctrl__quality-label {
  font-size: 12px;
  color: var(--text-secondary);
  min-width: 0;
  max-width: 40%;
  overflow-wrap: anywhere;
}

.ctrl__quality-select {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  font-family: var(--font);
  color: var(--text-primary);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 3px 6px;
  cursor: pointer;
  outline: none;
  text-overflow: ellipsis;
}
.ctrl__quality-select:focus-visible,
.ctrl__slider-input:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 2px;
}
.ctrl__quality-select:disabled { opacity: .5; cursor: not-allowed; }

.ctrl__quality-hint {
  font-size: 10px;
  color: var(--text-tertiary);
  padding: 0 12px;
  width: 100%;
  overflow-wrap: anywhere;
}

/* ── Slider (shared by progress + volume) ── */
.ctrl__slider-wrap {
  position: relative;
  flex: 1;
  min-width: 0;
  height: 16px;
  display: flex;
  align-items: center;
}

.ctrl__slider-wrap--vol {
  height: 14px;
}

.ctrl__slider-track {
  position: absolute;
  left: 0;
  right: 0;
  height: 3px;
  background: var(--border);
  border-radius: 2px;
  overflow: hidden;
  pointer-events: none;
  transition: height 0.15s ease;
}

.ctrl__slider-wrap:hover .ctrl__slider-track {
  height: 4px;
}

.ctrl__slider-fill {
  height: 100%;
  background: var(--primary);
  border-radius: 2px;
  transition: width 0.1s linear;
}

.ctrl__slider-wrap--vol .ctrl__slider-fill {
  background: var(--text-secondary);
}

.ctrl__slider-wrap--vol:hover .ctrl__slider-fill {
  background: var(--text-primary);
}

.ctrl__slider-input {
  position: relative;
  width: 100%;
  min-width: 0;
  height: 100%;
  margin: 0;
  -webkit-appearance: none;
  appearance: none;
  background: transparent;
  cursor: pointer;
  outline: none;
}

/* Thumb: hidden by default, show on hover */
.ctrl__slider-input::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 0;
  height: 0;
  border-radius: 50%;
  background: var(--primary);
  box-shadow: none;
  transition: width 0.15s ease, height 0.15s ease, box-shadow 0.15s ease;
}

.ctrl__slider-wrap:hover .ctrl__slider-input::-webkit-slider-thumb {
  width: 10px;
  height: 10px;
  box-shadow: 0 0 0 3px rgba(var(--primary-rgb), 0.15);
}

.ctrl__slider-input:active::-webkit-slider-thumb {
  width: 12px;
  height: 12px;
  box-shadow: 0 0 0 4px rgba(var(--primary-rgb), 0.2);
}

/* Volume thumb: gray */
.ctrl__slider-wrap--vol .ctrl__slider-input::-webkit-slider-thumb {
  background: var(--text-secondary);
  box-shadow: none;
}

.ctrl__slider-wrap--vol:hover .ctrl__slider-input::-webkit-slider-thumb {
  width: 10px;
  height: 10px;
  box-shadow: 0 0 0 3px rgba(107, 114, 128, 0.15);
}

.ctrl__slider-wrap--vol .ctrl__slider-input:active::-webkit-slider-thumb {
  width: 12px;
  height: 12px;
  box-shadow: 0 0 0 4px rgba(107, 114, 128, 0.2);
}

/* ── Progress ── */
.ctrl__progress {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 12px;
  width: 100%;
}

.ctrl__time {
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  color: var(--text-tertiary);
  min-width: 32px;
  flex-shrink: 0;
  text-align: center;
  user-select: none;
}

/* ── Volume ── */
.ctrl__vol {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 12px;
  width: 100%;
  color: var(--text-tertiary);
}

.ctrl__vol-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: inherit;
  cursor: pointer;
  padding: 2px;
  border-radius: 4px;
  flex-shrink: 0;
  transition: background 0.15s;
}
.ctrl__vol-icon:hover {
  background: var(--bg-secondary);
}

/* ── Stop ── */
.ctrl__stop {
  width: calc(100% - 24px);
  padding: 10px 24px;
  margin: 8px 12px 12px;
  font-size: 14px;
  font-weight: 600;
  color: var(--on-primary);
  background: var(--primary);
  overflow-wrap: anywhere;
  border: none;
  border-radius: 10px;
  cursor: pointer;
  transition: background .15s;
  font-family: var(--font);
}
.ctrl__stop:hover:not(:disabled) { background: var(--primary-hover); }
.ctrl__stop:disabled { opacity: .5; cursor: not-allowed; }

/* ── Transition ── */
.ctrl-fade-enter-active,
.ctrl-fade-leave-active {
  transition: opacity 0.2s ease;
}
.ctrl-fade-enter-active .ctrl-modal,
.ctrl-fade-leave-active .ctrl-modal {
  transition: transform 0.2s ease, opacity 0.2s ease;
}
.ctrl-fade-enter-from,
.ctrl-fade-leave-to {
  opacity: 0;
}
.ctrl-fade-enter-from .ctrl-modal {
  transform: scale(0.9);
}
.ctrl-fade-leave-to .ctrl-modal {
  transform: scale(0.9);
}

</style>
