<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMediaStore } from '@/stores/media'
import { useDeviceStore } from '@/stores/device'
import { useCastStore } from '@/stores/cast'
import { usePlaylistStore } from '@/stores/playlist'
import { useHistoryStore, type HistoryEntry } from '@/stores/history'
import { readClipboardText } from '@/api/commands'
import type { Device } from '@/types/device'
import Modal from './Modal.vue'

const { t } = useI18n()
const mediaStore = useMediaStore()
const deviceStore = useDeviceStore()
const castStore = useCastStore()
const playlist = usePlaylistStore()

const isDragOver = ref(false)
const pickerError = ref<string | null>(null)
const showQueuePanel = ref(false)
const queueDragIndex = ref<number | null>(null)
// 点了队列的「投屏」、正在等用户选设备：选定后要走「投队列」而不是「投单个文件」
const pendingQueueCast = ref(false)

// Device selection modal state
const showDeviceModal = ref(false)

// ── Media preview URL (asset:// protocol) ──

const convertFileSrc = ref<((filePath: string, protocol?: string) => string) | null>(null)

// Video thumbnail extracted via <video> + canvas
const videoThumb = ref<string | null>(null)

function captureFrame(video: HTMLVideoElement) {
  try {
    if (!video.videoWidth || !video.videoHeight) return
    const canvas = document.createElement('canvas')
    canvas.width = video.videoWidth
    canvas.height = video.videoHeight
    const ctx = canvas.getContext('2d')
    if (ctx) {
      ctx.drawImage(video, 0, 0)
      const dataUrl = canvas.toDataURL('image/jpeg', 0.7)
      videoThumb.value = dataUrl
      mediaStore.setThumbnail(dataUrl)
    }
  } catch {
    // CORS or tainted canvas
  }
}

function extractVideoThumb(url: string) {
  videoThumb.value = null
  const video = document.createElement('video')
  video.muted = true
  video.preload = 'auto'
  video.playsInline = true
  video.crossOrigin = 'anonymous'
  video.style.cssText = 'position:fixed;left:-9999px;width:1px;height:1px;opacity:0'
  document.body.appendChild(video)

  let done = false
  const finish = () => {
    if (done) return
    done = true
    captureFrame(video)
    video.pause()
    video.removeAttribute('src')
    video.remove()
  }

  video.addEventListener('loadeddata', () => {
    if (video.duration > 1.5) {
      video.currentTime = 1
    } else {
      finish()
    }
  })
  video.addEventListener('seeked', () => {
    finish()
  })

  // Fallback: if seeked never fires
  video.addEventListener('loadeddata', () => {
    setTimeout(() => { if (!done) finish() }, 800)
  })

  video.addEventListener('error', () => {
    video.remove()
  })

  video.src = url
}

const mediaPreviewUrl = computed(() => {
  const info = mediaStore.mediaInfo
  if (!info) return null

  // Prefer thumbnail (Bilibili cover or extracted video frame)
  if (info.thumbnail) return info.thumbnail

  // Local image file in Tauri → asset:// URL
  if (inTauri && info.media_type === 'file' && convertFileSrc.value && info.mime_type?.startsWith('image/')) {
    return convertFileSrc.value(info.uri)
  }
  // Local file in browser → blob URL from File object
  if (!inTauri && info.media_type === 'file' && mediaStore.localFile && info.mime_type?.startsWith('image/')) {
    return URL.createObjectURL(mediaStore.localFile)
  }
  // Remote URL (image direct link) → use directly
  if (info.media_type === 'url' && info.mime_type?.startsWith('image/')) {
    return info.uri
  }
  return null
})

// ── Media type detection ──

type MediaCategory = 'video' | 'image' | 'audio' | 'link'

function getMediaCategory(): MediaCategory {
  const mime = mediaStore.mediaInfo?.mime_type ?? ''
  if (mime.startsWith('video/')) return 'video'
  if (mime.startsWith('image/')) return 'image'
  if (mime.startsWith('audio/')) return 'audio'
  return 'link'
}

const mediaCategory = computed<MediaCategory>(() => getMediaCategory())

// Watch for local video files → extract thumbnail frame
// Watch URI only — avoid re-triggering when setThumbnail updates mediaInfo
watch(() => mediaStore.mediaInfo?.uri, (uri) => {
  videoThumb.value = null
  const info = mediaStore.mediaInfo
  if (uri && info?.mime_type?.startsWith('video/') && info.media_type === 'file') {
    const url = inTauri && convertFileSrc.value
      ? convertFileSrc.value(uri)
      : (!inTauri && mediaStore.localFile ? URL.createObjectURL(mediaStore.localFile) : null)
    if (url) extractVideoThumb(url)
  }
})

const previewFailed = ref(false)
watch(mediaPreviewUrl, () => { previewFailed.value = false })

const categoryLabel = computed(() => {
  switch (mediaCategory.value) {
    case 'video': return t('media.video')
    case 'image': return t('media.image')
    case 'audio': return t('media.audio')
    case 'link': return t('media.link')
  }
})

const mediaTitle = computed(() => {
  const title = mediaStore.mediaInfo?.title ?? ''
  return title.length > 30 ? title.slice(0, 30) + '...' : title
})

// ── 错误信息翻译 ──
// 后端返回英文错误，前端用正则匹配翻译
function translateError(msg: string): string {
  // Unsupported format: .7z → media.unsupportedFormat
  const unsupportedMatch = msg.match(/^Unsupported format:\s*(.+)$/i)
  if (unsupportedMatch) {
    return t('media.unsupportedFormat', { ext: unsupportedMatch[1] })
  }
  return msg
}

const translatedError = computed(() => {
  if (!mediaStore.error) return null
  return translateError(mediaStore.error)
})

// ── Formatters ──

