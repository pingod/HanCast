# AGENTS.md — HanCast 项目指南

> 旧名 `CLAUDE.md`（Claude Code 私有约定），2026-10-15 改名为跨工具的 `AGENTS.md`；
> `CLAUDE.md` 现在只留三行指针指向本文件。

> 最后校对：2026-10-15，对应版本 v2.3.13（`package.json:4`）。
> 本文件是速览与硬约束；逐模块的深度内容在 **docs/README.md** 的索引里，别在这里找细节。

## 项目概述

**HanCast** 是基于 [xfangfang/Macast](https://github.com/xfangfang/Macast) 的二次开发项目，跨平台投屏应用，支持将媒体文件/链接投屏到局域网设备，同时可作为 DLNA 接收端。

- **项目名**: HanCast
- **作者**: lanzeweie@foxmail.com
- **版本**: 2.3.13（版本 SSOT 是 `package.json` 的 `version`，`scripts/sync-version.cjs:4`；改版本只走 `npm run version:set`，见「版本管理」）
- **性质**: 二次开发（非原项目官方更新）
- **原始代码**: [xfangfang/Macast](https://github.com/xfangfang/Macast)（原作者 xfangfang，最后更新 2022-01）。仓库里**不含** `Macast-main/`、`Macast-plugins-main/` 这些本地目录，1.x 源码只作为移植参考，见「原始代码复用情况」
- **目标架构**: Tauri 2.0 前端 + Rust 桥接 + Python Sidecar（+ MPV 接收端播放、FFmpeg 屏幕抓取）
- **协议**: GPL-3.0（继承原项目）

## 技术栈

| 层级 | 技术 | 版本 |
|------|------|------|
| 前端框架 | Tauri 2.0 | ^2.0.0 |
| 前端 UI | Vue 3 + TypeScript + Vite | Vue ^3.4.0 |
| 状态管理 | Pinia | ^2.1.7 |
| 路由 | vue-router | ^4.2.5 |
| 国际化 | vue-i18n | ^9.9.0 |
| 桥接层 | Rust (Tauri Core) | — |
| 后端服务 | Python Sidecar（Nuitka standalone 编译） | >= 3.11 |
| 协议支持 | DLNA/UPnP/SSDP | — |
| 媒体播放 | MPV（捆绑外部依赖，接收端播放） | CI 固定 v0.41.0 |
| 屏幕抓取 | FFmpeg（HLS 切片）+ 原生 helper | **不随包**；运行时检测本机已装，缺失按需下载（`ffmpeg_deps.py`） |
| 解析沙箱 | QuickJS（`quickjs` python 包，规则包 JS 执行） | >= 1.19.4 |
| HTTP 指纹 | curl_cffi（JA3 伪装）+ aiohttp（HLS 供流） | — |

## 项目结构

```
HanCast/
├── AGENTS.md                    # 本文件 — 编码代理的约束与命令速查（跨工具约定）
├── CLAUDE.md                    # 只作兼容指针，内容以 AGENTS.md 为准
├── README.md / README_en.md     # 中英双语门面
├── CHANGELOG.md                 # 每个 tag 一笔，含发布 commit
├── PRIVACY_POLICY.md
├── package.json                 # 前端依赖 + tauri/version 脚本
├── vite.config.ts               # Vite 配置（端口 1420）
├── tsconfig.json / env.d.ts
├── index.html                   # Vite 入口 HTML
│
├── src/                         # Vue 3 前端源码
│   ├── main.ts                  # 应用入口（Pinia/Router/i18n 初始化）
│   ├── App.vue                  # 根组件（主题检测、设置加载、全局事件订阅）
│   ├── api/
│   │   └── commands.ts          # Tauri invoke 封装 + 浏览器 mock
│   ├── components/              # 20 个组件，清单见 docs/HanCast-Frontend-Spec.md
│   │   ├── TitleBar.vue         # 自定义标题栏（最小化、关闭、设置）
│   │   ├── MediaInput.vue       # 拖拽 + URL 输入 + 剪贴板粘贴
│   │   ├── DeviceList.vue / DeviceCard.vue / AddDeviceModal.vue   # 设备
│   │   ├── CastControl.vue      # 投屏状态栏（播放/暂停/停止指示）
│   │   ├── CastController.vue   # 投屏控制面板（音量、进度、停止）
│   │   ├── ScreenCastPanel.vue  # 屏幕投屏（抓取源、观看链接、系统声音）
│   │   ├── FfmpegDepsModal.vue  # 「需要下载运行依赖」弹窗（缺失清单、安装命令、按需下载）
│   │   ├── GuardConfirmModal.vue / GuardDeviceList.vue  # 投屏确认与信任管理
│   │   ├── PluginSettings.vue   # 规则包管理（导入、更新、启停）
│   │   ├── LogsPanel.vue        # 后台日志面板
│   │   ├── UpdateModal.vue / DownloadModal.vue / AutostartPromptModal.vue
│   │   ├── StopWarningBanner.vue / Modal.vue / CustomSelect.vue / Footer.vue
│   ├── views/
│   │   ├── HomeView.vue         # 主视图
│   │   ├── SettingsView.vue     # 设置页（语言、DLNA 名称、后台参数、关于）
│   │   └── CastControllerView.vue # 整窗控制器变体，无路由/无 import（死代码，见 docs/HanCast-Frontend-Spec.md §10.11）
│   ├── stores/                  # 9 个 Pinia store
│   │   ├── cast.ts              # 投屏状态 + 事件订阅
│   │   ├── device.ts            # 设备列表（30s 自动刷新）
│   │   ├── media.ts             # 媒体输入状态机
│   │   ├── settings.ts          # 应用设置
│   │   ├── screencast.ts        # 屏幕投屏状态
│   │   ├── guard.ts             # 投屏确认 / 信任设备
│   │   ├── logs.ts              # 日志面板取数
│   │   ├── update.ts            # 自更新与规则包更新
│   │   └── theme.ts             # 主题检测
│   ├── types/                   # TypeScript 类型定义（与 store 一一对应）
│   ├── locales/                 # zh-CN.json / en-US.json
│   ├── router/index.ts          # Hash 路由：/ (Home), /settings（仅这两条，src/router/index.ts:8,13）
│   ├── styles/                  # variables.css（亮/暗）、global.css
│   └── assets/icon.svg
│
├── src-tauri/                   # Tauri Rust 后端
│   ├── Cargo.toml               # Rust 依赖
│   ├── tauri.conf.json          # base 配置（平台无关的 resources / bundle）；版本号由 sync-version 从 package.json 刷入
│   ├── tauri.windows.conf.json  # Windows 覆盖（数组整体替换，见文档）
│   ├── tauri.macos.conf.json    # macOS 覆盖
│   ├── tauri.linux.conf.json    # Linux 覆盖
│   ├── build.rs                 # tauri_build::build()
│   ├── icons/                   # 应用图标
│   └── src/
│       ├── main.rs              # 入口，调用 hancast_lib::run()
│       ├── lib.rs               # Tauri 应用设置 + 66 个 #[tauri::command]（清单见 generate_handler!，lib.rs:1373-1440）
│       └── sidecar.rs           # Python Sidecar 管理器（stdin/stdout JSON，COMMAND_TIMEOUT=60s）
│
├── hancast-backend/              # Python Sidecar 后端
│   ├── pyproject.toml           # Python 项目配置（版本由 sync-version 同步）
│   ├── requirements.txt / uv.lock
│   ├── README.md                # Sidecar 范围速查（协议信封、三条铁律、命令分组）
│   ├── hancast_sidecar/
│   │   ├── main.py              # Sidecar 入口（148 行：UTF-8 stdio、stdin/stdout JSON 循环、双击兜底）
│   │   ├── commands.py          # CommandHandler — 命令路由（59 条注册表 commands.py:110-197）
│   │   ├── params.py            # 后台参数 PARAM_SPECS（32 项，params.py:41-265）
│   │   ├── ssdp.py              # SSDP 设备发现（1322 行；投屏准入见 _provides_cast_service）
│   │   ├── protocol/
│   │   │   ├── dlna.py          # DLNA 控制端实现（1229 行，AVTransport/RenderingControl SOAP）
│   │   │   └── server.py        # DLNA 描述服务（Description.xml、SCPD、事件 SUBSCRIBE）
│   │   ├── renderer/
│   │   │   ├── base.py          # 渲染器基类
│   │   │   └── mpv.py           # MPV 渲染器（IPC: named pipe / unix socket）
│   │   ├── media/
│   │   │   ├── parser.py        # 媒体文件/URL 解析器
│   │   │   ├── share_resolver.py # 分享口令/短链/网页 → 可播直链（站点规则全部来自已装规则包）
│   │   │   ├── server.py        # 本地文件 HTTP 服务（Range + 边下边播代理）
│   │   │   ├── failures.py      # 解析失败台账（供前端/日志回看）
│   │   │   └── rules/           # 规则引擎：base / declarative（JSON）/ jsruntime（QuickJS）/ jsplugins（包管理）/ crypto
│   │   ├── screencast/
│   │   │   ├── manager.py       # 会话编排、观看链接（/r/<session_id>）
│   │   │   ├── capture.py       # 窗口/屏幕抓取源
│   │   │   ├── ffmpeg_manager.py # FFmpeg 命令拼装与 HLS 切片（1048 行；检测委托 utils/ffmpeg_deps.py）
│   │   │   ├── loopback.py      # Windows 系统声音（WASAPI 环回，pyaudiowpatch）
│   │   │   ├── macos_tap.py     # macOS 屏幕/音频原生 helper
│   │   │   ├── httpd.py         # 观看页 HTTP 服务（默认 8765，绑 0.0.0.0）
│   │   │   └── www/             # 观看页（index.html + hls.min.js）
│   │   ├── security/guard.py    # Device Guard：未知设备投屏确认、信任/黑名单
│   │   ├── models/              # cast / device / media / mpv / session dataclass
│   │   ├── utils/
│   │   │   ├── config.py        # 配置管理（AppData JSON 文件）
│   │   │   ├── protocol_io.py   # stdout 单写者（唯一能写协议通道的出口）
│   │   │   ├── httpclient.py    # 统一 HTTP 客户端（curl_cffi JA3 指纹 + 代理）
│   │   │   ├── identity.py      # 设备身份（device_identity.json）
│   │   │   ├── mpv_manager.py   # 捆绑 mpv 路径与进程管理
│   │   │   ├── ffmpeg_deps.py   # FFmpeg 依赖：本机检测 + 缺失时按需下载（524 行）
│   │   │   ├── platform_info.py # 平台探测
│   │   │   └── logger.py        # 日志设置（滚动文件 + read_logs 取数）
│   │   └── xml/                 # UPnP 描述 XML（Description / AVTransport / ConnectionManager / RenderingControl / SinkProtocolInfo.csv / setting.html）
│   ├── scripts/
│   │   ├── build_sidecar.py     # Nuitka 构建脚本 + flatten_dependencies()
│   │   └── run_sidecar.py       # 独立 Sidecar 运行器
│   └── tests/                   # 23 个 test_*.py + 2 个 smoke_*.py + conftest.py + data/（清单见 docs/开发指南.md）
│
├── rule-packs/                  # 官方规则包源码与产物
│   ├── hancast-bilibili / hancast-douyin / hancast-template
│   ├── index.json               # 官方索引；线上地址是 main 分支 raw 路径 `https://raw.githubusercontent.com/pingod/HanCast/main/rule-packs/index.json`（`params.py:21`）
│   └── dist/                    # 打包 zip（CI 随 draft Release 发布）
├── scripts/                     # 壳层脚本：sync-version / check-version / sync-sidecar / check-resources（均为 .cjs）
├── tools/                       # 47 个顶层脚本 + 1 个目录型验收包（browser-channel-livecheck/），索引与计数口径见 tools/README.md
├── mpv/                         # 本地放置的 MPV（`.gitignore:86-87` 只留 portable_config/）
│   └── portable_config/mpv.conf # 入库：弹幕/OSD 脚本、着色器、字体
├── docs/                        # 设计文档，**入口是 docs/README.md**
│   ├── README.md                # 按问题找文档 + 阅读路径 + 取代关系 + 文档维护约定
│   ├── 架构总览.md / 开发指南.md / 故障排查.md / 构建与发布.md
│   ├── HanCast-Frontend-Spec.md / HanCast-Frontend-API.md
│   ├── 屏幕投屏.md / 投屏确认.md / 分享解析规则.md / 解析插件开发指南.md
│   ├── 反爬实现取证与浏览器通道.md
│   ├── HanCast-Backend-Spec.md  # ⚠️ 2.0 期历史稿，已被取代（横幅说明）
│   ├── Macast-Frontend-*.md     # ⚠️ 历史稿，被 HanCast-Frontend-*.md 取代
│   └── images/ examples/ 原型图.png
└── .claude/                     # Claude Code 配置
    └── memory/                  # 项目记忆与进度追踪
```

> 历史说明：早期清单里的 `Macast-main/`、`Macast-plugins-main/`、`_bmad/`、`_bmad-output/`
> 已不在仓库中（1.x 源码只作上游参考，且 `.gitignore:82` 明确忽略 `Macast-main/`；BMad 产物已清理）；
> `hancast_sidecar/types/` 实际目录名是 `models/`；
> 仓库根 `ffmpeg/`（曾放待捆绑的静态 ffmpeg，构建脚本同步到 `src-tauri/ffmpeg/`）连同
> `ffmpeg/README.txt` 已于 **v2.3.11 删除**——FFmpeg 不再随包，改为本机检测 + 缺失按需下载。

## 核心架构

### 进程通信架构

```
┌─────────────────┐     Tauri invoke()     ┌─────────────────┐
│  Vue 3 Frontend  │ ←────────────────────→ │  Rust Backend    │
│  (WebView)       │                        │  (Tauri Core)    │
└─────────────────┘                        └────────┬────────┘
                                                    │ stdin/stdout JSON（行分隔）
                                                    ▼
                                           ┌─────────────────┐
                                           │  Python Sidecar  │  (Nuitka standalone)
                                           └────────┬────────┘
        ┌──────────┬──────────┬──────────┬──────────┼───────────┬───────────┐
        ▼          ▼          ▼          ▼          ▼           ▼           ▼
   ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌─────────┐ ┌─────────┐
   │ SSDP   │ │ DLNA   │ │ DLNA   │ │ Media  │ │  MPV   │ │Screencast│ │  Rules  │
   │Service │ │Protocol│ │ Server │ │ Server │ │Renderer│ │FFmpeg+HLS│ │ QuickJS │
   └────────┘ └────────┘ └────────┘ └────────┘ └────────┘ └─────────┘ └─────────┘
                                          │
                                     Device Guard（入站投屏确认，security/guard.py）
```

### Sidecar JSON 协议

**请求** (Rust → Python stdin):
```json
{"id": <u64>, "cmd": "<command_name>", "params": {<args>}}
```

**响应** (Python stdout → Rust，成功时不带 `error`，失败时不带 `data`)：
```json
{"id": <u64>, "success": true, "data": <any>}
{"id": <u64>, "success": false, "error": "<message>"}
```

**事件** (Python stdout → Rust，无 id 字段):
```json
{"event": "<event_name>", "data": <object>}
```

**三条铁律**（改 Sidecar 前必读，细节见 `hancast-backend/README.md`）：

1. **stdout 只有一个写者**：所有出通道的字节必须经 `utils/protocol_io.py`（`emit_response` / `emit_event`）。
   `print()`、未被重定向的第三方库、异常回溯都会把裸行混进协议流，Rust 侧直接解析失败。
2. **每个请求必须有一个响应**：包括异常路径。漏发就让 Rust 等满 `COMMAND_TIMEOUT`（60s，`sidecar.rs:18`）。
3. **UTF-8 stdio 在 logger 初始化之前强制**（`main.py:21-28`）：Windows 打包版默认 cp936/ascii，
   中文日志行会先被编码器吞掉，v2.2.18 就是这么撞到 `'ascii' codec can't encode characters` 的。

事件总账：**Sidecar 主动推送 6 条**（`ready`、`device_found`、`device_lost`、`cast_state_changed`、
`cast_confirm_request`、`screen_cast_state_changed`），Rust 侧另发 2 条（`update-available`、`autostart-changed`）；
前端合计监听 8 条。`ready` 两侧都没有订阅者（只作握手痕迹），`cast_error` 是前端遗留的死监听（`src/stores/cast.ts:637`）。
完整清单与触发时机见 docs/HanCast-Frontend-API.md。

### Tauri 命令列表（节选，完整清单以 `src-tauri/src/lib.rs` 的 `generate_handler!`（lib.rs:1373-1440，**66 条**）为准）

Sidecar 侧的注册表是 **59 条**（`commands.py:110-197`）——差的那几条由 Rust 自己实现（窗口、剪贴板、开机自启、
Store 版判定、日志导出等），不下发 Python；`exit` 由 Sidecar 主循环拦下（`main.py:126-128`）。
Rust 命令名与 Sidecar `cmd` 名不一定同名（例如 Rust 的 `plugin_list` 下发的是 Sidecar 的规则包命令），
逐条对应关系见 docs/HanCast-Frontend-API.md。

| 类别 | 命令 | 说明 |
|------|------|------|
| 窗口 | `minimize_window` / `close_window` | 最小化 / 隐藏到托盘 |
| 设备 | `get_devices` / `refresh_devices` | 获取 / 刷新设备列表 |
| 设备 | `set_default_device` / `rename_device` / `remove_device` / `hide_device` | 设备管理 |
| 设备 | `add_device` / `get_manual_devices` / `list_network_interfaces` | 手动添加设备、网卡枚举 |
| 媒体 | `parse_media_file` / `parse_media_url` | 解析本地文件 / URL |
| 媒体 | `resolve_share` | 解析分享口令/短链/网页 → 可播直链（站点规则来自已装规则包） |
| 媒体 | `get_resolve_result` | 轮询异步解析任务结果 |
| 媒体 | `failure_list` / `failure_delete` / `failure_clear` | 解析失败台账（`media/failures.py`） |
| 规则包 | `plugin_list` / `rule_inventory` | 已装包与规则清单 |
| 规则包 | `plugin_import` / `plugin_remove` / `plugin_pull` / `plugin_check_updates` | 导入、移除、按索引拉取、检查更新 |
| 规则包 | `plugin_set_rule_enabled` / `plugin_test` | 单条规则启停、沙箱内试跑 |
| 媒体 | `read_clipboard` | Rust 侧读系统剪贴板（WKWebView 无 clipboard-read 权限） |
| 媒体 | `write_clipboard` | Rust 侧写系统剪贴板（Web API reject 不受同步 try/catch 保护） |
| 投屏 | `start_cast` / `stop_cast` / `pause_cast` / `resume_cast` / `seek_cast` | 播控 |
| 投屏 | `get_cast_state` / `get_cast_url` / `set_volume` / `set_mute` | 状态与音量 |
| 屏幕投屏 | `list_screen_sources` / `start_screen_cast` / `stop_screen_cast` / `screen_cast_status` | 抓取源列表、开关会话、状态轮询 |
| 屏幕投屏 | `set_ffmpeg_path` | 手动指定 FFmpeg 路径（优先级高于本机检测） |
| 运行依赖 | `ffmpeg_status` / `ffmpeg_download_start` / `ffmpeg_download_status` / `open_ffmpeg_dir` | FFmpeg 检测报告、按需下载（后台任务 + 轮询）、打开本程序依赖目录 |
| 投屏确认 | `respond_cast_confirm` | 前端回答未知设备的入站投屏请求 |
| 投屏确认 | `get_guard_devices` / `remove_guard_device` / `set_guard_policy` | 信任 / 黑名单管理 |
| 投屏确认 | `get_guard_settings` / `save_guard_settings` | 确认开关与超时档位 |
| 设置 | `get_settings` / `save_settings` | 应用设置 |
| 后台参数 | `get_params` / `save_params` | `PARAM_SPECS` 32 项（`params.py:41-265`） |
| 日志 | `get_logs` / `export_logs` / `set_log_level` | 面板取数、导出、级别 |
| 播放环境 | `check_mpv` / `set_mpv_path` | 捆绑 MPV 探测与自定义路径 |
| 更新与自启 | `check_update` / `ignore_update_version` / `is_store_version` / `get_autostart` / `set_autostart` | 自更新、Store 版判定、开机自启 |

### Python 后端服务

| 服务 | 端口 | 说明 |
|------|------|------|
| DLNA Server | 默认 8080，占用则回退随机 | 描述 XML、SCPDXML、入站 SOAP 处理。端口由 `commands.py:86-90` 传入，冲突回退见 `protocol/server.py:339-346`；**真实端口回灌给 SSDP**（`commands.py:211` `self.ssdp._port = self.dlna_server.get_port()`），否则设备抓到 8080 却取不到描述文件 |
| SSDP Service | 1900 | UDP 多播，设备发现（NOTIFY + M-SEARCH）；组播被系统阻断时回退子网广播（`ssdp.py:32,416,556-557`），NOTIFY 周期 30s 对齐 `CACHE-CONTROL: max-age=66`（`ssdp.py:6`）。**发现不扫描端口**，且描述必须声明 `AVTransport` 服务才登记（`ssdp.py:1074`、`ssdp.py:1169-1175`，v2.3.13），路由器/NAS 不进列表，判据 `tools/verify-ssdp-cast-gate.py` |
| Media Server | 0 = 自动选择 | 本地文件 HTTP 服务（Range + 边下边播代理），`config.media_port` 默认 0（`utils/config.py:112-114`、`commands.py:221`） |
| Screencast HTTP | 默认 8765 | 观看页 + HLS 供流（`screencast/httpd.py:32,84`），观看链接带每会话新生成的短随机段 `/r/<session_id>` |
| MPV Renderer | IPC | 通过 named pipe (Windows) / unix socket (Linux/macOS) 控制 MPV |
| FFmpeg | 子进程 | 屏幕抓取切片成 HLS，由 `screencast/ffmpeg_manager.py` 拼装命令；**不随包**，可执行文件由 `utils/ffmpeg_deps.py` 在运行时定位（手动路径 → 本机 PATH/常见安装目录 → 本程序下载的 `<配置目录>/bin/`） |

**这些 HTTP 服务一律绑 `0.0.0.0`，不是回环**——电视要取描述文件与媒体流、浏览器观看页要拉 HLS，
绑 `127.0.0.1` 等于功能全废。代价与缓解见「安全约束」。

## 原始代码复用情况

| 文件 | 用途 | 状态 |
|------|------|------|
| `macast/ssdp.py` | SSDP 设备发现 | ✅ 已移植到 `hancast_sidecar/ssdp.py` |
| `macast/protocol.py` | DLNA 协议 | ✅ 已移植到 `hancast_sidecar/protocol/dlna.py` |
| `macast/renderer.py` | 渲染器基类 | ✅ 已移植到 `hancast_sidecar/renderer/base.py` |
| `macast_renderer/mpv.py` | MPV 播放器 | ✅ 已移植到 `hancast_sidecar/renderer/mpv.py` |
| `macast/server.py` | HTTP 服务 | ✅ 重写为 `protocol/server.py` + `media/server.py`（使用 http.server） |
| `macast/gui.py` | 系统托盘 | ❌ 废弃，由 Tauri 前端替代 |
| `macast/utils.py` | 工具函数 | ✅ 部分复用到 `utils/config.py` + `utils/logger.py` |
| `macast/xml/*.xml` | UPnP 描述 | ✅ 直接复用到 `hancast_sidecar/xml/` |

## 依赖

### 前端 (package.json)
```json
{
  "@tauri-apps/api": "^2.0.0",
  "@tauri-apps/plugin-dialog": "^2.7.1",
  "@tauri-apps/plugin-shell": "^2.0.0",
  "pinia": "^2.1.7",
  "vue": "^3.4.0",
  "vue-i18n": "^9.9.0",
  "vue-router": "^4.2.5"
}
```

devDependencies 里 `@tauri-apps/cli` 是 `npm run tauri …` 的入口，`vue-tsc` 挂在 `npm run build` 前做类型检查。

### Rust (Cargo.toml)
```toml
tauri = { version = "2", features = ["protocol-asset", "tray-icon"] }
tauri-plugin-shell = "2"
tauri-plugin-clipboard-manager = "2"
tauri-plugin-dialog = "2"
tauri-plugin-autostart = "2"
tauri-plugin-single-instance = "2"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tokio = { version = "1", features = ["sync", "macros", "rt-multi-thread", "time"] }
```

### Python (pyproject.toml)
```toml
requires-python = ">=3.11"
dependencies = [
    "requests>=2.28.0",
    "lxml>=4.9.0",
    "netifaces>=0.11.0",
    "aiohttp>=3.9.0",                          # 观看页直接供 HLS（aiortc 已移除）
    "pyaudiowpatch>=0.2.12.9; sys_platform == 'win32'",  # Windows WASAPI 环回采系统声音
    "quickjs>=1.19.4",                         # 规则包 JS 沙箱
    "cssselect>=1.6.0",                        # 声明式 JSON 规则的 CSS 选择器
    "curl_cffi>=0.16.3",                       # TLS/JA3 指纹伪造，装不上自动退回 requests
]

[project.optional-dependencies]
dev = ["pytest>=7.0", "pytest-cov>=4.0", "nuitka>=2.0", "ordered-set>=4.0"]
```

## 开发工作流

### 启动开发环境
```bash
# 启动 Tauri 开发模式（自动启动 Vite 前端 + Python Sidecar）
npm run tauri dev      # 走 devDependencies 里的 @tauri-apps/cli；cargo tauri 需要全局 CLI，仓库不用

# 仅前端开发（浏览器模式，使用 mock 数据）
npm run dev

# 仅 Python 后端
cd hancast-backend && uv run python -m hancast_sidecar.main
```

### 生产构建
```bash
# 构建 Tauri 应用（beforeBuildCommand 会先跑 scripts/sync-version.cjs 同步版本号）
npm run tauri build

# 构建 Python Sidecar（Nuitka，含 uv sync --all-extras）
npm run build:sidecar
```

### 完整构建顺序（本地与 CI 一致，缺一步就 glob 报错）

`tauri build` 的 `beforeBuildCommand` 现在**只做版本同步**（`node scripts/sync-version.cjs`），
其余动作 CI 拆成了独立步骤，本地照抄同样顺序：

```bash
npm run tauri build            # 或 npx tauri build --bundles <targets>
# 等价于按序执行：
#   1. node scripts/sync-version.cjs   （beforeBuildCommand，版本 SSOT 刷进各清单）
#   2. npm run build                   （vue-tsc 类型检查 + vite build → dist/）
#   3. node scripts/sync-sidecar.cjs   （把 hancast_sidecar/ 源码同步进 src-tauri/）
#   4. npm run build:sidecar           （Nuitka standalone → src-tauri/hancast-sidecar/，
#                                       再由 flatten_dependencies() 把 *.dll/*.pyd/*.so 提升到 src-tauri/ 根）
#   5. node scripts/check-resources.cjs（Preflight：逐个 glob 验命中）
#   6. tauri bundle
```

步骤 3/4 的产物就是 `bundle.resources` 的 glob 目标，所以 Preflight 必须排在它们之后、
`tauri build` 之前——放太晚只能等 `build.rs` 报含糊的 glob 错误（`.github/workflows/build.yml:238-252`）。

**构建前还需要手工准备**（CI 里由 workflow 步骤自动下载）：

1. `src-tauri/mpv/` 里要有 mpv 可执行文件；仓库根 `mpv/` 被 gitignore，只有 `portable_config/` 入库，
   CI 会先把已提交的 `mpv/portable_config/` 补进 `src-tauri/mpv/portable_config/`（`build.yml:156-160`）。
2. macOS 只有 `mpv.app/` 是有效载荷：`mpv/*` 这种非递归 glob 能看见目录却不会递归，
   历史上因此打出过不含 mpv 的 app，`scripts/check-resources.cjs:12-21` 专门记录了这一坑。

> **FFmpeg 不在准备清单里**：v2.3.11 起不再捆绑，`bundle.resources` 的 `ffmpeg/*` 与 CI 的
> 「Download & unpack FFmpeg」步骤（曾从 `eugeneware/ffmpeg-static` 的 `b6.1.1` 取静态单文件）
> 一并删除，安装包因此小掉一大截。运行时由 `utils/ffmpeg_deps.py` 检测本机 FFmpeg，
> 缺失时前端弹「需要下载运行依赖」，用户自装或让程序下载到 `<配置目录>/bin/`。

### CI 构建（GitHub Actions）

`.github/workflows/build.yml`，单个 `build` job + 3 条矩阵：

| 矩阵项 | Runner | 产物 | mpv 来源 |
|--------|--------|------|----------|
| `macos-arm64` | `macos-14` | `app`, `dmg` | 官方 release `macos-15-arm` zip |
| `windows-x64` | `windows-latest` | `nsis` | 官方 release `x86_64-pc-windows-msvc` zip |
| `linux-x64` | `ubuntu-latest` | `deb`, `rpm`, `appimage` | `apt-get install mpv`（官方 release 无 Linux 产物）|

- **触发**：推送 `v*` tag，或手动 `workflow_dispatch`
- **mpv 版本**：`env.MPV_TAG` 固定为 `v0.41.0`（不用 GitHub API 查 tag，
  避免共享 runner 上 `api.github.com` 的 60 次/小时匿名限流）
- **签名**：不做。macOS 用户需绕过 Gatekeeper，Windows 可能触发 SmartScreen
- **Preflight**：`node scripts/check-resources.cjs` 在 `tauri build` 之前跑，
  glob 无命中就带上 `src-tauri/` 实际清单失败，避免 build.rs 的含糊报错。
  为此 CI 把 `beforeBuildCommand` 拆成了独立步骤（frontend → sync-sidecar →
  Nuitka sidecar → Preflight → tauri bundle），所以 base 配置的
  `beforeBuildCommand` 已简化为只做版本同步。
- **Release**：仅在 `build` 成功、**ref 是 tag**、且（tag 推送 或 `make_release=true`）
  时创建 draft release。`make_release` 输入默认 **false**——在 `main` 上手动
  dispatch 只构建、不发布（曾用 `refs/heads/main` 当 tag 发 release，被 API 400 拒掉）。

### 构建产物路径

| 类型 | 路径 | 说明 |
|------|------|------|
| 绿色版（便携版） | `src-tauri/target/release/dist-portable/` | 解压即运行，无需安装 |
| 安装包 | `src-tauri/target/release/bundle/nsis/` | 安装后目录结构与绿色版一致 |

**重要**：绿色版和安装后的运行目录结构完全一致，只是分发方式不同。绿色版是打包后的完整运行目录，不是临时构建目录。

### 运行目录结构

两种方式的运行目录结构相同：

```
运行目录/
├── HanCast.exe                         # 主程序（Tauri + Rust）
├── hancast-sidecar.exe                 # Python 后端（DLNA/SSDP/媒体解析）
│                                        # Nuitka standalone 单体二进制，依赖已并入
├── *.dll / *.pyd / *.so                # Python 扩展模块（平台相关，见平台配置）
│
├── hancast_sidecar/xml/                # UPnP 描述文件（DLNA 必需）
│   ├── Description.xml
│   ├── AVTransport.xml
│   ├── ConnectionManager.xml
│   ├── RenderingControl.xml
│   └── SinkProtocolInfo.csv
│
└── mpv/                                # MPV 播放器（捆绑）
    ├── mpv.exe                          # Windows；macOS 为 mpv.app/（整体保留）
    │                                    # Linux 为 mpv
    └── portable_config/
        ├── mpv.conf                     # 仓库中提交的配置
        ├── scripts/                     # 弹幕、OSD 等插件
        ├── shaders/                     # 视频着色器（Anime4K）
        ├── fonts/                       # 字体文件
        └── watch_later/                 # 播放进度记忆
```

> ⚠️ `certifi/`、`lxml/`、`charset_normalizer/` 这三个独立目录已**不在**运行目录中——
> 那是早期 PyInstaller onedir 的遗留布局，本项目改用 Nuitka 后依赖已静态并入 sidecar 可执行文件。
> 详见下一节。

**核心组件**：
- `HanCast.exe`：Tauri 前端 + Rust 桥接层
- `hancast-sidecar-*.exe`：Python 后端（DLNA/SSDP/媒体解析）
- `mpv/`：捆绑的 MPV 播放器
- `hancast_sidecar/xml/`：UPnP 协议描述文件

### bundle.resources 与 Nuitka 依赖提升

> ⚠️ 本节说明为何 `bundle.resources` 被拆成 base + 三个平台配置文件。
> `.json` 不支持注释，所以文档只能写在这里。

**硬性规则：每个 glob 都必须在【当前平台】匹配到至少一个文件。**

tauri-build 的 `build.rs` 对任何含 `*` 的 glob，只要匹配结果为空就直接失败：

```
glob pattern <pattern> path not found or didn't match any files.
```

而且它在第一个失败的 glob 处就中断，后续 glob 根本不会被校验，排查成本很高。

**平台相关清单放不进 base 文件**，因此拆成：

| 文件 | 内容 |
|------|------|
| `src-tauri/tauri.conf.json` | 仅平台无关子集：`mpv/*`、`mpv/portable_config/**/*`、`hancast_sidecar/**/*` |
| `src-tauri/tauri.windows.conf.json` | `mpv/*`、`mpv/portable_config/**/*`、`*.dll`、`*.pyd`、`hancast_sidecar/**/*` |
| `src-tauri/tauri.macos.conf.json` | `mpv/mpv.app/**/*`、`mpv/portable_config/**/*`、`*.so`、`hancast_sidecar/**/*` |
| `src-tauri/tauri.linux.conf.json` | `mpv/*`、`mpv/portable_config/**/*`、`*.so`、`hancast_sidecar/**/*` |

⚠️ **Tauri 的平台配置合并用 `json_patch::merge`（RFC 7386 JSON Merge Patch），数组是整体替换而非逐元素合并。**
所以每个平台文件必须写出**完整**的 `bundle.resources` 数组，不能只写增量。

**glob 不递归，且命中的目录会被静默丢弃**（上游 crate tauri-utils 里的 resources.rs，不在本仓；行为已在 `scripts/check-resources.cjs:12-21` 的注释里复现说明）：

- `glob::glob("mpv/*")` 只列 `mpv/` 的**直接子项**，即使 `require_literal_separator=false`，
  `*` 也不跨目录层级；要递归必须显式写 `**`。
- 迭代器遇到**目录**命中会直接跳过，不递归进去（`if entry.is_dir() { skip }`）。
- 所以 `GlobPathNotFound` 的触发条件是**一个文件都没匹配到**——只匹配到目录同样失败。

⚠️ macOS 就栽在这上面：`mpv/*` 看得见 `mpv.app/` 目录，但目录被丢弃，
app 打出来没有 mpv，反倒把 44 MB 的 `mpv.tar.gz` 打进 bundle。
修法：macos 配置显式写 `mpv/mpv.app/**/*`，CI 解完 tarball 后删掉它。
`check-resources.cjs` 已按这套语义复刻：只数**文件**命中，并对「glob 命中了目录、
但目录下一个文件都不会进 bundle」的 orphaned 情况打 `warn`。

**动态库 glob 必须是顶层**（`*.so` 而非 `**/*.so`）：
`**/*.so` 会把 `src-tauri/target/` 下的整个 Rust 构建产物打进安装包。

**依赖提升流程**：`build_sidecar.py` 的 `flatten_dependencies()` 把 Nuitka dist
（`src-tauri/hancast-sidecar/`）里除可执行文件外的依赖，合并复制到 `src-tauri/` 根部，
这样它们才能被上面的 `*.dll` / `*.pyd` / `*.so` glob 捕获。
可执行文件本身留在 `hancast-sidecar/` 里供 `externalBin` 使用，不做重复。

**历史遗留 → 已被 v2.3.7 产物证伪**：`certifi/**/*`、`lxml/**/*`、`charset_normalizer/**/*`
曾被认为是 **PyInstaller onedir** 的残留、「Nuitka 产出目录里没有这些独立目录」，
但 v2.3.7 的 `Contents/Resources/` 里实打实躺着这三个目录——Nuitka standalone 会把
**含数据文件或扩展模块**的包保留成顶层目录（`certifi/cacert.pem`、`lxml/etree.so`、
`charset_normalizer` 的 mypyc `.so`），它们只能靠目录级 glob 进 bundle。

这条错误认知是有代价的：#71 加 curl_cffi 时按「Nuitka 全静态并入」推断，只往
`build_sidecar.py` 加了 `--include-package=curl_cffi`，没动 `bundle.resources`，
于是 Nuitka 产出的 `curl_cffi/`（里面是 `_wrapper.abi3.so`，顶层 `*.so` 抓不到）
被整目录丢弃 → 打包版 `import curl_cffi` 失败 → 按设计降级回 requests，
表现是「解析一切正常，只是 JA3 还是 Python 的」，不翻 bundle 永远发现不了。

**规则**：`build_sidecar.py` 每加一个 `--include-package`，就要检查该包在 Nuitka 产出里
是否以**目录**形态存在；是目录就**三个平台 conf 各加一条 `<pkg>/**/*`**（数组整体替换，不增量合并）。
`check-resources.cjs` 现在会自动抓这件事：它从 `build_sidecar.py` 读 `--include-package=`
清单，产出里有该目录而 `bundle.resources` 没列，就直接失败并点名，不再依赖人记得来改文档。

**新增 glob 后必须验证**（CI 与本地都跑）：

```bash
node scripts/check-resources.cjs        # 本地
```

它按平台复刻 Tauri 的合并语义和 glob 语义（目录命中不算数，orphaned 目录打 warn），
逐个 glob 打印**文件**命中数量；任一 glob 一个文件都没匹配到就打印
`src-tauri/` 实际内容并以非 0 退出，把 build.rs 那种含糊报错变成精确诊断。

⚠️ **仓库根的 `mpv/portable_config/` 与 `src-tauri/mpv/` 是两套目录**：
`mpv/*` 被 gitignore、仅 `mpv/portable_config/` 入库，位置在**仓库根**；
而 `bundle.resources` 的路径都相对 `src-tauri/`。所以 CI 的三个平台步骤都必须
把 `mpv/portable_config/` 拷进 `src-tauri/mpv/portable_config/`，否则
`mpv/portable_config/**/*` 无命中、构建直接失败。

⚠️ **它必须跑在 sidecar 构建之后、`tauri build` 之前。**
`hancast_sidecar/**/*` 和 `*.so` / `*.dll` / `*.pyd` 都是 Nuitka 的产物，
在 `beforeBuildCommand` 执行前根本不存在——这也是 CI 把 `beforeBuildCommand`
拆成独立步骤的原因（见上一节）。

### 版本管理

项目采用 **单一真相源（SSOT）** 策略，以 `package.json` 的 `version` 字段为唯一版本源头。

**架构**：
```
package.json (SSOT)
      │
      ▼
scripts/sync-version.cjs
      │
      ├──▶ src-tauri/tauri.conf.json
      ├──▶ src-tauri/Cargo.toml
      ├──▶ src-tauri/Cargo.lock (本地包 HanCast 那条)
      ├──▶ hancast-backend/pyproject.toml
      ├──▶ hancast-backend/hancast_sidecar/utils/config.py
      ├──▶ src/api/commands.ts (mock 数据)
      └──▶ src/stores/settings.ts (默认值)
```

**用法**：
```bash
# 查看当前版本（只打印 package.json 的 version）
npm run version

# 设置新版本并刷进 7 处契约点
npm run version:set -- 2.3.7

# 无参数同步（prebuild 钩子 + tauri 的 beforeBuildCommand 走的就是这条）
npm run version:sync
```

无参数模式带 **SSOT 滞后闸门**（`sync-version.cjs:123-153`）：任何契约点比 `package.json` 新就 exit 1——
v2.3.3 发布漏 bump `package.json`，钩子把旧版本忠实地刷回全部契约点，产物自报 2.3.2，
Windows 安装包文件名都露馅。闸门让它在毫秒级炸掉，而不是安静发出一版错误号的产品。

**注意**：不要手动修改各文件中的版本号，统一通过 `npm run version:set` 命令管理。

### 测试
```bash
# Python 测试（全量）
cd hancast-backend && uv run pytest

# 只跑相关用例（本项目常规做法：不整套刷）
cd hancast-backend && uv run pytest tests/test_params.py tests/test_jsplugins.py

# Rust：本地没有完整工具链时先打桩，再 cargo check
bash tools/rust-check.sh

# 前端没有测试脚本（package.json 里没有 test）；类型检查走 npm run build 的 vue-tsc
node tools/verify-params-ui.mjs        # 参数表与 tag 源码逐键比对（UI 侧验收）
python3 tools/audit-doc-refs.py        # 文档引用的 文件:行号 是否还成立
```

`tools/` 下 47 个顶层脚本（+ `browser-channel-livecheck/` 目录型验收包）按用途分类见 tools/README.md（计数口径同那里）；端到端验收脚本的判据写在
docs/构建与发布.md §6 与各专题文档，不要凭脚本退出码 0 就当作功能可用。

## 窗口配置

窗口尺寸有两处来源，改一处不够：

| 来源 | 值 | 作用 |
|------|----|------|
| `src-tauri/tauri.conf.json` `app.windows[0]` | 434×634，min 346×472，max 800×900，`decorations:false`、`transparent:true`、`visible:false`、`dragDropEnabled:true` | 首启的声明尺寸 |
| `src-tauri/src/lib.rs:32-36` `WindowConfig::default()` | 434×634；最小尺寸在 Rust 侧硬编码 `MIN_WIDTH=346 / MIN_HEIGHT=472`（`lib.rs:1092-1093`，恢复与记录时各 clamp 一次，`lib.rs:1098,1118-1119`） | Rust 恢复/写入 `window_config.json` 时的默认值与下限 |

用户拖过窗口后，真实尺寸记在 AppData 的 `window_config.json`（`lib.rs:1068`），启动时优先读它，
写入带 **500ms 防抖**（`lib.rs:84,90`）。所以「改了 tauri.conf.json 但界面尺寸没变」通常是本机
`window_config.json` 在覆盖，删掉该文件再看。

**注意**：窗口特效（vibrancy/blur）尚未实现，当前仅使用透明窗口。

## 跨平台 WebView 兼容性

| 平台 | 引擎 | 注意事项 |
|------|------|----------|
| Windows 10/11 | WebView2 (Chromium) | 支持大部分现代 CSS/JS |
| macOS | WebKit (Safari) | 限制较多，避免最新特性 |
| Linux | WebKitGTK | 功能最少，需严格测试 |

**CSS 兼容规则**:
- 使用标准 Flexbox/Grid 布局
- `-webkit-app-region` 用于标题栏拖拽（Tauri 必需）
- 字体回退链：`-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif`

## 安全约束

1. **文件拖拽**：验证文件类型和大小，防止恶意文件
2. **URL 解析**：防止 SSRF 攻击，限制可访问范围
3. **本地服务**：**必须绑定 `0.0.0.0`**（`protocol/server.py:332`、`media/server.py:600`、
   `screencast/httpd.py:65`）——电视和浏览器观看页要能拉流与取描述文件，绑 `127.0.0.1` 等于功能全废。
   代价是本机媒体/观看页端口对局域网可见，因此：端口随机分配、观看链接里带每次会话新生成的
   短随机段（`watch_url = http://<ip>:<port>/r/<session_id>`，`screencast/manager.py:157,328`）、
   投屏请求过 Device Guard 确认（`security/guard.py`），并且**只在本机使用时开启屏幕投屏**。
   > 这条曾被写成「仅监听 127.0.0.1，不暴露到网络」，是与实现相反的历史描述，已按代码更正。
4. **Sidecar 通信**：stdin/stdout 管道，不暴露网络端口
5. **规则包沙箱**：站点脚本跑在 QuickJS 里（`media/rules/jsruntime.py`），只有 `host.*` 受限接口，
   拿不到 Python 运行时；导入侧限额见 `media/rules/jsplugins.py:44-47`
   （ZIP ≤2MB、条目 ≤128、解压后 ≤8MB、单文件 ≤2MB），官方索引产物按 sha256 校验后才落盘。

## 代码风格

### Python
- 遵循 PEP 8
- 使用 type hints
- 日志使用 `logging` 模块
- 使用 `dataclass` 定义数据类型

### Rust
- 遵循 `rustfmt` 默认配置
- 使用 `thiserror` 处理错误
- 异步使用 `tokio`

### 前端
- Vue 3 Composition API + TypeScript
- CSS 变量实现主题切换
- 组件化开发
- Pinia 状态管理

## 文档维护约定（硬要求）

文档入口是 **docs/README.md**（按问题找文档、阅读路径、取代关系、完整约定都在那里）。
本文件只放每次改动都会撞到的六条：

1. **改代码的同一轮改文档**，不是「有空再补」。命令、事件、后台参数（`params.py` 的 `PARAM_SPECS`）、
   端口、落盘文件、平台限制都是对外契约，文档滞后等于契约过期。发布轮还要补 `CHANGELOG.md` 一节
   （用户可感知的变化 + 发布提交号）。
2. **每条关键论断挂 `文件:行号`**，行号会漂，所以大改后跑体检：
   ```bash
   python3 tools/audit-doc-refs.py                      # 全仓 md
   python3 tools/audit-doc-refs.py docs/故障排查.md --show   # 单份，附带被引原文
   ```
   「文件找不到」要自己分辨是已退役的历史文件名（写成「曾经/已于 vX 删除」即可）还是真写错；
   运行时才生成的落盘文件（`hancast_setting.json`、`window_config.json` 等）脚本已放行。
3. **每份文档头部留一行「最后校对：YYYY-MM-DD，对应版本 vX.Y.Z」**，改了内容就改这行。
4. **数字必须写口径**。例：`#[tauri::command]` 66 条（`lib.rs:1373-1440`）/ Sidecar 注册 59 条
   （`commands.py:110-197`）/ 前端 `commands.ts` 导出 65 个 / 事件 8 条（Python 6 + Rust 2）。
   不写口径，下次改动看起来就像文档坏了。
5. **别静默删证据**。被推翻的结论保留「历史一句」（为什么曾经这么做、哪一版改的），
   就像本文件「安全约束」第 3 条那样标注更正来源。
6. **主动维护**：动到上述契约面的改动，收尾前必须自己回答「哪几份文档现在说谎了」并当场改掉，
   不等他提醒。新增文档时在 docs/README.md §1 登记一行，并在 docs/架构总览.md 的专文索引里加链接。

## 记忆系统

项目进度和关键决策记录在 `.claude/memory/` 目录：
- `progress.md` — 开发进度追踪
- `decisions.md` — 架构决策记录
- `issues.md` — 已知问题与解决方案
- `context.md` — 项目上下文快照

**重要**：每次会话开始时读取 memory 文件，结束时更新进度。

## 知识图谱（Understand Anything）

项目使用 [understand-anything](https://github.com/understand-anything/understand-anything) 插件生成代码知识图谱，用于可视化项目架构。

> 这是**可选的本地开发工具**，产物 `.understand-anything/` 已被 gitignore（`.gitignore:89`），
> 仓库里不保证存在；下表的文件名、34KB、12 步导览都是某次本机生成的结果，不是项目契约。
> 项目架构的权威描述是 [docs/架构总览.md](docs/架构总览.md)，不要以图谱为准。

### 生成知识图谱

```bash
# 首次全量分析（生成 .understand-anything/knowledge-graph.json）
/understand-anything:understand

# 指定语言（中文输出）
/understand-anything:understand --language zh

# 增量更新（仅分析 git 变更文件）
/understand-anything:understand

# 强制全量重建
/understand-anything:understand --full

# LLM 审查模式（更详细但更慢）
/understand-anything:understand --review

# 禁用自动更新
/understand-anything:understand --no-auto-update
```

### 启动仪表板

```bash
# 启动交互式知识图谱仪表板（浏览器可视化）
/understand-anything:understand-dashboard
```

仪表板启动后访问 `http://127.0.0.1:5173`，可交互式浏览：
- 项目架构层级（前端 UI → Tauri 桥接 → Python 后端）
- 文件间依赖关系（imports、calls、configures 等）
- 12 步导览（从 README 到 MPV 渲染器）
- 函数/类级别节点

### 知识图谱文件

| 文件 | 说明 |
|------|------|
| `.understand-anything/knowledge-graph.json` | 知识图谱主文件（34KB） |
| `.understand-anything/meta.json` | 分析元数据（时间、commit hash） |
| `.understand-anything/.understandignore` | 排除规则（类似 .gitignore） |

### 知识图谱 Schema

- **节点类型**：`file`、`function`、`class`、`config`、`document`、`service`、`pipeline`、`table`、`endpoint`、`schema`、`resource`、`module`、`concept`
- **边类型**：`imports`、`exports`、`contains`、`inherits`、`implements`、`calls`、`depends_on`、`tested_by`、`configures`、`documents` 等 26 种
- **层级**：前端 UI、Tauri 桥接层、Python 后端、项目配置

### 注意事项

- 图谱在 git commit 变更后自动增量更新（需 `--auto-update`）
- 后台代理需要可用的 Claude 模型（如 `sonnet`），`opus-4-8` 在部分账号不可用
- 图谱语言默认跟随会话语言（中文会话生成中文描述）

## 图标生成

替换 `src-tauri/icons/icon.png`（建议使用 1024x1024 px 透明背景 PNG）后，在项目根目录下运行：

```bash
npm run tauri icon ./src-tauri/icons/icon.png