# HanCast — 前端设计规格

> 本文档回答：HanCast 桌面端（Tauri 2 + Vue 3）的界面层是怎么搭起来的——窗口壳层、路由视图、组件与 Store 全量清单、真实数据流时序、样式与国际化机制、浏览器 mock 模式，以及写代码前必须知道的约束与坑。
>
> 最后校对：2026-10-24，对应版本 v2.3.20

术语约定：**Sidecar** = Python 后台进程（`hancast-backend/`，打包为 `hancast-sidecar`）；**壳层** = Rust/Tauri 层（`src-tauri/`）；**规则包** = JS 解析插件 zip；**声明式规则** = JSON 站点规则；**屏幕投屏** = screencast（把电脑屏幕投出去）；**接收投屏** = 本机作为 DLNA 渲染端被别的设备投；**投屏确认** = Device Guard；**后台参数** = `params.py` 可调项。

本轮仅校对前端美化与 bug 修复涉及的事实、引用；相关行号以 2026-10-24 工作区为准，其余沿用 2026-10-15 的取证记录，未作全量版本/行号刷新。未能核实的论断一律标注「⚠️ 未验证」。

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
| `transparent` | `true` | :26 | 窗口透明配置仍保留，但本轮页面/卡片已使用实色 CSS 背景（`src/styles/variables.css:11-14`、`:64-67`），不等同于系统毛玻璃。**Linux 例外**：`tauri.linux.conf.json` 整份重写该窗口对象为 `transparent:false` + 不透明 `backgroundColor`（关 dmabuf 后透明窗口会漏提交 damage 露出桌面，v2.3.18，机制见 [故障排查.md](故障排查.md) §8.4） |
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
- `externalBin`：`hancast-sidecar/hancast-sidecar`（:46）；base `resources` 只有三条——`mpv/*`、`mpv/portable_config/**/*`、`hancast_sidecar/**/*`（:47-51），依赖库 glob 由三份平台 conf 各自完整重写（数组整体替换，见 `AGENTS.md` 的 bundle.resources 一节）。**FFmpeg 曾在这张清单里（`ffmpeg/*`），v2.3.11 起删除**：不再随包，改为本机检测 + 缺失按需下载（[屏幕投屏.md](屏幕投屏.md) §5.8）。
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
| `HomeView.vue` | 178 | 在用，主页面 |
| `SettingsView.vue` | 1165 | 在用，设置页 |
| `CastControllerView.vue` | 323 | 死代码 |

### 3.3 HomeView 布局（ASCII）

挂载点行号来自 `src/views/HomeView.vue` 模板：

```
┌──────────────────────────────────────┐
│ TitleBar (:24)                        │ 44px，可拖拽
├──────────────────────────────────────┤
│ ┌ main ────────────────────────────┐ │
│ │ 标题/副标题 + 屏幕投屏 (:26-46)    │ │ 点击打开 ScreenCastPanel
│ │ MediaInput (:48)                  │ │ 拖拽/粘贴/URL 输入 + 预览
│ │ lastError 提示 (:49-52)            │ │ 投屏操作失败原因，可关闭
│ │ StopWarningBanner (:53-57)        │ │ 条件渲染：停止未送达提示
│ │ DeviceList (:58)                  │ │ 设备卡片列表（含 DeviceCard）
│ └──────────────────────────────────┘ │
│ Footer (:60)                          │ 仅设备列表为空时显示
├──────────────────────────────────────┤
│ CastController (:62)   ← Teleport 模态│ 投屏中控制器（多设备）
│ ScreenCastPanel (:64)  ← Teleport 模态│ 屏幕投屏面板
│ AutostartPromptModal (:66-69)         │ 首次投屏成功后弹自启询问
└──────────────────────────────────────┘
```

标题栏高度来自 `src/styles/variables.css:50`。主内容区允许纵向滚动并设 `min-height: 0`（`HomeView.vue:81-89`）；窗高 ≤560px 时设备列表取消内层滚动，交回页面滚动（`DeviceList.vue:212-217`）。`HomeView.vue:53-57` 处注释解释了 StopWarningBanner 为什么挂在页面层：控制器和投屏条停止后都会关闭，只有页面留得住这句话。

**路由只持有视图，不再持有持久订阅的生命周期**：HomeView 仍消费 cast/screencast（`HomeView.vue:16-19`），device/cast/screencast 的首次初始化已上移 App（见 §3.5），切设置再返回不会停掉设备刷新、投屏轮询或屏幕投屏事件监听。

### 3.4 SettingsView 布局（`src/views/SettingsView.vue`，1165 行）

自带 header（返回键 + 标题，`data-tauri-drag-region` 于 :257，同样可拖窗）。分区自上而下：

| 分区 | 内容 | 证据 |
|---|---|---|
| General | 主题（CustomSelect → `themeStore.apply`）、语言（CustomSelect → `locale.value`，:289-290）、开机自启 toggle（`settingsStore.toggleAutostart`） | :273-309 |
| Cast（接收投屏） | 设备名 friendly_name（blur 保存，:63-65）、网络接口（`listNetworkInterfaces`，:42-48） | :316-336 |
| Device Guard（投屏确认） | enabled 开关、confirm_timeout（10/15/30/60 秒，:359-369）、GuardDeviceList 展开区（:390） | :344-392 |
| 后台参数 | `getParams`/`saveParams`；本地 `paramDraft` 草稿；按 section 分组；类型 text/string/number/enum/bool；`overridden` 显示「已改」徽标；单项重置（:199-201） | :425-487 |
| 解析插件 | PluginSettings 展开区（:533），入口按钮显示 pluginList 徽标计数（:215-222） | :517-535 |
| About | 版本号；查看日志（`logsStore.open()`）；导出日志（`exportLogs`）；「检查更新」或商店版时显示 MS Store 链接（:561-583）；仓库/QQ 群/许可证外链 | :538-602 |