function formatFileSize(bytes: number | null): string | null {
  if (!bytes) return null
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB'
  return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB'
}

function formatDuration(seconds: number | null): string | null {
  if (!seconds || seconds <= 0) return null
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  const mm = String(m).padStart(2, '0')
  const ss = String(s).padStart(2, '0')
  if (h > 0) return `${h}:${mm}:${ss}`
  return `${mm}:${ss}`
}

/** Build the subtitle line: "video/mp4 · 48.8 MB · 01:23:45" */
const mediaSubtitle = computed(() => {
  const info = mediaStore.mediaInfo
  if (!info) return ''
  const parts: string[] = []
  // MIME type (short form)
  if (info.mime_type) {
    parts.push(info.mime_type)
  }
  const size = formatFileSize(info.file_size)
  if (size) parts.push(size)
  const dur = formatDuration(info.duration)
  if (dur) parts.push(dur)
  return parts.join(' · ') || categoryLabel.value
})

// ── Tauri detection & drag-drop ──
// dragDropEnabled: true → Tauri native handler provides absolute file paths.
// URL drags from browsers produce empty paths; we show a paste hint in that case.
//
// ⚠️ 已知的 Tauri 架构限制（非本项目 bug）
//
// Tauri 的拖拽系统采用原生 OS 级拦截机制，与 WebView 内的 HTML5 拖拽 API 存在根本性冲突：
//   - dragDropEnabled: true  → 可获取文件绝对路径，但 HTML5 drag 事件被完全阻断
//   - dragDropEnabled: false → HTML5 事件可用，但 Chromium WebView 不暴露文件绝对路径
//
// 实验验证（本项目 2026-07）：
//   - 文件拖入：原生事件 ✅ paths=['C:\...\1.mp4']，HTML5 事件 ❌ 被拦截
//   - URL 拖入（首次）：原生事件 ❌ 无反应，HTML5 事件 ❌ 被拦截
//   - URL 拖入（文件拖过后）：原生事件 ⚠️ paths=[]，HTML5 事件 ❌ DataTransfer 无 text/uri-list
//
// 社区确认的已知限制：
//   - GitHub Issue #14055 (2025-08): React 开发者复现完全一致的场景
//     → Tauri 团队标记 "not planned"，原因是 Windows 平台技术限制，无法直接修复
//   - GitHub Issue #14373 (2025-10): 更广泛的拖拽机制讨论
//     → 核心维护者 FabianLars 确认正在开发"不会破坏原生行为的新替代实现"
//     → dormouse 项目明确表示"等待上游 Tauri 修复 #14373"
//   - Tauri v2 官方文档:
//     "Disabling [dragDropEnabled] is required to use HTML5 drag and drop
//      on the frontend on Windows."
//
// 当前策略：文件拖拽走原生 handler（完美），URL 拖拽引导用户粘贴（降级）。
// 待 Tauri 上游修复后可统一为 HTML5 方案。
//
let inTauri = false
let unlistenDrag: (() => void) | null = null
const showPasteHint = ref(false)

// Auto-hide paste hint when user interacts with URL input or media becomes ready
watch(() => mediaStore.urlInput, (val) => {
  if (val.trim()) showPasteHint.value = false
})
watch(() => mediaStore.state, (s) => {
  if (s === 'ready' || s === 'parsing') showPasteHint.value = false
})

// ── HTML5 drag events (for browser dev mode without Tauri) ──
const dragCounter = ref(0)

function onHtmlDragEnter(e: DragEvent) {
  e.preventDefault()
  dragCounter.value++
  mediaStore.setDragOver()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
}

function onHtmlDragLeave(e: DragEvent) {
  e.preventDefault()
  dragCounter.value--
  if (dragCounter.value <= 0) {
    dragCounter.value = 0
    mediaStore.clearDragOver()
  }
}

function onHtmlDragOver(e: DragEvent) {
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
}

function onHtmlDrop(e: DragEvent) {
  e.preventDefault()
  dragCounter.value = 0
  mediaStore.clearDragOver()

  // Browser dev mode only: try to extract URL from DataTransfer
  const dt = e.dataTransfer
  if (!dt) return

  if (dt.types.includes('text/uri-list')) {
    const raw = dt.getData('text/uri-list')
    if (raw) {
      const urls = raw.split('\n').filter(l => l.trim() && !l.startsWith('#'))
      if (urls.length > 0) {
        const url = urls[0].trim()
        if (/^https?:\/\//i.test(url)) {
          mediaStore.setInputChange(url)
          dropQueueForSingleMedia()
          mediaStore.parseUrl(url)
          return
        }
      }
    }
  }
  if (dt.types.includes('text/plain')) {
    const text = dt.getData('text/plain')?.trim()
    if (text && /^https?:\/\//i.test(text)) {
      mediaStore.setInputChange(text)
      dropQueueForSingleMedia()
      mediaStore.parseUrl(text)
    }
  }
}

// ── Tauri native drag-drop (file paths) ──

onMounted(async () => {
  // Detect Tauri runtime
  if (window.__TAURI_INTERNALS__) {
    try {
      const core = await import('@tauri-apps/api/core')
      convertFileSrc.value = core.convertFileSrc
      inTauri = true
    } catch {
      // Module load failed
    }
  }

  // Register native drag-drop handler (provides absolute file paths)
  if (inTauri) {
    try {
      const { getCurrentWebview } = await import('@tauri-apps/api/webview')
      const webview = getCurrentWebview()

      unlistenDrag = await webview.onDragDropEvent((event) => {
        if (event.payload.type === 'enter' || event.payload.type === 'over') {
          mediaStore.setDragOver()
        } else if (event.payload.type === 'drop') {
          mediaStore.clearDragOver()
          const paths = event.payload.paths
          if (paths && paths.length > 0) {
            // File drop: has absolute path
            showPasteHint.value = false
            dropQueueForSingleMedia()
            mediaStore.parseFile(paths[0])
          } else {
            // URL drag from browser: paths is empty
            // WebView2 blocks HTML5 DataTransfer access, so we can't extract
            // the URL automatically. Prompt user to paste it.
            showPasteHint.value = true
            mediaStore.urlInput = ''
            mediaStore.state = 'idle'
          }
        } else {
          mediaStore.clearDragOver()
        }
      })
    } catch {
    }
  }
})

