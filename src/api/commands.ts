/**
 * Tauri invoke command wrappers.
 * These are placeholders — real implementations will call the Rust backend.
 */
import type { Device } from '@/types/device'
import type { MediaInfo } from '@/types/media'
import type { CastState, CastUrlInfo } from '@/types/cast'
import type { AppSettings, NetworkInterfacesInfo, ParamSpec, SaveParamsResult } from '@/types/settings'
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

/** 浏览器 dev 的插件仓库（plugin_* 命令的内存态）
 *
 * 程序不带规则，所以这里模拟的就是「装了包」的样子：官方包与用户包同权，
 * 都能删、都能停。dev 里看不到真实索引，检查更新用一份内存假索引。
 */
const mockPlugins: PluginInfo[] = [
  {
    name: 'demo-site',
    version: '1.0.0',
    author: 'example',
    description: '演示插件：页面阶段提取 m3u8',
    rules: [
      { id: 'main', name: 'main', hosts: ['example.com', 'demo.example'], stage: 'page', enabled: true },
      { id: 'legacy', name: 'legacy', hosts: ['old.example'], stage: 'url', enabled: false },
    ],
  },
  {
    name: 'hancast-bilibili',
    version: '1.0.0',
    author: 'HanCast',
    description: 'B 站解析：APP 签名多线路取流',
    rules: [
      { id: 'bilibili-playurl', name: 'B 站 playurl', hosts: ['bilibili.com', 'b23.tv'], stage: 'url', enabled: true },
    ],
  },
]

/** dev 用假索引：一条可更新（hancast-bilibili）、一条本机没装（hancast-douyin） */
const mockIndexPacks: PackIndexEntry[] = [
  { name: 'hancast-bilibili', version: '1.1.0', url: 'https://example.invalid/hancast-bilibili-1.1.0.zip', description: 'B 站解析：APP 签名多线路取流', sha256: 'aa'.repeat(32) },
  { name: 'hancast-douyin', version: '1.0.0', url: 'https://example.invalid/hancast-douyin-1.0.0.zip', description: '抖音分享页解析', sha256: 'bb'.repeat(32) },
]

const mockDeclarative: DeclarativeRuleInfo[] = [
  {
    name: 'maccms-box',
    file: '~/Library/Application Support/HanCast/share_rules/maccms-box.json',
    hosts: ['dyrsxm.cc'],
    stage: 'page',
    enabled: true,
    steps: 4,
  },
]

/** 清单的两个命令共用同一份内存态：包开关不能把 JSON 段清空 */
function mockInventory(): RuleInventory {
  return {
    rules_dir: '~/Library/Application Support/HanCast/share_rules',
    script_packs: mockPlugins.map((p) => ({ ...p, rules: p.rules.map((r) => ({ ...r })) })),
    declarative: mockDeclarative.map((d) => ({ ...d })),
  }
}

/** 解析失败台账的内存态：mock 模式下也能演示删域名/删单条/清空 */
const mockFailureHosts: FailureHost[] = [
  {
    host: 'weibo.com',
    count: 3,
    first_at: 1759000000,
    last_at: 1759600000,
    entries: [
      { url: 'https://weibo.com/tv/show/1035:4800000000000000', error: '页面里没有可播放地址', count: 2, last_at: 1759600000 },
      { url: 'https://video.weibo.com/show?1lxz3u', error: '接口返回 403', count: 1, last_at: 1759500000 },
    ],
  },
  {
    host: 'v.ixigua.com',
    count: 1,
    first_at: 1759400000,
    last_at: 1759400000,
    entries: [
      { url: 'https://v.ixigua.com/hdlA2B3/', error: '短链展开后没有媒体', count: 1, last_at: 1759400000 },
    ],
  },
]

function mockLedger(): FailureLedger {
  return {
    enabled: true,
    path: '~/Library/Application Support/HanCast/parse_failures.json',
    hosts: mockFailureHosts.map((h) => ({ ...h, entries: h.entries.map((e) => ({ ...e })) })),
    total: mockFailureHosts.reduce((n, h) => n + h.count, 0),
  }
}

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
  version: '2.3.6',
  media_port: 8080,
  default_device: null,
  network_interface: '',
  settings: {},
}

// 后台参数登记表在后端 params.py；dev 模式下只放三种控件类型各一项代表，
// 用于设置页布局验证，不代表真实参数集。
const mockParamOverrides: Record<string, number | boolean | string> = {}