分区使用实色卡片，宽度 100%、上限 720px，正文独立纵向滚动（`SettingsView.vue:653-675`）。后台参数在 ≤480px 改为标签/控件上下排列，≤380px 常规字段也改为纵排；长帮助文案与插件行可换行（`:1028-1034`、`:1099-1154`）。返回、展开入口使用原生 button；字段有 label/ARIA 名称，toggle 保留可聚焦 input 与焦点环（`:258`、`:317-324`、`:372`、`:427-470`、`:833-845`）。

### 3.5 App.vue 全局层

`src/App.vue:43-52` 在 router-view 之外全局挂载 4 个模态：`GuardConfirmModal`(:46)、`UpdateModal`(:48)、`LogsPanel`(:50)、`AddDeviceModal`(:51)——它们需要跨路由可弹。另：非 DEV 构建禁用右键菜单（:26-39）；`onMounted` 拉取 `fetchSettings`（:31-32）。

**App 的 setup 首先调用 `useDeviceStore()`、`useCastStore()`、`useScreenCastStore()`（`App.vue:19-24`）**。Pinia setup store 是单例，但其组件生命周期钩子归首次调用者；因此 device 的 30s 刷新与事件（`src/stores/device.ts:148-158`）、cast 的恢复/轮询/模拟进度与事件（`src/stores/cast.ts:727-741`）、screencast 的状态事件与下载轮询清理（`src/stores/screencast.ts:282-291`）均归 App 生命周期。历史上首次调用落在 HomeView 子树，进入设置会卸载这些订阅，返回又不会重跑单例 setup；本轮上移所有权修复，模态视图本身仍按路由挂载。cast 另用 `disposed` 防止根组件卸载后恢复请求重启轮询，并即时释放迟到的原生注册结果（`cast.ts:168-179`、`:221-225`、`:634-637`、`:736-741`）。

---

## 4. 组件清单（20 个，`src/components/`）

> 数量为实际 `ls` 清点结果：20 个 `.vue` 文件，一个不漏。「挂载者」列为模板中的实际引用位置。