onUnmounted(() => {
  unlistenDrag?.()
})

async function onPasteFromClipboard() {
  const text = (await readClipboardText()).trim()
  if (!text) {
    pickerError.value = t('media.clipboardEmpty')
    return
  }
  pickerError.value = null
  dropQueueForSingleMedia()
  mediaStore.setInputChange(text.replace(/\s+/g, ' '))
  mediaStore.parseUrl(text)
}

/**
 * 直接 Ctrl+V 到输入框：分享口令文案（非纯链接）自动提取并立即解析；
 * 纯链接保持默认粘贴行为，便于用户手动编辑。
 */
function onUrlPaste(e: ClipboardEvent) {
  const text = (e.clipboardData?.getData('text') ?? '').trim()
  if (!text || /^https?:\/\/\S+$/i.test(text)) return
  e.preventDefault()
  const cleaned = text.replace(/\s+/g, ' ')
  dropQueueForSingleMedia()
  mediaStore.setInputChange(cleaned)
  mediaStore.parseUrl(text)
}

async function onParse() {
  dropQueueForSingleMedia()
  await mediaStore.parseUrl()
}

// 按钮是否可用：有在线设备即可点击
const hasTarget = computed(() => onlineDevices.value.length > 0)

/** Online devices available for casting */
const onlineDevices = computed(() => {
  return deviceStore.devices.filter(d => d.status === 'online')
})

async function onCast() {
  // 队列优先：有播放队列时走「投队列」，由 castQueueToTarget 弹出设备选择框
  if (playlist.hasQueue) {
    await castQueueToTarget()
    return
  }
  if (mediaStore.mediaInfo) {
    const target = pickTargetDevice()
    if (!target) return
    await castStore.startCast(target.id, mediaStore.mediaInfo.uri, mediaStore.mediaInfo)
  }
}

/** 挑一个可投的在线设备；没有就弹选择框（返回 null） */
function pickTargetDevice(): Device | null {
  let targetDevice: Device | null = null
  if (deviceStore.selectedDevice && deviceStore.selectedDevice.status === 'online') {
    targetDevice = deviceStore.selectedDevice
  }
  if (!targetDevice) {
    targetDevice = deviceStore.devices.find(d => d.is_default && d.status === 'online') ?? null
  }
  if (!targetDevice) {
    showDeviceModal.value = true
    return null
  }
  return targetDevice
}

/**
 * 选了单个文件/链接时，把上一次的文件夹队列作废。
 * 不作废的话 `onCast()` 会先看 `playlist.hasQueue` 走「投队列」分支，
 * 于是「明明选的是新文件，投出去却还是上一个文件夹的列表」。
 */
function dropQueueForSingleMedia() {
  if (playlist.hasQueue) playlist.reset()
}

/** 把队列按当前顺序投给目标设备（跳过未勾选） */
async function castQueueToTarget() {
  // 文件夹/队列投屏是多设备场景：总是让用户挑目标设备，别默默用默认设备。
  // 注意队列面板这时还开着，且与设备选择弹窗同为 z-index:100（后渲染的队列面板会盖住选择弹窗），
  // 所以先把队列面板关掉再弹选择框，否则用户根本看不到可选设备。
  showQueuePanel.value = false
  pendingQueueCast.value = true
  showDeviceModal.value = true
  return false
}

/** 投队列第一项并记住设备（供 onSelectDevice 复用） */
async function castQueueFirstTo(deviceId: string) {
  const first = playlist.playableItems[0]
  if (!first) {
    pickerError.value = t('media.queueEmpty')
    return false
  }
  const ok = await castStore.castQueueItem(deviceId, first, { startsQueue: true })
  if (ok) showQueuePanel.value = false
  return ok
}

async function onSelectDevice(deviceId: string) {
  showDeviceModal.value = false
  // 从队列的「投屏」进来的：选定设备后投队列；否则投单个文件
  const castQueue = pendingQueueCast.value
  pendingQueueCast.value = false
  if (castQueue) {
    await castQueueFirstTo(deviceId)
    return
  }
  if (mediaStore.mediaInfo) {
    await castStore.startCast(deviceId, mediaStore.mediaInfo.uri, mediaStore.mediaInfo)
  }
}

/** 关掉设备选择框（取消 / 点遮罩）：若是为投队列而弹的，把队列面板还回去，别让用户丢掉列表 */
function onCloseDeviceModal() {
  showDeviceModal.value = false
  if (pendingQueueCast.value) {
    pendingQueueCast.value = false
    if (playlist.hasQueue) showQueuePanel.value = true
  }
}

function onClear() {
  mediaStore.reset()
}

// File picker: try Tauri dialog first, fall back to HTML file input
async function openFilePicker() {
  pickerError.value = null
  try {
    const { open } = await import('@tauri-apps/plugin-dialog')
    const selected = await open({
      multiple: false,
      filters: [{
        name: 'Media',
        extensions: ['mp4', 'mkv', 'avi', 'mov', 'webm', 'flv', 'mp3', 'flac', 'wav', 'aac', 'ogg', 'm4a', 'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp']
      }]
    })
    if (selected && typeof selected === 'string') {
      dropQueueForSingleMedia()
      await mediaStore.parseFile(selected)
    }
  } catch (err: any) {
    pickerError.value = String(err?.message ?? err)
    // Browser fallback
    fileInput.value?.click()
  }
}

