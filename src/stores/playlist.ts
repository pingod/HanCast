import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import type { MediaInfo } from '@/types/media'
import { listFolderMedia } from '@/api/commands'

/** 队列里的一项：媒体信息 + 是否勾选（不勾的不参与播放） */
export interface QueueItem {
  id: string
  media: MediaInfo
}

export type PlayMode = 'sequential' | 'shuffle' | 'repeat-one'

/** 顺序记忆落盘：{ [folder]: 有序的 uri 列表 } */
const ORDER_KEY = 'hancast.playlist.order'

function loadOrderMap(): Record<string, string[]> {
  try {
    const raw = localStorage.getItem(ORDER_KEY)
    if (!raw) return {}
    const obj = JSON.parse(raw)
    return obj && typeof obj === 'object' ? obj : {}
  } catch {
    return {}
  }
}

function saveOrderMap(map: Record<string, string[]>) {
  try {
    localStorage.setItem(ORDER_KEY, JSON.stringify(map))
  } catch {
    // 受限 WebView 下 storage 可能抛错，顺序记忆是锦上添花，不能让它打断投屏
  }
}

/** 稳定的项 id：同一文件在同一队列里恒等，便于拖拽/删除定位 */
function makeId(uri: string, index: number): string {
  let h = 0
  for (let i = 0; i < uri.length; i++) {
    h = (h * 31 + uri.charCodeAt(i)) | 0
  }
  return `${(h >>> 0).toString(36)}-${index}`
}

