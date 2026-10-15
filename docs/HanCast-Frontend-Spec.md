# HanCast — 前端设计规格

> 本文档回答：HanCast 桌面端（Tauri 2 + Vue 3）的界面层是怎么搭起来的——窗口壳层、路由视图、组件与 Store 全量清单、真实数据流时序、样式与国际化机制、浏览器 mock 模式，以及写代码前必须知道的约束与坑。
>
> 最后校对：2026-10-15，对应版本 v2.3.10（`package.json`，tag `v2.3.10`）

术语约定：**Sidecar** = Python 后台进程（`hancast-backend/`，打包为 `hancast-sidecar`）；**壳层** = Rust/Tauri 层（`src-tauri/`）；**规则包** = JS 解析插件 zip；**声明式规则** = JSON 站点规则；**屏幕投屏** = screencast（把电脑屏幕投出去）；**接收投屏** = 本机作为 DLNA 渲染端被别的设备投；**投屏确认** = Device Guard；**后台参数** = `params.py` 可调项。

所有行号以 2026-10-15 的工作区代码为准。未能核实的论断一律标注「⚠️ 未验证」。

---

## 1. 技术栈与版本（实数）

### 1.1 前端（`package.json`）

| 类别 | 包 | 版本声明 |
|---|---|---|
| 框架 | `vue` | ^3.4.0 |
| 状态 | `pinia` | ^2.1.7 |
| 路由 | `vue-router` | ^4.2.5 |
| 国际化 | `vue-i18n` | ^9.9.0 |
| Tauri 桥 | `@tauri-apps/api` | ^2.0.0 |
| Tauri 插件 | `@tauri-apps/plugin-dialog` | ^2.7.1 |
| Tauri 插件 | `@tauri-apps/plugin-shell` | ^2.0.0 |
| 构建 | `vite` | ^5.0.0 |
| 构建 | `@vitejs/plugin-vue` | ^5.0.0 |
| 类型 | `typescript` | ^5.3.0 |
| 类型检查 | `vue-tsc` | ^2.2.12 |

npm scripts：`dev` = `vite`、`build` = `vue-tsc && vite build`、`build:sidecar` = `uv`（Sidecar 打包）。

**没有 UI 组件库**。`package.json` 中不存在 Element Plus / Ant Design Vue 等任何组件库依赖；Modal、下拉框（`CustomSelect`）、开关等全部手写（`src/components/Modal.vue`、`src/components/CustomSelect.vue`）。样式为纯 CSS（无 Sass/Less/Tailwind）。

### 1.2 TypeScript / Vite 配置

- `tsconfig.json`：`target: ES2020`、`strict: true`、`moduleResolution: bundler`、路径别名 `@/*` → `src/*`。
- `vite.config.ts`：alias `@` → `src`；`base: './'`（Tauri 打包后走相对路径加载）；dev server 端口 `1420` 且 `strictPort`。
- `env.d.ts`：为 `window.__TAURI_INTERNALS__`（`invoke` / `transformCallback` / `metadata`）补类型声明——这是浏览器 mock 模式的检测点（见 §9）。

### 1.3 壳层（`src-tauri/Cargo.toml`）

| 依赖 | 版本 | 用途 |
|---|---|---|
| `tauri` | 2（features：`protocol-asset`、`tray-icon`） | 窗口/托盘/asset 协议 |
| `tauri-plugin-shell` | 2 | 打开外部链接、执行进程 |
| `tauri-plugin-clipboard-manager` | 2 | Rust 侧剪贴板（见 §10.1） |
| `tauri-plugin-dialog` | 2 | 原生文件选择/消息框 |
| `tauri-plugin-autostart` | 2 | 开机自启（非 MSIX 平台） |
| `tauri-plugin-single-instance` | 2 | 单实例聚焦 |
| `serde` / `serde_json` | 1 | 序列化 |
| `tokio` | 1（sync/macros/rt-multi-thread/time） | 异步运行时 |
| `base64` | 0.22 | 插件 zip 导入等 |
| `mime_guess` | 2 | 本地文件 MIME 推断 |
| `chrono` | 0.4 | 时间戳 |
| `windows` | 0.62（仅 `cfg(windows)`，features：ApplicationModel/Foundation） | MSIX StartupTask 自启动 |

`Cargo.toml` 版本 2.3.5，注释注明「version synced from tauri.conf.json」。edition 2021。

### 1.4 Sidecar（`hancast-backend/pyproject.toml`，与前端相关的部分）

`hancast-sidecar` 2.3.5，`requires-python >= 3.11`。运行时依赖：`requests`、`lxml`、`netifaces`、`aiohttp`（浏览器观看 HLS 分发，注释说明 aiortc/WebRTC 已移除）、`pyaudiowpatch`（仅 `sys_platform == 'win32'`，Windows WASAPI 环回采系统声音）、`quickjs`（规则包 JS 引擎）、`cssselect`。构建工具链 `hatchling` + `uv`；发布用 `nuitka`（dev 依赖）。

---

## 2. 窗口与壳层配置

### 2.1 主窗口参数（`src-tauri/tauri.conf.json`）

| 参数 | 值 | 行号 | 说明 |
|---|---|---|---|
| `label` / `title` | `main` / HanCast | — | 单窗口应用 |
| `width` × `height` | 434 × 634 | — | 竖长小窗 |
| `minWidth` × `minHeight` | 346 × 472 | — | Rust 侧恢复窗口尺寸时也做同样下限保护（`src-tauri/src/lib.rs:1091-1095`） |
| `maxWidth` × `maxHeight` | 800 × 900 | — | 另在 setup 里 `set_maximizable(false)`（`lib.rs:1108`） |
| `decorations` | `false` | :25 | 无边框 → 必须自绘 TitleBar |
| `transparent` | `true` | :26 | 背景透明，配合 CSS 半透明背景色 |
| `visible` | `false` | :28 | 启动隐藏，由 Rust setup 决定何时显示（`--minimized`/`--autostart` 启动时保持最小化，`lib.rs:1081-1085`） |
| `dragDropEnabled` | `true` | :29 | Tauri 原生文件拖拽开启（代价见 §10.2） |
| `withGlobalTauri` | `true` | :13 | 全局 `window.__TAURI__` |

窗口尺寸/位置持久化：Rust `WindowConfig` 默认 434×634（`lib.rs:32-39`），移动/缩放 500ms 防抖写入 `window_config.json`，下次启动恢复。

### 2.2 为什么自定义标题栏

`decorations: false` 后系统标题栏消失，由 `src/components/TitleBar.vue` 自绘（Logo + 标题 + 主题切换 + 设置 + 最小化 + 关闭）。拖拽区域的实现证据在 `TitleBar.vue:47-48` 注释：