| # | 组件 | 职责 | props | emits | 依赖 store | 被谁挂载 |
|---|---|---|---|---|---|---|
| 1 | `TitleBar.vue` | 自绘标题栏：拖拽区、主题切换、设置入口、最小化、关闭（首次弹确认框，`localStorage 'hancast-close-modal-dismissed'`） | 无 | 无 | theme | `HomeView.vue:24` |
| 2 | `MediaInput.vue` | 媒体输入区：原生文件拖拽、URL 输入/粘贴、粘贴按钮（走 Rust 剪贴板）、口令解析、`<video>`+canvas 抽帧缩略图（:29-88）、文件选择（dialog → `<input>` 降级 :439-458）、设备选择弹窗、投屏触发；拖放区可 Enter/Space 打开文件（:492-502），清空按钮有名称（:529） | 无 | 无 | media、device、cast | `HomeView.vue:48`；内嵌 `Modal`(:601) |
| 3 | `DeviceList.vue` | 设备列表：数量徽标与头部三按钮（添加、日志、刷新）；刷新旋转/禁用跟随 `deviceStore.refresh()` 的 Promise，在 `finally` 结束（:15-21、:57），不再固定转 10s；空态含添加按钮 | 无 | 无 | device、logs | `HomeView.vue:58` |
| 4 | `DeviceCard.vue` | 单设备卡片：状态灯、默认徽标、投屏按钮（需 `mediaStore.isReady` 且非 `castStore.loading`，:178-189）、casting 态标题+恢复控制器按钮（:162-174，≤420px 隐藏标题 :437-439）、自定义右键菜单（Teleport，:233-244）、改名/移除 Modal（:211/:225）；**已移除**旧装饰性「多房间同步」复选框，独立 speaker 会话不代表同步播放（:158） | `device: Device` | 无（直接调 store） | device、media、cast | `DeviceList.vue` |
| 5 | `Footer.vue` | 底部提示，仅 `devices.length === 0` 时显示 | 无 | 无 | device | `HomeView.vue:60` |
| 6 | `CastController.vue` | Teleport 模态投屏控制器：seek 锁定由主动 watch 解锁，并有请求结束/5s 兜底（:145-163、:199-221，见 §10.8）；音量 50ms 防抖捕获目标设备（:112-121）；图片/视频两种模式、按聚焦设备展示、操作失败 `lastError` 提示（:244-248）；无缩略图的视频/音频 URI 不交给 img（:64-85） | 无 | 无 | cast、device | `HomeView.vue:62` |
| 7 | `StopWarningBanner.vue` | 「已停止但电视可能还在播」黄色横幅 | `warning: StopWarning \| null` | `close` | 无（数据由父级传入） | `HomeView.vue:53-57`（数据源 `cast.stopWarning`）、`ScreenCastPanel.vue:394`、`CastControllerView.vue:89`（死代码） |
| 8 | `ScreenCastPanel.vue` | 屏幕投屏面板：目标 browser（HLS 观看链接）/ dlna（投电视）；源 screen/window；音频（麦克风/系统声音，平台差异见 §10.9-10.10）；分辨率/FPS/码率 CustomSelect；watch_url 邀请链接 + 三级降级复制（:167-184） | 无 | 无 | screencast、device | `HomeView.vue:64`；内嵌 `Modal`(:188)、`FfmpegDepsModal`(:419) |
| 9 | `Modal.vue` | 通用模态基座：Teleport 到 body、overlay 点击关闭、图标/标题、4 档尺寸 `default/small/mini/wide`；外层留 16px 边距、框体 `min-width: 0` + `max-width/max-height: 100%`、纵向滚动（:112-141，wide 溢出已修，见 §10.5）；最上层 Modal 处理 Escape/Tab 焦点循环、打开聚焦/关闭或卸载还原焦点（:28-71）；dialog/aria-modal（:78），具名插槽 btn-cancel/confirm/danger | `visible`、`title?`、`showIcon?`、`size?` | `close` | 无 | AddDeviceModal、AutostartPromptModal、DeviceCard(×2)、DownloadModal、LogsPanel、MediaInput、ScreenCastPanel、TitleBar、UpdateModal |
| 10 | `CustomSelect.vue` | 手写下拉框：click-outside/失焦关闭；方向键、Home/End 移动选项；Escape 关闭并还焦点（阻止冒泡，不连带关闭外层 Modal），Tab 关闭但不困住焦点（:30-89）；listbox/option 语义（:100-149），菜单限高 `min(240px, 40vh)`（:213-229） | `modelValue`、`options`、`ariaLabel?`、`ariaLabelledby?`（:9-14，无 disabled prop） | `update:modelValue` | 无 | SettingsView（5 处模板：:273/:287/:329/:359/:457）、ScreenCastPanel(×8)、LogsPanel(×3) |
| 11 | `AddDeviceModal.vue` | 手动添加设备弹窗：IP/端口表单，成功后 900ms 延迟关闭（:22）；显示已添加的 manualDevices 列表 | 无（visible 由 `deviceStore.showAddModal` 驱动） | 无 | device | `App.vue:51`；内嵌 `Modal`(:28) |
| 12 | `GuardConfirmModal.vue` | 投屏确认弹窗（Device Guard）：设备信息 + 倒计时（到时自动拒绝）、拒绝/仅此次/始终允许三按钮 → `guardStore.handleResponse`；z-index 200；**倒计时文案硬编码中文「秒」「后自动拒绝」（:37-38），未走 i18n** | 无（visible 由 `guardStore.pendingRequest` 驱动） | 无 | guard | `App.vue:46` |
| 13 | `GuardDeviceList.vue` | 投屏确认设备管理：trusted/blacklisted 双 tab、切换策略、移除 | 无 | 无 | guard | `SettingsView.vue:390`（展开区） |
| 14 | `UpdateModal.vue` | 更新提示：`isVisible = showModal && has_update`；changelog 去 markdown 截 300 字（:16-25）；「忽略此版本」/「下载」→ DownloadModal | 无 | 无 | update | `App.vue:48`；内嵌 `Modal`(:45)、`DownloadModal`(:66) |
| 15 | `DownloadModal.vue` | 下载渠道选择：GitHub / Gitee / MS Store（商店版），`plugin-shell open` → `window.open` 降级（:19-27） | `visible` | `close` | update | `UpdateModal.vue:66`；内嵌 `Modal`(:46) |
| 16 | `LogsPanel.vue` | 应用内日志查看：Modal size=wide；模块/级别/条数/关键字过滤（关键字 450ms 防抖 :73-78）；autoTail 自动滚底（:81-86）；`modLabel` 把模块名中的点换成下划线再当 i18n key（:38-41，点号是 vue-i18n 路径分隔符，不能直接用） | 无（visible 由 `logsStore` 驱动） | 无 | logs | `App.vue:50`；内嵌 `Modal`(:110)、`CustomSelect`(×3) |
| 17 | `PluginSettings.vue` | 解析插件管理（847 行）：`rule_inventory` 双段列表（脚本规则包 + 声明式规则）；导入 zip（dialog → base64 `<input>` 降级 :51-83）或 URL 导入；单条规则开关；官方索引查更新 / `plugin_pull` 安装（sha256 校验在壳层/Sidecar）；两段式删除确认 4s（:168-178）；`plugin_test` 试跑；解析失败台账（按域名分组，删域名/删单条/清空/复制/timeAgo） | 无 | 无 | 无（组件内直接调 `api/commands.ts`） | `SettingsView.vue:533`（展开区） |
| 18 | `AutostartPromptModal.vue` | 首次投屏成功后的自启询问弹窗：开启（`toggleAutostart`）/跳过，都会 `markAsked`（localStorage `has_asked_autostart`） | `visible` | `close` | settings | `HomeView.vue:66-69`；内嵌 `Modal`(:34) |
| 19 | `CastControl.vue` | 旧版内嵌投屏控制条（427 行）：画质选择器（qualities ≥ 2 才显示）+ 进度条 + 播放控制。**死代码**——全仓 grep 无任何 import | — | — | — | 无（未被挂载） |
| 20 | `FfmpegDepsModal.vue` | 「需要下载运行依赖」弹窗（418 行，v2.3.11 FFmpeg 去捆绑）：逐依赖显示检测状态与真实异常原文（`ffmpeg_status` 的 `items/missing`）；按平台给出包管理器安装命令（`install_commands`）与「打开安装目录」（`open_ffmpeg_dir`）；「重新检测」走 `refresh=true`；「下载并继续」起后台下载（`ffmpeg_download_start`）后按 1s 轮询 `ffmpeg_download_status`（`screencast.ts:122`；后端进度写入节流 0.4s，`ffmpeg_deps.py:373`）渲染进度/速度；`done` 时带 `refresh=true` 重测一次并**自动收起弹窗**（`:24` 的 watcher——「下载并继续」的「继续」是回到投屏面板，由用户再点一次开始投屏，程序不自动重放），安装命令可一键复制（`writeClipboardText`） | 无（visible 由 `screencastStore.showDepsModal` 驱动） | 无 | screencast | `ScreenCastPanel.vue:419`；内嵌 `Modal`(:4) |

---

## 5. Store 清单（9 个，`src/stores/`）

全部为 Pinia **setup store**（`defineStore('x', () => {...})`）。需要组件生命周期的 store 在内部使用 `onMounted`/`onUnmounted` 管理定时器与事件监听；device/cast/screencast 由 App 首次创建，清理随 App 而非路由卸载（`src/App.vue:19-24`，见 §3.5）。Tauri 事件通常动态 import 后 `listen`，unlisten 函数收进数组、卸载时统一释放；浏览器 mock 下注册失败被 catch。theme 的系统主题监听则使用 **store scope** 的 `onScopeDispose` 清理（`src/stores/theme.ts:37-45`），不是组件卸载钩子。

