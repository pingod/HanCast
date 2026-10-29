/** 规则包给出的一个可选画质档位 */
export interface MediaQuality {
  id: string
  label: string
}

export interface MediaInfo {
  media_type: 'file' | 'url'
  uri: string
  title: string
  duration: number | null
  mime_type: string
  file_size: number | null
  thumbnail: string | null
  /** 可选画质（规则包声明才有；直链/本地文件为空） */
  qualities?: MediaQuality[]
  /** 本次交付用的画质 id */
  quality?: string
  /** 解析来源页地址：切换画质时按它重新解析 */
  source_url?: string
  /** 本地文件的原始路径（投屏历史靠它重新解析；后端不回这个字段，由前端补） */
  file_path?: string
}

export type MediaInputState = 'idle' | 'dragover' | 'input' | 'parsing' | 'ready'