> data-tauri-drag-region 是 Tauri 官方的拖拽机制，走 IPC start_dragging；-webkit-app-region 在 macOS WKWebView 上是空操作，必须两者都留。

关闭按钮**不退出进程**：Rust 侧 `CloseRequested` 回调 `prevent_close()` + `hide()`（`lib.rs:1123-1130`），`close_window` 命令同样是隐藏（`lib.rs:156-160`）——即「关闭隐藏到托盘」。首次关闭时 TitleBar 弹确认框告知（`TitleBar.vue:88`，`localStorage 'hancast-close-modal-dismissed'` 记「不再提醒」）。

### 2.3 托盘（`lib.rs`）

- 托盘菜单：复制投屏地址 / 显示窗口 / 开机自启开关 / 退出（`lib.rs:959-991`、`1159-1199`）。
- 左键单击托盘：show + unminimize + set_focus（`lib.rs:1183-1198`）。
- 每秒轮询 `get_cast_url` 刷新托盘菜单里的投屏地址（`lib.rs:1243-1303`）。
- 单实例：二次启动时聚焦已有窗口（`tauri-plugin-single-instance`，`lib.rs:1003-1009`）。

### 2.4 开机自启动

两条实现路径（`lib.rs:855-957`）：

1. **MSIX（微软商店版）**：走 Windows `StartupTask` API（`windows` crate，仅 `cfg(windows)`）；商店版检测 `is_store_version`（`lib.rs:698-712`）。
2. **其他平台**：`tauri-plugin-autostart`。

`set_autostart` 成功后 Rust 主动 emit `autostart-changed` 事件（`lib.rs:955`），前端 `settings` store 监听同步（`src/stores/settings.ts:28-38`）——因为托盘菜单里也能切自启，前端不能只信自己的本地状态。

### 2.5 CSP 与 `unsafe-eval`（有提交历史证据）

`tauri.conf.json:34` 的 CSP：

```
default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' asset: http://asset.localhost blob: data:; connect-src 'self' ipc: tauri: http://ipc.localhost; font-src 'self' data:
```

`unsafe-eval` **不是随手加的，也不能删**。git 提交历史证据：

```
953801e 2026-10-11 fix: 恢复 CSP unsafe-eval（vue-i18n 运行时编译依赖，缺失导致窗口空白）+ 三平台补 ffmpeg 资源；版本 2.2.4
```

即 vue-i18n（非预编译模式）需要运行时 `new Function` 编译消息模板，去掉 `unsafe-eval` 后整个窗口白屏。该提交是「恢复」，说明曾尝试移除并翻车。

### 2.6 asset 协议与打包资源

- assetProtocol：`enable: true`，scope `**/*`（`tauri.conf.json:35-40`）——本地文件缩略图/播放经 `asset:` 协议进 WebView。
- `externalBin`：`hancast-sidecar/hancast-sidecar`（:46）；resources 含 mpv、ffmpeg、hancast_sidecar（:47-52）。
- Windows NSIS 安装器语言：SimpChinese + English（:53-57）。

### 2.7 capabilities（`src-tauri/capabilities/default.json`）

窗口范围 `["main"]`。权限：`core:default`、`core:window:allow-start-dragging`（自定义标题栏拖拽）、`core:window:allow-minimize/hide/show/set-focus`、`core:event:default`、`shell:allow-open/execute/spawn`、`clipboard-manager:allow-write-text/read-text`、`dialog:default`、`autostart:default`。

### 2.8 壳层其他与 UI 相关的行为

- 启动 3 秒后自动检查更新，有新版 emit `update-available`（`lib.rs:1201-1241`）。
- `stop_cast` 返回体中的 `control_failed` 字段被 Rust 注释点名「不能丢」（`lib.rs:455-458`）：本地会话已释放但电视未确认停止时，前端靠它显示 StopWarningBanner（见 §10.7）。

---

## 3. 路由与视图结构

### 3.1 路由表（`src/router/index.ts`）

| 路径 | name | 组件 | 加载方式 |
|---|---|---|---|
| `/` | home | `src/views/HomeView.vue` | 同步 import |
| `/settings` | settings | `src/views/SettingsView.vue` | 懒加载 `() => import(...)` |

- 历史模式：`createWebHashHistory`（hash 路由）。Tauri 打包后页面经自定义协议加载，hash 模式避免深层路径 404。
- `src/views/CastControllerView.vue`（323 行，整窗控制器变体）**不在路由表里**，全仓 grep 无任何 import——死代码（见 §10.11）。

### 3.2 视图清单（3 个）

| 视图 | 行数 | 状态 |
|---|---|---|
| `HomeView.vue` | ~65 | 在用，主页面 |
| `SettingsView.vue` | 967 | 在用，设置页 |
| `CastControllerView.vue` | 323 | 死代码 |

### 3.3 HomeView 布局（ASCII）

挂载点行号来自 `src/views/HomeView.vue` 模板：

```
┌──────────────────────────────────────┐
│ TitleBar (:24)          [🌙][⚙][─][✕]│ 32px，可拖拽
├──────────────────────────────────────┤
│ ┌ main ────────────────────────────┐ │
│ │ 屏幕投屏入口条 (:27-44)            │ │ 点击打开 ScreenCastPanel
│ │ MediaInput (:46)                  │ │ 拖拽/粘贴/URL 输入 + 预览
│ │ StopWarningBanner (:48-51)        │ │ 条件渲染：停止未送达提示
│ │ DeviceList (:52)                  │ │ 设备卡片列表（含 DeviceCard）
│ └──────────────────────────────────┘ │
│ Footer (:54)                          │ 仅设备列表为空时显示
├──────────────────────────────────────┤
│ CastController (:56)   ← Teleport 模态│ 投屏中控制器（多设备）
│ ScreenCastPanel (:58)  ← Teleport 模态│ 屏幕投屏面板
│ AutostartPromptModal (:60-63)         │ 首次投屏成功后弹自启询问
└──────────────────────────────────────┘
```

`HomeView.vue:48-51` 处注释解释了 StopWarningBanner 为什么挂在页面层：控制器和投屏条停止后都会关闭，只有页面留得住这句话。

### 3.4 SettingsView 布局（`src/views/SettingsView.vue`，967 行）

自带 header（返回键 + 标题，`data-tauri-drag-region` 于 :257，同样可拖窗）。分区自上而下：

