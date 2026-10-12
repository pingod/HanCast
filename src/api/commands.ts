/**
 * Tauri invoke command wrappers.
 * These are placeholders — real implementations will call the Rust backend.
 */
import type { Device } from '@/types/device'
import type { MediaInfo } from '@/types/media'
import type { CastState, CastUrlInfo } from '@/types/cast'
import type { AppSettings, NetworkInterfacesInfo } from '@/types/settings'
import type { GuardDevicesResponse, GuardSettings } from '@/types/guard'
import type { UpdateInfo } from '@/types/update'
import type { LogPage, LogQuery } from '@/types/logs'
import type {
  ScreenSources,
  ScreenCastState,
  ScreenCastStartParams,
} from '@/types/screencast'

// ── Helpers ──

let invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>

async function initInvoke() {
  if (invoke) return invoke
  try {
    // Check if Tauri runtime is actually available (not just the module)
    if (window.__TAURI_INTERNALS__?.metadata) {
      const core = await import('@tauri-apps/api/core')
      invoke = core.invoke
    } else {
      invoke = mockInvoke as unknown as typeof invoke
    }
  } catch {
    // Running outside Tauri (e.g. browser dev), use mock fallback
    invoke = mockInvoke as unknown as typeof invoke
  }
  return invoke
}

// ── Mock fallback for browser dev ──

const MOCK_DEVICES: Device[] = [
  {
    id: 'samsung-tv-001',
    name: 'Samsung TV',
    type: 'tv',
    ip: '192.168.1.100',
    port: 8008,
    status: 'online',
    is_default: true,
    manufacturer: 'Samsung',
    model_name: 'QN65Q80B',
    udn: 'uuid:samsung-tv-001',
  },
  {
    id: 'living-room-speaker',
    name: '客厅音箱',
    type: 'speaker',
    ip: '192.168.1.101',
    port: 8008,
    status: 'online',
    is_default: false,
    manufacturer: 'Sonos',
    model_name: 'One SL',
    udn: 'uuid:sonos-one-sl',
  },
  {
    id: 'study-speaker',
    name: '书房音箱',
    type: 'speaker',
    ip: '192.168.1.102',
    port: 8008,
    status: 'online',
    is_default: false,
    manufacturer: 'Sonos',
    model_name: 'One SL',
    udn: 'uuid:sonos-study',
  },
  {
    id: 'mi-box-001',
    name: '小米盒子',
    type: 'box',
    ip: '192.168.1.103',
    port: 8008,
    status: 'offline',
    is_default: false,
    manufacturer: 'Xiaomi',
    model_name: 'Mi Box S',
    udn: 'uuid:mi-box-001',
  },
]

const MOCK_SETTINGS: AppSettings = {
  usn: 'hancast-uuid-001',
  friendly_name: 'HanCast',
  version: '2.2.4',
  media_port: 8080,
  default_device: null,
  network_interface: '',
  settings: {},
}

const MOCK_CAST_STATE: CastState = {
  status: 'idle',
  device_id: null,
  media: null,
  position: 0,
  volume: 80,
  is_muted: false,
  positionTime: '00:00:00',
  durationTime: '00:00:00',
}

// 日志级别 / 手动设备：dev 模式下要能自洽回读，否则面板开关看起来没生效
let mockLogLevel = 'INFO'
const mockManualDevices: string[] = []

