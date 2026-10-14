/**
 * 屏幕投屏（电脑投屏到 DLNA 电视 / 浏览器观看，HLS 分发）
 */

export interface ScreenSourceItem {
  id: string
  title: string
  /** 仅显示器源（id 形如 screen:x,y,w,h）携带 */
  width?: number
  height?: number
  primary?: boolean
}

export interface AudioDeviceItem {
  name: string
  kind: 'mic' | 'system'
  /** macOS 上由 HanCast 自建的 Audio Tap 环回（索引会变，后端每次按名称重解析） */
  tap?: boolean
  /** Windows WASAPI 环回：ffmpeg 没有这个输入设备，由 sidecar 灌 PCM */
  loopback?: boolean
  /** 这块环回对应的正是系统当前默认输出（只有它才真的在出声） */
  default?: boolean
}

export interface ScreenSources {
  screens: ScreenSourceItem[]
  windows: ScreenSourceItem[]
  audio_inputs: AudioDeviceItem[]
  /** sidecar 的 sys.platform：'darwin' | 'win32' | 'linux' */
  platform?: string
  ffmpeg: {
    ok: boolean
    path?: string | null
    version?: string | null
    source?: string
    error?: string
  }
}

export interface ScreenCastState {
  running: boolean
  target?: 'dlna' | 'browser' | null
  source?: { type: string; id?: string; title?: string } | null
  device_id?: string | null
  session_id?: string | null
  watch_url?: string | null
  url?: string | null
  viewers?: number
  error?: string | null
  started_at?: number | null
  ffmpeg?: ScreenSources['ffmpeg']
}

export interface ScreenCastStartParams {
  target: 'dlna' | 'browser'
  device_id?: string
  source: { type: 'screen' | 'window'; id?: string; title?: string }
  audio?: {
    mic?: boolean
    mic_device?: string
    system?: boolean
    system_device?: string
  }
  video?: {
    width?: number
    height?: number
    fps?: number
    bitrate?: string
  }
}