| 分区 | 内容 | 证据 |
|---|---|---|
| General | 主题（CustomSelect → `themeStore.apply`）、语言（CustomSelect → `locale.value`，:287）、开机自启 toggle（`settingsStore.toggleAutostart`） | :273-287 |
| Cast（接收投屏） | 设备名 friendly_name（blur 保存，:63-65）、网络接口（`listNetworkInterfaces`，:42-48） | :325 |
| Device Guard（投屏确认） | enabled 开关、confirm_timeout（10/15/30/60 秒，:353）、GuardDeviceList 展开区（:383） | :353-385 |
| 后台参数 | `getParams`/`saveParams`；本地 `paramDraft` 草稿；按 section 分组；类型 text/string/number/enum/bool；`overridden` 显示「已改」徽标；单项重置（:199-201） | :448 |
| 解析插件 | PluginSettings 展开区（:522），入口按钮显示 pluginList 徽标计数（:215-222） | :522 |
| About | 版本号；查看日志（`logsStore.open()`）；导出日志（`exportLogs`）；「检查更新」或商店版时显示 MS Store 链接（:550-572）；仓库/QQ 群/许可证外链 | — |

### 3.5 App.vue 全局层

`src/App.vue:34-43` 在 router-view 之外全局挂载 4 个模态：`GuardConfirmModal`(:37)、`UpdateModal`(:39)、`LogsPanel`(:41)、`AddDeviceModal`(:42)——它们需要跨路由可弹。另：非 DEV 构建禁用右键菜单（:24-26）；`onMounted` 拉取 `fetchSettings`（:23）。

---

## 4. 组件清单（20 个，`src/components/`）

> 数量为实际 `ls` 清点结果：20 个 `.vue` 文件，一个不漏。「挂载者」列为模板中的实际引用位置。

| # | 组件 | 职责 | props | emits | 依赖 store | 被谁挂载 |
|---|---|---|---|---|---|---|
| 1 | `TitleBar.vue` | 自绘标题栏：拖拽区、主题切换、设置入口、最小化、关闭（首次弹确认框，`localStorage 'hancast-close-modal-dismissed'`） | 无 | 无 | theme | `HomeView.vue:24` |
| 2 | `MediaInput.vue` | 媒体输入区（1084 行）：原生文件拖拽、URL 输入/粘贴、粘贴按钮（走 Rust 剪贴板）、口令解析、`<video>`+canvas 抽帧缩略图（:29-88）、文件选择（dialog → `<input>` 降级 :439-458）、设备选择弹窗、投屏触发 | 无 | 无 | media、device、cast | `HomeView.vue:46`；内嵌 `Modal`(:598) |
| 3 | `DeviceList.vue` | 设备列表：头部三按钮（添加设备→`deviceStore.openAddModal`、日志→`logsStore.open`、刷新，10s 内旋转动画）、空态含添加按钮 | 无 | 无 | device、logs | `HomeView.vue:52` |
| 4 | `DeviceCard.vue` | 单设备卡片：状态灯、投屏按钮（需 `mediaStore.isReady`，:181）、casting 态显示标题+恢复控制器按钮（:162-174）、自定义右键菜单（Teleport，:233-243）、改名/移除 Modal（:210/:224）、speaker 类型显示装饰性「多房间同步」复选框（:151-158，纯 UI，无功能） | `device: Device` | 无（直接调 store） | device、media、cast | `DeviceList.vue` |
| 5 | `Footer.vue` | 底部提示，仅 `devices.length === 0` 时显示 | 无 | 无 | device | `HomeView.vue:54` |
| 6 | `CastController.vue` | Teleport 模态投屏控制器：进度条（seek 锁定机制 :112-122，seek 后锁住直到轮询位置追近 3s 内，防止进度条被拉回）、音量 50ms 防抖（:88-95）、图片/视频两种模式、多设备切换 | 无 | 无 | cast、device | `HomeView.vue:56` |
| 7 | `StopWarningBanner.vue` | 「已停止但电视可能还在播」黄色横幅 | `warning: StopWarning \| null` | `close` | 无（数据由父级传入） | `HomeView.vue:48-51`（数据源 `cast.stopWarning`）、`ScreenCastPanel.vue:394`、`CastControllerView.vue:89`（死代码） |
| 8 | `ScreenCastPanel.vue` | 屏幕投屏面板（640 行）：目标 browser（HLS 观看链接）/ dlna（投电视）；源 screen/window；音频（麦克风/系统声音，平台差异见 §10.9-10.10）；分辨率/FPS/码率 CustomSelect；watch_url 邀请链接 + 三级降级复制（:167-184） | 无 | 无 | screencast、device | `HomeView.vue:58`；内嵌 `Modal`(:188)、`FfmpegDepsModal`(:419) |
| 9 | `Modal.vue` | 通用模态基座：Teleport 到 body、overlay 点击关闭、图标/标题、4 档尺寸 `default/small/mini/wide`（wide `min-width: 520px`，:83-86，注意超过默认窗宽 434px，见 §10.5）、具名插槽按钮样式 btn-cancel/confirm/danger | `visible`、`title?`、`showIcon?`、`size?` | `close` | 无 | AddDeviceModal、AutostartPromptModal、DeviceCard(×2)、DownloadModal、LogsPanel、MediaInput、ScreenCastPanel、TitleBar、UpdateModal |
| 10 | `CustomSelect.vue` | 手写下拉框：click-outside + Escape 关闭 | `modelValue`、`options`、`disabled?` | `update:modelValue` | 无 | SettingsView(×4：:273/:286/:325/:353/:448)、ScreenCastPanel(×8)、LogsPanel(×3) |
| 11 | `AddDeviceModal.vue` | 手动添加设备弹窗：IP/端口表单，成功后 900ms 延迟关闭（:22）；显示已添加的 manualDevices 列表 | 无（visible 由 `deviceStore.showAddModal` 驱动） | 无 | device | `App.vue:42`；内嵌 `Modal`(:28) |
| 12 | `GuardConfirmModal.vue` | 投屏确认弹窗（Device Guard）：设备信息 + 倒计时（到时自动拒绝）、拒绝/仅此次/始终允许三按钮 → `guardStore.handleResponse`；z-index 200；**倒计时文案硬编码中文「秒」「后自动拒绝」（:37-38），未走 i18n** | 无（visible 由 `guardStore.pendingRequest` 驱动） | 无 | guard | `App.vue:37` |
| 13 | `GuardDeviceList.vue` | 投屏确认设备管理：trusted/blacklisted 双 tab、切换策略、移除 | 无 | 无 | guard | `SettingsView.vue:383`（展开区） |
| 14 | `UpdateModal.vue` | 更新提示：`isVisible = showModal && has_update`；changelog 去 markdown 截 300 字（:16-25）；「忽略此版本」/「下载」→ DownloadModal | 无 | 无 | update | `App.vue:39`；内嵌 `Modal`(:45)、`DownloadModal`(:66) |
| 15 | `DownloadModal.vue` | 下载渠道选择：GitHub / Gitee / MS Store（商店版），`plugin-shell open` → `window.open` 降级（:19-27） | `visible` | `close` | update | `UpdateModal.vue:66`；内嵌 `Modal`(:46) |
| 16 | `LogsPanel.vue` | 应用内日志查看：Modal size=wide；模块/级别/条数/关键字过滤（关键字 450ms 防抖 :73-78）；autoTail 自动滚底（:81-86）；`modLabel` 把模块名中的点换成下划线再当 i18n key（:38-41，点号是 vue-i18n 路径分隔符，不能直接用） | 无（visible 由 `logsStore` 驱动） | 无 | logs | `App.vue:41`；内嵌 `Modal`(:110)、`CustomSelect`(×3) |
| 17 | `PluginSettings.vue` | 解析插件管理（847 行）：`rule_inventory` 双段列表（脚本规则包 + 声明式规则）；导入 zip（dialog → base64 `<input>` 降级 :51-83）或 URL 导入；单条规则开关；官方索引查更新 / `plugin_pull` 安装（sha256 校验在壳层/Sidecar）；两段式删除确认 4s（:168-178）；`plugin_test` 试跑；解析失败台账（按域名分组，删域名/删单条/清空/复制/timeAgo） | 无 | 无 | 无（组件内直接调 `api/commands.ts`） | `SettingsView.vue:522`（展开区） |
| 18 | `AutostartPromptModal.vue` | 首次投屏成功后的自启询问弹窗：开启（`toggleAutostart`）/跳过，都会 `markAsked`（localStorage `has_asked_autostart`） | `visible` | `close` | settings | `HomeView.vue:60-63`；内嵌 `Modal`(:34) |
| 19 | `CastControl.vue` | 旧版内嵌投屏控制条（427 行）：画质选择器（qualities ≥ 2 才显示）+ 进度条 + 播放控制。**死代码**——全仓 grep 无任何 import | — | — | — | 无（未被挂载） |
| 20 | `FfmpegDepsModal.vue` | 「需要下载运行依赖」弹窗（418 行，v2.3.11 FFmpeg 去捆绑）：逐依赖显示检测状态与真实异常原文（`ffmpeg_status` 的 `items/missing`）；按平台给出包管理器安装命令（`install_commands`）与「打开安装目录」（`open_ffmpeg_dir`）；「重新检测」走 `refresh=true`；「下载并继续」起后台下载（`ffmpeg_download_start`）后按 1s 轮询 `ffmpeg_download_status`（`screencast.ts:122`；后端进度写入节流 0.4s，`ffmpeg_deps.py:373`）渲染进度/速度；`done` 时带 `refresh=true` 重测一次并**自动收起弹窗**（`:24` 的 watcher——「下载并继续」的「继续」是回到投屏面板，由用户再点一次开始投屏，程序不自动重放），安装命令可一键复制（`writeClipboardText`） | 无（visible 由 `screencastStore.showDepsModal` 驱动） | 无 | screencast | `ScreenCastPanel.vue:419`；内嵌 `Modal`(:4) |

