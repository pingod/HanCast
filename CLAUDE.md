# CLAUDE.md — HanCast 项目指南

## 项目概述

**HanCast** 是基于 [xfangfang/Macast](https://github.com/xfangfang/Macast) 的二次开发项目，跨平台投屏应用，支持将媒体文件/链接投屏到局域网设备，同时可作为 DLNA 接收端。

- **项目名**: HanCast
- **作者**: lanzeweie@foxmail.com
- **版本**: 2.0.1
- **性质**: 二次开发（非原项目官方更新）
- **原始代码**: `Macast-main/`（原作者 xfangfang，最后更新 2022-01）
- **目标架构**: Tauri 2.0 前端 + Rust 桥接 + Python Sidecar
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
| 后端服务 | Python Sidecar | >= 3.11 |
| 协议支持 | DLNA/UPnP/SSDP | — |
| 媒体播放 | MPV（外部依赖） | — |

## 项目结构

```
G:\Code\Macast-Han\
├── CLAUDE.md                    # 本文件 — 项目核心约束
├── README.md
├── package.json                 # 前端依赖
├── vite.config.ts               # Vite 配置
├── tsconfig.json                # TypeScript 配置
├── index.html                   # Vite 入口 HTML
├── env.d.ts                     # Vue 类型声明
│
├── src/                         # Vue 3 前端源码
│   ├── main.ts                  # 应用入口（Pinia/Router/i18n 初始化）
│   ├── App.vue                  # 根组件（主题检测、设置加载）
│   ├── api/
│   │   └── commands.ts          # Tauri invoke 封装 + 浏览器 mock
│   ├── components/
│   │   ├── TitleBar.vue         # 自定义标题栏（最小化、关闭、设置）
│   │   ├── MediaInput.vue       # 拖拽 + URL 输入 + 剪贴板粘贴
│   │   ├── CastControl.vue      # 投屏状态栏（播放/暂停/停止指示）
│   │   ├── DeviceList.vue       # 设备列表（含刷新）
│   │   ├── DeviceCard.vue       # 单个设备卡片（投屏、重命名、移除、设默认）
│   │   └── Footer.vue           # "无设备" 提示底栏
│   ├── views/
│   │   ├── HomeView.vue         # 主视图
│   │   └── SettingsView.vue     # 设置页（语言、DLNA 名称、端口、关于）
│   ├── stores/                  # Pinia 状态管理
│   │   ├── cast.ts              # 投屏状态
│   │   ├── device.ts            # 设备列表（30s 自动刷新）
│   │   ├── media.ts             # 媒体输入状态机
│   │   └── settings.ts          # 应用设置
│   ├── types/                   # TypeScript 类型定义
│   │   ├── cast.ts
│   │   ├── device.ts
│   │   ├── media.ts
│   │   ├── settings.ts
│   │   └── index.ts
│   ├── locales/                 # 国际化资源
│   │   ├── zh-CN.json
│   │   └── en-US.json
│   ├── router/
│   │   └── index.ts             # Hash 路由：/ (Home), /settings (Settings)
│   └── styles/
│       ├── variables.css        # CSS 变量（亮色/暗色主题）
│       └── global.css           # 全局样式
│
├── src-tauri/                   # Tauri Rust 后端
│   ├── Cargo.toml               # Rust 依赖
│   ├── tauri.conf.json          # Tauri 配置（base，仅平台无关的 resources）
│   ├── tauri.windows.conf.json  # Windows 覆盖（数组整体替换，见文档）
│   ├── tauri.macos.conf.json    # macOS 覆盖
│   ├── tauri.linux.conf.json    # Linux 覆盖
│   ├── build.rs                 # tauri_build::build()
│   ├── icons/                   # 应用图标
│   └── src/
│       ├── main.rs              # 入口，调用 hancast_lib::run()
│       ├── lib.rs               # Tauri 应用设置 + 20 个命令定义
│       └── sidecar.rs           # Python Sidecar 管理器（stdin/stdout JSON）
│
├── hancast-backend/              # Python Sidecar 后端
│   ├── pyproject.toml           # Python 项目配置
│   ├── requirements.txt
│   ├── uv.lock
│   ├── hancast_sidecar/
│   │   ├── main.py              # Sidecar 入口（stdin/stdout JSON 循环）
│   │   ├── commands.py          # CommandHandler — 命令路由
│   │   ├── ssdp.py              # SSDP 设备发现（~540 行）
│   │   ├── protocol/
│   │   │   ├── dlna.py          # DLNA 协议实现（~840 行）
│   │   │   └── server.py        # DLNA HTTP 服务（描述、SOAP、SUBSCRIBE）
│   │   ├── renderer/
│   │   │   ├── base.py          # 渲染器基类
│   │   │   └── mpv.py           # MPV 渲染器（IPC: named pipe/unix socket）
│   │   ├── media/
│   │   │   ├── parser.py        # 媒体文件/URL 解析器
│   │   │   └── server.py        # 本地文件 HTTP 服务（支持 Range）
│   │   ├── types/
│   │   │   ├── cast.py          # CastState dataclass
│   │   │   ├── device.py        # Device dataclass
│   │   │   └── media.py         # MediaInfo dataclass
│   │   ├── utils/
│   │   │   ├── config.py        # 配置管理（AppData JSON 文件）
│   │   │   └── logger.py        # 日志设置
│   │   └── xml/                 # UPnP 描述 XML
│   │       ├── Description.xml
│   │       ├── AVTransport.xml
│   │       ├── ConnectionManager.xml
│   │       ├── RenderingControl.xml
│   │       ├── SinkProtocolInfo.csv
│   │       └── setting.html
│   ├── scripts/
│   │   ├── build_sidecar.py     # Nuitka 构建脚本 + flatten_dependencies()
│   │   └── run_sidecar.py       # 独立 Sidecar 运行器
│   └── tests/
│       ├── test_commands.py
│       └── test_imports.py
│
├── Macast-main/                 # 原始 1.x Python 源码（参考用）
├── Macast-plugins-main/         # 原始 Macast 插件（参考用）
├── mpv/                         # 捆绑的 MPV（gitignore，只提交 portable_config/）
│   └── portable_config/mpv.conf
├── docs/                        # 设计文档
│   ├── HanCast-Backend-Spec.md
│   ├── Macast-Frontend-API.md
│   ├── Macast-Frontend-Spec.md
│   └── 原型图.png
├── _bmad/                       # BMad 方法论配置
├── _bmad-output/                # BMad 规划/实现产物
└── .claude/                     # Claude Code 配置
    └── memory/                  # 项目记忆与进度追踪
```

## 核心架构

### 进程通信架构

```
┌─────────────────┐     Tauri invoke()     ┌─────────────────┐
│  Vue 3 Frontend  │ ←────────────────────→ │  Rust Backend    │
│  (WebView)       │                        │  (Tauri Core)    │
└─────────────────┘                        └────────┬────────┘
                                                    │ stdin/stdout JSON
                                                    ▼
                                           ┌─────────────────┐
                                           │  Python Sidecar  │
                                           └────────┬────────┘
                                                    │
                              ┌──────────┬──────────┼──────────┬──────────┐
                              ▼          ▼          ▼          ▼          ▼
                         ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
                         │ SSDP   │ │ DLNA   │ │ DLNA   │ │ Media  │ │ MPV    │
                         │ Service│ │Protocol│ │ Server │ │ Server │ │Renderer│
                         └────────┘ └────────┘ └────────┘ └────────┘ └────────┘
```

### Sidecar JSON 协议

**请求** (Rust → Python stdin):
```json
{"id": <u64>, "cmd": "<command_name>", "params": {<args>}}
```

**响应** (Python stdout → Rust):
```json
{"id": <u64>, "success": true, "data": <any>, "error": null}
```

**事件** (Python stdout → Rust，无 id 字段):
```json
{"event": "<event_name>", "data": <object>}
```

### Tauri 命令列表（20 个）

| 类别 | 命令 | 说明 |
|------|------|------|
| 窗口 | `minimize_window` | 最小化窗口 |
| 窗口 | `close_window` | 隐藏到托盘 |
| 设备 | `get_devices` | 获取设备列表 |
| 设备 | `refresh_devices` | 刷新设备列表 |
| 设备 | `set_default_device` | 设置默认设备 |
| 设备 | `rename_device` | 重命名设备 |
| 设备 | `remove_device` | 移除设备 |
| 媒体 | `parse_media_file` | 解析媒体文件 |
| 媒体 | `parse_media_url` | 解析媒体 URL |
| 投屏 | `start_cast` | 开始投屏 |
| 投屏 | `stop_cast` | 停止投屏 |
| 投屏 | `pause_cast` | 暂停投屏 |
| 投屏 | `resume_cast` | 恢复投屏 |
| 投屏 | `seek_cast` | 跳转进度 |
| 投屏 | `get_cast_state` | 获取投屏状态 |
| 投屏 | `get_cast_url` | 获取当前投屏 URL 和进度 |
| 投屏 | `set_volume` | 设置音量 |
| 投屏 | `set_mute` | 设置静音 |
| 设置 | `get_settings` | 获取设置 |
| 设置 | `save_settings` | 保存设置 |

### Python 后端服务

| 服务 | 端口 | 说明 |
|------|------|------|
| DLNA Server | 8080 | 描述 XML、SCPDXML、SOAP 请求处理 |
| SSDP Service | 1900 | UDP 多播，设备发现（NOTIFY + M-SEARCH） |
| Media Server | 动态 | 本地文件 HTTP 服务（支持 Range 请求） |
| MPV Renderer | IPC | 通过 named pipe (Windows) / unix socket (Linux/macOS) 控制 MPV |

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
  "@tauri-apps/plugin-shell": "^2.0.0",
  "pinia": "^2.1.7",
  "vue": "^3.4.0",
  "vue-i18n": "^9.9.0",
  "vue-router": "^4.2.5"
}
```

### Rust (Cargo.toml)
```toml
tauri = { version = "2", features = ["tray-icon"] }
tauri-plugin-shell = "2"
tauri-plugin-clipboard-manager = "2"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tokio = { version = "1", features = ["sync", "macros", "rt-multi-thread"] }
```

### Python (pyproject.toml)
```toml
requires-python = ">=3.11"
dependencies = [
    "requests>=2.28.0",
    "lxml>=4.9.0",
    "netifaces>=0.11.0",
]
```

## 开发工作流

### 启动开发环境
```bash
# 启动 Tauri 开发模式（自动启动 Vite 前端 + Python Sidecar）
cargo tauri dev