const fileInput = ref<HTMLInputElement>()
function onFileSelected(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) {
    dropQueueForSingleMedia()
    mediaStore.parseFile(file)
  }
}

// ── 文件夹投屏 / 播放队列 ──
async function openFolderPicker() {
  pickerError.value = null
  try {
    const { open } = await import('@tauri-apps/plugin-dialog')
    const dir = await open({ directory: true, multiple: false })
    if (dir && typeof dir === 'string') {
      await playlist.loadFolder(dir)
      showQueuePanel.value = true
      if (playlist.error === 'empty') pickerError.value = t('media.queueEmpty')
    }
  } catch (err: any) {
    pickerError.value = String(err?.message ?? err)
  }
}

// 队列拖拽排序
function onQueueDragStart(index: number) {
  queueDragIndex.value = index
}
function onQueueDrop(index: number) {
  if (queueDragIndex.value !== null) {
    playlist.moveItem(queueDragIndex.value, index)
    queueDragIndex.value = null
  }
}
function onQueueDragEnd() {
  queueDragIndex.value = null
}

function closeQueue() {
  showQueuePanel.value = false
}

/** 切下一集/上一集：只在有活跃投屏且有队列时可用 */
async function onQueueNext() {
  await castStore.skipToNext()
}
async function onQueuePrev() {
  await castStore.skipToPrev()
}

const modeLabel = computed(() => {
  const m = playlist.playMode
  if (m === 'shuffle') return t('media.modeShuffle')
  if (m === 'repeat-one') return t('media.modeRepeatOne')
  return t('media.modeSequential')
})
function cycleMode() {
  const order = ['sequential', 'shuffle', 'repeat-one'] as const
  const i = order.indexOf(playlist.playMode)
  playlist.setPlayMode(order[(i + 1) % order.length])
}

// ── 投屏历史（按设备） ──
const history = useHistoryStore()
const showHistoryPanel = ref(false)

/** 按设备分组，最近投过的设备排前面；设备离线也保留（历史是记录，不是在线列表） */
const historyGroups = computed(() =>
  history.deviceIdsByRecency().map((deviceId) => {
    const items = history.forDevice(deviceId)
    return {
      deviceId,
      deviceName: items[0]?.deviceName ?? deviceId,
      online: deviceStore.devices.some((d) => d.id === deviceId && d.status === 'online'),
      items,
    }
  })
)