function mockManualDevice(addr: string): Device {
  const bare = addr.replace(/^https?:\/\//, '')
  const host = bare.split('/')[0].split(':')[0]
  const portMatch = bare.match(/:(\d{1,5})(?:\/|$)/)
  return {
    id: `manual-${host}`,
    name: `手动设备 ${host}`,
    type: 'tv',
    ip: host,
    port: portMatch ? Number(portMatch[1]) : 8080,
    status: 'online',
    is_default: false,
    udn: `uuid:manual-${host}`,
  }
}

async function mockInvoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  await new Promise((r) => setTimeout(r, 300))

  switch (cmd) {
    case 'get_devices':
    case 'refresh_devices':
      // 真机里手动设备存在于 SSDP 注册表，get_devices 会一并返回
      return [...MOCK_DEVICES, ...mockManualDevices.map(mockManualDevice)] as T
    case 'get_cast_state':
      return MOCK_CAST_STATE as T
    case 'get_settings':
      return MOCK_SETTINGS as T
    case 'list_network_interfaces':
      return {
        interfaces: [
          { name: 'en0', ip: '192.168.1.10', netmask: '255.255.255.0', is_default: true },
          { name: 'en1', ip: '192.168.2.10', netmask: '255.255.255.0', is_default: false },
        ],
        selected: '',
      } as T
    case 'get_autostart':
      return false as T
    case 'set_autostart':
      return undefined as T
    case 'parse_media_file': {
      const fp = String(args?.filePath ?? '')
      const name = fp.split(/[/\\]/).pop() ?? 'Unknown'
      const ext = name.split('.').pop()?.toLowerCase() ?? ''
      const mimeMap: Record<string, string> = {
        mp4: 'video/mp4', mkv: 'video/x-matroska', avi: 'video/x-msvideo',
        mov: 'video/quicktime', webm: 'video/webm', flv: 'video/x-flv',
        mp3: 'audio/mpeg', flac: 'audio/flac', wav: 'audio/wav',
        jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
        gif: 'image/gif', webp: 'image/webp', bmp: 'image/bmp',
      }
      return {
        media_type: 'file',
        uri: fp,
        title: name,
        mime_type: mimeMap[ext] ?? 'application/octet-stream',
        file_size: 1024 * 1024 * 50,
        duration: null,
        thumbnail: null,
      } as T
    }
    case 'parse_media_url':
      return {
        media_type: 'url',
        uri: args?.url ?? '',
        title: String(args?.url ?? '').split('/').pop() ?? 'Unknown',
        mime_type: 'video/mp4',
        file_size: null,
        duration: null,
        thumbnail: null,
      } as T
    case 'read_clipboard':
      // 浏览器 dev 模式：交回 navigator.clipboard 处理
      return '' as T
    case 'write_clipboard':
      navigator.clipboard?.writeText(String(args?.text ?? '')).catch(() => {})
      return undefined as T
    case 'resolve_share': {
      // 浏览器 dev 模式：只做本地提取（口令文本取第一个链接，按扩展名猜 MIME）
      const raw = String(args?.text ?? '')
      const urlMatch = raw.match(/https?:\/\/[^\s"'<>，。；！？、【】]+/i)
      const url = urlMatch ? urlMatch[0].replace(/[),.;!?'"]+$/, '') : raw.trim()
      const ext = url.split('/').pop()?.split('?')[0]?.split('.').pop()?.toLowerCase() ?? ''
      const mimeMap: Record<string, string> = {
        mp4: 'video/mp4', mkv: 'video/x-matroska', webm: 'video/webm',
        mov: 'video/quicktime', flv: 'video/x-flv', m3u8: 'application/vnd.apple.mpegurl',
        mp3: 'audio/mpeg', aac: 'audio/aac', ogg: 'audio/ogg', m4a: 'audio/mp4',
        jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', gif: 'image/gif',
      }
      return {
        media_type: 'url',
        uri: url,
        title: decodeURIComponent(url.split('/').pop()?.split('?')[0] || url) || url,
        mime_type: mimeMap[ext] ?? '',
        file_size: null,
        duration: null,
        thumbnail: null,
        source_url: url,
        via: 'direct',
      } as T
    }
    case 'get_cast_url':
      return {
        url: 'http://example.com/video.mp4',
        title: 'Sample Video',
        duration: '00:05:30',
        position: '00:02:15',
        status: 'PLAYING',
      } as T
    case 'get_guard_devices':
      return {
        trusted: [],
        blacklisted: [],
      } as T
    case 'get_guard_settings':
      return {
        enabled: true,
        confirm_timeout: 15,
      } as T
    case 'respond_cast_confirm':
      return true as T
    case 'remove_guard_device':
      return true as T
    case 'set_guard_policy':
      return true as T
    case 'save_guard_settings':
      return {} as T
    case 'check_update':
      return {
        has_update: false,
        current: '2.2.4',
        latest: '2.2.4',
        url: '',
        body: '',
        source: 'github',
      } as T
    case 'ignore_update_version':
      return true as T
    case 'is_store_version':
      return false as T
    case 'export_logs':
      return true as T
    case 'list_screen_sources':
      return {
        screens: [{ id: 'desktop', title: '整个屏幕' }],
        windows: [],
        audio_inputs: [
          { name: '麦克风 (Realtek)', kind: 'mic' },
          { name: '立体声混音', kind: 'system' },
        ],
        ffmpeg: { ok: true, path: 'ffmpeg', version: 'mock' },
      } as T
    case 'start_screen_cast':
      return { ok: true, target: 'browser', session_id: 'abc123', watch_url: 'http://127.0.0.1:8765/r/abc123' } as T
    case 'stop_screen_cast':
      return { ok: true } as T
    case 'screen_cast_status':
      return { running: false, ffmpeg: { ok: true } } as T
    case 'set_ffmpeg_path':
      return { ok: true } as T
    case 'add_device': {
      const addr = String(args?.address ?? '').trim()
      if (!addr) throw new Error('请输入设备 IP 或描述地址')
      if (!mockManualDevices.includes(addr)) mockManualDevices.push(addr)
      return mockManualDevice(addr) as T
    }
    case 'get_manual_devices':
      return [...mockManualDevices] as T
    case 'get_logs': {
      // dev 模式：样本行覆盖多模块/多级别，过滤器才看得出效果
      // get_logs 走 Rust 的 params 形参，所以查询条件在 args.params 里
      const q = (args?.params ?? args ?? {}) as Record<string, unknown>
      const sample = [
        { ts: '2026-10-12 10:00:00,000', module: 'hancast.ssdp', level: 'INFO', msg: 'SSDP search sent on 192.168.1.5:54641' },
        { ts: '2026-10-12 10:00:01,120', module: 'hancast.screencast', level: 'WARNING', msg: 'DLNA target did not fetch the playlist within 12s' },
        { ts: '2026-10-12 10:00:02,480', module: 'hancast.media', level: 'ERROR', msg: 'resolve_share failed: 403 for https://v.douyin.com/xxxx' },
        { ts: '2026-10-12 10:00:03,900', module: 'hancast.commands', level: 'DEBUG', msg: 'recv start_screen_cast {"target":"browser"}' },
      ].map((l) => ({ ...l, raw: `[${l.ts}] ${l.module} ${l.level}: ${l.msg}` }))
      const wantModule = q.module ? String(q.module) : ''
      const wantLevel = q.level ? String(q.level).toUpperCase() : ''
      const kw = q.keyword ? String(q.keyword).toLowerCase() : ''
      const lines = sample.filter((l) =>
        (!wantModule || l.module.startsWith(wantModule)) &&
        (!wantLevel || l.level === wantLevel) &&
        (!kw || l.raw.toLowerCase().includes(kw))
      )
      const limit = Math.max(1, Number(args?.limit ?? 400))
      return {
        lines: lines.slice(-limit),
        total: lines.length,
        truncated: false,
        path: 'hancast.log',
        level: mockLogLevel,
        modules: [
          { id: 'hancast.ssdp', label: '设备发现 (SSDP/UPnP)' },
          { id: 'hancast.screencast', label: '屏幕投屏' },
          { id: 'hancast.media', label: '媒体与链接解析' },
          { id: 'hancast.commands', label: '命令处理' },
        ],
      } as T
    }
    case 'set_log_level':
      mockLogLevel = String(args?.level ?? 'INFO').toUpperCase()
      return { ok: true, level: mockLogLevel } as T
    default:
      return {} as T
  }
}

// ── Public API ──

// Device management
export async function getDevices(): Promise<Device[]> {
  const invoke = await initInvoke()
  return invoke<Device[]>('get_devices')
}

export async function refreshDevices(): Promise<Device[]> {
  const invoke = await initInvoke()
  return invoke<Device[]>('refresh_devices')
}

export async function setDefaultDevice(id: string): Promise<void> {
  const invoke = await initInvoke()
  await invoke('set_default_device', { id })
}

export async function renameDevice(id: string, name: string): Promise<void> {
  const invoke = await initInvoke()
  await invoke('rename_device', { id, name })
}

export async function removeDevice(id: string): Promise<void> {
  const invoke = await initInvoke()
  await invoke('remove_device', { id })
}

export async function hideDevice(id: string): Promise<void> {
  const invoke = await initInvoke()
  await invoke('hide_device', { id })
}

/** 手动添加设备：IP / IP:端口 / 完整 description.xml 地址 */
export async function addDevice(address: string): Promise<Device> {
  const invoke = await initInvoke()
  return invoke<Device>('add_device', { address })
}

/** 已持久化的手动设备地址（重启后仍会自动重连） */
export async function getManualDevices(): Promise<string[]> {
  const invoke = await initInvoke()
  return invoke<string[]>('get_manual_devices')
}

// Media parsing
export async function parseMediaFile(filePath: string): Promise<MediaInfo> {
  const invoke = await initInvoke()
  return invoke<MediaInfo>('parse_media_file', { filePath })
}

export async function parseMediaUrl(url: string): Promise<MediaInfo> {
  const invoke = await initInvoke()
  return invoke<MediaInfo>('parse_media_url', { url })
}

/** 分享文本解析结果：MediaInfo + 提取出的原始地址与解析通道 */
export type ShareResolveResult = MediaInfo & {
  source_url?: string
  via?: 'bilibili' | 'douyin' | 'page' | 'direct'
  author?: string
  bvid?: string
}

interface ResolveTaskStart {
  task_id: string
  status: 'pending'
}
interface ResolveTaskEntry {
  status: 'pending' | 'done' | 'error'
  result?: ShareResolveResult
  error?: string
}

/**
 * 从任意粘贴文本（分享口令/短链/网页/直链）解析可播放媒体。
 * 后端为异步任务：resolve_share 立即返回 task_id，轮询 get_resolve_result
 * 直到 done/error（sidecar 单条解析预算 25s，这里给 45s 兜底）。
 */
export async function resolveShare(text: string): Promise<ShareResolveResult> {
  const invoke = await initInvoke()
  const start = await invoke<ResolveTaskStart & Partial<ShareResolveResult>>(
    'resolve_share', { text })

  // 兼容直接返回结果的实现（浏览器 mock / 旧版）
  if (!start.task_id) {
    if (!start.uri) throw new Error('解析结果为空')
    return start as ShareResolveResult
  }

  const deadline = Date.now() + 45_000
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 400))
    const entry = await invoke<ResolveTaskEntry>('get_resolve_result', {
      taskId: start.task_id,
    })
    if (entry.status === 'done' && entry.result) return entry.result
    if (entry.status === 'error') throw new Error(entry.error || '解析失败')
  }
  throw new Error('解析超时，请稍后重试')
}

