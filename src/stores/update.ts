import { ref, onMounted, onUnmounted } from 'vue'
import { defineStore } from 'pinia'
import type { UpdateAsset, UpdateInfo, UpdatePlan, UpdateTask } from '@/types/update'
import {
  checkUpdate,
  getParams,
  ignoreUpdateVersion as apiIgnoreUpdateVersion,
  isStoreVersion,
  openUpdateDir,
  saveParams,
  updateApply,
  updateDownloadCancel,
  updateDownloadStart,
  updateDownloadStatus,
  updateInstallPlan,
  updateReleaseInfo,
} from '@/api/commands'
import type { UnlistenFn } from '@tauri-apps/api/event'

/** 轮询间隔：与 FFmpeg 依赖下载同一个节奏（stores/screencast.ts:134） */
const POLL_MS = 1000

/** 壳层能自己装掉的产物类型（与 `update/manager.py` 的 AUTO_APPLY 同一份名单） */
const AUTO_KINDS = ['nsis', 'dmg', 'appimage']

/** GitHub 镜像那两个后台参数的 key（`params.py` 的 network 分组，改那边要一起改这里） */
const PARAM_MIRROR_ENABLED = 'github.mirror_enabled'
const PARAM_MIRROR_PREFIX = 'github.mirror_prefix'