# 仅前端开发（浏览器模式，使用 mock 数据）
npm run dev

# 仅 Python 后端
cd hancast-backend && uv run python -m hancast_sidecar.main
```

### 生产构建
```bash
# 构建 Tauri 应用（prebuild 自动同步版本号）
cargo tauri build

# 构建 Python Sidecar（Nuitka）
cd hancast-backend && python scripts/build_sidecar.py
```

**构建前必须做的事**（`tauri build` 不会替你做）：

1. `src-tauri/mpv/` 里要有 mpv 可执行文件 —— CI 里由 workflow 步骤准备，
   本地需手动放置（`mpv/` 被 gitignore，只有 `portable_config/` 入库）。
2. 运行 `node scripts/check-resources.cjs` 确认 `bundle.resources` 每个 glob 都有命中。

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
  glob 无命中就带上 `src-tauri/` 实际清单失败，避免 build.rs 的含糊报错
- **Release**：仅在 `build` 成功且（tag 推送 或 `make_release=true`）时创建 draft release

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
    ├── mpv.exe                          # macOS 为 mpv.app/Contents/MacOS/mpv
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
| `src-tauri/tauri.windows.conf.json` | 追加 `*.dll`、`*.pyd` |
| `src-tauri/tauri.macos.conf.json` | 追加 `*.so` |
| `src-tauri/tauri.linux.conf.json` | 追加 `*.so` |

⚠️ **Tauri 的平台配置合并用 `json_patch::merge`（RFC 7386 JSON Merge Patch），数组是整体替换而非逐元素合并。**
所以每个平台文件必须写出**完整**的 `bundle.resources` 数组，不能只写增量。

**动态库 glob 必须是顶层**（`*.so` 而非 `**/*.so`）：
`**/*.so` 会把 `src-tauri/target/` 下的整个 Rust 构建产物打进安装包。

**依赖提升流程**：`build_sidecar.py` 的 `flatten_dependencies()` 把 Nuitka dist
（`src-tauri/hancast-sidecar/`）里除可执行文件外的依赖，合并复制到 `src-tauri/` 根部，
这样它们才能被上面的 `*.dll` / `*.pyd` / `*.so` glob 捕获。
可执行文件本身留在 `hancast-sidecar/` 里供 `externalBin` 使用，不做重复。

**历史遗留**：`certifi/**/*`、`lxml/**/*`、`charset_normalizer/**/*` 曾长期躺在
`bundle.resources` 里，但那是 **PyInstaller onedir** 的目录布局，本项目早已迁移到 Nuitka。
Nuitka standalone 会把依赖静态并入可执行文件（37–57MB 的单体二进制即证据），
产出目录里没有这些独立目录，所以它们已在全部配置中移除。**不要凭猜测重新加回去。**

**新增 glob 后必须验证**（CI 与本地都跑）：

```bash
node scripts/check-resources.cjs        # 本地
```

它按平台复刻 Tauri 的合并语义，逐个 glob 打印命中数量；任一 glob 为 0 就打印
`src-tauri/` 实际内容并以非 0 退出，把 build.rs 那种含糊报错变成精确诊断。
CI 里它作为 `Preflight` 步骤跑在 `tauri build` 之前。

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
      ├──▶ hancast-backend/pyproject.toml
      ├──▶ hancast-backend/hancast_sidecar/utils/config.py
      ├──▶ src/api/commands.ts (mock 数据)
      └──▶ src/stores/settings.ts (默认值)
```