/**
 * 读取系统剪贴板文本。
 *
 * macOS 的 WKWebView 不给页面 clipboard-read 权限，navigator.clipboard.readText()
 * 会静默 reject，所以优先走 Rust 侧；浏览器开发模式再回退到 Web API。
 */
export async function readClipboardText(): Promise<string> {
  const invoke = await initInvoke()
  try {
    const text = await invoke<string>('read_clipboard')
    if (typeof text === 'string' && text) return text
  } catch {
    /* 旧版 Rust 无此命令，落到 Web API */
  }
  try {
    return await navigator.clipboard.readText()
  } catch {
    return ''
  }
}

/** 写入系统剪贴板，失败抛错交由调用方降级处理。 */
export async function writeClipboardText(text: string): Promise<void> {
  const invoke = await initInvoke()
  await invoke('write_clipboard', { text })
}

// Cast control — all commands accept optional deviceId for multi-device support
export async function startCast(deviceId: string, mediaUri: string, mimeType?: string): Promise<void> {
  const invoke = await initInvoke()
  await invoke('start_cast', { deviceId, mediaUri, mimeType })
}

export async function stopCast(deviceId?: string): Promise<void> {
  const invoke = await initInvoke()
  await invoke('stop_cast', deviceId ? { deviceId } : {})
}

