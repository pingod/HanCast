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

export interface DeviceLostEvent {
  id: string
}

export interface CastErrorEvent {
  message: string
}