---

## 5. Store 清单（9 个，`src/stores/`）

全部为 Pinia **setup store**（`defineStore('x', () => {...})`），在 store 内部使用 `onMounted`/`onUnmounted` 管理定时器与事件监听。事件监听统一模式：`await import('@tauri-apps/api/event')` 动态导入后 `listen`，unlisten 函数收进数组、卸载时统一释放——这样浏览器 mock 模式下 import 失败也不会崩。

| Store | 行数 | 核心 state（类型） | 主要 actions | 订阅事件 | 调用的壳层命令 | 与其他 store 耦合 |
|---|---|---|---|---|---|---|
| `cast.ts` | 715 | `sessions: Record<string, CastSession>`、`focusedDeviceId: string\|null`、`showController`、`stopWarning: StopWarning\|null`、`qualitySwitching`、`showAutostartPrompt`、`loading`；兼容用 computed `castState`（:70-93，把多设备折叠成单设备视图） | `startCast`(:311-362)、`stopCast`(:386-427)、`switchQuality`(:457-513，重解析 + restorePosition 4 次重试 seek)、pause/resume/seek/setVolume/setMute、`startPolling`（1s `getCastUrl`；状态只升不降 :219；轮询不删会话 :245）、`startSim`(:261-283，前端每秒 +1 模拟进度)、`checkAutostartPrompt`(:365-384，localStorage `has_asked_autostart` + 6s 延迟)、`openController`/`dismissStopWarning` | `cast_state_changed`(:564-633)、`cast_error`(:637-645) | `start_cast`、`stop_cast`、`pause_cast`、`resume_cast`、`seek_cast`、`set_volume`、`set_mute`、`get_cast_url` | 无 |
| `device.ts` | 179 | `devices: Device[]`、`selectedDevice`、`manualDevices`、`showAddModal` | `refreshDevices`（30s 自动刷新 :152）、`addDevice`(:94-112，直接并入设备列表)、`openAddModal`、rename/remove/setDefault/hide | `device_found`(:121-135，upsert)、`device_lost`(:138-142) | `get_devices`、`refresh_devices`、`add_device`、`get_manual_devices`、`rename_device`、`remove_device`、`hide_device`、`set_default_device` | 无 |
| `media.ts` | 185 | `state: 'idle'\|'dragover'\|'input'\|'parsing'\|'ready'`、`mediaInfo: MediaInfo\|null`、`urlInput`、`error` | `parseFile`(:68-95，双模式：路径字符串→Tauri `parse_media_file`；File 对象→浏览器 createObjectURL)、`parseUrl`(:102-160，见 §6 链路 1)、`setThumbnail`、`reset` | 无 | `parse_media_file`、（经 `resolveShare`）`resolve_share` + `get_resolve_result` | 无 |
| `guard.ts` | 190 | `pendingRequest: CastConfirmRequest\|null`、`countdown`、`settings: GuardSettings`、`trustedDevices`/`blacklistedDevices: GuardDevice[]`、`loading` | `handleResponse`(:72-86)、`fetchDevices`、`fetchSettings`、`removeDevice`、`setPolicy`、倒计时（到 0 自动拒绝 `handleResponse(false,'once')` :33-39）；`setupListeners` 在 `onMounted` 之外也被立即调用一次（:171，保证 App 级弹窗不依赖组件挂载时机） | `cast_confirm_request`(:126-149：guard 禁用时自动批准 :129-132；已有 pending 时拒绝新请求 :134-137；`win.show()+setFocus()` 把窗口拉到前台 :141-148) | `respond_cast_confirm`、`get_guard_devices`、`remove_guard_device`、`set_guard_policy`、`get_guard_settings`、`save_guard_settings` | 无 |
| `settings.ts` | 83 | `settings: AppSettings\|null` | `fetchSettings`、`saveSettings`（增量合并）、`toggleAutostart` | `autostart-changed`(:28-38，托盘改自启时同步) | `get_settings`、`save_settings`、`get_autostart`、`set_autostart` | 无 |
| `theme.ts` | 49 | `mode: 'light'\|'dark'\|'system'`、`resolved` | `apply`/`toggle`(:33-35)；`persist`(:16-19)：`document.documentElement.setAttribute('data-theme', …)` + `localStorage 'hancast-theme'`；system 模式监听 `matchMedia('(prefers-color-scheme: dark)')` change（:41-46） | 无（matchMedia 非 Tauri 事件） | 无 | 无 |
| `logs.ts` | 124 | `visible`、`page: LogPage\|null`、`query: LogQuery`、`level` | `open`/`close`、`refresh`（`TAIL_INTERVAL = 3000` :7；撞车时排队 `pendingRefresh` :33-36）、`toggleDebug`（setLogLevel DEBUG/INFO） | 无 | `get_logs`、`set_log_level`、`export_logs` | 无 |
| `update.ts` | 131 | `info: UpdateInfo\|null`、`modalState: UpdateModalState`、`showModal`、`storeVersion` | `checkUpdate`（商店版跳过 :28-31）、`ignoreVersion`、`openDownload`(:71-76，window.open) | `update-available`(:94-97，Rust 启动 3s 后推送) | `check_update`、`ignore_update_version`、`is_store_version` | 无 |
| `screencast.ts` | 322 | `state: ScreenCastState`、`sources: ScreenSources\|null`、`stopWarning`、`showDepsModal`(:37)、`ffmpegReport: FfmpegStatus\|null`(:38)、`downloadTask: FfmpegDownloadTask\|null`(:39)、`downloading`(:40)、`reportLoading`(:41)；computed `ffmpegOk`(:46) | `fetchSources`(:56-70，顺带把 `sources.ffmpeg` 回填进 `ffmpegReport`)、`refreshFfmpegReport`(:73-88，`refresh=false` 吃后端 30s 缓存，弹窗首开秒出)、`startFfmpegDownload`(:114-127，起任务后 1s `setInterval` 轮询 `pollDownload` :97-111，`done` 自动重测)、`resumeDownloadPolling`(:143-157，定时器随组件卸载而死，重开面板必须向后端对齐一次，否则界面永停「下载中...」而后端早下完)、`openFfmpegDir`(:129-139)、`openDepsModal`/`closeDepsModal`(:159-167)、`start`(:177-196)、`stop`(:198-231，`control_failed` → stopWarning，**借 `useDeviceStore` 把 device_id 翻译成设备名** :221)、`setFfmpegPath`(:233-247)、`dismissStopWarning`(:320) | `screen_cast_state_changed`(:266-273) | `list_screen_sources`、`start_screen_cast`、`stop_screen_cast`、`screen_cast_status`、`set_ffmpeg_path`、`ffmpeg_status`、`ffmpeg_download_start`、`ffmpeg_download_status`、`open_ffmpeg_dir` | **→ device**（唯一一处 store 间依赖，grep 验证） |