export const useUpdateStore = defineStore('update', () => {
  // ── State ──
  const updateInfo = ref<UpdateInfo | null>(null)
  const showModal = ref(false)
  const loading = ref(false)
  const error = ref('')
  const storeVersion = ref(false)

  // 下载与安装
  const downloadTask = ref<UpdateTask | null>(null)
  const downloading = ref(false)
  const applying = ref(false)
  const plan = ref<UpdatePlan | null>(null)
  /** 产物清单加载中（弹窗里换一种安装包时用它禁用控件） */
  const assetsLoading = ref(false)

  // 镜像开关：这两个值就是那两个后台参数的影子
  const mirrorEnabled = ref(false)
  const mirrorPrefix = ref('')
  const mirrorSaving = ref(false)

  let unlistenFns: UnlistenFn[] = []
  let listenersSetup = false
  let pollTimer: number | undefined
  /**
   * 代际号：谁最后发起「起下载 / 对齐状态」，谁才有权写任务表与 `downloading`。
   *
   * 这条不是防御性代码，是浏览器实测抓出来的竞态：弹窗打开时 `resumeDownload()` 会
   * 发一次状态查询，用户手快立刻点「立即下载并安装」——`startDownload` 先落下
   * `downloading = true`，随后**那条更早发出、更晚返回**的 resume 回包带着旧的
   * `idle` 回来，把 `downloading` 按回 false 并把任务表覆盖成 idle。表现是：
   * 后端确实在下载（下一次轮询进度照常往上涨），界面却不显示进度条、主按钮也不
   * 切成「取消」（tools/verify-update-ui.mjs 的场景 A 第一次跑就是红在这三条上）。
   */
  let epoch = 0
  /** 起下载的请求是否还在路上（此期间任何 idle / 非 pending 的回包都不许作数） */
  let starting = false

  async function init() {
    storeVersion.value = await isStoreVersion()
  }

  // ── 检查 ──

  /** 手动检查更新（设置页按钮与弹窗里的「重新检查」都走这里） */
  async function check(currentVersion: string, force = true) {
    // 微软商店版本由 Store 自动管理更新，跳过检查
    if (storeVersion.value) {
      error.value = 'store_version'
      return null
    }
    loading.value = true
    error.value = ''
    try {
      const result = await checkUpdate(currentVersion, force)
      if (result.has_update) {
        updateInfo.value = result
        showModal.value = true
        // 顺手把镜像开关的当前状态读出来，弹窗一开就能看到它
        void loadMirrorSettings()
      } else {
        // Sidecar 把失败原因放在返回体的 `error` 键里而不是抛错。这里必须把它显出来，
        // 否则用户只看到「已是最新版本」，而真相是「根本没查到」——v2.4.11 之前
        // 这个键连类型里都没有，界面只能一律报「检查失败」。
        if (result.error) {
          error.value = result.error
        } else if (force) {
          error.value = 'no_update' // 用于 UI 提示"已是最新版本"
        }
      }
      return result
    } catch (e) {
      error.value = String(e)
      return null
    } finally {
      loading.value = false
    }
  }

  /**
   * 换一种安装包（Linux 上 deb / rpm / AppImage / Arch 四选一）。
   *
   * 重新取一次清单，而不是只在前端已有的 `assets` 里挑：`check_update` 那次可能
   * 压根没带产物（无更新时不给），也可能已经过了 60 秒缓存。
   */
  async function loadAssets(version?: string) {
    assetsLoading.value = true
    try {
      const info = await updateReleaseInfo(version || updateInfo.value?.latest || '')
      if (updateInfo.value) {
        updateInfo.value = { ...updateInfo.value, ...info } as UpdateInfo
      }
      return info
    } catch (e) {
      error.value = String(e)
      return null
    } finally {
      assetsLoading.value = false
    }
  }

  // ── 镜像开关 ──

  async function loadMirrorSettings() {
    try {
      const specs = await getParams()
      const on = specs.find((s) => s.key === PARAM_MIRROR_ENABLED)
      const pre = specs.find((s) => s.key === PARAM_MIRROR_PREFIX)
      mirrorEnabled.value = Boolean(on?.value ?? on?.default ?? false)
      mirrorPrefix.value = String(pre?.value ?? pre?.default ?? '')
    } catch {
      // 读不到参数表就当没开：更新弹窗不能因为参数面板挂了就不给下载
    }
  }

  /** 保存镜像开关；返回失败原因（界面要能说出为什么没改成），成功返回空串 */
  /**
   * 保存镜像相关参数；返回失败原因（界面要能说出为什么没改成），成功返回空串。
   *
   * `prefix === null` 表示**这次不动前缀**：勾上开关那一刻，前缀可能还没从参数表
   * 异步读回来，拿输入框的空串一起提交，等于把用户配好的镜像地址清掉
   * （tools/verify-update-ui.mjs 的「前缀默认值来自参数表」那条判据就是为它加的）。
   */
  async function saveMirrorSettings(enabled: boolean, prefix: string | null): Promise<string> {
    mirrorSaving.value = true
    try {
      const payload: Record<string, boolean | string> = { [PARAM_MIRROR_ENABLED]: enabled }
      if (prefix !== null) payload[PARAM_MIRROR_PREFIX] = prefix
      const res = await saveParams(payload)
      if (!res?.ok) return (res?.errors || []).join('；') || '保存失败'
      mirrorEnabled.value = enabled
      if (prefix !== null) mirrorPrefix.value = prefix
      return ''
    } catch (e) {
      return String(e)
    } finally {
      mirrorSaving.value = false
    }
  }

  // ── 下载 ──

  function stopPolling() {
    if (pollTimer !== undefined) {
      window.clearInterval(pollTimer)
      pollTimer = undefined
    }
  }

  /**
   * 轮询一次任务表。`gen` 是调用方发起时领的**代际号**：只有仍是当代的调用才允许写状态。
   *
   * 这条守卫不是洁癖，是浏览器实测抓出来的竞态：弹窗一打开 `resumeDownload()` 会查一次
   * 状态，用户紧接着点「立即下载并安装」——那条更早发出、更晚返回的 resume 回包带着旧的
   * `idle` 落回 store，把 `downloading` 按回 false、把任务表覆盖成 idle。表现就是：
   * 后端确实在下载（下一轮进度照常往上涨），界面却没有进度条、主按钮也不切成「取消」
   * （`tools/verify-update-ui.mjs` 场景 A 第一次跑就红在这三条上）。
   */
  async function pollDownload(gen: number) {
    try {
      const task = await updateDownloadStatus()
      if (gen !== epoch) return
      downloadTask.value = task
      // 起下载的请求还在路上时，`idle` 只说明「后端还没收到」，不许当终态处理
      if (task.status === 'idle' && starting) return
      if (task.status !== 'pending') {
        stopPolling()
        downloading.value = false
        if (task.status === 'done') void refreshPlan()
      }
    } catch (e) {
      if (gen !== epoch) return
      stopPolling()
      downloading.value = false
      downloadTask.value = { status: 'error', stage: 'failed', error: String(e) }
    }
  }

  /** 起下载；`asset` 为空 = 用本平台默认产物。同名任务进行中时后端幂等返回，不会重复起线程 */
  async function startDownload(asset?: UpdateAsset | string) {
    if (downloading.value) return
    const name = typeof asset === 'string' ? asset : asset?.name || ''
    const gen = ++epoch
    downloading.value = true
    starting = true
    error.value = ''
    plan.value = null
    // 本地先占位：后端返回之前界面就该有「连接中」的形状
    downloadTask.value = { status: 'pending', stage: 'connecting', asset: name }
    try {
      const task = await updateDownloadStart(name, updateInfo.value?.latest || '')
      if (gen !== epoch) return
      downloadTask.value = task
      stopPolling()
      pollTimer = window.setInterval(() => void pollDownload(gen), POLL_MS)
    } catch (e) {
      if (gen !== epoch) return
      stopPolling()
      downloading.value = false
      downloadTask.value = { status: 'error', stage: 'failed', error: String(e) }
    } finally {
      // 只在本代仍是当代时才收：否则下一代的 start 会被这次收尾误清掉
      if (gen === epoch) starting = false
    }
  }

  async function cancelDownload() {
    try {
      const gen = epoch
      const task = await updateDownloadCancel()
      // 取消是**请求**不是结果：终态仍由轮询给出。这里若直接把界面写成「已取消」，
      // 用户会看到「已取消」而磁盘还在写。
      if (gen === epoch && task.status !== 'pending') {
        downloadTask.value = task
        stopPolling()
        downloading.value = false
      }
    } catch (e) {
      error.value = String(e)
    }
  }

  /**
   * 弹窗重开时与后端对齐一次。
   *
   * 定时器挂在 store 上而不是组件上，但用户关掉窗口再打开时界面是全新的；
   * 不对齐就会停在「下载中」而后端早完成了——与 stores/screencast.ts:153 同一个坑。
   */
  async function resumeDownload() {
    const gen = ++epoch
    try {
      const task = await updateDownloadStatus()
      // 起下载正在路上（或已有更新的一代）时，这条旧回包直接作废
      if (gen !== epoch || starting) return
      downloadTask.value = task
      if (task.status === 'pending') {
        downloading.value = true
        stopPolling()
        pollTimer = window.setInterval(() => void pollDownload(gen), POLL_MS)
      } else {
        downloading.value = false
        if (task.status === 'done') void refreshPlan()
      }
    } catch {
      // 浏览器模式或 sidecar 没起来：没有下载这回事，界面按「还没下」渲染
    }
  }

  async function refreshPlan() {
    const task = downloadTask.value
    try {
      plan.value = await updateInstallPlan()
    } catch (e) {
      // 取装法失败**不能**把安装这条路锁死：按任务表自己拼一份够用就用的计划。
      // 真出过这档事——壳层漏转发 `update_install_plan` 时，打包版每次下载完
      // 「退出并安装」都是灰的，而浏览器 mock 有这条 case，测试全绿看不出来。
      const kind = task?.kind || ''
      if (task && task.status === 'done' && kind) {
        plan.value = {
          kind, asset_kind: kind, path: task.dest || '', asset: task.asset,
          version: task.version, sha256: task.sha256_actual, verified: task.verified,
          auto: AUTO_KINDS.includes(kind),
        }
      } else {
        plan.value = null
      }
      error.value = String(e)
    }
  }

  /**
   * 退出并安装。
   *
   * **调用前把话说完**：这条路成功就不再回到本进程（应用要退出）。
   * `manual` 为真表示这类产物要提权（deb/rpm/Arch），命令在 `plan.command` 里，
   * 由界面给复制按钮——壳层不替用户 sudo。
   */
  async function applyUpdate(): Promise<{ ok: boolean; manual?: boolean; error?: string }> {
    if (applying.value) return { ok: false, error: 'already' }
    applying.value = true
    try {
      const res = await updateApply()
      if (res.manual) {
        applying.value = false
        await refreshPlan()
      }
      return { ok: Boolean(res.ok), manual: res.manual }
    } catch (e) {
      applying.value = false
      const msg = String(e)
      error.value = msg
      return { ok: false, error: msg }
    }
  }

  async function revealDownload() {
    try {
      await openUpdateDir()
    } catch (e) {
      error.value = String(e)
    }
  }

  // ── 弹窗与忽略 ──

  async function ignoreVersion() {
    if (!updateInfo.value) return
    try {
      await apiIgnoreUpdateVersion(updateInfo.value.latest)
    } catch (e) {
      console.error('Failed to ignore update version:', e)
    }
    closeModal()
  }

  function closeModal() {
    showModal.value = false
  }

  /**
   * 打开发布页兜底（只有我们自己的 GitHub Release 页；v2.4.11 之前这里还挂着
   * gitee 与微软商店两个渠道）。
   *
   * 必须走 `plugin-shell.open`：Tauri 的 webview 里 `window.open` 不会交给系统浏览器，
   * 老代码那一句在打包版上是静默失败的（被删掉的 DownloadModal 用的正是 shell.open，
   * 拆掉它的时候这个差别差点跟着一起消失）。浏览器 dev 模式没有壳层，才退回 `window.open`。
   */
  async function openReleasePage() {
    const url = updateInfo.value?.url
    if (!url) return
    try {
      const { open } = await import('@tauri-apps/plugin-shell')
      await open(url)
    } catch {
      try {
        window.open(url, '_blank')
      } catch {
        error.value = '打不开发布页'
      }
    }
  }

  // ── 事件（Rust 启动自检） ──

  async function setupListeners() {
    if (listenersSetup) return
    listenersSetup = true

    await init()

    // 商店版本不监听自动更新事件
    if (storeVersion.value) return

    try {
      const { listen } = await import('@tauri-apps/api/event')

      // Rust 启动时自动检查，发现更新后 emit 此事件（载荷就是 check_update 的返回体）
      const unlisten = await listen<UpdateInfo>('update-available', (event) => {
        updateInfo.value = event.payload
        showModal.value = true
        void loadMirrorSettings()
        void resumeDownload()
      })
      unlistenFns.push(unlisten)

      // 下载完成由 Sidecar 直接报一声：轮询是 1 秒一次，靠它对齐会让人多等一秒，
      // 而「下完了却还转着圈」正是用户最容易以为卡死的那一刻
      const unlistenDone = await listen<{ version?: string; asset?: string; path?: string; verified?: boolean }>(
        'update_download_done', () => { void resumeDownload() })
      unlistenFns.push(unlistenDone)
    } catch {
      // Not in Tauri environment — ignore
    }
  }

  function cleanup() {
    unlistenFns.forEach((fn) => fn())
    unlistenFns = []
    listenersSetup = false
    stopPolling()
  }

  onMounted(() => {
    setupListeners()
  })

  onUnmounted(() => {
    cleanup()
  })

  return {
    // state
    updateInfo,
    showModal,
    loading,
    error,
    storeVersion,
    downloadTask,
    downloading,
    applying,
    plan,
    assetsLoading,
    mirrorEnabled,
    mirrorPrefix,
    mirrorSaving,
    // actions
    init,
    check,
    loadAssets,
    loadMirrorSettings,
    saveMirrorSettings,
    startDownload,
    cancelDownload,
    resumeDownload,
    refreshPlan,
    applyUpdate,
    revealDownload,
    ignoreVersion,
    closeModal,
    openReleasePage,
  }
})