function formatHistoryTime(at: number) {
  const d = new Date(at)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** 点历史条目重投到当初那台设备；队列条目先把那个文件夹重新读出来 */
async function castFromHistory(entry: HistoryEntry) {
  showHistoryPanel.value = false
  pickerError.value = null
  if (entry.kind === 'queue' && entry.folder) {
    await playlist.loadFolder(entry.folder)
    if (playlist.error) {
      pickerError.value = playlist.error === 'empty'
        ? t('media.queueEmpty')
        : String(playlist.error)
      return
    }
    await castQueueFirstTo(entry.deviceId)
    return
  }
  await castStore.startCast(entry.deviceId, entry.uri, {
    title: entry.title,
    mime_type: entry.mime_type,
    thumbnail: entry.thumbnail ?? null,
  })
}

function clearHistory(deviceId: string) {
  history.clear(deviceId)
}
</script>

<template>
  <div
    class="media-input"
    :class="{
      'media-input--dragover': isDragOver || mediaStore.state === 'dragover',
      'media-input--parsing': mediaStore.state === 'parsing',
      'media-input--ready': mediaStore.state === 'ready',
    }"
    @dragenter="onHtmlDragEnter"
    @dragleave="onHtmlDragLeave"
    @dragover.prevent="onHtmlDragOver"
    @drop.prevent="onHtmlDrop"
  >
    <input
      ref="fileInput"
      type="file"
      accept="video/*,audio/*,image/*"
      style="display: none"
      @change="onFileSelected"
    />

    <!-- ═══ Drag / Default State ═══ -->
    <div
      v-if="mediaStore.state !== 'ready'"
      class="media-input__dropzone"
      role="button"
      tabindex="0"
      :aria-label="t('media.dragHint')"
      :aria-busy="mediaStore.isParsing"
      @click="openFilePicker"
      @keydown.enter.prevent="openFilePicker"
      @keydown.space.prevent="openFilePicker"
    >
      <div class="media-input__icon">
        <svg aria-hidden="true" viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M3 8V6a2 2 0 012-2h5l2 3h7a2 2 0 012 2v10a2 2 0 01-2 2H5a2 2 0 01-2-2Z" />
          <path d="m10 11 5 3-5 3Z" />
        </svg>
      </div>

      <!-- Parsing spinner -->
      <div v-if="mediaStore.state === 'parsing'" class="media-input__parsing">
        <span class="media-input__spinner" />
        <span>{{ t('media.parsing') }}</span>
      </div>
      <template v-else>
        <p v-if="showPasteHint" class="media-input__paste-hint">
          🔗 {{ t('media.urlDragDetected') }}
        </p>
        <p v-else class="media-input__hint">{{ t('media.dragHint') }}</p>
        <p class="media-input__subhint">{{ t('media.pasteHint') }}</p>
        <p v-if="pickerError" class="media-input__error">⚠ {{ pickerError }}</p>
        <p v-if="translatedError" class="media-input__error">⚠ {{ translatedError }}</p>
        <p v-if="mediaStore.ledgerHint" class="media-input__ledger-hint">{{ t('media.resolveLedgerHint') }}</p>
        <div v-if="!mediaStore.isParsing" class="media-input__folder-row">
          <button
            class="media-input__folder-btn"
            type="button"
            @click.stop="openFolderPicker"
          >
            📁 {{ t('media.pickFolder') }}
          </button>
          <button
            class="media-input__folder-btn"
            type="button"
            :title="t('media.historyOpen')"
            @click.stop="showHistoryPanel = true"
          >
            🕘 {{ t('media.historyOpen') }}
          </button>
        </div>
      </template>
    </div>

    <!-- ═══ Ready State — Media Preview Card ═══ -->
    <div v-else class="media-input__preview">
      <button class="media-input__clear" @click="onClear" :title="t('media.clear')" :aria-label="t('media.clear')">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
          <line x1="18" y1="6" x2="6" y2="18" />
          <line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>

      <!-- Left: preview thumbnail -->
      <div class="media-input__preview-left">
        <!-- thumbnail (image) -->
        <img
          v-if="mediaPreviewUrl && !previewFailed"
          :src="mediaPreviewUrl"
          class="media-input__thumb"
          :alt="mediaStore.mediaInfo?.title"
          @error="previewFailed = true"
        />
        <svg v-else class="media-input__preview-icon" viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.4" :aria-label="categoryLabel" role="img">
          <rect x="3" y="3" width="18" height="18" rx="4" />
          <path v-if="mediaCategory === 'audio'" d="M14 8v8m0-8 3-1v7M10 16a2 2 0 104 0 2 2 0 00-4 0Z" />
          <path v-else-if="mediaCategory === 'image'" d="m3 16 5-5 5 5 3-3 5 5M15 8h.01" />
          <path v-else d="m10 8 6 4-6 4Z" />
        </svg>
      </div>

      <!-- Right: info + actions -->
      <div class="media-input__preview-right">
        <div class="media-input__preview-info">
          <span class="media-input__preview-title" :title="mediaStore.mediaInfo?.title">{{ mediaTitle }}</span>
          <span class="media-input__preview-sub">{{ mediaSubtitle }}</span>
        </div>
        <button
          class="media-input__cast-btn"
          @click="onCast"
          :disabled="castStore.loading || !hasTarget"
        >
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M2 16.1A5 5 0 015.9 20M2 12.05A9 9 0 019.95 20M2 8V6a2 2 0 012-2h16a2 2 0 012 2v12a2 2 0 01-2 2h-6" />
            <circle cx="2" cy="20" r="1" fill="currentColor" />
          </svg>
          {{ t('devices.cast') }}
        </button>
      </div>
    </div>

    <!-- ═══ URL Input Bar (hidden when ready) ═══ -->
    <div v-if="mediaStore.state !== 'ready'" class="media-input__urlbar">
      <svg class="media-input__urlicon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71" />
        <path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71" />
      </svg>
      <input
        class="media-input__urlinput"
        type="text"
        :placeholder="t('media.linkPlaceholder')"
        :aria-label="t('media.linkPlaceholder')"
        :value="mediaStore.urlInput"
        @input="mediaStore.setInputChange(($event.target as HTMLInputElement).value)"
        @paste="onUrlPaste"
        @keydown.enter="onParse"
      />
      <button class="media-input__paste" @click="onPasteFromClipboard">
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="9" y="9" width="13" height="13" rx="2" />
          <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
        </svg>
        {{ t('media.paste') }}
      </button>
      <button
        class="media-input__parse"
        @click="onParse"
        :disabled="!mediaStore.urlInput.trim() || mediaStore.isParsing"
      >
        {{ t('media.parse') }}
      </button>
    </div>

    <!-- ═══ Device Selection Modal ═══ -->
    <Modal
      :visible="showDeviceModal"
      :title="t('devices.selectDevice')"
      :show-icon="true"
      @close="onCloseDeviceModal"
    >
      <p class="device-modal__hint">{{ t('devices.selectDeviceHint') }}</p>
      <div class="device-modal__list">
        <button
          v-for="device in onlineDevices"
          :key="device.id"
          class="device-modal__item"
          @click="onSelectDevice(device.id)"
        >
          <span class="device-modal__status" />
          <div class="device-modal__info">
            <span class="device-modal__name">{{ device.name }}</span>
            <span class="device-modal__ip">{{ device.ip }}</span>
          </div>
          <span v-if="device.is_default" class="device-modal__badge">{{ t('devices.defaultBadge') }}</span>
          <span v-if="castStore.getSession(device.id)" class="device-modal__badge device-modal__badge--casting">{{ t('devices.casting') }}</span>
        </button>
      </div>
      <template #actions>
        <button class="btn-cancel" @click="onCloseDeviceModal">{{ t('common.cancel') }}</button>
      </template>
    </Modal>

    <!-- ═══ Cast History Panel（按设备的投屏历史）═══ -->
    <Modal
      :visible="showHistoryPanel"
      :title="t('media.historyTitle')"
      :show-icon="false"
      @close="showHistoryPanel = false"
    >
      <div class="history-panel">
        <p v-if="historyGroups.length === 0" class="history-panel__hint">
          {{ t('media.historyEmpty') }}
        </p>
        <div v-for="group in historyGroups" :key="group.deviceId" class="history-panel__group">
          <div class="history-panel__group-head">
            <span class="history-panel__device" :title="group.deviceName">
              <span class="history-panel__dot" :class="{ 'history-panel__dot--offline': !group.online }" />
              {{ group.deviceName }}
              <span v-if="!group.online" class="history-panel__offline">· {{ t('media.historyOffline') }}</span>
            </span>
            <button class="history-panel__clear" type="button" @click="clearHistory(group.deviceId)">
              {{ t('media.historyClear') }}
            </button>
          </div>
          <ul class="history-panel__list" role="list">
            <li v-for="entry in group.items" :key="entry.id" class="history-panel__item">
              <button
                class="history-panel__cast"
                type="button"
                :disabled="!group.online || castStore.loading"
                :title="entry.uri"
                @click="castFromHistory(entry)"
              >
                <span class="history-panel__icon">{{ entry.kind === 'queue' ? '📁' : entry.kind === 'url' ? '🔗' : '🎬' }}</span>
                <span class="history-panel__body">
                  <span class="history-panel__title">{{ entry.title }}</span>
                  <span class="history-panel__meta">
                    <template v-if="entry.kind === 'queue' && entry.folder">{{ entry.folder }}</template>
                    <template v-else>{{ entry.uri.split('/').pop() }}</template>
                    <template v-if="entry.kind === 'queue' && entry.count"> · {{ t('media.historyCount', { n: entry.count }) }}</template>
                    · {{ formatHistoryTime(entry.at) }}
                  </span>
                </span>
              </button>
              <button
                class="history-panel__remove"
                type="button"
                :title="t('media.historyRemove')"
                @click="history.removeEntry(entry.id)"
              >✕</button>
            </li>
          </ul>
        </div>
      </div>
    </Modal>

    <!-- ═══ Playlist Queue Panel（文件夹投屏队列）═══ -->
    <Modal
      :visible="showQueuePanel"
      :title="t('media.queueTitle')"
      :show-icon="false"
      @close="closeQueue"
    >
      <div class="queue-panel">
        <div class="queue-panel__head">
          <span class="queue-panel__folder" :title="playlist.folder">
            📁 {{ playlist.folder || t('media.queueNoFolder') }}
          </span>
          <span class="queue-panel__count">
            {{ t('media.queueCount', { n: playlist.items.length, sel: playlist.total }) }}
          </span>
        </div>

        <div class="queue-panel__toolbar">
          <button class="queue-panel__chip" type="button" @click="playlist.selectAll()">
            {{ t('media.queueSelectAll') }}
          </button>
          <button class="queue-panel__chip" type="button" @click="playlist.selectNone()">
            {{ t('media.queueSelectNone') }}
          </button>
          <button
            class="queue-panel__chip queue-panel__chip--mode"
            type="button"
            :title="t('media.modeHint')"
            @click="cycleMode"
          >
            🔁 {{ modeLabel }}
          </button>
          <span v-if="playlist.playingIndex" class="queue-panel__nowplaying">
            {{ t('media.queueNowPlaying', { n: playlist.playingIndex, total: playlist.total }) }}
          </span>
        </div>

        <p v-if="playlist.loading" class="queue-panel__hint">{{ t('media.parsing') }}</p>
        <p v-else-if="playlist.items.length === 0" class="queue-panel__hint">
          {{ t('media.queueEmpty') }}
        </p>

        <ul v-else class="queue-panel__list" role="list">
          <li
            v-for="(item, index) in playlist.items"
            :key="item.id"
            class="queue-panel__item"
            :class="{
              'queue-panel__item--playing': playlist.playingId === item.id,
              'queue-panel__item--unselected': !playlist.isSelected(item.id),
              'queue-panel__item--dragging': queueDragIndex === index,
            }"
            draggable="true"
            @dragstart="onQueueDragStart(index)"
            @dragover.prevent
            @drop.prevent="onQueueDrop(index)"
            @dragend="onQueueDragEnd"
          >
            <input
              class="queue-panel__check"
              type="checkbox"
              :checked="playlist.isSelected(item.id)"
              @change="playlist.toggleSelected(item.id)"
            />
            <span class="queue-panel__index">{{ index + 1 }}</span>
            <span class="queue-panel__grip" :title="t('media.queueDragHint')">⠿</span>
            <span class="queue-panel__name" :title="item.media.title || item.media.uri">
              {{ item.media.title || item.media.uri }}
            </span>
            <span class="queue-panel__ops">
              <button type="button" :disabled="index === 0" :title="t('media.queueUp')" @click="playlist.moveUp(item.id)">↑</button>
              <button type="button" :disabled="index === playlist.items.length - 1" :title="t('media.queueDown')" @click="playlist.moveDown(item.id)">↓</button>
              <button type="button" :title="t('media.queueRemove')" @click="playlist.removeItem(item.id)">✕</button>
            </span>
          </li>
        </ul>
      </div>
      <template #actions>
        <button class="btn-cancel" @click="closeQueue">{{ t('common.cancel') }}</button>
        <button
          class="btn-confirm"
          :disabled="playlist.total === 0"
          @click="onCast"
        >
          {{ t('media.castQueue') }}
        </button>
      </template>
    </Modal>

    <!-- ═══ Drag Overlay (遮罩 + 动效) ═══ -->
    <Transition name="drag-overlay">
      <div v-if="isDragOver || mediaStore.state === 'dragover'" class="media-input__overlay">
        <div class="media-input__overlay-content">
          <div class="media-input__overlay-icon">
            <svg viewBox="0 0 64 64" width="48" height="48" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="8" y="18" width="48" height="36" rx="4" />
              <path d="M32 28v16M24 36l8 8 8-8" />
            </svg>
          </div>
          <p class="media-input__overlay-text">{{ t('media.dropHere') }}</p>
          <p class="media-input__overlay-subtext">{{ t('media.dropHint') }}</p>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.media-input {
  position: relative;
  border: 1px solid var(--border);
  border-radius: var(--r-xl);
  padding: var(--sp-lg);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--sp-lg);
  flex-shrink: 0;
  min-width: 0;
  box-shadow: var(--shadow-sm);
  transition: border-color var(--transition-normal), background var(--transition-normal);
  background: var(--bg-card);
  overflow: hidden;
}