---

## 6. 数据流时序（两条真实链路）

### 链路 1：粘贴分享口令 → 解析 → 选设备 → 投屏成功

```
用户                MediaInput.vue        commands.ts          lib.rs            Sidecar        cast.ts
 │ 点[粘贴]/Ctrl+V      │                     │                  │                 │              │
 ├────────────────────►│ onPasteFromClipboard │                  │                 │              │
 │                     │  (:362-371)          │                  │                 │              │
 │                     ├─readClipboardText──►│ (:934-947)       │                 │              │
 │                     │                     ├─read_clipboard──►│ (:414-417)      │              │
 │                     │                     │◄─剪贴板文本───────┤                 │              │
 │                     │ mediaStore.parseUrl  │                  │                 │              │
 │                     ├────────────────────►│ resolveShare     │                 │              │
 │                     │        media.ts:102-160                 │                 │              │
 │                     │                     ├─resolve_share───►│ (:256-268)─────►│ 后台线程解析   │
 │                     │                     │◄─{task_id}────────┤                 │              │
 │                     │                     ├─get_resolve_result 每400ms轮询，45s兜底 (:684-705)  │
 │                     │                     │                  │ (:270-278)─────►│              │
 │                     │                     │◄─{status:done, result:MediaInfo}────┤              │
 │                     │  state='ready'，输入框回显 source_url，预览缩略图             │              │
 │ 点[投屏]             │ onCast (:398-425)    │                  │                 │              │
 │────────────────────►│ 优先级: selected→default→弹设备选择Modal  │                 │              │
 │                     ├──────────────────────────────────────────────────────────►│ startCast    │
 │                     │                     │                  │                 │  (:311-362)   │
 │                     │                     │                  │◄─start_cast─────┤              │
 │                     │                     │                  │ (:430-448)─────►│ DLNA 投递     │
 │                     │                     │                  │                 │              │
 │                     │        乐观更新：立即 status='playing' (:329)、startSim 每秒+1 (:261-283)、│
 │                     │        startPolling 1s getCastUrl、openController、checkAutostartPrompt  │
 │                     │                     │                  │                 │              │
 │                     │◄─────────────事件 cast_state_changed───────────────────────┤ (:564-633)   │
 │                     │        PLAYING/PAUSED/STOPPED/NO_MEDIA_PRESENT 映射为权威状态；           │
 │                     │        connecting/playing/paused 会话不被瞬时 stopped 移除                │
```

要点：

1. 剪贴板读取走 Rust（`read_clipboard`），Web API 仅降级（`commands.ts:934-947`，原因见 §10.1）。
2. `resolve_share` 是**异步任务**：立即返回 `task_id`，前端 400ms 轮询 `get_resolve_result`，45s 兜底超时（`commands.ts:680-705`；Sidecar 单条解析预算 25s，注释写明）。兼容「直接返回结果」的旧实现/浏览器 mock（`commands.ts:690-694`）。
3. 后端解析失败时**不再无条件透传**：只有从文本里抠出的地址本身带媒体扩展名（`media.ts:15-19` 白名单）才 `extractFirstUrl` + 按扩展名推 MIME 透传；网页地址一律如实报错（`media.ts:142-182`）。旧口径「能抠出链接就降级透传」会让随便一个 HTML 页面也能点投屏、点了却毫无动作，且失败被吞进台账之外无人看见。报错时 `ledgerHint` 置真，输入框下方直接指出「设置 → 解析插件 → 解析失败台账（按域名分组）」入口（`MediaInput.vue:522`）。
4. 投屏目标选择优先级：选中设备（在线）→ 默认设备（在线）→ 弹设备选择 Modal（`MediaInput.vue:398-425`）。
5. `startCast` 不等事件，**立即乐观置 playing**（`cast.ts:329` 注释「Set playing immediately — don't wait for event」），同时启动本地进度模拟与 1s 轮询；真实状态以 `cast_state_changed` 事件为权威源。
6. Ctrl+V 直接进输入框时：纯链接保持默认粘贴（可编辑），**非纯链接的口令文本**才拦截并自动解析（`MediaInput.vue:377-384`）。

