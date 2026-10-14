export interface AppSettings {
  usn: string
  friendly_name: string
  version: string
  media_port: number
  default_device: string | null
  network_interface: string
  settings: Record<string, unknown>
}

export interface NetworkInterface {
  name: string
  ip: string
  netmask: string
  is_default: boolean
}

export interface NetworkInterfacesInfo {
  interfaces: NetworkInterface[]
  selected: string
}

export type ParamType = 'number' | 'bool' | 'enum' | 'text'

export interface ParamSpec {
  key: string
  section: string
  type: ParamType
  default: number | boolean | string
  /** number 型才有 */
  min?: number
  max?: number
  unit?: string
  /** enum 型才有 */
  options?: string[]
  label_zh: string
  label_en: string
  help_zh?: string
  help_en?: string
  /** 后端 specs() 附带：当前生效值 */
  value: number | boolean | string
  /** 是否被覆盖过（UI 用来标「已改」） */
  overridden: boolean
}

export interface SaveParamsResult {
  ok: boolean
  errors: string[]
  specs: ParamSpec[]
}

export interface DeviceLostEvent {
  id: string
}

export interface CastErrorEvent {
  message: string
}