export async function pauseCast(deviceId?: string): Promise<void> {
  const invoke = await initInvoke()
  await invoke('pause_cast', deviceId ? { deviceId } : {})
}

export async function resumeCast(deviceId?: string): Promise<void> {
  const invoke = await initInvoke()
  await invoke('resume_cast', deviceId ? { deviceId } : {})
}

export async function seekCast(position: string, deviceId?: string): Promise<void> {
  const invoke = await initInvoke()
  await invoke('seek_cast', { position, ...(deviceId ? { deviceId } : {}) })
}

export async function getCastState(deviceId?: string): Promise<CastState> {
  const invoke = await initInvoke()
  return invoke<CastState>('get_cast_state', deviceId ? { deviceId } : {})
}

export async function getCastUrl(deviceId?: string): Promise<CastUrlInfo> {
  const invoke = await initInvoke()
  return invoke<CastUrlInfo>('get_cast_url', deviceId ? { deviceId } : {})
}

export async function setVolume(volume: number, deviceId?: string): Promise<number> {
  const invoke = await initInvoke()
  return invoke<number>('set_volume', { volume, ...(deviceId ? { deviceId } : {}) })
}

export async function setMute(muted: boolean, deviceId?: string): Promise<void> {
  const invoke = await initInvoke()
  await invoke('set_mute', { muted, ...(deviceId ? { deviceId } : {}) })
}