| Store | 行数 | 核心 state（类型） | 主要 actions | 订阅事件 | 调用的壳层命令 | 与其他 store 耦合 |
|---|---|---|---|---|---|---|
| `cast.ts` | 795 | `sessions: Record<string, CastSession>`、`focusedDeviceId: string\|null`、`showController`、`stopWarning: StopWarning\|null`、`lastError: string\|null`、`qualitySwitching`、`showAutostartPrompt`、`loading`；兼容用 computed `castState`（:88-111，把多设备折叠成聚焦设备视图） | `fetchState`(:175-219，恢复时也校验操作代次)、`startCast`(:341-399)、`stopCast`(:422-466)、`switchQuality`(:510-576，重解析 + restorePosition 最多 4 次 seek)、pause/resume/seek/setVolume/setMute、`startPolling`（1s tick、单飞与 epoch/逐设备操作 token 过滤旧响应，:221-288，见 §10.8）、`startSim`(:290-313，前端每秒 +1 模拟进度)、`checkAutostartPrompt`(:402-420，localStorage `has_asked_autostart` + 6s 延迟)、`openController`/`dismissStopWarning`/`dismissError` | `cast_state_changed`(:636-706)、`cast_error`(:711-720) | `get_cast_state`、`start_cast`、`stop_cast`、`pause_cast`、`resume_cast`、`seek_cast`、`set_volume`、`set_mute`、`get_cast_url` | 无 |
| `device.ts` | 179 | `devices: Device[]`、`selectedDevice`、`manualDevices`、`showAddModal` | `fetchDevices`（30s 自动取列表 :148-152）、`refresh`（手动发现 :38-47）、`addDevice`(:94-112，直接并入设备列表)、`openAddModal`、rename/setDefault/hide | `device_found`(:121-135，upsert)、`device_lost`(:138-142) | `get_devices`、`refresh_devices`、`add_device`、`get_manual_devices`、`rename_device`、`remove_device`、`hide_device`、`set_default_device` | 无 |
| `media.ts` | 230 | `state: 'idle'\|'dragover'\|'input'\|'parsing'\|'ready'`、`mediaInfo: MediaInfo\|null`、`localFile: File\|null`、`urlInput`、`error`、`ledgerHint` | `parseFile`(:83-116，路径字符串→Tauri；File 对象→本地元数据 + localFile，预览 URL 由 MediaInput 生成)、`parseUrl`(:124-203，见 §6 链路 1)、`setInputChange`/`reset` 递增 `parseGeneration` 使旧请求失效（:42-78）；新解析清空旧预览，仅最新代次可写回成功/失败，直链降级容忍畸形百分号（:173-180）；`setThumbnail` | 无 | `parse_media_file`、（经 `resolveShare`）`resolve_share` + `get_resolve_result` | 无 |
| `guard.ts` | 190 | `pendingRequest: CastConfirmRequest\|null`、`countdown`、`settings: GuardSettings`、`trustedDevices`/`blacklistedDevices: GuardDevice[]`、`loading` | `handleResponse`(:72-86)、`fetchDevices`、`fetchSettings`、`removeDevice`、`setPolicy`、倒计时（到 0 自动拒绝 `handleResponse(false,'once')` :33-39）；`setupListeners` 在 `onMounted` 之外也被立即调用一次（:171，保证 App 级弹窗不依赖组件挂载时机） | `cast_confirm_request`(:126-149：guard 禁用时自动批准 :129-132；已有 pending 时拒绝新请求 :134-137；`win.show()+setFocus()` 把窗口拉到前台 :141-148) | `respond_cast_confirm`、`get_guard_devices`、`remove_guard_device`、`set_guard_policy`、`get_guard_settings`、`save_guard_settings` | 无 |
| `settings.ts` | 83 | `settings: AppSettings\|null` | `fetchSettings`、`saveSettings`（增量合并）、`toggleAutostart` | `autostart-changed`(:28-38，托盘改自启时同步) | `get_settings`、`save_settings`、`get_autostart`、`set_autostart` | 无 |
| `theme.ts` | 49 | `mode: 'light'\|'dark'\|'system'`、内部响应式 `systemDark`、computed `applied`（:9-17；不是 `resolved`） | `apply`/`toggle`(:24-32)；`persist`(:19-22)：写 `data-theme` + `localStorage 'hancast-theme'`；非法已存模式回退 system；持续监听 matchMedia change 更新 systemDark，system 模式才更新 DOM，`onScopeDispose` 移除监听（:37-45，见 §7.2） | 无（matchMedia 非 Tauri 事件） | 无 | 无 |
| `logs.ts` | 124 | `visible`、`page: LogPage\|null`、`query: LogQuery`、`level` | `open`/`close`、`refresh`（`TAIL_INTERVAL = 3000` :7；撞车时排队 `pendingRefresh` :33-36）、`toggleDebug`（setLogLevel DEBUG/INFO） | 无 | `get_logs`、`set_log_level`、`export_logs` | 无 |
| `update.ts` | 131 | `info: UpdateInfo\|null`、`modalState: UpdateModalState`、`showModal`、`storeVersion` | `checkUpdate`（商店版跳过 :28-31）、`ignoreVersion`、`openDownload`(:71-76，window.open) | `update-available`(:94-97，Rust 启动 3s 后推送) | `check_update`、`ignore_update_version`、`is_store_version` | 无 |
| `screencast.ts` | 327 | `state: ScreenCastState`、`sources: ScreenSources\|null`、`stopWarning`、`showDepsModal`(:37)、`ffmpegReport: FfmpegStatus\|null`(:38)、`downloadTask: FfmpegDownloadTask\|null`(:39)、`downloading`(:40)、`reportLoading`(:41)；computed `ffmpegOk`(:46) | `fetchSources`(:56-70，顺带把 `sources.ffmpeg` 回填进 `ffmpegReport`)、`refreshFfmpegReport`(:73-88，`refresh=false` 吃后端 30s 缓存，弹窗首开秒出)、`startFfmpegDownload`(:114-127，起任务后 1s `setInterval` 轮询 `pollDownload` :97-111，`done` 自动重测)、`resumeDownloadPolling`(:143-157，打开依赖弹窗时向后端对齐；下载定时器现在由 App 生命周期持有，路由切换不再触发 :287-291 的清理，旧「关面板就断」注释不再代表当前挂载关系)、`openFfmpegDir`(:129-139)、`openDepsModal`/`closeDepsModal`(:159-167)、`start`(:177-196)、`stop`(:198-231，`control_failed` → stopWarning，**借 `useDeviceStore` 把 device_id 翻译成设备名** :221)、`setFfmpegPath`(:233-247)、`dismissStopWarning`(:324) | `screen_cast_state_changed`(:268-274，迟到注册在卸载后立即释放 :275-276) | `list_screen_sources`、`start_screen_cast`、`stop_screen_cast`、`screen_cast_status`、`set_ffmpeg_path`、`ffmpeg_status`、`ffmpeg_download_start`、`ffmpeg_download_status`、`open_ffmpeg_dir` | **→ device**（唯一一处 store 间依赖，grep 验证） |

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
 │                     │        media.ts:124-203                 │                 │              │
 │                     │                     ├─resolve_share───►│ (:256-268)─────►│ 后台线程解析   │
 │                     │                     │◄─{task_id}────────┤                 │              │
 │                     │                     ├─get_resolve_result 每400ms轮询，45s兜底 (:684-705)  │
 │                     │                     │                  │ (:270-278)─────►│              │
 │                     │                     │◄─{status:done, result:MediaInfo}────┤              │
 │                     │  state='ready'，输入框回显 source_url，预览缩略图             │              │
 │ 点[投屏]             │ onCast (:398-425)    │                  │                 │              │
 │────────────────────►│ 优先级: selected→default→弹设备选择Modal  │                 │              │
 │                     ├──────────────────────────────────────────────────────────►│ startCast    │
 │                     │                     │                  │                 │  (:341-399)   │
 │                     │                     │                  │◄─start_cast─────┤              │
 │                     │                     │                  │ (:430-448)─────►│ DLNA 投递     │
 │                     │                     │                  │                 │              │
 │                     │        命令成功后乐观 status='playing' (:363)、startSim 每秒+1 (:290-313)、│
 │                     │        startPolling 1s 单飞检查、openController、checkAutostartPrompt     │
 │                     │                     │                  │                 │              │
 │                     │◄─────────────事件 cast_state_changed───────────────────────┤ (:636-706)   │
 │                     │        PLAYING/PAUSED/STOPPED/NO_MEDIA_PRESENT 映射为权威状态；           │
 │                     │        connecting/playing/paused 会话不被瞬时 stopped 移除                │
