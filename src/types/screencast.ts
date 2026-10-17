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
  /** 给人看的名字（Linux 的 PulseAudio 源名是 alsa_output.pci-…，不能直接摆在界面上） */
  title?: string
  /** macOS 上由 HanCast 自建的 Audio Tap 环回（索引会变，后端每次按名称重解析） */
  tap?: boolean
  /** Windows WASAPI 环回：ffmpeg 没有这个输入设备，由 sidecar 灌 PCM */
  loopback?: boolean
  /** Linux PipeWire/PulseAudio 上某条输出的镜像（<sink>.monitor），抓它=抓系统声音 */
  monitor?: boolean
  /** 这块环回/镜像对应的正是系统当前默认输出（只有它才真的在出声） */
  default?: boolean
}

/** 「需要下载运行依赖」弹窗里的单个依赖项 */
export interface FfmpegDepItem {
  key: string
  label: string
  required: boolean
  found: boolean
  path: string | null
  /** manual=用户指定 / system=本机已装 / downloaded=本程序下的 */
  source: string | null
  version: string | null
}

export interface FfmpegStatus {
  ok: boolean
  path?: string | null
  version?: string | null
  source?: string | null
  ffprobe_path?: string | null
  items: FfmpegDepItem[]
  missing: string[]
  optional_missing: string[]
  managed_dir: string
  platform: string
  arch: string
  install_commands: string[]
  download: { available?: boolean; kind?: string; url?: string }
  error?: string | null
}

export interface FfmpegDownloadTask {
  status: 'idle' | 'pending' | 'done' | 'error'
  stage?: string
  task_id?: string
  received?: number
  total?: number
  percent?: number | null
  speed?: number
  elapsed?: number
  url?: string
  error?: string | null
  result?: { ok: boolean; paths: string[]; dir: string } | null
}

export interface ScreenSources {
  screens: ScreenSourceItem[]
  windows: ScreenSourceItem[]
  audio_inputs: AudioDeviceItem[]
  /** sidecar 的 sys.platform：'darwin' | 'win32' | 'linux' */
  platform?: string
  /** Linux 抓屏能力快照：Wayland 会话里 helper 是否可用（不可用就会退回黑屏） */
  capture?: { wayland?: boolean; helper?: string | null; reason?: string }
  ffmpeg: FfmpegStatus
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
