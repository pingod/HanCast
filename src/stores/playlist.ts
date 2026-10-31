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
/** 队列本体落盘：关掉程序再打开还在，不用重新挑一遍文件夹 */
const QUEUE_KEY = 'hancast.playlist.queue'

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

/** 落盘的队列形状。只存本地文件项——分享链的直链带签名、几小时就过期，存下来只会给用户一堆点不动的条目 */
interface PersistedQueue {
  items: QueueItem[]
  selected: string[]
  folder: string
  playMode: PlayMode
  stopAfterCurrent: boolean
}

function loadQueue(): PersistedQueue | null {
  try {
    const raw = localStorage.getItem(QUEUE_KEY)
    if (!raw) return null
    const obj = JSON.parse(raw)
    if (!obj || !Array.isArray(obj.items)) return null
    const items = obj.items.filter(
      (it: any): it is QueueItem =>
        !!it && typeof it.id === 'string' && !!it.media &&
        typeof it.media.uri === 'string' && it.media.media_type === 'file',
    )
    if (items.length === 0) return null
    return {
      items,
      selected: Array.isArray(obj.selected) ? obj.selected.filter((s: any) => typeof s === 'string') : [],
      folder: typeof obj.folder === 'string' ? obj.folder : '',
      playMode: (['sequential', 'shuffle', 'repeat-one'].includes(obj.playMode)
        ? obj.playMode : 'sequential') as PlayMode,
      stopAfterCurrent: obj.stopAfterCurrent === true,
    }
  } catch {
    return null
  }
}

function saveQueue(q: PersistedQueue) {
  try {
    // 只持久化本地文件项：url 直链会过期，存了也会变成死条目
    const items = q.items.filter((it) => it.media.media_type === 'file')
    localStorage.setItem(QUEUE_KEY, JSON.stringify({ ...q, items }))
  } catch {
    // 写不进去就只在内存里活着，队列是便利功能
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
  const restored = loadQueue()

  /** 当前队列（已按用户排序） */
  const items = ref<QueueItem[]>(restored?.items ?? [])
  /** 勾选参与播放的项 id 集合 */
  const selected = ref<Set<string>>(
    restored
      ? new Set(
          restored.selected.length
            ? restored.selected.filter((id) => restored.items.some((it) => it.id === id))
            : restored.items.map((it) => it.id),
        )
      : new Set(),
  )
  /** 正在投的是哪一项（用于控制器显示「第 n / N」与高亮） */
  const playingId = ref<string | null>(null)
  /** 队列来源文件夹，用于顺序记忆 */
  const folder = ref<string>(restored?.folder ?? '')
  const loading = ref(false)
  const error = ref<string | null>(null)

  const playMode = ref<PlayMode>(restored?.playMode ?? 'sequential')
  /** 播完当前这一项就停，不自动续下一集（睡前看完一集就够用） */
  const stopAfterCurrent = ref<boolean>(restored?.stopAfterCurrent ?? false)

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

  /** 把当前队列整体写盘（每次变更后调用） */
  function persist() {
    saveQueue({
      items: items.value,
      selected: Array.from(selected.value),
      folder: folder.value,
      playMode: playMode.value,
      stopAfterCurrent: stopAfterCurrent.value,
    })
  }

  function reset() {
    items.value = []
    selected.value = new Set()
    playingId.value = null
    folder.value = ''
    error.value = null
    persist()
  }

  function isSelected(id: string) {
    return selected.value.has(id)
  }

  function toggleSelected(id: string) {
    const next = new Set(selected.value)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    selected.value = next
    persist()
  }

  function selectAll() {
    selected.value = new Set(items.value.map((it) => it.id))
    persist()
  }

  function selectNone() {
    selected.value = new Set()
    persist()
  }

  function setPlayingId(id: string | null) {
    playingId.value = id
  }

  function setStopAfterCurrent(v: boolean) {
    stopAfterCurrent.value = v
    persist()
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

  /** 选文件夹：拉清单 → 用记忆顺序重排 → 全选（替换现有队列） */
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
      persist()
    } catch (err) {
      error.value = String(err)
      items.value = []
      selected.value = new Set()
    } finally {
      loading.value = false
    }
  }

  /** 把一批媒体追加到现有队列尾（不打断当前队列/正在播放的那一项） */
  function appendItems(list: MediaInfo[]) {
    if (!list.length) return 0
    const existing = new Set(items.value.map((it) => it.media.uri))
    const base = items.value.length
    const added: QueueItem[] = []
    let n = 0
    for (const m of list) {
      if (!m || !m.uri || existing.has(m.uri)) continue
      existing.add(m.uri)
      added.push({ id: makeId(m.uri, base + n), media: m })
      n++
    }
    if (added.length) {
      items.value = [...items.value, ...added]
      const sel = new Set(selected.value)
      for (const it of added) sel.add(it.id)
      selected.value = sel
      persist()
    }
    return added.length
  }

  /** 追加一个文件夹（不动现有队列：投完当前文件夹可以直接接着放另一个文件夹） */
  async function appendFolder(folderPath: string) {
    loading.value = true
    error.value = null
    try {
      const listing = await listFolderMedia(folderPath)
      const files = listing.files ?? []
      if (!folder.value) {
        folder.value = listing.folder || folderPath
      }
      const added = appendItems(files)
      if (items.value.length === 0) error.value = 'empty'
      return added
    } catch (err) {
      error.value = String(err)
      return 0
    } finally {
      loading.value = false
    }
  }

  /** 拖拽排序：把 from 移到 to 位置 */
  function moveItem(fromIndex: number, toIndex: number) {
    if (fromIndex === toIndex) return
    if (fromIndex < 0 || fromIndex >= items.value.length) return
    if (toIndex < 0 || toIndex >= items.value.length) return
    const arr = items.value.slice()
    const [moved] = arr.splice(fromIndex, 1)
    arr.splice(toIndex, 0, moved)
    items.value = arr
    rememberOrder()
    persist()
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
    persist()
  }

  function setPlayMode(mode: PlayMode) {
    playMode.value = mode
    persist()
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
    stopAfterCurrent,
    playableItems,
    total,
    playingIndex,
    hasQueue,
    reset,
    isSelected,
    toggleSelected,
    selectAll,
    selectNone,
    setPlayingId,
    setStopAfterCurrent,
    loadFolder,
    appendItems,
    appendFolder,
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