function mockParamSpecs(): ParamSpec[] {
  const base: Omit<ParamSpec, 'value' | 'overridden'>[] = [
    {
      key: 'screencast.browser_segment_seconds', section: 'screencast', type: 'number',
      default: 1, min: 1, max: 10, unit: 's',
      label_zh: '浏览器通道分片时长', label_en: 'Browser HLS segment length',
      help_zh: '越短延迟越低，但分片太碎会增加播放列表请求与解码压力',
      help_en: 'Shorter = lower latency, but more playlist requests and keyframe pressure',
    },
    {
      key: 'screencast.x264_preset', section: 'screencast', type: 'enum', default: 'veryfast',
      options: ['ultrafast', 'superfast', 'veryfast', 'faster', 'fast'],
      label_zh: '编码速度档', label_en: 'x264 preset',
      help_zh: '越快越省 CPU 但同码率画质更差', help_en: 'Faster saves CPU but costs quality',
    },
    {
      key: 'player.low_latency_mode', section: 'player', type: 'bool', default: true,
      label_zh: 'hls.js 低延迟模式', label_en: 'hls.js low latency mode',
      help_zh: '关闭后播放器按普通 HLS 保守缓冲', help_en: 'Off = conservative buffering',
    },
    {
      key: 'rules.index_url', section: 'rules', type: 'text',
      default: 'https://raw.githubusercontent.com/pingod/HanCast/main/rule-packs/index.json',
      label_zh: '规则包索引地址', label_en: 'Rule pack index URL',
      help_zh: '「检查规则包更新」读取的清单；清单里每个包的下载地址只认 https',
      help_en: 'Index read by "check for pack updates"; listed pack URLs must be https',
    },
    {
      key: 'rules.index_timeout_s', section: 'rules', type: 'number',
      default: 40, min: 5, max: 120, unit: 's',
      label_zh: '规则包索引超时', label_en: 'Rule pack index timeout',
      help_zh: '拉索引和查更新时单次网络请求的等待上限',
      help_en: 'Per-request wait limit for fetching the index and update checks',
    },
  ]
  return base.map((s) => ({
    ...s,
    value: mockParamOverrides[s.key] ?? s.default,
    overridden: s.key in mockParamOverrides,
  }))
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
    case 'get_params':
      return { specs: mockParamSpecs() } as T
    case 'save_params': {
      const values = (args?.values ?? {}) as Record<string, number | boolean | string>
      for (const [key, value] of Object.entries(values)) {
        if (mockParamSpecs().some((s) => s.default === value)) {
          delete mockParamOverrides[key]
        } else {
          mockParamOverrides[key] = value
        }
      }
      return { ok: true, errors: [], specs: mockParamSpecs() } as T
    }
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
      // B 站类链接：dev 模式下也要有多画质可点，否则画质选择器无从验证
      if (/bilibili\.com|b23\.tv/i.test(url)) {
        const q = String(args?.quality ?? '') || '80'
        return {
          media_type: 'url',
          uri: `https://mock.bilivideo.com/v/${q}.mp4`,
          title: '示例视频（浏览器 mock）',
          mime_type: 'video/mp4',
          file_size: null,
          duration: null,
          thumbnail: null,
          qualities: [
            { id: '80', label: '1080P 高清' },
            { id: '64', label: '720P 超清' },
            { id: '32', label: '480P 清晰' },
          ],
          quality: q,
          source_url: url,
          via: 'mock/hancast-bilibili',
        } as T
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
    case 'plugin_list':
      return { plugins: mockPlugins.map((p) => ({ ...p, rules: p.rules.map((r) => ({ ...r })) })) } as T
    case 'rule_inventory':
      return mockInventory() as T
    case 'failure_list':
      return mockLedger() as T
    case 'failure_delete': {
      const host = String(args?.host ?? '').toLowerCase()
      const url = String(args?.url ?? '').toLowerCase()
      if (url) {
        for (const h of mockFailureHosts) {
          h.entries = h.entries.filter((e) => e.url.toLowerCase() !== url)
          h.count = h.entries.reduce((s, e) => s + e.count, 0)
        }
      } else if (host) {
        const i = mockFailureHosts.findIndex((h) => h.host === host)
        if (i >= 0) mockFailureHosts.splice(i, 1)
      }
      for (let j = mockFailureHosts.length - 1; j >= 0; j--) {
        if (!mockFailureHosts[j].entries.length) mockFailureHosts.splice(j, 1)
      }
      return mockLedger() as T
    }
    case 'failure_clear':
      mockFailureHosts.length = 0
      return mockLedger() as T
    case 'plugin_check_updates': {
      const installed = new Map(mockPlugins.map((p) => [p.name, p.version]))
      const cmp = (v: string) => v.split('.').map((n) => parseInt(n, 10) || 0)
      const newer = (a: string, b: string) => {
        const x = cmp(a), y = cmp(b)
        for (let i = 0; i < Math.max(x.length, y.length); i++) {
          const d = (x[i] ?? 0) - (y[i] ?? 0)
          if (d !== 0) return d > 0
        }
        return false
      }
      const updates: PackUpdateEntry[] = []
      const available: PackIndexEntry[] = []
      for (const meta of mockIndexPacks) {
        const current = installed.get(meta.name)
        if (current === undefined) available.push(meta)
        else if (newer(meta.version, current))
          updates.push({ ...meta, current })
      }
      return {
        index_url: 'https://example.invalid/rule-packs/index.json',
        updates, available, installed: mockPlugins.length,
      } as T
    }
    case 'plugin_pull': {
      const name = String(args?.name ?? '')
      const meta = mockIndexPacks.find((p) => p.name === name)
      if (!meta) throw new Error(`mock: 索引里没有 ${name}`)
      const existing = mockPlugins.find((p) => p.name === name)
      if (existing) existing.version = meta.version
      else mockPlugins.push({
        name, version: meta.version, author: 'HanCast',
        description: meta.description,
        rules: [{ id: `${name}-main`, name: 'main', hosts: ['example.com'], stage: 'page', enabled: true }],
      })
      return { plugins: mockPlugins.map((p) => ({ ...p, rules: p.rules.map((r) => ({ ...r })) })) } as T
    }
    case 'plugin_import': {
      const name = args?.url
        ? decodeURIComponent(String(args.url).split('/').pop() || 'plugin').replace(/\.zip$/i, '')
        : 'imported-plugin'
      const existing = mockPlugins.find((p) => p.name === name)
      const plugin: PluginInfo = existing ?? {
        name,
        version: '0.1.0',
        author: 'mock',
        description: '',
        rules: [],
      }
      if (!existing) mockPlugins.push(plugin)
      plugin.rules = [
        { id: 'main', name: 'main', hosts: ['example.com'], stage: 'page', enabled: true },
      ]
      return { plugins: mockPlugins.map((p) => ({ ...p, rules: p.rules.map((r) => ({ ...r })) })) } as T
    }
    case 'plugin_remove': {
      const name = String(args?.name ?? '')
      const idx = mockPlugins.findIndex((p) => p.name === name)
      if (idx >= 0) mockPlugins.splice(idx, 1)
      return { plugins: mockPlugins.map((p) => ({ ...p, rules: p.rules.map((r) => ({ ...r })) })) } as T
    }
    case 'plugin_set_rule_enabled': {
      const plugin = mockPlugins.find((p) => p.name === String(args?.plugin ?? ''))
      const rule = plugin?.rules.find((r) => r.id === String(args?.rule ?? ''))
      if (rule) rule.enabled = Boolean(args?.enabled)
      return { plugins: mockPlugins.map((p) => ({ ...p, rules: p.rules.map((r) => ({ ...r })) })) } as T
    }
    case 'plugin_test': {
      const raw = String(args?.text ?? '')
      const urlMatch = raw.match(/https?:\/\/[^\s"'<>，。；！？、【】]+/i)
      const url = urlMatch ? urlMatch[0] : ''
      if (!url) throw new Error('mock: 未找到链接')
      // 同步返回结果（无 task_id），走调用方的「直接结果」兼容通道
      return {
        media_type: 'url',
        uri: url,
        title: decodeURIComponent(url.split('/').pop()?.split('?')[0] || url) || url,
        mime_type: 'video/mp4',
        file_size: null,
        duration: null,
        thumbnail: null,
        via: 'mock/main',
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
    case 'stop_cast':
      return { ok: true, stopped: [], control_failed: [], reasons: {} } as T
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
        current: '2.3.6',
        latest: '2.3.6',
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
        platform: /Mac|iPhone|iPad/.test(navigator.platform) ? 'darwin' : 'win32',
        ffmpeg: { ok: true, path: 'ffmpeg', version: 'mock' },
      } as T
    case 'start_screen_cast':
      return { ok: true, target: 'browser', session_id: 'abc123', watch_url: 'http://127.0.0.1:8765/r/abc123' } as T
    case 'stop_screen_cast':
      return { ok: true, control_failed: [], reasons: {} } as T
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
  /** 解析通道：内置平台 / 站点规则 id / JS 插件的「插件名/规则名」 */
  via?: string
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
export async function resolveShare(text: string, quality = ''): Promise<ShareResolveResult> {
  const invoke = await initInvoke()
  const start = await invoke<ResolveTaskStart & Partial<ShareResolveResult>>(
    'resolve_share', { text, quality })

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

// ── 解析插件（JS 规则包）管理 ──

/** 插件内单条规则元信息（不含 resolve 函数） */
export interface PluginRuleInfo {
  id: string
  name: string
  hosts: string[]
  stage: 'url' | 'page'
  enabled: boolean
}

export interface PluginInfo {
  name: string
  version: string
  author: string
  description: string
  rules: PluginRuleInfo[]
  broken?: boolean
  error?: string
  update_url?: string
}

/** 官方索引里的一个包条目 */
export interface PackIndexEntry {
  name: string
  version: string
  url: string
  description: string
  sha256?: string
}

/** 已装包有新版本 */
export interface PackUpdateEntry extends PackIndexEntry {
  current: string
}

export interface PackUpdateCheck {
  index_url: string
  updates: PackUpdateEntry[]
  available: PackIndexEntry[]
  installed: number
}

/** 外置声明式 JSON 规则（<配置目录>/share_rules/*.json） */
export interface DeclarativeRuleInfo {
  name: string
  file: string
  hosts: string[]
  stage: string
  enabled: boolean
  steps?: number
  error?: string
}

export interface RuleInventory {
  rules_dir: string
  script_packs: PluginInfo[]
  declarative: DeclarativeRuleInfo[]
}

/** 解析失败台账：一条没解出来的地址 */
export interface FailureEntry {
  url: string
  error: string
  count: number
  last_at: number
}

/** 台账里按域名分组的一堆 */
export interface FailureHost {
  host: string
  count: number
  first_at: number
  last_at: number
  entries: FailureEntry[]
}

export interface FailureLedger {
  enabled: boolean
  path: string
  hosts: FailureHost[]
  total: number
}

export async function pluginList(): Promise<PluginInfo[]> {
  const invoke = await initInvoke()
  const resp = await invoke<{ plugins: PluginInfo[] }>('plugin_list', {})
  return resp.plugins ?? []
}

export async function pluginImportFile(filePath: string): Promise<PluginInfo[]> {
  const invoke = await initInvoke()
  const resp = await invoke<{ plugins: PluginInfo[] }>('plugin_import', { filePath })
  return resp.plugins ?? []
}

export async function pluginImportData(dataBase64: string): Promise<PluginInfo[]> {
  const invoke = await initInvoke()
  const resp = await invoke<{ plugins: PluginInfo[] }>('plugin_import', { dataBase64 })
  return resp.plugins ?? []
}

export async function pluginImportUrl(url: string): Promise<PluginInfo[]> {
  const invoke = await initInvoke()
  const resp = await invoke<{ plugins: PluginInfo[] }>('plugin_import', { url })
  return resp.plugins ?? []
}

export async function pluginRemove(name: string): Promise<PluginInfo[]> {
  const invoke = await initInvoke()
  const resp = await invoke<{ plugins: PluginInfo[] }>('plugin_remove', { name })
  return resp.plugins ?? []
}

export async function pluginSetRuleEnabled(
  plugin: string,
  rule: string,
  enabled: boolean,
): Promise<PluginInfo[]> {
  const invoke = await initInvoke()
  const resp = await invoke<{ plugins: PluginInfo[] }>('plugin_set_rule_enabled', {
    plugin,
    rule,
    enabled,
  })
  return resp.plugins ?? []
}

/**
 * 规则总览：脚本包 / 声明式 JSON 两种来源一屏列全。
 */
export async function ruleInventory(): Promise<RuleInventory> {
  const invoke = await initInvoke()
  const resp = await invoke<RuleInventory>('rule_inventory', {})
  return {
    rules_dir: resp.rules_dir ?? '',
    script_packs: resp.script_packs ?? [],
    declarative: resp.declarative ?? [],
  }
}

/** 解析失败台账：哪些站解不出来、卡在哪一步（按域名分组，供以后写规则） */
export async function failureList(): Promise<FailureLedger> {
  const invoke = await initInvoke()
  return normalizeLedger(await invoke<FailureLedger>('failure_list', {}))
}

/** 删台账里的一个域名（传 host）或其中一条地址（传 url） */
export async function failureDelete(host = '', url = ''): Promise<FailureLedger> {
  const invoke = await initInvoke()
  return normalizeLedger(
    await invoke<FailureLedger>('failure_delete', { host: host || null, url: url || null }),
  )
}

export async function failureClear(): Promise<FailureLedger> {
  const invoke = await initInvoke()
  return normalizeLedger(await invoke<FailureLedger>('failure_clear', {}))
}

function normalizeLedger(resp: FailureLedger): FailureLedger {
  return {
    enabled: resp.enabled !== false,
    path: resp.path ?? '',
    hosts: resp.hosts ?? [],
    total: resp.total ?? 0,
  }
}

/** 对照官方索引：哪些包有新版本、哪些还没装（indexUrl 留空走后台参数） */
export async function pluginCheckUpdates(
  indexUrl = '',
): Promise<PackUpdateCheck> {
  const invoke = await initInvoke()
  const resp = await invoke<PackUpdateCheck>('plugin_check_updates', {
    indexUrl: indexUrl || null,
  })
  return {
    index_url: resp.index_url ?? '',
    updates: resp.updates ?? [],
    available: resp.available ?? [],
    installed: resp.installed ?? 0,
  }
}

/** 从索引装/更新一个包；indexUrl 留空走后台参数里的官方索引。
 * 摘要校验在后端完成：包按索引条目下载，sha256 对不上就不落盘。 */
export async function pluginPull(name: string, indexUrl = ''): Promise<PluginInfo[]> {
  const invoke = await initInvoke()
  const resp = await invoke<{ plugins: PluginInfo[] }>('plugin_pull', {
    name,
    indexUrl: indexUrl || null,
  })
  return resp.plugins ?? []
}

/**
 * 用一段分享文本试跑某插件，复用 resolveShare 的任务轮询通道。
 */
export async function pluginTest(plugin: string, text: string): Promise<ShareResolveResult> {
  const invoke = await initInvoke()
  const start = await invoke<{ task_id?: string } & Partial<ShareResolveResult>>(
    'plugin_test', { plugin, text })

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

// 停止类命令的返回体：control_failed 是「本地会话已释放、但电视没确认收到
// Stop」的设备列表，前端必须据此提示，不能把「界面关了」当成「电视停了」。
export interface StopCastResult {
  ok: boolean
  stopped?: string[]
  control_failed?: string[]
  reasons?: Record<string, string>
  already_stopped?: boolean
}

/** 界面用的停止告警：哪些设备没确认收到 Stop、以及后端给的原因 */
export interface StopWarning {
  devices: string[]
  reasons: string[]
}

export async function stopCast(deviceId?: string): Promise<StopCastResult> {
  const invoke = await initInvoke()
  return invoke<StopCastResult>('stop_cast', deviceId ? { deviceId } : {})
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

/** 后台可调参数登记表（后端 params.py 渲染设置页用） */
export async function getParams(): Promise<ParamSpec[]> {
  const invoke = await initInvoke()
  const res = await invoke<{ specs: ParamSpec[] }>('get_params')
  return res.specs ?? []
}

/** 提交参数改动，返回校验结果与最新登记表 */
export async function saveParams(values: Record<string, number | boolean | string>): Promise<SaveParamsResult> {
  const invoke = await initInvoke()
  return invoke<SaveParamsResult>('save_params', { values })
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

export async function stopScreenCast(): Promise<StopCastResult> {
  const invoke = await initInvoke()
  return invoke<StopCastResult>('stop_screen_cast')
}

export async function getScreenCastStatus(): Promise<ScreenCastState> {
  const invoke = await initInvoke()
  return invoke<ScreenCastState>('screen_cast_status')
}

export async function setFfmpegPath(path: string): Promise<{ ok: boolean; error?: string }> {
  const invoke = await initInvoke()
  return invoke('set_ffmpeg_path', { path })
}