.media-input--dragover {
  border-color: var(--primary);
  background: var(--primary-light);
}

.media-input--ready {
  border-color: var(--border-focus);
  padding: var(--sp-lg);
  gap: var(--sp-md);
}

/* ── Dropzone ── */
.media-input__dropzone {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 12px 8px 8px;
  border: 1px dashed var(--border-dashed);
  border-radius: var(--r-lg);
  text-align: center;
  cursor: pointer;
}

.media-input__dropzone:hover {
  border-color: var(--border-focus);
}

.media-input__icon {
  display: grid;
  place-items: center;
  width: 48px;
  height: 48px;
  border-radius: 14px;
  color: var(--primary);
  background: var(--primary-light);
  margin-bottom: 4px;
}

.media-input__hint {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.media-input__subhint {
  font-size: 12px;
  color: var(--text-secondary);
}

.media-input__error {
  font-size: 12px;
  color: var(--danger);
  max-width: 100%;
  /* 失败原因是一整句话（站点 + 状态码 + 下一步动作），截断等于把最有用的后半句丢掉：
     用户只剩「解析失败：https://www.66dpw.vip/…」，分不清是没规则、超时还是被风控。
     长 URL 里没有可断点，所以要 overflow-wrap 才真能折下来。 */
  white-space: normal;
  overflow-wrap: anywhere;
  line-height: 1.45;
}

.media-input__ledger-hint {
  font-size: 12px;
  color: var(--text-tertiary);
}

.media-input__paste-hint {
  font-size: 14px;
  font-weight: 500;
  color: var(--primary);
  animation: pulse-glow 1.5s ease-in-out infinite;
}

@keyframes pulse-glow {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}

/* ── Parsing spinner ── */
.media-input__parsing {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  font-size: 14px;
  color: var(--text-secondary);
}

.media-input__spinner {
  width: 16px;
  height: 16px;
  border: 2px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* ── Preview Card ── */
.media-input__preview {
  display: flex;
  gap: var(--sp-md);
  width: 100%;
  position: relative;
  align-items: stretch;
}

.media-input__clear {
  position: absolute;
  top: 0;
  right: 0;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--r-full);
  color: var(--text-tertiary);
  transition: all var(--transition-fast);
  z-index: 1;
}

.media-input__clear:hover {
  background: var(--bg-input);
  color: var(--text-primary);
}

/* Left: preview thumbnail */
.media-input__preview-left {
  flex-shrink: 0;
  width: 96px;
  height: 96px;
  border-radius: var(--r-lg);
  overflow: hidden;
  background: var(--bg-input);
  display: flex;
  align-items: center;
  justify-content: center;
}

.media-input__preview-icon {
  color: var(--primary);
}

.media-input__thumb {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

/* Right: info + actions */
.media-input__preview-right {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: var(--sp-sm);
  padding-right: 24px;
}

.media-input__preview-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.media-input__preview-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.media-input__preview-sub {
  font-size: 12px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── Cast button ── */
.media-input__cast-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-sm);
  width: 100%;
  padding: 8px 18px;
  font-size: 13px;
  font-weight: 500;
  color: var(--on-primary);
  background: var(--primary);
  border-radius: var(--r-lg);
  transition: background var(--transition-fast);
}

.media-input__cast-btn:hover:not(:disabled) {
  background: var(--primary-hover);
}

.media-input__cast-btn:disabled {
  background: var(--border);
  color: var(--text-tertiary);
  cursor: not-allowed;
}

/* ── URL bar ── */
.media-input__urlbar {
  display: flex;
  align-items: center;
  gap: var(--sp-sm);
  width: 100%;
  background: var(--bg-input);
  border-radius: var(--r-md);
  padding: var(--sp-xs) 6px;
  border: 1px solid var(--border);
}

.media-input__urlbar:focus-within {
  border-color: var(--border-focus);
}

.media-input__urlicon {
  color: var(--text-tertiary);
  flex-shrink: 0;
  margin-left: var(--sp-xs);
}

.media-input__urlinput {
  flex: 1;
  min-width: 0;
  padding: var(--sp-xs) 0;
  font-size: 13px;
  color: var(--text-primary);
}

.media-input__urlinput::placeholder {
  color: var(--text-tertiary);
}

.media-input__paste {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: var(--sp-xs) var(--sp-sm);
  font-size: 12px;
  color: var(--text-secondary);
  border-radius: var(--r-sm);
  white-space: nowrap;
  flex-shrink: 0;
}

.media-input__paste:hover {
  background: var(--border);
  color: var(--text-primary);
}

.media-input__parse {
  padding: 6px 14px;
  font-size: 13px;
  font-weight: 500;
  color: var(--on-primary);
  background: var(--primary);
  border-radius: var(--r-sm);
  white-space: nowrap;
  flex-shrink: 0;
  transition: background var(--transition-fast);
}

.media-input__parse:hover:not(:disabled) {
  background: var(--primary-hover);
}

.media-input__parse:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ── Device Selection Modal ── */
.device-modal__hint {
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: var(--sp-md);
}

.device-modal__list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-xs);
  max-height: 240px;
  overflow-y: auto;
}

