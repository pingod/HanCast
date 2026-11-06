/**
 * 手机镜像接收（Android → 本机，adb + scrcpy）
 *
 * 方向与 `types/screencast.ts` 相反：那边是本机屏推到电视/浏览器，这边是
 * 手机屏接到本机。窗口由外部 scrcpy 打开，HanCast 只负责工具链与进程编排。
 */

/** adb / scrcpy 检测结果 */
export interface MirrorToolInfo {
  key: 'adb' | 'scrcpy'
  found: boolean
  path: string | null
  version: string | null
  /** manual=用户指定 / system=本机已装 */
  source: 'manual' | 'system' | null
  error: string | null
}

/** 一台 ADB 设备（`adb devices -l` 的一行） */
export interface MirrorDevice {
  serial: string
  /** adb 原始状态：device / unauthorized / offline … */
  state: string
  online: boolean
  model: string
  product?: string | null
  transport_id?: string | null
  wireless: boolean
}

export interface MirrorState {
  running: boolean
  serial: string | null
  model: string | null
  started_at: string | null
  pid: number | null
  /** 窗口由外部 scrcpy 打开，不是本程序内嵌的 */
  window: 'external'
  error: string | null
}

export interface MirrorStatus extends MirrorState {
  ok: boolean
  platform: string
  tools: MirrorToolInfo[]
  install_commands: string[]
  /** scrcpy >= 2.0 才有音频转发；老版本面板要说明「为什么没有声音」 */
  scrcpy_audio_supported: boolean
  scrcpy_audio_min_version: string
}

export interface MirrorDevicesReport {
  devices: MirrorDevice[]
  error: string | null
  tools: MirrorToolInfo[]
}

export interface MirrorConnectResult {
  ok: boolean
  error?: string
  message?: string
  address?: string
  device?: MirrorDevice | null
}

/** 面板里记住的画质偏好（localStorage `hancast.mirror.settings`） */
export interface MirrorPrefs {
  max_size: number
  max_fps: number
  video_bitrate: number
  audio: boolean
  control: boolean
  turn_screen_off: boolean
  stay_awake: boolean
  always_on_top: boolean
}

export interface MirrorStartParams extends MirrorPrefs {
  serial: string
  window_title?: string
}

export interface MirrorStartResult extends Partial<MirrorState> {
  ok: boolean
  error?: string
  tools?: MirrorToolInfo[]
  device?: MirrorDevice
}