export const usePlaylistStore = defineStore('playlist', () => {
  /** 当前队列（已按用户排序） */
  const items = ref<QueueItem[]>([])
  /** 勾选参与播放的项 id 集合 */
  const selected = ref<Set<string>>(new Set())
  /** 正在投的是哪一项（用于控制器显示「第 n / N」与高亮） */
  const playingId = ref<string | null>(null)
  /** 队列来源文件夹，用于顺序记忆 */
  const folder = ref<string>('')
  const loading = ref(false)
  const error = ref<string | null>(null)

  const playMode = ref<PlayMode>('sequential')

  /** 参与播放的项（按队列顺序、已勾选） */
  const playableItems = computed(() =>
    items.value.filter((it) => selected.value.has(it.id))
  )
  const total = computed(() => playableItems.value.length)
  /** 1-based 序号，0 表示不在队列中 */
  const playingIndex = computed(() =>
    playingId.value ? playableItems.value.findIndex((it) => it.id === playingId.value) + 1 : 0
  )
  const hasQueue = computed(() => items.value.length > 0)

  function reset() {
    items.value = []
    selected.value = new Set()
    playingId.value = null
    folder.value = ''
    error.value = null
  }

  function isSelected(id: string) {
    return selected.value.has(id)
  }

  function toggleSelected(id: string) {
    const next = new Set(selected.value)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    selected.value = next
  }

  function selectAll() {
    selected.value = new Set(items.value.map((it) => it.id))
  }

  function selectNone() {
    selected.value = new Set()
  }

  /** 把顺序记忆写进 localStorage（当前文件夹） */
  function rememberOrder() {
    if (!folder.value) return
    const map = loadOrderMap()
    map[folder.value] = items.value.map((it) => it.media.uri)
    saveOrderMap(map)
  }

  /** 按上次记住的顺序重排新读到的文件；没记过就保持后端给的（自然序） */
  function applyRememberedOrder(fileList: MediaInfo[]): MediaInfo[] {
    const remembered = loadOrderMap()[folder.value]
    if (!remembered || remembered.length === 0) return fileList
    const byUri = new Map(fileList.map((m) => [m.uri, m]))
    const ordered: MediaInfo[] = []
    for (const uri of remembered) {
      const m = byUri.get(uri)
      if (m) {
        ordered.push(m)
        byUri.delete(uri)
      }
    }
    // 记住的顺序里没覆盖到的新文件，按后端自然序补在后面
    for (const m of fileList) {
      if (byUri.has(m.uri)) ordered.push(m)
    }
    return ordered
  }

  /** 选文件夹：拉清单 → 用记忆顺序重排 → 全选 */
  async function loadFolder(folderPath: string) {
    loading.value = true
    error.value = null
    try {
      const listing = await listFolderMedia(folderPath)
      folder.value = listing.folder || folderPath
      const ordered = applyRememberedOrder(listing.files ?? [])
      items.value = ordered.map((m, i) => ({ id: makeId(m.uri, i), media: m }))
      selected.value = new Set(items.value.map((it) => it.id))
      playingId.value = null
      if (items.value.length === 0) {
        error.value = 'empty'
      }
    } catch (err) {
      error.value = String(err)
      items.value = []
      selected.value = new Set()
    } finally {
      loading.value = false
    }
  }

  /** 拖拽排序：把 from 移到 to 位置（Vue Draggable 的 oldIndex/newIndex） */
  function moveItem(fromIndex: number, toIndex: number) {
    if (fromIndex === toIndex) return
    if (fromIndex < 0 || fromIndex >= items.value.length) return
    if (toIndex < 0 || toIndex >= items.value.length) return
    const arr = items.value.slice()
    const [moved] = arr.splice(fromIndex, 1)
    arr.splice(toIndex, 0, moved)
    items.value = arr
    rememberOrder()
  }

  /** 上移/下移（键盘可达的排序手段） */
  function moveUp(id: string) {
    const i = items.value.findIndex((it) => it.id === id)
    if (i > 0) moveItem(i, i - 1)
  }
  function moveDown(id: string) {
    const i = items.value.findIndex((it) => it.id === id)
    if (i >= 0 && i < items.value.length - 1) moveItem(i, i + 1)
  }

  function removeItem(id: string) {
    items.value = items.value.filter((it) => it.id !== id)
    const next = new Set(selected.value)
    next.delete(id)
    selected.value = next
    if (playingId.value === id) playingId.value = null
    rememberOrder()
  }

  function setPlayMode(mode: PlayMode) {
    playMode.value = mode
  }

  /**
   * 当前项播完后该投哪一项，返回 null 表示队列结束（不自动续播）。
   * - repeat-one：重复当前项
   * - shuffle：随机挑一项「不是当前」的（只有一项就重复它）
   * - sequential：按顺序下一项
   */
  function nextItem(): QueueItem | null {
    const list = playableItems.value
    if (list.length === 0) return null
    const cur = playingId.value
    const idx = cur ? list.findIndex((it) => it.id === cur) : -1
    if (playMode.value === 'repeat-one') {
      return idx >= 0 ? list[idx] : list[0]
    }
    if (playMode.value === 'shuffle') {
      if (list.length === 1) return list[0]
      const others = list.filter((it) => it.id !== cur)
      return others[Math.floor(Math.random() * others.length)] ?? null
    }
    // sequential
    if (idx < 0) return list[0]
    if (idx + 1 < list.length) return list[idx + 1]
    return null
  }

  /** 手动切下一项（控制器「下一集」按钮）；到底则回环到第一项 */
  function manualNext(): QueueItem | null {
    const list = playableItems.value
    if (list.length === 0) return null
    const cur = playingId.value
    const idx = cur ? list.findIndex((it) => it.id === cur) : -1
    const nextIdx = idx + 1 >= list.length ? 0 : idx + 1
    return list[nextIdx]
  }

  function manualPrev(): QueueItem | null {
    const list = playableItems.value
    if (list.length === 0) return null
    const cur = playingId.value
    const idx = cur ? list.findIndex((it) => it.id === cur) : 0
    const prevIdx = idx - 1 < 0 ? list.length - 1 : idx - 1
    return list[prevIdx]
  }

  return {
    items,
    selected,
    playingId,
    folder,
    loading,
    error,
    playMode,
    playableItems,
    total,
    playingIndex,
    hasQueue,
    reset,
    isSelected,
    toggleSelected,
    selectAll,
    selectNone,
    loadFolder,
    moveItem,
    moveUp,
    moveDown,
    removeItem,
    setPlayMode,
    nextItem,
    manualNext,
    manualPrev,
    rememberOrder,
  }
})