// Settings
export async function getSettings(): Promise<AppSettings> {
  const invoke = await initInvoke()
  return invoke<AppSettings>('get_settings')
}

export async function saveSettings(settings: Partial<AppSettings>): Promise<void> {
  const invoke = await initInvoke()
  await invoke('save_settings', { settings })
}

export async function listNetworkInterfaces(): Promise<NetworkInterfacesInfo> {
  const invoke = await initInvoke()
  return invoke<NetworkInterfacesInfo>('list_network_interfaces')
}

// Autostart
export async function getAutostart(): Promise<boolean> {
  const invoke = await initInvoke()
  return invoke<boolean>('get_autostart')
}

export async function setAutostart(enabled: boolean): Promise<void> {
  const invoke = await initInvoke()
  await invoke('set_autostart', { enabled })
}

// Window control
export async function minimizeWindow(): Promise<void> {
  try {
    const invoke = await initInvoke()
    await invoke('minimize_window')
  } catch {
    // Ignore in browser dev
  }
}

export async function closeWindow(): Promise<void> {
  try {
    const invoke = await initInvoke()
    await invoke('close_window')
  } catch {
    // Ignore in browser dev
  }
}

// ── Device Guard ──

export async function respondCastConfirm(
  requestId: string,
  approved: boolean,
  policy: string,
): Promise<boolean> {
  const invoke = await initInvoke()
  return invoke<boolean>('respond_cast_confirm', { requestId, approved, policy })
}

export async function getGuardDevices(): Promise<GuardDevicesResponse> {
  const invoke = await initInvoke()
  return invoke<GuardDevicesResponse>('get_guard_devices')
}

export async function removeGuardDevice(deviceKey: string): Promise<boolean> {
  const invoke = await initInvoke()
  return invoke<boolean>('remove_guard_device', { deviceKey })
}

export async function setGuardPolicy(
  deviceKey: string,
  policy: 'trusted' | 'blacklisted',
): Promise<boolean> {
  const invoke = await initInvoke()
  return invoke<boolean>('set_guard_policy', { deviceKey, policy })
}

export async function getGuardSettings(): Promise<GuardSettings> {
  const invoke = await initInvoke()
  return invoke<GuardSettings>('get_guard_settings')
}

export async function saveGuardSettings(settings: {
  enabled?: boolean
  confirm_timeout?: number
}): Promise<void> {
  const invoke = await initInvoke()
  await invoke('save_guard_settings', { settings })
}

// ── Export Logs ──

export async function exportLogs(): Promise<boolean> {
  const invoke = await initInvoke()
  return invoke<boolean>('export_logs')
}

export async function getLogs(query: LogQuery = {}): Promise<LogPage> {
  const invoke = await initInvoke()
  return invoke<LogPage>('get_logs', { params: { ...query } })
}

export async function setLogLevel(level: string): Promise<{ ok: boolean; level: string }> {
  const invoke = await initInvoke()
  return invoke('set_log_level', { level })
}

// ── Update Check ──

export async function checkUpdate(currentVersion: string, force?: boolean): Promise<UpdateInfo> {
  const invoke = await initInvoke()
  return invoke<UpdateInfo>('check_update', { currentVersion, force })
}

export async function ignoreUpdateVersion(version: string): Promise<boolean> {
  const invoke = await initInvoke()
  return invoke<boolean>('ignore_update_version', { version })
}

// ── Microsoft Store Detection ──

export async function isStoreVersion(): Promise<boolean> {
  try {
    const invoke = await initInvoke()
    return invoke<boolean>('is_store_version')
  } catch {
    return false
  }
}

// ── Screen Cast (电脑投屏) ──

export async function listScreenSources(): Promise<ScreenSources> {
  const invoke = await initInvoke()
  return invoke<ScreenSources>('list_screen_sources')
}

export async function startScreenCast(params: ScreenCastStartParams): Promise<{ ok: boolean; error?: string; watch_url?: string; session_id?: string; url?: string }> {
  const invoke = await initInvoke()
  return invoke('start_screen_cast', { params })
}

export async function stopScreenCast(): Promise<{ ok: boolean }> {
  const invoke = await initInvoke()
  return invoke('stop_screen_cast')
}

export async function getScreenCastStatus(): Promise<ScreenCastState> {
  const invoke = await initInvoke()
  return invoke<ScreenCastState>('screen_cast_status')
}

export async function setFfmpegPath(path: string): Promise<{ ok: boolean; error?: string }> {
  const invoke = await initInvoke()
  return invoke('set_ffmpeg_path', { path })
}