```

要点：

1. 剪贴板读取走 Rust（`read_clipboard`），Web API 仅降级（`commands.ts:934-947`，原因见 §10.1）。
2. `resolve_share` 是**异步任务**：立即返回 `task_id`，前端 400ms 轮询 `get_resolve_result`，45s 兜底超时（`commands.ts:680-705`；Sidecar 单条解析预算 25s，注释写明）。兼容「直接返回结果」的旧实现/浏览器 mock（`commands.ts:690-694`）。
3. 后端解析失败时**不再无条件透传**：只有从文本里抠出的地址本身带媒体扩展名（`media.ts:15-19` 白名单）才 `extractFirstUrl` + 按扩展名推 MIME 透传；网页地址一律如实报错（`media.ts:158-203`）。旧口径「能抠出链接就降级透传」会让随便一个 HTML 页面也能点投屏、点了却毫无动作，且失败被吞进台账之外无人看见。报错时 `ledgerHint` 置真，输入框下方直接指出「设置 → 解析插件 → 解析失败台账（按域名分组）」入口（`MediaInput.vue:523`）；本轮为降级文件名解码加 try/catch，畸形 `%` 不会再次抛错而卡住 parsing（`media.ts:173-180`）。
4. 投屏目标选择优先级：选中设备（在线）→ 默认设备（在线）→ 弹设备选择 Modal（`MediaInput.vue:398-425`）。
5. `startCast` **等待命令成功但不等事件**，然后乐观置 playing（`cast.ts:355-363` 注释「Set playing immediately — don't wait for event」），同时启动本地进度模拟与 1s 单飞轮询；状态事件处理另见 §10.6，操作代次防护见 §10.8。
6. Ctrl+V 直接进输入框时：纯链接保持默认粘贴（可编辑），**非纯链接的口令文本**才拦截并自动解析（`MediaInput.vue:377-384`）。
7. 本轮 `parseGeneration` 统一隔离文件/URL 的在途解析：新解析、清空、编辑输入均使旧结果失效，成功和异常分支都先验代次（`media.ts:42-78`、`:83-116`、`:124-159`）。输入框通过 `setInputChange` 更新（`MediaInput.vue:579-580`），同时清掉旧媒体/本地预览和错误，避免旧响应复活已清空或替换的内容；历史上没有此保护，慢请求可能覆盖较新的选择。这里只阻止前端写回，不取消后端任务。

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
    │               ├─emit cast_state_changed──────►cast.ts (:636-706) 更新会话状态          │
```

要点：

1. `cast_confirm_request` 事件由 `guard` store 监听；`GuardConfirmModal` 在 `App.vue:46` 全局挂载，所以任何路由下都能弹。
2. 倒计时到 0 **自动拒绝**（`guard.ts:33-39`），策略 `once`。
3. 应答四态：`once`（仅此次）/ `always`（始终允许，进 trusted）/ `reject` / `blacklist`，经 `respond_cast_confirm(request_id, approved, policy)`（`lib.rs:626-640`）回传。
4. 收到请求时前端主动 `show() + setFocus()` 把窗口拉到前台（`guard.ts:141-148`）——投屏确认必须让用户看见。
5. MPV 播放本身发生在壳层/Sidecar（`check_mpv`/`set_mpv_path` 命令存在于 `lib.rs:1307-1370` 的 invoke_handler 清单）；前端不参与播放，只通过 `cast_state_changed` 感知状态。旁证：`CastControllerView.vue:76-77` 注释「走 store：带 device_id 打到电视；裸 invoke 会落到本地 MPV」。⚠️ MPV 进程管理细节属后端范围，本文未验证其实现。