### 链路 2：手机投屏到本机（接收投屏）→ 投屏确认 → MPV 播放

```
手机(控制点)        Sidecar            lib.rs             guard.ts           GuardConfirmModal
    │ DLNA SetAVTransportURI+Play │                  │                 │                  │
    ├──────────────►│ Guard 开启且设备不受信          │                 │                  │
    │               ├─emit cast_confirm_request────►│ 转发事件────────►│ (:126-149)        │
    │               │               │                 │ 禁用→自动批准(:129-132)             │
    │               │               │                 │ 已有pending→拒新(:134-137)          │
    │               │               │                 │ 否则记pending+倒计时                │
    │               │               │                 │ win.show()+setFocus (:141-148)     │
    │               │               │                 ├───────────────►│ 渲染倒计时弹窗     │
    │               │               │                 │                │ 拒绝/仅此次/始终允许│
    │               │               │                 │◄─handleResponse(policy) (:72-86)   │
    │               │               │◄─respond_cast_confirm (:626-640)─┤                  │
    │               │◄──────────────┤                 │ 倒计时到0→自动拒绝 handleResponse(false,'once') (:33-39)
    │               │ 批准→URL 交给本机 MPV 播放（Sidecar/壳层管理 mpv 进程，前端不参与）      │
    │               ├─emit cast_state_changed──────►cast.ts (:564-633) 更新会话状态          │
```

要点：

1. `cast_confirm_request` 事件由 `guard` store 监听；`GuardConfirmModal` 在 `App.vue:37` 全局挂载，所以任何路由下都能弹。
2. 倒计时到 0 **自动拒绝**（`guard.ts:33-39`），策略 `once`。
3. 应答四态：`once`（仅此次）/ `always`（始终允许，进 trusted）/ `reject` / `blacklist`，经 `respond_cast_confirm(request_id, approved, policy)`（`lib.rs:626-640`）回传。
4. 收到请求时前端主动 `show() + setFocus()` 把窗口拉到前台（`guard.ts:141-148`）——投屏确认必须让用户看见。
5. MPV 播放本身发生在壳层/Sidecar（`check_mpv`/`set_mpv_path` 命令存在于 `lib.rs:1307-1370` 的 invoke_handler 清单）；前端不参与播放，只通过 `cast_state_changed` 感知状态。旁证：`CastControllerView.vue:76-77` 注释「走 store：带 device_id 打到电视；裸 invoke 会落到本地 MPV」。⚠️ MPV 进程管理细节属后端范围，本文未验证其实现。

---

## 7. 样式系统

### 7.1 `src/styles/variables.css`（87 行）变量全清单

`:root`（亮色默认值，实抄）：

| 分组 | 变量 = 值 |
|---|---|
| Primary | `--primary: #3B82F6`；`--primary-hover: #2563EB`；`--primary-active: #1D4ED8`；`--primary-light: rgba(59,130,246,0.08)` |
| Background（注释：配合系统特效，故用半透明 rgba） | `--bg-primary: rgba(255,255,255,0.95)`；`--bg-secondary: rgba(243,244,246,0.95)`；`--bg-card: rgba(255,255,255,0.98)`；`--bg-input: rgba(243,244,246,0.92)` |
| Text | `--text-primary: #111827`；`--text-secondary: #6B7280`；`--text-tertiary: #9CA3AF`；`--text-link: #3B82F6` |
| Status | `--status-online: #10B981`；`--status-busy: #F59E0B`；`--status-offline: #9CA3AF` |
| Border | `--border: #E5E7EB`；`--border-focus: #3B82F6`；`--border-dashed: #D1D5DB` |
| Spacing | `--sp-xs: 4px`；`--sp-sm: 8px`；`--sp-md: 12px`；`--sp-lg: 16px`；`--sp-xl: 24px`；`--sp-2xl: 32px` |
| Radius | `--r-sm: 4px`；`--r-md: 8px`；`--r-lg: 12px`；`--r-xl: 16px`；`--r-full: 9999px` |
| Shadow | `--shadow-sm: 0 1px 2px rgba(0,0,0,0.05)`；`--shadow-md: 0 4px 6px -1px rgba(0,0,0,0.1)`；`--shadow-lg: 0 10px 15px -3px rgba(0,0,0,0.1)` |
| Font | `--font: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif` |
| Titlebar | `--titlebar-height: 32px` |
| Transition | `--transition-fast: 150ms ease`；`--transition-normal: 250ms ease` |

`[data-theme="dark"]` 覆盖（暗色主色换靛蓝系）：`--primary: #6366F1`、`--primary-hover: #4F46E5`、`--primary-active: #4338CA`、`--primary-light: rgba(99,102,241,0.12)`；背景 `--bg-primary: rgba(22,20,50,0.95)`、`--bg-secondary: rgba(32,30,66,0.95)`、`--bg-card: rgba(32,30,66,0.98)`、`--bg-input: rgba(42,40,80,0.92)`；文字 `#F0EFF5 / #B8B5CC / #6E6B8A`、链接 `#818CF8`；边框 `rgba(99,102,241,0.15)` / `#6366F1` / `rgba(99,102,241,0.25)`；三档 shadow 加深。Spacing/Radius/Font/Titlebar/Transition 不随主题变。

### 7.2 主题落地 DOM 的机制

1. `theme` store 的 `persist()`（`src/stores/theme.ts:16-19`）：把解析后的 `light`/`dark` 写到 `document.documentElement` 的 `data-theme` 属性，同时把用户选择的 mode（含 `system`）存 `localStorage 'hancast-theme'`。
2. `main.ts:12-16` 在 `createApp` **之前**先读 localStorage 应用一次 `data-theme`——避免暗色用户首绘白闪。
3. `system` 模式：`matchMedia('(prefers-color-scheme: dark)')` 的 change 监听（`theme.ts:41-46`），系统切换实时跟随。

### 7.3 全局约定（`src/styles/global.css`，112 行）

- `@import './variables.css'` 后做全量 reset（`box-sizing: border-box`，margin/padding 清零）。
- `html, body`：`overflow: hidden`（窗口级滚动禁用，滚动只发生在组件内部容器）；`body` 背景 `transparent`（配合 `transparent: true` 窗口）、`font-size: 14px`、抗锯齿。
- 滚动条统一 6px 细样式（`::-webkit-scrollbar`，三平台 WebView 均支持 WebKit 系滚动条伪元素；Windows WebView2 也兼容）。
- button/input/textarea/a 全部去默认样式，`:focus-visible` 有 2px outline 兜底可访问性。
- 公共动画 keyframes：`spin`（刷新按钮）、`fadeIn`、`pulse`。
- 响应式仅一条：`@media (max-width: 360px)` 时 body 字号降为 13px。
- 组件样式一律 `<style scoped>`，纯 CSS，无预处理器。

