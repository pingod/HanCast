import { ref, computed, onMounted, onUnmounted } from 'vue'
import { defineStore } from 'pinia'
import type { CastState, CastSession } from '@/types/cast'
import { createSession } from '@/types/cast'
import type { MediaQuality } from '@/types/media'
import type { UnlistenFn } from '@tauri-apps/api/event'
import {
  getCastState,
  getCastUrl,
  startCast as apiStartCast,
  stopCast as apiStopCast,
  pauseCast as apiPauseCast,
  resumeCast as apiResumeCast,
  seekCast as apiSeekCast,
  setVolume as apiSetVolume,
  setMute as apiSetMute,
  getAutostart,
  resolveShare,
} from '@/api/commands'
import type { StopWarning } from '@/api/commands'
import { notify } from '@/api/commands'
import { tt } from '@/i18n'
import { useDeviceStore } from '@/stores/device'

/** Parse "HH:MM:SS" to total seconds */
function parseTimeToSeconds(time: string): number {
  if (!time) return 0
  const parts = time.split(':').map(Number)
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2]
  if (parts.length === 2) return parts[0] * 60 + parts[1]
  return parts[0] || 0
}

export const useCastStore = defineStore('cast', () => {
  // ── Multi-device sessions ──
  const sessions = ref<Record<string, CastSession>>({})
  const focusedDeviceId = ref<string | null>(null)
  const loading = ref(false)
  const lastError = ref<string | null>(null)
  let pollTimer: ReturnType<typeof setInterval> | null = null
  let pollInFlight = false
  let pollEpoch = 0

  // Each playback operation replaces the device's token. Polls issued before it
  // (or during it) cannot write back, even if the same device is stopped/recast.
  const deviceOperations = new Map<string, { pending: boolean }>()
  function beginOperation(deviceId: string) {
    const operation = { pending: true }
    deviceOperations.set(deviceId, operation)
    return operation
  }
  function isCurrentOperation(deviceId: string, operation: { pending: boolean }) {
    return deviceOperations.get(deviceId) === operation
  }
  function dismissError() {
    lastError.value = null
  }

  /** 设备名：首次投屏失败时本地还没有会话，只能去设备表里查 */
  function deviceLabel(deviceId: string) {
    return sessions.value[deviceId]?.device_name
      ?? useDeviceStore().devices.find((d) => d.id === deviceId)?.name
      ?? deviceId
  }

  /** 投屏失败要发系统通知：窗口收进托盘时，界面里的红条没人看得见 */
  function alertDevice(title: string, body: string) {
    void notify(title, body)
  }

  // ── Progress simulation timers ──
  // Key: deviceId, Value: timer that increments position every second
  const simTimers: Record<string, ReturnType<typeof setInterval>> = {}

  // ── Embedded controller modal state ──
  const showController = ref(false)

  // 「点了停止但电视没确认收到 Stop」的设备与原因，由界面翻译成一句话展示
  const stopWarning = ref<StopWarning | null>(null)

  // 投屏中切换画质进行中（重新解析 + 重新投放要几秒，期间锁住选择器）
  const qualitySwitching = ref(false)

  // ── Autostart prompt state ──
  const showAutostartPrompt = ref(false)

  // ── Computed: active session helpers ──
  const activeDeviceIds = computed(() =>
    Object.keys(sessions.value).filter(id =>
      ['connecting', 'playing', 'paused'].includes(sessions.value[id].status)
    )
  )

  const isCasting = computed(() => activeDeviceIds.value.length > 0)

  /** Get the session currently shown in the controller */
  const focusedSession = computed<CastSession | null>(() => {
    const id = focusedDeviceId.value
    return id ? (sessions.value[id] ?? null) : null
  })

  /** Backward-compat: castState looks like the old single-device state */
  const castState = computed<CastState>(() => {
    const s = focusedSession.value
    if (s) return {
      status: s.status,
      device_id: s.device_id,
      device_name: s.device_name,
      media: s.media,
      position: s.position,
      volume: s.volume,
      is_muted: s.is_muted,
      positionTime: s.positionTime,
      durationTime: s.durationTime,
    }
    return {
      status: 'idle',
      device_id: null,
      media: null,
      position: 0,
      volume: 80,
      is_muted: false,
      positionTime: '00:00:00',
      durationTime: '00:00:00',
    }
  })

  const isIdle = computed(() => castState.value.status === 'idle')
  const isPlaying = computed(() => castState.value.status === 'playing')
  const durationSeconds = computed(() => parseTimeToSeconds(castState.value.durationTime))
  const positionSeconds = computed(() => castState.value.position)

  // ── Session helpers ──
  function getSession(deviceId: string): CastSession | undefined {
    return sessions.value[deviceId]
  }

  function updateSession(deviceId: string, updates: Partial<CastSession>) {
    const existing = sessions.value[deviceId]
    if (!existing) return
    sessions.value = {
      ...sessions.value,
      [deviceId]: { ...existing, ...updates },
    }
  }

  function removeSession(deviceId: string) {
    deviceOperations.set(deviceId, { pending: false })
    stopSim(deviceId)
    const { [deviceId]: _, ...rest } = sessions.value
    sessions.value = rest
    if (focusedDeviceId.value === deviceId) {
      // User explicitly stopped this device → clear focus and hide controller
      // Do NOT auto-switch to another device; user can manually open another controller
      focusedDeviceId.value = null
      showController.value = false
    }
  }

  // ── Local state updates (for immediate visual feedback) ──
  function setLocalVolume(volume: number) {
    const id = focusedDeviceId.value
    if (id) updateSession(id, { volume })
  }

  // ── Controller ──
  function openController(deviceId?: string) {
    if (deviceId) focusedDeviceId.value = deviceId
    showController.value = true
  }

  function minimizeController() {
    showController.value = false
  }

  function restoreController(deviceId?: string) {
    if (deviceId) focusedDeviceId.value = deviceId
    showController.value = true
  }

  // ── Fetch state (on mount) ──
  const unlistenFns: UnlistenFn[] = []
  let disposed = false
  function registerUnlisten(fn: UnlistenFn) {
    // Native registration may finish after the root component has unmounted.
    if (disposed) fn()
    else unlistenFns.push(fn)
  }

  async function fetchState() {
    const snapshot = new Map([...deviceOperations].filter(([, op]) => !op.pending))
    try {
      const state = await getCastState() as any
      if (disposed) return false
      // 后端无 device_id 参数时返回 { sessions: {device_id: state}, current: state }
      // （sessions 包含所有设备的 CastState，current 是主渲染器的兼容状态）
      // 恢复所有非 idle 的活跃会话，避免应用重启后丢失正在投屏的会话。
      const sessionMap: Record<string, any> = state?.sessions ?? {}
      let restored = false
      for (const [deviceId, s] of Object.entries(sessionMap)) {
        if (!s || s.status === 'idle') continue
        const operation = deviceOperations.get(deviceId)
        if (operation?.pending || operation !== snapshot.get(deviceId)) continue
        // Skip if a session with this device_id is already 'connecting'
        // (i.e. startCast already created it and is still awaiting apiStartCast)
        const existing = sessions.value[deviceId]
        if (existing && existing.status === 'connecting') continue

        // 后端 position/duration 是 "HH:MM:SS" 字符串；
        // position 字段存秒数（number），positionTime 存字符串用于显示
        const positionStr = s.position ?? '00:00:00'
        sessions.value = {
          ...sessions.value,
          [deviceId]: {
            device_id: deviceId,
            device_name: s.device_name ?? deviceId,
            status: s.status,
            media: s.media ?? null,
            position: parseTimeToSeconds(positionStr),
            volume: s.volume ?? 80,
            is_muted: s.is_muted ?? false,
            positionTime: positionStr,
            durationTime: s.duration ?? '00:00:00',
          },
        }
        if (!focusedDeviceId.value) focusedDeviceId.value = deviceId
        restored = true
      }
      return restored
    } catch (err) {
      console.error('Failed to get cast state:', err)
      return false
    }
  }

  // ── Polling (one in-flight sweep, including across stop/restart) ──
  function startPolling() {
    if (disposed || pollTimer) return
    const epoch = ++pollEpoch
    pollTimer = setInterval(() => { void pollSessions(epoch) }, 1000)
  }

  async function pollSessions(epoch: number) {
    if (pollInFlight || epoch !== pollEpoch) return
    const ids = activeDeviceIds.value
    if (ids.length === 0) {
      stopPolling()
      return
    }
    pollInFlight = true
    const statusMap: Record<string, CastSession['status']> = {
      PLAYING: 'playing',
      PAUSED: 'paused',
      STOPPED: 'stopped',
      IDLE: 'idle',
    }
    try {
      for (const id of ids) {
        if (epoch !== pollEpoch) break
        const operation = deviceOperations.get(id)
        if (operation?.pending || !sessions.value[id]) continue
        try {
          const info = await getCastUrl(id)
          const session = sessions.value[id]
          if (!session || epoch !== pollEpoch || operation !== deviceOperations.get(id)) continue
          // 后端带 device_id 返回小写（playing/idle）、不带返回 DLNA 大写，统一归一
          const newStatus = statusMap[info.status.toUpperCase()] ?? session.status
          const updates: Partial<CastSession> = {}

          // Never downgrade playing/paused→stopped from polling alone;
          // events remain the source of truth for status.
          if (newStatus !== session.status && !(session.status === 'playing' || session.status === 'paused')) {
            updates.status = newStatus
          }

          const posSec = info.position ? parseTimeToSeconds(info.position) : 0
          const durSec = info.duration ? parseTimeToSeconds(info.duration) : 0
          const pollingDataInvalid = posSec === 0 && durSec === 0 && (session.status === 'playing' || session.status === 'paused')
          if (!pollingDataInvalid) {
            if (info.position) {
              // A fresh sample is authoritative, including legitimate backward seeks.
              updates.position = posSec
              updates.positionTime = formatSecondsToTime(posSec)
            }
            if (info.duration) updates.durationTime = info.duration
          }
          if (Object.keys(updates).length > 0) updateSession(id, updates)
        } catch {
          // Device may have gone offline; finally still releases the single-flight guard.
        }
      }
    } finally {
      pollInFlight = false
    }
  }

  function stopPolling() {
    ++pollEpoch
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  // ── Progress simulation (frontend-side position increment) ──
  function startSim(deviceId: string) {
    // Already simulating for this device
    if (simTimers[deviceId]) return
    simTimers[deviceId] = setInterval(() => {
      const session = sessions.value[deviceId]
      if (!session || session.status !== 'playing') {
        stopSim(deviceId)
        return
      }
      // Increment position by 1 second
      const newPosition = session.position + 1
      // Cap at duration if known
      const dur = parseTimeToSeconds(session.durationTime)
      if (dur > 0 && newPosition >= dur) {
        stopSim(deviceId)
        return
      }
      updateSession(deviceId, {
        position: newPosition,
        positionTime: formatSecondsToTime(newPosition),
      })
    }, 1000)
  }

  function stopSim(deviceId: string) {
    if (simTimers[deviceId]) {
      clearInterval(simTimers[deviceId])
      delete simTimers[deviceId]
    }
  }

  function stopAllSim() {
    for (const id of Object.keys(simTimers)) {
      clearInterval(simTimers[id])
      delete simTimers[id]
    }
  }

  /** Format total seconds back to "HH:MM:SS" */
  function formatSecondsToTime(seconds: number): string {
    const h = Math.floor(seconds / 3600)
    const m = Math.floor((seconds % 3600) / 60)
    const s = Math.floor(seconds % 60)
    const hh = String(h).padStart(2, '0')
    const mm = String(m).padStart(2, '0')
    const ss = String(s).padStart(2, '0')
    return `${hh}:${mm}:${ss}`
  }

  // ── Cast actions ──
  async function startCast(deviceId: string, mediaUri: string, mediaInfo?: {
    title?: string
    mime_type?: string
    thumbnail?: string | null
    qualities?: MediaQuality[]
    quality?: string
    source_url?: string
  }) {
    loading.value = true
    stopWarning.value = null
    dismissError()
    const operation = beginOperation(deviceId)
    stopSim(deviceId)
    try {
      await apiStartCast(deviceId, mediaUri, mediaInfo?.mime_type)
      if (!isCurrentOperation(deviceId, operation)) return
      // Preserve existing session's volume/mute if restarting on same device
      const existing = sessions.value[deviceId]
      sessions.value = {
        ...sessions.value,
        [deviceId]: {
          ...createSession(deviceId, existing?.device_name ?? deviceId),
          status: 'playing', // Set playing immediately — don't wait for event
          volume: existing?.volume ?? 80,
          is_muted: existing?.is_muted ?? false,
          media: {
            media_type: 'url',
            uri: mediaUri,
            title: mediaInfo?.title ?? mediaUri.split('/').pop() ?? mediaUri,
            mime_type: mediaInfo?.mime_type ?? '',
            file_size: null,
            duration: null,
            thumbnail: mediaInfo?.thumbnail ?? null,
            qualities: mediaInfo?.qualities ?? [],
            quality: mediaInfo?.quality ?? '',
            source_url: mediaInfo?.source_url ?? '',
          },
        },
      }
      focusedDeviceId.value = deviceId
      startPolling()
      openController(deviceId)
      // Start progress simulation immediately — don't wait for cast_state_changed event
      // The device will start playing very soon after apiStartCast succeeds
      startSim(deviceId)

      // Check if we should prompt for autostart (first successful cast)
      checkAutostartPrompt()
    } catch (err) {
      if (isCurrentOperation(deviceId, operation)) {
        lastError.value = String(err)
        alertDevice(tt('notify.castFailed'), `${deviceLabel(deviceId)}：${lastError.value}`)
        if (sessions.value[deviceId]) updateSession(deviceId, { status: 'error' })
      }
      console.error('Failed to start cast:', err)
    } finally {
      operation.pending = false
      loading.value = false
    }
  }

  /** Check if we should show the autostart prompt (first successful cast) */
  async function checkAutostartPrompt() {
    try {
      // Storage access can throw in restricted WebViews too.
      const hasAsked = localStorage.getItem('has_asked_autostart')
      if (hasAsked) return
      const isEnabled = await getAutostart()
      if (!isEnabled) {
        // Delay 6s to let the controller modal appear first
        setTimeout(() => {
          showAutostartPrompt.value = true
        }, 6000)
      } else {
        // Already enabled, mark as asked to avoid future checks
        localStorage.setItem('has_asked_autostart', 'true')
      }
    } catch (err) {
      console.error('Failed to check autostart status:', err)
    }
  }

  async function stopCast(deviceId?: string) {
    loading.value = true
    stopWarning.value = null
    dismissError()
    // Include pending first casts, which do not have a session yet.
    const ids = deviceId ? [deviceId] : [...new Set([
      ...Object.keys(sessions.value),
      ...[...deviceOperations].filter(([, op]) => op.pending).map(([id]) => id),
    ])]
    const operations = new Map(ids.map(id => [id, beginOperation(id)]))
    ids.forEach(stopSim)
    const names = ids.map((id) => sessions.value[id]?.device_name ?? id)
    let failed: string[] = []
    let reasons: string[] = []
    try {
      const res = await apiStopCast(deviceId)
      failed = res?.control_failed ?? []
      const map = res?.reasons ?? {}
      reasons = failed.map((id) => map[id] ?? '')
    } catch (err) {
      // 命令层本身失败（sidecar 超时/异常）时无法确认电视收到 Stop
      console.error('Failed to stop cast:', err)
      failed = ids
      reasons = ids.map(() => String(err))
    } finally {
      // 本地会话一律先释放：留着它只会让界面停在「正在投屏」，
      // 而这正是用户抱怨的「怎么点都停不掉」
      for (const [id, operation] of operations) {
        operation.pending = false
        // A new cast may already have started while Stop was awaiting the device.
        if (isCurrentOperation(id, operation)) removeSession(id)
      }
      if (activeDeviceIds.value.length === 0) {
        stopPolling()
        showController.value = false
      }
      if (failed.length) {
        const who = failed.map((id) => names[ids.indexOf(id)] ?? id)
        stopWarning.value = { devices: who, reasons: reasons.filter(Boolean) }
        const why = reasons.filter(Boolean).join('；')
        alertDevice(tt('notify.stopNotDelivered'),
                    `${who.join('、')}：${why || tt('notify.stopNotDeliveredBody')}`)
      }
      loading.value = false
    }
  }

  async function pauseCast(deviceId?: string) {
    const id = deviceId ?? focusedDeviceId.value
    if (!id) return
    dismissError()
    const operation = beginOperation(id)
    try {
      await apiPauseCast(id)
      if (!isCurrentOperation(id, operation)) return
      stopSim(id)
      updateSession(id, { status: 'paused' })
    } catch (err) {
      if (isCurrentOperation(id, operation)) lastError.value = String(err)
      console.error('Failed to pause cast:', err)
    } finally {
      operation.pending = false
      if (isCurrentOperation(id, operation) && sessions.value[id]?.status === 'playing') startSim(id)
    }
  }

  async function resumeCast(deviceId?: string) {
    const id = deviceId ?? focusedDeviceId.value
    if (!id) return
    dismissError()
    const operation = beginOperation(id)
    try {
      await apiResumeCast(id)
      if (!isCurrentOperation(id, operation)) return
      updateSession(id, { status: 'playing' })
      startSim(id)
    } catch (err) {
      if (isCurrentOperation(id, operation)) lastError.value = String(err)
      console.error('Failed to resume cast:', err)
    } finally {
      operation.pending = false
      if (isCurrentOperation(id, operation) && sessions.value[id]?.status === 'playing') startSim(id)
    }
  }

  /**
   * 投屏中切换画质：拿来源页 + 档位 id 重新解析，再重新投放，最后追回原进度。
   * 档位是「重解析的钥匙」而不是缓存直链 —— 站点直链会过期，重新解一次才有稳定答案。
   */
  async function switchQuality(qualityId: string, deviceId?: string) {
    const id = deviceId ?? focusedDeviceId.value
    const session = id ? sessions.value[id] : null
    if (!id || !session?.media || qualitySwitching.value) return
    const media = session.media
    const source = media.source_url ?? ''
    if (!source || (media.quality ?? '') === qualityId) return

    qualitySwitching.value = true
    dismissError()
    const operation = beginOperation(id)
    const resumeAt = session.positionTime
    stopSim(id)
    try {
      const info = await resolveShare(source, qualityId)
      if (!isCurrentOperation(id, operation)) return
      if (!info.uri) throw new Error('解析结果为空')
      await apiStartCast(id, info.uri, info.mime_type)
      if (!isCurrentOperation(id, operation)) return
      updateSession(id, {
        status: 'playing',
        position: 0,
        positionTime: '00:00:00',
        media: {
          ...media,
          uri: info.uri,
          mime_type: info.mime_type || media.mime_type,
          quality: info.quality || qualityId,
          qualities: info.qualities?.length ? info.qualities : media.qualities,
          thumbnail: info.thumbnail ?? media.thumbnail,
        },
      })
      startPolling()
      await restorePosition(id, resumeAt, operation)
    } catch (err) {
      if (isCurrentOperation(id, operation)) lastError.value = String(err)
      console.error('Failed to switch quality:', err)
    } finally {
      operation.pending = false
      qualitySwitching.value = false
      // Resolve/start/restore can all fail while the original session is still playing.
      if (isCurrentOperation(id, operation) && sessions.value[id]?.status === 'playing') startSim(id)
    }
  }

  /** 电视刚换地址时还在加载，Seek 会被拒；重试几次把进度追回来 */
  async function restorePosition(deviceId: string, positionTime: string, operation: { pending: boolean }) {
    if (!positionTime || positionTime === '00:00:00') return
    for (let attempt = 0; attempt < 4; attempt++) {
      await new Promise((r) => setTimeout(r, 1000))
      const session = sessions.value[deviceId]
      if (!isCurrentOperation(deviceId, operation) || !session || ['stopped', 'idle', 'error'].includes(session.status)) return
      try {
        await apiSeekCast(positionTime, deviceId)
        if (!isCurrentOperation(deviceId, operation)) return
        updateSession(deviceId, {
          position: parseTimeToSeconds(positionTime),
          positionTime,
        })
        return
      } catch (err) {
        if (!isCurrentOperation(deviceId, operation)) return
        // 仍在缓冲，稍后再试；最终失败交给 switchQuality 呈现原始原因。
        if (attempt === 3) throw err
      }
    }
  }

  async function seek(position: string, deviceId?: string) {
    const id = deviceId ?? focusedDeviceId.value
    if (!id) return
    dismissError()
    const operation = beginOperation(id)
    stopSim(id)
    try {
      await apiSeekCast(position, id)
      if (!isCurrentOperation(id, operation)) return
      // Update position immediately for responsive UI
      const posSec = parseTimeToSeconds(position)
      updateSession(id, { position: posSec, positionTime: position })
    } catch (err) {
      if (isCurrentOperation(id, operation)) lastError.value = String(err)
      console.error('Failed to seek:', err)
    } finally {
      operation.pending = false
      if (isCurrentOperation(id, operation) && sessions.value[id]?.status === 'playing') startSim(id)
    }
  }

  async function setVolume(volume: number, deviceId?: string): Promise<number> {
    const id = deviceId ?? focusedDeviceId.value
    if (!id) return volume
    dismissError()
    const operation = deviceOperations.get(id)
    try {
      const result = await apiSetVolume(volume, id)
      if (operation === deviceOperations.get(id)) updateSession(id, { volume: result })
      return result
    } catch (err) {
      lastError.value = String(err)
      console.error('Failed to set volume:', err)
      return sessions.value[id]?.volume ?? volume
    }
  }

  async function setMute(muted: boolean, deviceId?: string): Promise<void> {
    const id = deviceId ?? focusedDeviceId.value
    if (!id) return
    dismissError()
    const operation = deviceOperations.get(id)
    try {
      await apiSetMute(muted, id)
      if (operation === deviceOperations.get(id)) updateSession(id, { is_muted: muted })
    } catch (err) {
      lastError.value = String(err)
      console.error('Failed to set mute:', err)
    }
  }

  // ── Event listeners ──
  async function setupListeners() {
    try {
      const { listen } = await import('@tauri-apps/api/event')

      if (disposed) return
      registerUnlisten(
        await listen<{ device_id?: string; status: string; position?: string; duration?: string; volume?: number; is_muted?: boolean }>('cast_state_changed', (event) => {
          if (disposed) return
          const { device_id, status, position, duration } = event.payload
          const mappedStatus: Record<string, CastSession['status']> = {
            PLAYING: 'playing',
            PAUSED: 'paused',
            STOPPED: 'stopped',
            NO_MEDIA_PRESENT: 'idle',
          }
          const newStatus = mappedStatus[status] ?? status as CastSession['status']

          // Determine which device this event is for
          // Fallback: single session in store → use it; otherwise use focused device
          const sessionIds = Object.keys(sessions.value)
          const targetId = device_id
            ?? (sessionIds.length === 1 ? sessionIds[0] : focusedDeviceId.value)
          if (!targetId) return

          const session = sessions.value[targetId]
          if (!session) return

          // Read current status BEFORE updating (guard depends on it)
          const prevStatus = sessions.value[targetId]?.status

          // Build updates
          const updates: Partial<CastSession> = { status: newStatus }
          if (position) {
            updates.position = parseTimeToSeconds(position)
            updates.positionTime = position
          }
          if (duration) {
            updates.durationTime = duration
          }
          if (event.payload.volume !== undefined) {
            updates.volume = event.payload.volume
          }
          if (event.payload.is_muted !== undefined) {
            updates.is_muted = event.payload.is_muted
          }

          updateSession(targetId, updates)

          // Start/stop progress simulation based on status
          if (newStatus === 'playing') {
            startSim(targetId)
          } else if (newStatus === 'paused' || newStatus === 'stopped' || newStatus === 'idle') {
            stopSim(targetId)
          }

          // Video finished or device stopped
          // Never remove sessions that are connecting, playing, or paused
          // (DLNA may send transient NO_MEDIA_PRESENT / STOPPED during playback)
          if (newStatus === 'stopped' || newStatus === 'idle') {
            if (prevStatus === 'connecting' || prevStatus === 'playing' || prevStatus === 'paused') {
              return
            }
            const removeTargetId = targetId
            setTimeout(() => {
              const s = sessions.value[removeTargetId]
              if (s && (s.status === 'stopped' || s.status === 'idle')) {
                removeSession(removeTargetId)
              }
            }, 500)
            if (focusedDeviceId.value === targetId) {
              showController.value = false
            }
            if (activeDeviceIds.value.length <= 1) {
              stopPolling()
            }
          }
        })
      )

      if (disposed) return
      registerUnlisten(
        await listen<{ device_id?: string; message: string }>('cast_error', (event) => {
          if (disposed) return
          const targetId = event.payload.device_id ?? focusedDeviceId.value
          if (targetId && sessions.value[targetId]) {
            stopSim(targetId)
            updateSession(targetId, { status: 'error' })
          }
          lastError.value = String(event.payload.message)
          console.error('Cast error:', event.payload.message)
        })
      )
    } catch {
      // Running outside Tauri, skip listeners
    }
  }

  onMounted(() => {
    fetchState().then(() => {
      if (activeDeviceIds.value.length > 0) {
        startPolling()
      }
    })
    setupListeners()
  })

  onUnmounted(() => {
    disposed = true
    stopPolling()
    stopAllSim()
    unlistenFns.splice(0).forEach((fn) => fn())
  })

  return {
    // Multi-device state
    sessions,
    focusedDeviceId,
    focusedSession,
    activeDeviceIds,
    loading,
    showController,
    stopWarning,
    lastError,
    qualitySwitching,

    // Autostart prompt
    showAutostartPrompt,

    // Backward-compat (single-device view via focusedSession)
    castState,
    isIdle,
    isPlaying,
    isCasting,
    durationSeconds,
    positionSeconds,

    // Session helpers
    getSession,
    updateSession,

    // Controller
    openController,
    minimizeController,
    restoreController,

    // Local updates
    setLocalVolume,

    // Actions
    fetchState,
    startCast,
    stopCast,
    pauseCast,
    resumeCast,
    switchQuality,
    seek,
    setVolume,
    setMute,
    dismissError,

    // Autostart prompt actions
    closeAutostartPrompt: () => { showAutostartPrompt.value = false },
    dismissStopWarning: () => { stopWarning.value = null },
  }
})
