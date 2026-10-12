/**
 * 应用内日志查看（排错用）
 */

export interface LogLine {
  ts: string
  module: string
  level: string
  msg: string
  raw: string
}

export interface LogModule {
  id: string
  label: string
}

export interface LogPage {
  lines: LogLine[]
  total: number
  truncated: boolean
  path: string
  level: string
  modules: LogModule[]
}

export interface LogQuery {
  limit?: number
  module?: string
  level?: string
  keyword?: string
  include_older?: boolean
}