.device-modal__item {
  display: flex;
  align-items: center;
  gap: var(--sp-md);
  padding: var(--sp-md);
  border-radius: var(--r-md);
  transition: background var(--transition-fast);
  text-align: left;
}

.device-modal__item:hover {
  background: var(--bg-secondary);
}

.device-modal__status {
  width: 8px;
  height: 8px;
  border-radius: var(--r-full);
  background: var(--status-online);
  flex-shrink: 0;
}

.device-modal__info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.device-modal__name {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-primary);
}

.device-modal__ip {
  font-size: 11px;
  color: var(--text-tertiary);
}

.device-modal__badge {
  font-size: 10px;
  padding: 1px 6px;
  background: var(--primary-light);
  color: var(--primary);
  border-radius: var(--r-full);
  font-weight: 500;
  flex-shrink: 0;
}
.device-modal__badge--casting {
  background: rgba(251, 191, 36, 0.15);
  color: #D97706;
}

/* ═══ Drag Overlay ═══ */
.media-input__overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(var(--primary-rgb, 59, 130, 246), 0.15);
  backdrop-filter: blur(4px);
  border-radius: var(--r-xl);
  border: 2px dashed var(--primary);
  z-index: 10;
}

.media-input__overlay-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--sp-sm);
  animation: bounce-in 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
}

