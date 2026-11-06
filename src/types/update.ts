/**
 * 应用内自更新的类型（Sidecar `update/manager.py` 与壳层 `update_*` 命令的对外契约）
 *
 * 三个形状各有来源，别混：
 * - `UpdateInfo`   ← Sidecar `check_update`（v2.4.12 起带产物清单；老字段一个都没改）
 * - `UpdateTask`   ← Sidecar `update_download_status`（任务表，进度靠轮询）
 * - `UpdatePlan`   ← Sidecar `update_install_plan`；`UpdateApplyResult` ← 壳层 `update_apply`
 */

/** 一个可下载的产物（Release asset） */
export interface UpdateAsset {
  name: string
  url: string
  size: number
  /** GitHub 声明的 sha256（`digest` 去掉 `sha256:` 前缀）；空串 = 远端没给，无法校验 */
  sha256: string
  /** dmg | nsis | msi | deb | rpm | arch | appimage */
  kind: string
  os: string
  arch: string
  /** 与本机指令集是否一致；false 表示「能装，但不是原生包」 */
  arch_match: boolean
  priority: number
}

/** `check_update` 的返回 */
export interface UpdateInfo {
  has_update: boolean
  current: string
  latest: string
  url: string
  body: string
  /** 只剩 'github'：gitee 那条源是原作者的仓库，v2.4.12 从更新链路里删掉了 */
  source: 'github' | ''
  /** 取数失败的原因（v2.4.11 之前这个键存在但类型里没有，前端读不到） */
  error?: string
  /** 该版本被设过「不再提示」，只有非强制检查才会带 */
  ignored?: boolean
  /** 命中走的是国内镜像（值就是 `镜像`），用于界面如实标注来源 */
  via?: string
  platform?: { os: string; arch: string }
  assets?: UpdateAsset[]
  selected?: UpdateAsset | null
  /** 非空 = 这版没有本平台产物（值 = 平台名），界面要直说而不是「下载失败」 */
  no_asset_for_platform?: string
}

/** 下载任务表（`update_download_status`） */
export interface UpdateTask {
  status: 'idle' | 'pending' | 'done' | 'error' | 'cancelled'
  stage?: string
  task_id?: string
  version?: string
  asset?: string
  kind?: string
  url?: string
  dest?: string
  total?: number
  received?: number
  /** 无 Content-Length 时为 null —— 界面走不定长进度条 */
  percent?: number | null
  speed?: number
  elapsed?: number
  via?: string
  /** 从已有的半份文件接着下的起始字节（断点续传，0 = 从头） */
  resumed_from?: number
  sha256_expected?: string
  sha256_actual?: string
  verified?: boolean
  note?: string
  error?: string | null
}

/** `update_install_plan`：`kind == 'command'` 表示要提权，只能把命令交给用户 */
export interface UpdatePlan {
  ok?: boolean
  kind: 'dmg' | 'nsis' | 'appimage' | 'command' | string
  asset_kind?: string
  path: string
  asset?: string
  version?: string
  sha256?: string
  verified?: boolean
  auto: boolean
  command?: string
  note_zh?: string
  note_en?: string
}

/** 壳层 `update_apply` 的回执 */
export interface UpdateApplyResult {
  ok: boolean
  manual?: boolean
  kind?: string
  path?: string
  log?: string
  helper?: string
  plan?: UpdatePlan
}

/** 更新弹窗的状态机（UI 侧，不与任务表状态混用） */
export type UpdateModalState = 'hidden' | 'checking' | 'available' | 'latest' | 'error'