---

## 8. 国际化（vue-i18n）

### 8.1 结构与初始化

`src/main.ts:18-26`：`createI18n({ legacy: false, locale: 'zh-CN', fallbackLocale: 'en-US', messages: { 'zh-CN': zhCN, 'en-US': enUS } })`。Composition API 模式（`useI18n()` 的 `t`/`locale`）。

语言文件：`src/locales/zh-CN.json`、`src/locales/en-US.json`（各 280 行）。

### 8.2 key 命名约定

- 两级为主：`模块.camelCaseKey`。顶层命名空间 15 个（实抄）：`app`、`autostartPrompt`、`cast`、`close`、`common`、`controller`、`devices`、`footer`、`guard`、`logs`、`media`、`screencast`、`settings`、`theme`、`update`——与组件/Store 的模块划分一一对应。
- **点号陷阱**：动态拼 key 时，模块名里的点会被 vue-i18n 当成路径分隔符。`LogsPanel.vue:38-41` 的 `modLabel` 因此把日志模块名中的 `.` 换成 `_` 再查 key，并做了查表缓存。

### 8.3 两语言是否等键（脚本比对结果）

用脚本把两份 JSON 拍平后比对：**zh-CN 278 个叶子 key，en-US 278 个，差集双向均为 0——完全等键**。

### 8.4 覆盖范围与已知缺口

- 覆盖：标题栏、媒体输入、设备列表/卡片、控制器、屏幕投屏、投屏确认、日志、设置全部分区、更新、自启询问、关闭确认等全部用户可见文案。
- **已知缺口**：`GuardConfirmModal.vue:37-38` 倒计时文案「秒」「后自动拒绝」硬编码中文，未走 `t()`——英文界面下该弹窗会中英混排。
- **语言选择不持久化**：`SettingsView.vue:55-57` 的 `onLanguageChange` 只设 `locale.value`，全仓 grep 无任何写入 localStorage 或后端 settings 的代码；重启后回到 `main.ts` 写死的 `zh-CN`。这是现状事实（是否为待办 ⚠️ 未验证——仓库内没有找到相关 TODO/issue 注释）。

### 8.5 新增 key 的做法

1. 在 `zh-CN.json` 与 `en-US.json` 的同一命名空间下**成对**添加（保持等键，避免 fallback 到英文造成中文界面漏翻）。
2. 组件内 `const { t } = useI18n()`，模板 `{{ t('module.key') }}`。
3. 动态 key 注意 §8.2 的点号规则。

---

## 9. 浏览器 mock 模式

前端可以脱离 Tauri 直接 `npm run dev` 在浏览器里跑（`vite` 端口 1420），靠 `src/api/commands.ts` 的降级层：

### 9.1 检测与降级机制

`initInvoke()`（`commands.ts:22-37`）：检查 `window.__TAURI_INTERNALS__?.metadata` 是否存在——存在则动态 import `@tauri-apps/api/core` 用真 `invoke`；不存在（或 import 抛错）则整个 invoke 通道替换为 `mockInvoke`。**检测的是运行时对象而非模块**，因为 `withGlobalTauri: true` 时模块总能 import 成功，只有 `__TAURI_INTERNALS__` 才能区分环境（`env.d.ts` 为其补了类型）。

### 9.2 mockInvoke（`commands.ts:264-602`）

- 统一 300ms 模拟延迟；按命令名 switch，覆盖几乎全部命令（设备/解析/投屏/设置/参数/插件/台账/日志/更新/guard/屏幕投屏）。
- 内存态数据：`MOCK_DEVICES` 4 台设备（:126-175，含 tv/speaker/box 类型与在线/忙碌/离线状态）、`MOCK_SETTINGS`（:177-185）、`mockParamSpecs` 5 项后台参数（:191-231）、`mockPlugins`（:46-66）、`mockIndexPacks`（:69-72）、`mockDeclarative` 声明式规则（:74-83）、`mockFailureHosts` 失败台账（:95-115）。
- `resolve_share` mock：本地正则提取 URL；B 站链接返回多画质假结果（用于调画质切换 UI）。
- `read_clipboard` mock 返回 `''`（浏览器无权限读剪贴板）。
- 事件通道：store 里 `listen` 的 import 在浏览器会失败并被 catch，事件驱动的逻辑（`cast_state_changed` 等）在 mock 模式下**不会触发**；进度靠 `startSim` 本地模拟仍然可看。

### 9.3 浏览器里不可用（或行为不同）的功能

| 功能 | 浏览器表现 |
|---|---|
| 真实设备发现 / 真实投屏 / DLNA 全部 | mock 数据，无真实网络行为 |
| 系统剪贴板读取 | 返回空串；`readClipboardText` 会尝试 `navigator.clipboard.readText()` 降级（需用户授权，`commands.ts:942-946`） |
| 文件解析 | `media.ts:68-95` 双模式：`<input type=file>` 拿到 File 对象走 `URL.createObjectURL`，本地可预览 |
| 窗口控制（拖拽/最小化/关闭/托盘） | 全部无效（无 IPC）；`data-tauri-drag-region` 无行为 |
| 原生文件选择对话框 | `MediaInput.vue:439-458`、`PluginSettings.vue:51-83` 均有 dialog → HTML `<input>` 降级路径 |
| 打开外部链接 | `plugin-shell open` → `window.open` 降级（`DownloadModal.vue:19-27`） |
| 自启动 / 更新检查 / 日志导出 / MPV / ffmpeg 屏幕投屏 | mock 或静默失败 |

---

## 10. 前端已知约束与坑（均有代码/注释证据）

### 10.1 剪贴板必须走 Rust

`lib.rs:409-413` 注释原文：「WKWebView 默认不授予页面 clipboard-read 权限，前端 `navigator.clipboard.readText()` 在 macOS 上静默 reject，『粘贴』按钮因此失效——改由 Rust 侧（已装的 clipboard 插件）读取。」写入同理（`lib.rs:419-421`：Web API 的 reject 不受同步 try/catch 保护）。前端封装 `readClipboardText`：Rust 优先 → `navigator.clipboard` 降级（浏览器 mock 用）→ 空串（`commands.ts:934-947`）。

### 10.2 Tauri 原生拖拽与 HTML5 拖拽互斥

