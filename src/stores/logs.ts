import { ref, computed } from 'vue'
import { defineStore } from 'pinia'
import { getLogs, setLogLevel } from '@/api/commands'
import type { LogLine, LogModule } from '@/types/logs'

// 打开面板时的自动追日志间隔：排错时要看到"刚发生了什么"而不是历史快照
const TAIL_INTERVAL = 3000

export const useLogsStore = defineStore('logs', () => {
  const showPanel = ref(false)
  const lines = ref<LogLine[]>([])
  const modules = ref<LogModule[]>([])
  const total = ref(0)
  const truncated = ref(false)
  const path = ref('')
  const level = ref('INFO')
  const loading = ref(false)
  const error = ref<string | null>(null)

  // 过滤条件
  const module = ref('')
  const filterLevel = ref('')
  const keyword = ref('')
  const includeOlder = ref(false)
  const limit = ref(400)

  let tailTimer: number | undefined
  let pendingRefresh = false

  const debugOn = computed(() => level.value === 'DEBUG')

  async function refresh() {
    // 追日志和改过滤条件可能撞车：丢掉请求会让界面停留在旧过滤结果上，排队重跑
    if (loading.value) {
      pendingRefresh = true
      return
    }
    loading.value = true
    error.value = null
    try {
      const page = await getLogs({
        limit: limit.value,
        module: module.value || undefined,
        level: filterLevel.value || undefined,
        keyword: keyword.value.trim() || undefined,
        include_older: includeOlder.value,
      })
      lines.value = page.lines
      modules.value = page.modules
      total.value = page.total
      truncated.value = page.truncated
      path.value = page.path
      level.value = page.level
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
    } finally {
      loading.value = false
      if (pendingRefresh) {
        pendingRefresh = false
        refresh()
      }
    }
  }

  async function toggleDebug(on: boolean) {
    try {
      const result = await setLogLevel(on ? 'DEBUG' : 'INFO')
      level.value = result.level
      await refresh()
      return true
    } catch (err) {
      error.value = err instanceof Error ? err.message : String(err)
      return false
    }
  }

  function startTail() {
    stopTail()
    tailTimer = window.setInterval(() => {
      if (document.visibilityState !== 'hidden') refresh()
    }, TAIL_INTERVAL)
  }

  function stopTail() {
    if (tailTimer !== undefined) {
      window.clearInterval(tailTimer)
      tailTimer = undefined
    }
  }

  function open() {
    showPanel.value = true
    refresh().then(startTail)
  }

  function close() {
    showPanel.value = false
    stopTail()
  }

  return {
    showPanel,
    lines,
    modules,
    total,
    truncated,
    path,
    level,
    loading,
    error,
    module,
    filterLevel,
    keyword,
    includeOlder,
    limit,
    debugOn,
    refresh,
    toggleDebug,
    startTail,
    stopTail,
    open,
    close,
  }
})
