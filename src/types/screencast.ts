/**
 * 屏幕投屏（电脑投屏到 DLNA 电视 / 浏览器观看）
 */

export interface ScreenSourceItem {
  id: string
  title: string
}

export interface AudioDeviceItem {
  name: string
  kind: 'mic' | 'system'
}

export interface ScreenSources {
  screens: ScreenSourceItem[]
  windows: ScreenSourceItem[]
  audio_inputs: AudioDeviceItem[]
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
  target?: 'dlna' | 'webrtc' | null
  source?: { type: string; id?: string; title?: string } | null
  device_id?: string | null
  room_id?: string | null
  watch_url?: string | null
  url?: string | null
  viewers?: number
  error?: string | null
  started_at?: number | null
  ffmpeg?: ScreenSources['ffmpeg']
}

export interface ScreenCastStartParams {
  target: 'dlna' | 'webrtc'
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