`MediaInput.vue:219-241` 有大段注释记录：`dragDropEnabled: true`（当前配置，`tauri.conf.json:29`）时 WebView 收不到 HTML5 `dragover/drop` 事件，二者只能取其一；上游 issue tauri-apps/tauri#14055 标记 not planned、#14373 讨论过。项目采取的策略：**文件走 Tauri 原生 `onDragDropEvent`（拿绝对路径，交 Rust 解析）；URL/口令引导用户粘贴**（拖拽进入时显示 showPasteHint，`MediaInput.vue:331-352`；原生事件的 `paths` 为空时也回落到粘贴提示）。

### 10.3 CSP `unsafe-eval` 不能删

见 §2.5。删除会导致 vue-i18n 运行时编译失败、窗口白屏（commit `953801e`，2026-10-11 是「恢复」性修复）。

### 10.4 `-webkit-app-region` 在 macOS 是空操作

拖拽必须用 `data-tauri-drag-region`（走 IPC `start_dragging`，capability `core:window:allow-start-dragging`），且 TitleBar 里两者都保留（`TitleBar.vue:47-48` 注释 + :49-52 每个子元素都标了该属性）。

### 10.5 未用组件库 + wide Modal 与窗宽的矛盾

无 Element Plus（§1.1）。手写 `Modal` 的 `wide` 档 `min-width: 520px`（`Modal.vue:83-86`），**大于默认窗宽 434px**；LogsPanel 用的正是 wide。窗口可拉宽到 800px，但默认宽度下 wide 弹窗会横向溢出/挤压。⚠️ 未验证：实际渲染是否有 `max-width: 100%` 类兜底或视觉溢出程度，需运行时确认（本次任务不允许跑构建）。

### 10.6 DLNA 状态是「不可靠流」，前端做了三层防御

`cast.ts` 的注释与实现：(a) 轮询合并状态**只升不降**（:219）；(b) 轮询**不删会话**（:245）；(c) `cast_state_changed` 里 connecting/playing/paused 的会话不被瞬时 stopped/idle 移除（:614-618 注释：「DLNA may send transient NO_MEDIA_PRESENT / STOPPED during playback」），删除还要再等 500ms 复核（:620-627）。改这段逻辑前先读这些注释。

### 10.7 `stop_cast` 的 `control_failed` 不能吞

`lib.rs:455-458` 注释：本地会话已释放、但电视没收到 Stop 时，前端要靠返回体里的 `control_failed` 解释「界面停了、电视还在播」。落点：`cast.ts:386-427`（stopCast 本地一律先释放 + 置 `stopWarning`）→ `StopWarningBanner`（`HomeView.vue:48-51`）；屏幕投屏同款逻辑 `screencast.ts:198-231`。类型定义 `StopCastResult`/`StopWarning` 在 `commands.ts:1103-1117`。

### 10.8 进度条与轮询的打架：seek 锁定

控制器 seek 后，1s 轮询返回的旧位置会把进度条拉回去。`CastController.vue:112-122` 用 `seekLocked` 标志锁住进度显示，直到轮询位置追近 seek 目标（3s 误差内）才解锁。音量滑块 50ms 防抖（:88-95）。

### 10.9 macOS 麦克风授权（TCC）要由 WebView 代触发

`ScreenCastPanel.vue:106-110` 注释：macOS 的麦克风 TCC 权限按 **bundle** 判定，Sidecar 拉起的 ffmpeg 不会触发系统授权弹窗；所以开始屏幕投屏前，前端先在 WebView 里 `getUserMedia({ audio: true })` 拿一次授权（:111-129），成功后立即释放流。

### 10.10 Windows 系统声音是 WASAPI 环回「镜像设备」列表

`ScreenCastPanel.vue:78-80` 注释 + `pyproject.toml:23-26`：FFmpeg 没有 WASAPI 输入（上游 ticket #9408 未合并），Sidecar 用 `pyaudiowpatch` 把渲染设备采成 PCM。环回列表里**只有系统当前默认输出那条真的在出声**，UI 用 `defaultOutputSuffix` 标注，避免用户选错静音设备。

### 10.11 死代码（改动时别被误导）

- `src/components/CastControl.vue`（427 行，内嵌控制条 + 画质选择器）：全仓无 import。现役控制器是 `CastController.vue`。
- `src/views/CastControllerView.vue`（323 行，整窗控制器变体）：不在路由表、无 import。其中 :76-77 的注释（「走 store：带 device_id 打到电视；裸 invoke 会落到本地 MPV」）仍有文档价值。

### 10.12 渲染性能注意点

- 进度每秒跳动由 `startSim` 本地模拟（`cast.ts:261-283`），不靠高频轮询驱动渲染；轮询固定 1s（`getCastUrl`），日志 tail 固定 3s（`logs.ts:7`）。
- 高频输入都有防抖：日志关键字 450ms（`LogsPanel.vue:73-78`）、音量 50ms、窗口尺寸保存 500ms（Rust 侧）。
- 日志刷新撞车时排队而不并发（`logs.ts:33-36` `pendingRefresh`）。
- 缩略图抽帧用 `<video>` + canvas 在前端完成（`MediaInput.vue:29-88`），大图走 `asset:` 协议不进 base64。

### 10.13 其他平台/环境差异

- 多屏屏幕投屏默认选主屏（`ScreenCastPanel.vue:97-98` 注释）；显示器源 id 形如 `screen:x,y,w,h`（`types/screencast.ts`）。
- 商店版（MSIX）行为差异：自启走 StartupTask（§2.4）、跳过更新检查（`update.ts:28-31`）、About 区显示 MS Store 链接（`SettingsView.vue:550-572`）。
- 右键菜单是自定义 Teleport 菜单（`DeviceCard.vue:233-243`），且非 DEV 构建全局禁用了原生 contextmenu（`App.vue:24-26`）。
- 窗口恢复尺寸有最小值保护 346×472（`lib.rs:1091-1095`），并禁用最大化（:1108）——布局只需考虑 346–800px 宽度区间。

---

## 附：本文档取证范围

实读文件：`src-tauri/tauri.conf.json`、`src-tauri/capabilities/default.json`、`src-tauri/src/lib.rs`（1443 行全读）、`src-tauri/Cargo.toml`、`hancast-backend/pyproject.toml`、`package.json`、`vite.config.ts`、`tsconfig.json`、`env.d.ts`、`index.html`、`src/main.ts`、`src/App.vue`、`src/router/index.ts`、3 个视图（含 967 行 SettingsView 与 323 行 CastControllerView 全读）、20 个组件、9 个 store、`src/api/commands.ts`（1348 行全读）、`src/types/` 全部 9 个文件、`src/styles/` 2 个文件、`src/locales/` 2 个文件（脚本比对 key）、git 历史（`git log -S "unsafe-eval"`）。
