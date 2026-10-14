import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import type { MediaInfo, MediaInputState } from '@/types/media'
import { parseMediaFile, resolveShare } from '@/api/commands'

/** 从文本中提取第一个 http(s) 地址（后端解析失败时的本地降级） */
function extractFirstUrl(text: string): string | null {
  const m = text.match(/https?:\/\/[^\s"'<>，。；！？、【】]+/i)
  if (!m) return null
  return m[0].replace(/[),.;!?'"”]+$/, '') || null
}

/** Infer MIME type from filename extension */
function inferMime(name: string): string {
  const ext = name.split('.').pop()?.toLowerCase() ?? ''
  const map: Record<string, string> = {
    mp4: 'video/mp4', mkv: 'video/x-matroska', avi: 'video/x-msvideo',
    mov: 'video/quicktime', webm: 'video/webm', flv: 'video/x-flv',
    mp3: 'audio/mpeg', flac: 'audio/flac', wav: 'audio/wav',
    aac: 'audio/aac', ogg: 'audio/ogg', m4a: 'audio/mp4',
    jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
    gif: 'image/gif', webp: 'image/webp', bmp: 'image/bmp',
  }
  return map[ext] ?? 'application/octet-stream'
}

export const useMediaStore = defineStore('media', () => {
  const state = ref<MediaInputState>('idle')
  const mediaInfo = ref<MediaInfo | null>(null)
  const urlInput = ref('')
  const error = ref<string | null>(null)

  const isReady = computed(() => state.value === 'ready' && mediaInfo.value !== null)
  const isParsing = computed(() => state.value === 'parsing')

  function reset() {
    state.value = 'idle'
    mediaInfo.value = null
    urlInput.value = ''
    error.value = null
    localFile.value = null
  }

  function setDragOver() {
    if (state.value === 'idle') {
      state.value = 'dragover'
    }
  }

  function clearDragOver() {
    if (state.value === 'dragover') {
      state.value = 'idle'
    }
  }

  function setInputChange(value: string) {
    urlInput.value = value
    if (value.trim()) {
      state.value = 'input'
    } else if (state.value === 'input') {
      state.value = 'idle'
    }
  }

  /** Store the browser File object so the component can create preview URLs */
  const localFile = ref<File | null>(null)

  async function parseFile(filePathOrFile: string | File) {
    state.value = 'parsing'
    error.value = null
    try {
      if (typeof filePathOrFile === 'string') {
        // Tauri mode: send path to backend
        mediaInfo.value = await parseMediaFile(filePathOrFile)
        localFile.value = null
      } else {
        // Browser mode: extract info from File object directly
        const file = filePathOrFile
        localFile.value = file
        mediaInfo.value = {
          media_type: 'file',
          uri: file.name,
          title: file.name,
          mime_type: file.type || inferMime(file.name),
          file_size: file.size,
          duration: null,
          thumbnail: null,
        }
      }
      state.value = 'ready'
    } catch (err) {
      error.value = String(err)
      state.value = 'idle'
    }
  }

  /**
   * 解析粘贴内容：支持纯链接、B站/抖音/小红书分享口令文本、短链。
   * 后端 resolve_share 负责文本提取 + 短链展开 + 页面直链解析；
   * 失败且能提取出链接时降级为透传原始地址（沿用旧行为）。
   */
  async function parseUrl(input?: string, quality = '') {
    const raw = (input ?? urlInput.value).trim()
    if (!raw) return

    state.value = 'parsing'
    error.value = null
    try {
      const info = await resolveShare(raw, quality)
      if (!info.uri) throw new Error('解析结果为空')
      mediaInfo.value = {
        media_type: 'url',
        uri: info.uri,
        title: info.title || raw,
        mime_type: info.mime_type ?? '',
        file_size: info.file_size ?? null,
        duration: info.duration ?? null,
        thumbnail: info.thumbnail ?? null,
        qualities: info.qualities ?? [],
        quality: info.quality ?? quality,
        // 切换画质要拿它再解一次（口令里的短链已展开，重解更快）
        source_url: info.source_url || raw,
      }
      // 输入框回显提取出的干净地址，便于用户确认投屏目标
      if (info.source_url) urlInput.value = info.source_url
      state.value = 'ready'
      return
    } catch (err) {
      const fallbackUrl = extractFirstUrl(raw)
      if (!fallbackUrl) {
        error.value = String(err)
        state.value = 'idle'
        return
      }
      // 后端解析失败（站点防抓取/无流媒体/无后端）：本地按扩展名推断后透传
      const pathParts = fallbackUrl.split('?')[0].split('/')
      const title = decodeURIComponent(pathParts[pathParts.length - 1] || '') || fallbackUrl
      const ext = title.split('.').pop()?.toLowerCase() ?? ''
      const mimeMap: Record<string, string> = {
        mp4: 'video/mp4', mkv: 'video/x-matroska', avi: 'video/x-msvideo',
        mov: 'video/quicktime', webm: 'video/webm', flv: 'video/x-flv',
        m3u8: 'application/vnd.apple.mpegurl',
        mp3: 'audio/mpeg', flac: 'audio/flac', wav: 'audio/wav',
        aac: 'audio/aac', ogg: 'audio/ogg', m4a: 'audio/mp4',
        jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
        gif: 'image/gif', webp: 'image/webp', bmp: 'image/bmp',
      }

      mediaInfo.value = {
        media_type: 'url',
        uri: fallbackUrl,
        title,
        mime_type: mimeMap[ext] ?? '',
        file_size: null,
        duration: null,
        thumbnail: null,
      }
      state.value = 'ready'
    }
  }

  /** Update thumbnail on current media info (used after video frame extraction) */
  function setThumbnail(thumb: string | null) {
    if (mediaInfo.value) {
      mediaInfo.value = { ...mediaInfo.value, thumbnail: thumb }
    }
  }

  return {
    state,
    mediaInfo,
    localFile,
    urlInput,
    error,
    isReady,
    isParsing,
    reset,
    setDragOver,
    clearDragOver,
    setInputChange,
    parseFile,
    parseUrl,
    setThumbnail,
  }
})