---

## 7. 样式系统

### 7.1 `src/styles/variables.css` 变量清单

本轮采用**原生桌面风格的紫色强调色 + 实色亮暗表面**，不是引入原生 UI 控件库/系统毛玻璃。历史上的亮蓝/暗靛蓝与半透明背景已替换，遮罩、阴影等仍可使用 alpha（`variables.css:3-14`、`:55-86`；`Modal.vue:115`）。

`:root`（亮色默认值，`color-scheme: light`，`variables.css:1-53`）：

| 分组 | 变量 = 值 |
|---|---|
| Primary（:4-9） | `--primary: #7056d8`；`--primary-hover: #6045c5`；`--primary-active: #5139ad`；`--primary-rgb: 112, 86, 216`；`--primary-light: #f0ecfb`；`--on-primary: #ffffff` |
| Background（:11-14，实色） | `--bg-primary: #f6f6f9`；`--bg-secondary: #eeeef3`；`--bg-card: #ffffff`；`--bg-input: #f5f5f8` |
| Text（:16-19） | `--text-primary: #242330`；`--text-secondary: #656373`；`--text-tertiary: #767382`；`--text-link: #684dce` |
| Status / Danger（:21-25） | `--status-online: #218361`；`--status-busy: #aa6a16`；`--status-offline: #8c8998`；`--danger: #c63e4c`；`--danger-light: #fff0f1` |
| Border（:27-29） | `--border: #e5e3ec`；`--border-focus: #7056d8`；`--border-dashed: #d7d2e6` |
| Spacing（:31-36） | `--sp-xs: 4px`；`--sp-sm: 8px`；`--sp-md: 12px`；`--sp-lg: 16px`；`--sp-xl: 24px`；`--sp-2xl: 32px` |
| Radius（:38-42） | `--r-sm: 6px`；`--r-md: 8px`；`--r-lg: 12px`；`--r-xl: 16px`；`--r-full: 9999px` |
| Shadow（:44-46） | `--shadow-sm: 0 1px 3px rgba(33,27,56,0.03)`；`--shadow-md: 0 4px 16px rgba(33,27,56,0.06)`；`--shadow-lg: 0 16px 48px rgba(33,27,56,0.16)` |
| Font（:48-49） | `--font: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif` |
| Titlebar（:50） | `--titlebar-height: 44px` |
| Transition（:51-52） | `--transition-fast: 150ms ease`；`--transition-normal: 220ms ease` |

`[data-theme="dark"]`（`variables.css:55-87`）设 `color-scheme: dark`，仍为紫色系：`--primary: #a58af5`、`--primary-hover: #b49cfb`、`--primary-active: #9576e9`、`--primary-rgb: 165, 138, 245`、`--primary-light: #302a45`、`--on-primary: #211831`；背景 `#19181f / #2b2933 / #23222b / #1c1b23`（依次 primary/secondary/card/input）；文字 `#f1eff7 / #b5b0c2 / #9993a8`、链接 `#baa2ff`；状态 online/busy/offline 为 `#63c6a0 / #e3b66c / #8c859a`，danger/light 为 `#ff929e / #3b252e`；边框 `#37333f / #a58af5 / #494151`；三档 shadow 分别为 `0 1px 3px rgba(0,0,0,0.12)`、`0 4px 16px rgba(0,0,0,0.16)`、`0 16px 48px rgba(0,0,0,0.4)`。Spacing/Radius/Font/Titlebar/Transition 不随主题变。

### 7.2 主题落地 DOM 的机制

1. `theme` store 读取/校验已存模式，非法值回退 `system`；`systemDark` 是响应式 ref，computed `applied` 结合它与 `mode` 得到视觉主题（`src/stores/theme.ts:9-17`）。`persist()` 把 `applied` 写到 `document.documentElement` 的 `data-theme`，把 mode（含 `system`）存 `localStorage 'hancast-theme'`（`:19-22`）。
2. `main.ts:12-16` 在 `createApp` **之前**先读 localStorage 应用一次 `data-theme`——避免暗色用户首绘白闪。
3. matchMedia change **始终更新 `systemDark`**，仅 system 模式跟随修改 DOM；显式亮/暗不会被系统切换覆盖，但切回 system 立即使用最新系统值。监听随 store scope 销毁释放（`theme.ts:37-45`）。标题栏 toggle 基于当前 `applied` 翻转成显式亮/暗（`:29-32`）。历史上 computed 直接读非响应式 `matchMedia.matches` 会缓存旧主题，仅改 DOM 导致图标/下一次 toggle 不一致，本轮改为 reactive 数据源。

### 7.3 全局约定（`src/styles/global.css`）

- `@import './variables.css'` 后做全量 reset（`box-sizing: border-box`，margin/padding 清零，:1-8）。
- `html, body`：`overflow: hidden`（窗口级滚动禁用，滚动发生在页面/组件容器）；`body` 背景仍为 `transparent`、字号 14px、抗锯齿（:10-24），但 Home/Settings 自身用实色 `--bg-primary`（`HomeView.vue:74-79`、`SettingsView.vue:608-615`）。
- 滚动条统一 6px WebKit 伪元素样式（:31-47）；本轮未据此声称三平台实测。
- button/input/textarea/a 去默认样式；全局 **button/a/[tabindex]** 的 `:focus-visible` 为 2px outline（:55-101），input/textarea 仅设 accent-color，焦点样式由组件补充，不能泛称所有输入都有全局焦点环。
- 公共动画 keyframes：`spin`（刷新按钮）、`fadeIn`、`pulse`（:103-117）；`prefers-reduced-motion: reduce` 全局缩短动画/过渡并禁用平滑滚动，覆盖组件内动效（:119-127）。
- 全局 `@media (max-width: 360px)` 将 body 字号降为 13px（:129-134）；页面另有窄窗/矮窗布局分支（§3.3/3.4）。
- 组件样式以 `<style scoped>` 为主，纯 CSS，无预处理器；不是全部 scoped，例如 DeviceCard 的 Teleport 右键菜单另有非 scoped 样式（`DeviceCard.vue:478-512`）。