**用法**：
```bash
# 查看当前版本
npm run version:sync

# 设置新版本并同步所有文件
npm run version:set -- 2.0.0

# 构建时自动同步（prebuild 钩子，已内置在 npm run build 中）
```

**注意**：不要手动修改各文件中的版本号，统一通过 `npm run version:set` 命令管理。

### 测试
```bash
# Python 测试
cd hancast-backend && uv run pytest

# 前端测试（待实现）
npm run test
```

## 窗口配置

```json
{
  "width": 420,
  "height": 600,
  "minWidth": 360,
  "minHeight": 480,
  "maxWidth": 800,
  "maxHeight": 900,
  "decorations": false,
  "transparent": true
}
```

**注意**: 窗口特效（vibrancy/blur）尚未实现，当前仅使用透明窗口。

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
3. **本地服务**：仅监听 `127.0.0.1`，不暴露到网络
4. **Sidecar 通信**：stdin/stdout 管道，不暴露网络端口

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

## 记忆系统

项目进度和关键决策记录在 `.claude/memory/` 目录：
- `progress.md` — 开发进度追踪
- `decisions.md` — 架构决策记录
- `issues.md` — 已知问题与解决方案
- `context.md` — 项目上下文快照

**重要**：每次会话开始时读取 memory 文件，结束时更新进度。

## 知识图谱（Understand Anything）

项目使用 [understand-anything](https://github.com/understand-anything/understand-anything) 插件生成代码知识图谱，用于可视化项目架构。

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