.media-input__overlay-icon {
  color: var(--primary);
  animation: float 1.5s ease-in-out infinite;
}

.media-input__overlay-text {
  font-size: 16px;
  font-weight: 600;
  color: var(--primary);
}

.media-input__overlay-subtext {
  font-size: 12px;
  color: var(--text-secondary);
}

/* Transition animations */
.drag-overlay-enter-active {
  animation: fade-in 0.2s ease-out;
}

.drag-overlay-leave-active {
  animation: fade-out 0.15s ease-in;
}

@keyframes fade-in {
  from {
    opacity: 0;
    transform: scale(0.95);
  }
  to {
    opacity: 1;
    transform: scale(1);
  }
}

@keyframes fade-out {
  from {
    opacity: 1;
    transform: scale(1);
  }
  to {
    opacity: 0;
    transform: scale(0.95);
  }
}

@keyframes bounce-in {
  0% {
    transform: scale(0.8);
    opacity: 0;
  }
  50% {
    transform: scale(1.05);
  }
  100% {
    transform: scale(1);
    opacity: 1;
  }
}

@keyframes float {
  0%, 100% {
    transform: translateY(0);
  }
  50% {
    transform: translateY(-8px);
  }
}

/* ── 文件夹投屏按钮 ── */
.media-input__folder-btn {
  margin-top: 10px;
  padding: 6px 14px;
  font-size: 12px;
  color: var(--text-secondary);
  background: var(--bg-surface);
  border: 1px dashed var(--border-color);
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s;
}
.media-input__folder-btn:hover {
  color: var(--accent);
  border-color: var(--accent);
}

.media-input__folder-row {
  display: flex;
  gap: 8px;
  justify-content: center;
  flex-wrap: wrap;
}
.media-input__folder-row .media-input__folder-btn {
  margin-top: 10px;
}

/* ── 投屏历史面板 ── */
.history-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-height: 60vh;
  overflow-y: auto;
}
.history-panel__hint {
  margin: 0;
  font-size: 12px;
  color: var(--text-secondary);
  text-align: center;
  padding: 16px 0;
}
.history-panel__group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.history-panel__group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.history-panel__device {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.history-panel__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #3ecf8e;
  flex: none;
}
.history-panel__dot--offline {
  background: var(--text-secondary);
  opacity: 0.5;
}
.history-panel__offline {
  font-weight: 400;
  color: var(--text-secondary);
}
.history-panel__clear {
  flex: none;
  padding: 2px 8px;
  font-size: 11px;
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  cursor: pointer;
}
.history-panel__clear:hover {
  color: var(--accent);
  border-color: var(--accent);
}
.history-panel__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.history-panel__item {
  display: flex;
  align-items: center;
  gap: 4px;
}
.history-panel__cast {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
  text-align: left;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s;
}
.history-panel__cast:hover:not(:disabled) {
  border-color: var(--accent);
}
.history-panel__cast:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.history-panel__icon {
  flex: none;
  font-size: 13px;
}
.history-panel__body {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.history-panel__title {
  font-size: 12px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.history-panel__meta {
  font-size: 11px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.history-panel__remove {
  flex: none;
  padding: 2px 6px;
  font-size: 11px;
  color: var(--text-secondary);
  background: transparent;
  border: none;
  cursor: pointer;
  border-radius: 4px;
}
.history-panel__remove:hover {
  color: var(--accent);
}


/* ── 播放队列面板 ── */
.queue-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 52vh;
}
.queue-panel__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}
.queue-panel__folder {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 60%;
}
.queue-panel__toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.queue-panel__chip {
  padding: 3px 8px;
  font-size: 11px;
  border-radius: 6px;
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
  color: var(--text-secondary);
  cursor: pointer;
}
.queue-panel__chip:hover {
  color: var(--accent);
  border-color: var(--accent);
}
.queue-panel__nowplaying {
  font-size: 11px;
  color: var(--accent);
  margin-left: auto;
}
.queue-panel__hint {
  font-size: 12px;
  color: var(--text-secondary);
  text-align: center;
  padding: 16px 0;
}
.queue-panel__list {
  list-style: none;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  border: 1px solid var(--border-color);
  border-radius: 8px;
}
.queue-panel__item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  font-size: 12px;
  border-bottom: 1px solid var(--border-color);
  cursor: grab;
}
.queue-panel__item:last-child {
  border-bottom: none;
}
.queue-panel__item--playing {
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}
.queue-panel__item--unselected {
  opacity: 0.45;
}
.queue-panel__item--dragging {
  opacity: 0.3;
}
.queue-panel__check {
  flex: none;
  cursor: pointer;
}
.queue-panel__index {
  flex: none;
  width: 20px;
  text-align: right;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}
.queue-panel__grip {
  flex: none;
  color: var(--text-secondary);
  cursor: grab;
}
.queue-panel__name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.queue-panel__ops {
  flex: none;
  display: flex;
  gap: 2px;
}
.queue-panel__ops button {
  width: 22px;
  height: 22px;
  padding: 0;
  font-size: 12px;
  line-height: 1;
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
  color: var(--text-secondary);
  border-radius: 5px;
  cursor: pointer;
}
.queue-panel__ops button:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--accent);
}
.queue-panel__ops button:disabled {
  opacity: 0.35;
  cursor: default;
}
</style>