---

## 8. 国际化（vue-i18n）

### 8.1 结构与初始化

`src/main.ts:18-26`：`createI18n({ legacy: false, locale: 'zh-CN', fallbackLocale: 'en-US', messages: { 'zh-CN': zhCN, 'en-US': enUS } })`。Composition API 模式（`useI18n()` 的 `t`/`locale`）。

语言文件：`src/locales/zh-CN.json`、`src/locales/en-US.json`（2026-10-24 工作区各 323 行，包含本轮新增首页文案）。

### 8.2 key 命名约定

- 两级为主：`模块.camelCaseKey`。顶层命名空间 16 个（2026-10-24 按首个点号前缀去重）：`app`、`autostartPrompt`、`cast`、`close`、`common`、`controller`、`devices`、`footer`、`guard`、`home`、`logs`、`media`、`screencast`、`settings`、`theme`、`update`。本轮新增 `home`，旧取证为 15 个。
- **点号陷阱**：动态拼 key 时，模块名里的点会被 vue-i18n 当成路径分隔符。`LogsPanel.vue:38-41` 的 `modLabel` 因此把日志模块名中的 `.` 换成 `_` 再查 key，并做了查表缓存。

### 8.3 两语言是否等键（脚本比对结果）

2026-10-24 读取两份扁平 JSON 比对：**zh-CN / en-US 各 321 个 key，双向差集为 0**。此前 278 个是历史取证数，不能继续当作现值。

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
| 文件解析 | `media.ts:83-116` 双模式：`<input type=file>` 的 File 留在 `localFile`；`MediaInput.vue:102-103`、`:128-136` 生成 `URL.createObjectURL` 做图片/视频预览 |
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

### 10.5 未用组件库 + wide Modal 的视口约束（本轮已修）

无 Element Plus（§1.1）。历史上 `wide` 的 `min-width: 520px` 超过默认窗宽 434px，LogsPanel 等弹窗会被挤出窗口。本轮去掉该下限：overlay 固定铺满视口并留 16px 内边距，框体 `min-width: 0`、`max-width: 100%`、`max-height: 100%`、`overflow-y: auto` 和长文本换行，wide 仅设期望宽 760px（`src/components/Modal.vue:112-141`），实际可收缩到视口可用宽高，不要求用户先拉宽窗口。

Modal 打开后聚焦 dialog，自带 Escape 关闭、Tab/Shift+Tab 循环与关闭/卸载还焦点（`Modal.vue:28-78`）；键盘处理仅作用于 DOM 中最后一个 `.modal-overlay`，不应外推到所有自绘弹层。`CastController` 是独立实现，不用 Modal，另有视口减 24px 的宽高上限及纵向滚动（`CastController.vue:375-393`）。

### 10.6 DLNA 状态是「不可靠流」，前端做了三层防御

`src/stores/cast.ts` 的注释与实现：(a) 当前 playing/paused 会话不由轮询改写状态（:255-259，旧文简写「只升不降」，不是状态全序）；(b) 轮询不删会话（:228-280）；(c) `cast_state_changed` 对原 connecting/playing/paused 会话收到 stopped/idle 时暂不删除，但**仍先写入事件状态并停模拟进度**（:676-691），其他删除路径再等 500ms 复核（:692-698）。事件与轮询的防御不能混为「忽略停止状态」。轮询的新单飞与操作代次防护见 §10.8。

### 10.7 `stop_cast` 的 `control_failed` 不能吞

`lib.rs:455-458` 注释：本地会话已释放、但电视没收到 Stop 时，前端要靠返回体里的 `control_failed` 解释「界面停了、电视还在播」。落点：`cast.ts:422-466`（stopCast 在 finally 释放仍属于本次操作的会话 + 置 `stopWarning`，不会删掉等待 Stop 时新开的同设备会话）→ `StopWarningBanner`（`HomeView.vue:53-57`）；屏幕投屏同款逻辑 `screencast.ts:198-231`。类型定义 `StopCastResult`/`StopWarning` 在 `commands.ts:1103-1117`。

### 10.8 进度条与轮询的打架：主动解锁 + 单飞 + 操作代次

历史上解锁逻辑放在无人消费的 computed 中，惰性 getter 不执行，seek 显示可能一直锁住；轮询也曾可重叠，并用只向前的位置合并掩盖旧响应。本轮分别处理：

- **组件交互锁**：`CastController.vue:145-149` 用主动 `watch` 观察位置、时长和锁目标，距目标小于 3 秒即解锁；seek 请求完成（失败同样返回）或 5s 兜底也解锁。`seekRequest` 保证旧请求结束不释放新拖动的锁（`:199-220`）。切设备、隐藏控制器、卸载都重置交互并清除定时器（`:151-163`）。音量仍为 50ms 防抖，但回调捕获操作时的设备 id，不向后来聚焦的设备发音量（`:112-121`）。
- **轮询单飞与生命周期代次**：`src/stores/cast.ts:221-288` 每 1s 触发一次检查；`pollInFlight` 保证整个逐设备扫描只有一轮在途，慢请求期间跳过 tick。`pollEpoch` 隔离 stop/restart，旧轮响应无权写回；停止定时器不会提前释放在途锁，只有 `finally` 释放。因此不是「每秒必有一次新请求」。
- **逐设备操作代次**：start/stop/pause/resume/seek/画质切换替换设备 token（`cast.ts:41-50`、`:341-597`）；轮询跳过 pending 操作，返回时检查 epoch、会话仍存在与 token 身份（`:243-250`）。旧 start/stop/seek/画质恢复的会话写回不能覆盖同设备新操作，首次恢复 `fetchState` 也检查 token 快照（`:175-192`）。新鲜轮询位置允许合法回退（向后 seek），仅 playing/paused 时位置与时长全零的样本不采纳（`:261-271`）；这不是后端取消机制，也不代表无 token 的事件流已能辨认旧响应。

