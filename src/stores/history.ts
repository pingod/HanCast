import { ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * 投屏历史（按设备分组）。
 *
 * 动机：电视只有一个「最近投的」记忆，而用户手上常常有好几台设备，
 * 想给某台重投上次那个文件夹/文件时无从查起——所以历史按设备记，
 * 每条都能一键重投到当初那台设备上。
 *
 * 只记「谁、投了什么、什么时候」，绝不记解析出来的直链：
 * 分享链的直链带签名且几小时就过期，存下来只会给用户一个点不动的条目。
 */
export type HistoryKind = 'file' | 'url' | 'queue'

export interface HistoryEntry {
  /** `${deviceId}::${uri}`，同一设备同一 URI 只保留最新一次 */
  id: string
  deviceId: string
  deviceName: string
  uri: string
  title: string
  mime_type: string
  thumbnail?: string | null
  kind: HistoryKind
  /** kind 为 queue 时的来源文件夹 */
  folder?: string
  /** kind 为 queue 时的参与播放项数 */
  count?: number
  /** kind 为 file 时本地文件的原始路径：重投要走「重新解析」而不是拿旧直链再投一次 */
  filePath?: string
  /** kind 为 url 时的解析来源页：分享链的直链带签名、几小时就过期，重投要回到来源页重新解 */
  sourceUrl?: string
  /** 投过几次（同一设备同一 uri 重复投累加，界面上用来排序/提示） */
  castCount?: number
  /** 毫秒时间戳（最近一次投放） */
  at: number
}

const STORAGE_KEY = 'hancast.cast.history'
/** 单台设备最多留多少条 */
const PER_DEVICE_MAX = 20
/** 全局上限（设备很多时的兜底） */
const TOTAL_MAX = 200

function load(): HistoryEntry[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr)) return []
    return arr.filter((e): e is HistoryEntry =>
      !!e && typeof e.id === 'string' && typeof e.deviceId === 'string' && typeof e.uri === 'string'
    )
  } catch {
    // 受限 WebView 下 storage 可能抛错；历史是便利功能，不能让它打断投屏
    return []
  }
}

function save(list: HistoryEntry[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(list))
  } catch {
    // 同上：写不进去就只在内存里活着
  }
}

function entryId(deviceId: string, uri: string) {
  return `${deviceId}::${uri}`
}

export const useHistoryStore = defineStore('history', () => {
  /** 全部历史，按时间倒序 */
  const entries = ref<HistoryEntry[]>(load())

  /** 某台设备的历史（时间倒序） */
  function forDevice(deviceId: string): HistoryEntry[] {
    return entries.value.filter((e) => e.deviceId === deviceId)
  }

  /** 有过投放记录的设备 id，按最近一次投放时间倒序 */
  function deviceIdsByRecency(): string[] {
    const seen: string[] = []
    for (const e of entries.value) {
      if (!seen.includes(e.deviceId)) seen.push(e.deviceId)
    }
    return seen
  }

  function record(entry: Omit<HistoryEntry, 'id' | 'castCount'>) {
    const id = entryId(entry.deviceId, entry.uri)
    const prev = entries.value.find((e) => e.id === id)
    // 同一设备同一 URI 再投一次：只把条目提到最前、次数 +1，不产生重复行
    const castCount = (prev?.castCount ?? 0) + 1
    const rest = entries.value.filter((e) => e.id !== id)
    const next = [{ ...entry, id, castCount }, ...rest].sort((a, b) => b.at - a.at)
    // 先按设备截断，再按全局截断
    const perDevice = new Map<string, number>()
    const capped: HistoryEntry[] = []
    for (const e of next) {
      const n = (perDevice.get(e.deviceId) ?? 0) + 1
      if (n > PER_DEVICE_MAX) continue
      perDevice.set(e.deviceId, n)
      capped.push(e)
      if (capped.length >= TOTAL_MAX) break
    }
    entries.value = capped
    save(capped)
  }

  function removeEntry(id: string) {
    entries.value = entries.value.filter((e) => e.id !== id)
    save(entries.value)
  }

  /** deviceId 省略时清空全部 */
  function clear(deviceId?: string) {
    entries.value = deviceId
      ? entries.value.filter((e) => e.deviceId !== deviceId)
      : []
    save(entries.value)
  }

  return { entries, forDevice, deviceIdsByRecency, record, removeEntry, clear }
})