播控错误写入 `lastError`，由首页与控制器显示原始原因（`HomeView.vue:49-52`、`CastController.vue:244-248`）。seek/画质失败后如当前会话仍 playing 会恢复模拟进度；画质恢复位置最多试 4 次，最后一次失败不再静默吞掉（`cast.ts:544-596`）。画质 select 在请求后回读当前会话，失败时复位所选档位（`CastController.vue:54-60`）。上述为前端防护，不等于电视已执行命令；本轮验证边界见附录。

### 10.9 macOS 麦克风授权（TCC）要由 WebView 代触发

`ScreenCastPanel.vue:106-110` 注释：macOS 的麦克风 TCC 权限按 **bundle** 判定，Sidecar 拉起的 ffmpeg 不会触发系统授权弹窗；所以开始屏幕投屏前，前端先在 WebView 里 `getUserMedia({ audio: true })` 拿一次授权（:111-129），成功后立即释放流。

### 10.10 Windows 系统声音是 WASAPI 环回「镜像设备」列表

`ScreenCastPanel.vue:78-80` 注释 + `pyproject.toml:23-26`：FFmpeg 没有 WASAPI 输入（上游 ticket #9408 未合并），Sidecar 用 `pyaudiowpatch` 把渲染设备采成 PCM。环回列表里**只有系统当前默认输出那条真的在出声**，UI 用 `defaultOutputSuffix` 标注，避免用户选错静音设备。

### 10.11 死代码（改动时别被误导）

- `src/components/CastControl.vue`（427 行，内嵌控制条 + 画质选择器）：全仓无 import。现役控制器是 `CastController.vue`。
- `src/views/CastControllerView.vue`（323 行，整窗控制器变体）：不在路由表、无 import。其中 :76-77 的注释（「走 store：带 device_id 打到电视；裸 invoke 会落到本地 MPV」）仍有文档价值。

### 10.12 渲染性能注意点

- 进度每秒跳动由 `startSim` 本地模拟（`cast.ts:290-313`），新鲜轮询仍可校正位置；投屏轮询每 1s tick、在途则跳过、逐设备扫描，不能再写成固定每秒并发拉取（`cast.ts:221-288`）。日志 tail 固定 3s（`logs.ts:7`）。
- 日志关键字 450ms 防抖（`LogsPanel.vue:73-78`）、音量 change 后 50ms 防抖（`CastController.vue:112-121`，input 本地即时更新）、窗口尺寸保存 500ms（Rust 侧）。
- 日志刷新撞车时排队而不并发（`logs.ts:33-36` `pendingRefresh`）；投屏轮询使用的是跳过 tick 的单飞策略，不是日志的排队策略。
- 缩略图抽帧用 `<video>` + canvas 在前端完成并生成 JPEG data URL（`MediaInput.vue:29-40`）；Tauri 本地预览源走 `asset:`，浏览器 File 则由组件生成 blob URL（`:90-109`、`:128-136`），不能把缩略图也写成「不进 base64」。

### 10.13 其他平台/环境差异

- 多屏屏幕投屏默认选主屏（`ScreenCastPanel.vue:97-98` 注释）；显示器源 id 形如 `screen:x,y,w,h`（`types/screencast.ts`）。
- 商店版（MSIX）行为差异：自启走 StartupTask（§2.4）、跳过更新检查（`update.ts:28-31`）、About 区显示 MS Store 链接（`SettingsView.vue:561-583`）。
- 右键菜单是自定义 Teleport 菜单（`DeviceCard.vue:233-243`），且非 DEV 构建全局禁用了原生 contextmenu（`App.vue:26-39`）。
- 窗口恢复尺寸有最小值保护 346×472（`lib.rs:1091-1095`），并禁用最大化（:1108）——布局只需考虑 346–800px 宽度区间。

---

## 附：本文档取证范围

2026-10-15 原始取证记录（保留，不代表本轮重新验证）：实读文件 `src-tauri/tauri.conf.json`、`src-tauri/capabilities/default.json`、`src-tauri/src/lib.rs`（1443 行全读）、`src-tauri/Cargo.toml`、`hancast-backend/pyproject.toml`、`package.json`、`vite.config.ts`、`tsconfig.json`、`env.d.ts`、`index.html`、`src/main.ts`、`src/App.vue`、`src/router/index.ts`、3 个视图（含 967 行 SettingsView 与 323 行 CastControllerView 全读）、20 个组件、9 个 store、`src/api/commands.ts`（1348 行全读）、`src/types/` 全部 9 个文件、`src/styles/` 2 个文件、`src/locales/` 2 个文件（脚本比对 key）、git 历史（`git log -S "unsafe-eval"`）。

2026-10-24 本轮补充：完整阅读本文，以及最新 App、HomeView/SettingsView、DeviceCard/List、MediaInput、Modal/CustomSelect、CastController、media/theme/cast/device/screencast stores、两份 styles 与 main.ts；核对相关工作区 diff。只更新受影响事实，不将旧取证日期/计数整体换成新结果。

**本轮验证结果与边界**：`tools/verify-media-theme.cjs` 的 73 项、`tools/verify-cast-ui.cjs` 原 20 项及封面新增 1 项、`tools/verify-app-lifecycle.cjs` 的 2 项均通过（合计 96 个不同用例，按受影响部分分批运行，未跑全仓测试）。这些测试编译真实源码并使用 Vue/Pinia，但命令/设备/计时器为内存替身，不访问 Sidecar、网络或用户设置。类型检查与最终 Vite 构建通过；中英各 321 个文案键一致。

浏览器运行真实前端（内建 mock 设备），已验 346×472 / 434×634 / 800×900 无横向溢出，及英文设置/高级参数、键盘下拉选择、日志弹窗焦点与边界、屏幕面板、解析→投屏控制→暂停/停止/清空。最终 [浅色截图](images/ui-native-light.png) / [深色截图](images/ui-native-dark.png) 已保留。不得据此声称真实电视投屏成功或三平台 WebView/安装包验证通过；后端待修问题见 [架构总览 §11](架构总览.md#11-2026-10-24-架构复查与增强顺序)。
