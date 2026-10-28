# HanCast — 前端 API 参考

> 回答一个问题：前端 `invoke()` 出去的每一条命令，究竟由壳层（Rust/Tauri）就地执行、还是转发给 Sidecar（Python 后台进程）；它的入参、返回结构、可能错误，以及 Sidecar 主动推给前端的事件分别是什么。
>
> 最后校对：2026-10-28，对应版本 v2.4.1（`package.json:4`。本轮（2026-10-28）：投 TCL 电视「投不上」——渲染端只认 URL 后缀，交付直链加后缀门改走代理补后缀（docs/故障排查.md §5.5），顺带修 models/session.py 丢 mime_type；media/server.py、media/share_resolver.py、models/session.py 行号按实测重校。 （`package.json:4`。引用行号与硬数字由 `tools/check-doc-freshness.py` 机械核对；上一轮：`protocol/dlna.py:77-84` 因入站 SOAP 容错下移到 :111-118；上一轮：§6.7 补 macOS 通知日志落进同一份 hancast.log 的例外）

---

## 0. 统计

| 项 | 数量 | 出处 |
| --- | --- | --- |
| **命令总数** | **66** | `src-tauri/src/lib.rs:1373-1440`（`generate_handler!` 注册表，逐条数过） |
| ├─ 壳层本地执行 | 10 | `minimize_window` / `close_window` / `read_clipboard` / `write_clipboard` / `export_logs` / `is_store_version` / `get_autostart` / `set_autostart` / `open_ffmpeg_dir`（v2.3.11，先下发 `ffmpeg_status` 取目录再调系统文件管理器，`lib.rs:832-859`）/ `notify_os`（v2.3.22，macOS 走 `UNUserNotificationCenter`，其余平台回 `Err` 让前端退回通知插件） |
| └─ 转发 Sidecar | 57 | 其余全部走 `sidecar.send_command(...)` |
| **事件总数** | **8** | 见 §3.1 表（Python 侧 6 + 壳层侧 2） |
| 前端封装函数 | 65 | `src/api/commands.ts` 中 `export async function` 计数 |
| ├─ 无独立封装的命令 | 3 | `get_resolve_result`（内联在 `resolveShare` / `pluginTest` 轮询里）、`check_mpv`、`set_mpv_path`（`src/` 下无任何调用点） |
| └─ 一对多封装 | 1 | `plugin_import` → `pluginImportFile` / `pluginImportData` / `pluginImportUrl`（`commands.ts:997`） |
| Sidecar 命令注册表条目 | 62 | `hancast-backend/hancast_sidecar/commands.py:131-224` |
| 后台参数（`params.py`）条目 | 32 | `hancast-backend/hancast_sidecar/params.py:41-265` |

**66 与 59 为什么对不上**：57 条转发里，`parse_media_file` 与 `parse_media_url` 在壳层是两条命令、到 Sidecar 归并成一条 `parse_media`（`lib.rs:229-268`），57 − 1 = 56 个不同的 Sidecar 命令名；Sidecar 另有三条**没有对应壳层命令**——`unhide_device`、`get_hidden_devices`（`commands.py:142`，前端调不到）与 `browser_result`（`commands.py:192`，只由壳层的浏览器通道反向 RPC 下发）。56 + 3 = 59。此外 `exit` 不走注册表，是主循环直接拦下的特殊命令（`main.py:126-128`）。

---

## 1. 三层调用链

### 1.1 全景

```
Vue 组件 / Pinia store
        │  import { xxx } from '@/api/commands'
        ▼
src/api/commands.ts        前端封装层：initInvoke() → invoke<T>(cmd, args)
        │  Tauri IPC（JSON 序列化，参数名 camelCase）
        ▼
src-tauri/src/lib.rs       壳层：#[tauri::command] 函数
        │                   ├─ 9 条就地执行（剪贴板 / 窗口 / 托盘自启 / 日志导出 / 商店判定 / FFmpeg 目录）
        │                   └─ 57 条 → sidecar.send_command("cmd", json!({...}))
        ▼
src-tauri/src/sidecar.rs   SidecarManager：stdin 写一行 JSON，oneshot 等一行 JSON
        │  子进程 stdin / stdout（行分隔 JSON，UTF-8）
        ▼
hancast_sidecar/main.py    for line in sys.stdin → handler.execute(cmd, params)
        │
        ▼
hancast_sidecar/commands.py  CommandHandler._commands 注册表（62 条）
```

Sidecar 的启动方式：开发态 `uv run python -m hancast_sidecar.main`（`sidecar.rs:155-172`），打包态用 Nuitka 产物 `hancast-sidecar*`（`sidecar.rs:173-213`，macOS 在 `Contents/MacOS/` 下找，Windows/Linux 在 `resource_dir` 下找，`sidecar.rs:44-129`）。找不到二进制时 `setup()` 直接中止启动（`lib.rs:1162-1166`）。

### 1.2 三种信封

协议定义在 `sidecar.rs:131-136` 的注释里，实现在 `utils/protocol_io.py`：

```jsonc
// 请求（Rust → Python，一行一条）
{ "id": 7, "cmd": "start_cast", "params": { "device_id": "uuid:...", "media_uri": "http://..." } }

// 响应（Python → Rust）：data 与 error 互斥，只出现其一
{ "id": 7, "success": true,  "data": { ... } }
{ "id": 7, "success": false, "error": "Device not found: uuid:..." }

// 事件（Python → Rust，无 id）
{ "event": "device_found", "data": { ... } }
```

- 写入端只有一把锁：`protocol_io._write_lock`（`protocol_io.py:25`），`_write_line` 显式写 `\n` 并立即 `flush`（`protocol_io.py:28-36`）。任何绕过它直接 `print()` 的代码都可能把两行 JSON 交错成一行，而壳层解析失败只是 `eprintln!` 一句然后**丢掉整行**（`sidecar.rs:364-366`），结果是那条命令挂到 60 秒超时。
- `emit_response` 只在成功时带 `data`、失败时带 `error`（`protocol_io.py:39-53`）；`emit_event` 的 `data` 缺省为 `{}`（`protocol_io.py:56-59`）。
- 读取端按字节累积、以 `\n` 切帧（`sidecar.rs:278-297`），单行超过 8MB 直接丢弃（`sidecar.rs:301-307`）。Sidecar 的 stderr 不进协议，只被转印成 `[Sidecar stderr] …`（`sidecar.rs:309-312`）。

### 1.3 超时与错误冒泡

| 层 | 上限 | 出处 |
| --- | --- | --- |
| 壳层等单条命令 | **60s** | `sidecar.rs:18`（`COMMAND_TIMEOUT`） |
| Sidecar 单次分享解析预算 | 25s | `media/share_resolver.py:34`（`_TOTAL_BUDGET`） |
| 前端 `resolveShare` / `pluginTest` 轮询 | 45s，每 400ms 一次 | `commands.ts:717-727`、`commands.ts:949-958` |
| 并发解析任务 | 最多 3 个 pending | `commands.py:629-630` |
| 解析任务结果保留 | 完成后 10 分钟过期清理 | `commands.py:622-626` |
| 更新检查 HTTP | GitHub 1s / Gitee 3s | `commands.py:872-881` |
| 退出时等 Sidecar | Windows 3000ms，其余 1000ms，超时 force kill | `sidecar.rs:447-498` |

冒泡路径（每一步都是**字符串**，不是结构化错误）：

```
Python 异常
  → main.py:134-140 捕获：JSONDecodeError → "Invalid JSON: ..."
                        KeyError → "Missing field: 'xxx'"
                        其他 Exception → str(e)
  → emit_response(id, False, error=<字符串>)
  → sidecar.rs:423-437：success != true → Err(error 原文)
  → lib.rs 的 #[tauri::command] 签名一律 Result<T, String>
  → 前端 invoke() reject，reason 是**裸字符串**（不是 Error 对象）
```

所以前端拿到的 rejection 必须先归一化。仓库里的通用写法：

```ts
addError.value = err instanceof Error ? err.message : String(err)
```

（`src/stores/device.ts:107`、`src/stores/logs.ts:55`；`src/components/PluginSettings.vue:39` 用的是另一种写法 `String(err?.message ?? err)`）

壳层自己也会在写入阶段失败，这类错误与 Sidecar 无关：

- `Sidecar process not running`（`sidecar.rs:390-392`，child 已被 take，通常是进程已退出）
- `Failed to write to sidecar: <e>`（`sidecar.rs:394-396`，stdin 管道断了）

超时与进程退出是两条独立的错误消息：

- `Sidecar did not respond to '<cmd>' within 60s`（`sidecar.rs:416-419`）
- `Sidecar process exited unexpectedly`（`sidecar.rs:409-413`，oneshot 发送端被 drop；进程退出时 `reader_loop` 还会 drain 掉所有 pending 并逐个送这条错误，`sidecar.rs:333-341`）

还有一类只出现在 Python 侧的错误：命令名不在注册表里 → `Unknown command: <cmd>`（`commands.py:251-255`）。正常路径不会触发，改协议时容易踩。

### 1.4 事件如何从 Python 走到前端

```
protocol_io.emit_event("device_found", {...})          # commands.py:281
  → stdout 一行 {"event":"device_found","data":{...}}
  → SidecarManager::handle_stdout_line                 # sidecar.rs:345-368
      if val["id"] 存在 → 派发给 pending oneshot（是响应）
      elif val["event"] 存在 → app.emit(event_name, data)   # sidecar.rs:358-361
  → Tauri 事件总线
  → 前端 const { listen } = await import('@tauri-apps/api/event')
     await listen<Device>('device_found', e => ...)     # stores/device.ts:122
```

关键点：**事件名不被壳层白名单过滤**，Python 里 `emit_event` 写什么名字，前端就能 `listen` 到什么名字。壳层自己也会发事件（`autostart-changed`、`update-available`），走的是同一个 `app.emit`，前端无法从名字上区分来源。

壳层还有一个**自监听**：`listen("autostart-changed", ...)`（`lib.rs:1273`），用于托盘菜单勾选态同步 —— 也就是这条事件同时有 Rust 和前端两个订阅者。

### 1.5 参数命名

Tauri 2 会把 JS 侧的 camelCase 自动映射到 Rust 的 snake_case 形参。约定：

- 前端传 `{ deviceId }`，Rust 形参写 `device_id: Option<String>`（例：`lib.rs:466-475`）。
- 但 **Rust → Sidecar 的 params 用 snake_case 原样传**：`json!({"device_id": device_id})`。Sidecar 里读的是 `params["device_id"]`（`commands.py:480`）。两套命名在壳层交接，改一处要跟着改另一处。
- `Option<T>` 形参在前端可以整个省略（`invoke('stop_cast', {})`，`commands.ts:1012`），Sidecar 用 `params.get(...)` 读，缺省即 `None`。

### 1.6 浏览器开发模式的 mock

`initInvoke()` 只有在 `window.__TAURI_INTERNALS__?.metadata` 存在时才用真 `invoke`，否则回落到 `mockInvoke`（`commands.ts:22-37`）。`mockInvoke` 是内存态假实现（`commands.ts:264-624`），**只覆盖部分命令**，其余走 `default: return {} as T`（`commands.ts:622`）—— 纯浏览器里任何未 mock 的命令都会「成功」返回空对象，不会报错。排查「浏览器里好好的、Tauri 里坏了」时先看这里。

---

## 2. 命令全表

约定：
- 「执行层」= **壳层**（Rust 就地完成，Sidecar 不参与）/ **Sidecar**（转发 Python）。
- 「可能的错误」列里标 **通用** 的，指 §1.3 那三条：`Sidecar did not respond to '<cmd>' within 60s`、`Sidecar process exited unexpectedly`、`Missing field: '<key>'`。壳层命令不写「通用」。
- 「行号」列：壳层定义 / Sidecar 处理函数。

### 2.1 设备发现与管理（8 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `get_devices` | `getDevices()` `commands.ts:784` | 无 | `Device[]`；已剔除隐藏设备（`commands.py:328-332`） | Sidecar | 通用 |
| `refresh_devices` | `refreshDevices()` `commands.ts:789` | 无 | `Device[]`；先 `ssdp.scan()` 再取（`commands.py:334-336`） | Sidecar | 通用 |
| `set_default_device` | `setDefaultDevice(id)` `commands.ts:639` | `id: string` | `void`（壳层丢弃 data，`lib.rs:179-180`） | Sidecar | 通用 |
| `rename_device` | `renameDevice(id, name)` `commands.ts:644` | `id: string`、`name: string` | `void` | Sidecar | 通用 |
| `remove_device` | `removeDevice(id)` `commands.ts:649` | `id: string` | `void`；同时从持久化的手动设备列表摘掉同 IP 条目（`commands.py:348-358`） | Sidecar | 通用。⚠️ 该封装在 `src/` 下**无调用点** |
| `hide_device` | `hideDevice(id)` `commands.ts:654` | `id: string` | `void`；SSDP 仍能发现，只是不再推给前端（`commands.py:376-380`） | Sidecar | 通用 |
| `add_device` | `addDevice(address)` `commands.ts:660` | `address: string`，接受 `192.168.1.68` / `ip:port` / 完整描述 URL | `Device`（`commands.py:371`） | Sidecar | `请输入设备 IP 或描述地址`（`commands.py:367`、`ssdp.py:1225`）；`无法识别的地址：<target>`（`ssdp.py:1268`）；`该地址不能投屏：设备描述里必须声明 AVTransport 服务（电视/盒子/带投屏的音箱才有，路由器与 NAS 没有）`（`ssdp.py:1231-1235`，v2.3.13 起手动添加与自动发现走同一条准入）；`设备 <名称> 已被隐藏，请先取消隐藏`（`ssdp.py:1240`）；描述抓取失败原因（`HTTP <code>` / `响应中没有 UPNP 设备描述` / 异常原文，`ssdp.py:1289-1300`） |
| `get_manual_devices` | `getManualDevices()` `commands.ts:821` | 无 | `string[]`（`config.manual_devices`，`commands.py:373-374`） | Sidecar | 通用 |

壳层定义：`lib.rs:164-226`。Sidecar 处理函数：`commands.py:328-392`。

Sidecar 侧还有两条**前端调不到**的命令：`unhide_device`（`commands.py:141`）、`get_hidden_devices`（`commands.py:142`）—— 壳层没有对应的 `#[tauri::command]`。隐藏是单向的，界面上没有「取消隐藏」入口。

`Device` 的键名坑见 §6.1。

### 2.2 投屏控制（9 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `start_cast` | `startCast(deviceId, mediaUri, mimeType?)` `commands.ts:989` | `deviceId: string`、`mediaUri: string`、`mimeType?: string` | `void`；成功后 Sidecar 立即补发一条 `cast_state_changed`（`commands.py:501`、`:515`） | Sidecar | `Device not found: <device_id>`（`commands.py:443`）；`session.play()` 抛出的控制点异常原文（`ControlUrlUnavailable` / `RendererUnresponsive`，`protocol/dlna.py:111-118`）。本地文件路径会被 Sidecar 换成媒体服务器 HTTP 地址（`commands.py:447-449`） |
| `stop_cast` | `stopCast(deviceId?)` `commands.ts:1010` | `deviceId?: string`（省略 = 停全部会话） | `{ ok: true, stopped: string[], control_failed: string[], reasons: Record<string,string> }`（`commands.py:504-505`） | Sidecar | 命令本身**不抛错**（幂等，先摘会话再发 SOAP Stop，`commands.py:476-505`）；「电视没收到 Stop」体现在 `control_failed` / `reasons` 里，不是异常 |
| `pause_cast` | `pauseCast(deviceId?)` `commands.ts:1015` | `deviceId?: string` | `void` | Sidecar | 通用；无会话时回落到主渲染器 `self.protocol.pause()`（`commands.py:520-526`） |
| `resume_cast` | `resumeCast(deviceId?)` `commands.ts:1020` | `deviceId?: string` | `void` | Sidecar | 通用；同上回落逻辑（`commands.py:528-534`） |
| `seek_cast` | `seekCast(position, deviceId?)` `commands.ts:1025` | `position: string`（`HH:MM:SS`）、`deviceId?: string` | `void` | Sidecar | 通用（`Missing field: 'position'`） |
| `get_cast_state` | `getCastState(deviceId?)` `commands.ts:1030` | `deviceId?: string` | **两种形状**：带 `deviceId` → `CastState`；不带 → `{ sessions: Record<deviceId, CastState>, current: CastState }`（`commands.py:544-552`） | Sidecar | 通用 |
| `get_cast_url` | `getCastUrl(deviceId?)` `commands.ts:1035` | `deviceId?: string` | **两种形状**：带 `deviceId` → `{device_id, device_name, url, title, duration, position, status, cast_state}`（`session.py:137-152`，`status` 小写）；不带 → `{url, title, duration, position, status}`（`commands.py:564-570`，`status` 是 DLNA 大写 `PLAYING`/`STOPPED`） | Sidecar | 通用 |
| `set_volume` | `setVolume(volume, deviceId?)` `commands.ts:1040` | `volume: number`（u32）、`deviceId?: string` | `number`（壳层 `as_u64()` 兜底回入参值，`lib.rs:543-545`） | Sidecar | 通用 |
| `set_mute` | `setMute(muted, deviceId?)` `commands.ts:1045` | `muted: boolean`、`deviceId?: string` | `void` | Sidecar | 通用 |

壳层定义：`lib.rs:445-564`。Sidecar 处理函数：`commands.py:442-590`。

多设备语义：每个设备一个 `DeviceCastSession`（`models/session.py:14-26`），会话内 1 秒轮询一次进度（`session.py:223-307`）；连续 10 次轮询失败即视为掉线（`session.py:25`，`POLL_FAIL_LIMIT`）。前端 `stores/cast.ts` 另有自己的 1 秒轮询（`cast.ts:193-251`），两条轮询链路并存。

壳层自己也在**每秒调一次 `get_cast_url`**（不带 `deviceId`），只为刷新托盘菜单「复制当前投屏地址」项（`lib.rs:1264-1318`）。这条流量不经过前端封装。

### 2.3 媒体解析与粘贴（4 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `parse_media_file` | `parseMediaFile(filePath)` `commands.ts:672` | `filePath: string` | `MediaInfo`（`models/media.py:22-39`） | Sidecar | 通用；解析异常原文。壳层把它改写成 Sidecar 的 `parse_media` + `{"type":"file","path":...}`（`lib.rs:229-239`） |
| `parse_media_url` | `parseMediaUrl(url)` `commands.ts:677` | `url: string` | `MediaInfo` | Sidecar | 同上，改写成 `{"type":"url","url":...}`（`lib.rs:256-267`）。⚠️ 该封装在 `src/` 下**无调用点** |
| `resolve_share` | `resolveShare(text, quality?)` `commands.ts:706` | `text: string`、`quality?: string` | **异步任务**：`{ task_id: string, status: "pending" }`（`commands.py:648`） | Sidecar | `粘贴内容为空`（`commands.py:616`）；`已有 3 个解析任务进行中，请稍后再试`（`commands.py:630`）；通用 |
| `list_folder_media` | `listFolderMedia(folderPath)` `commands.ts:876` | `folderPath: string`（壳层 `folder_path`，`lib.rs:247`） | `{ folder: string, files: MediaInfo[] }`，按文件名**自然序**（`第2集` 在 `第10集` 前） | Sidecar | `文件夹路径为空`；`Not a folder: …`（`parser.py:37`）；`无法读取文件夹: …`。只列清单**不探时长**（避免上百集串行 ffprobe），只列当前层文件、跳过子目录/隐藏文件/`._` 资源叉/不支持扩展名（`parser.py:27-76`）。**新增于文件夹投屏**（播放队列初始顺序靠它） |
| `get_resolve_result` | 无独立封装，内联在 `resolveShare`（`commands.ts:872`）与 `pluginTest`（`commands.ts:1094`） | `taskId: string` | `{ status: "pending" \| "done" \| "error", result?: ShareResolveResult, error?: string, ts: number }`（`commands.py:650-657`） | Sidecar | `解析任务不存在或已过期`（`commands.py:655`）；通用 |

壳层定义：`lib.rs:228-265`（含 `list_folder_media` `lib.rs:247-256`）。Sidecar 处理函数：`commands.py:592-671`（`_list_folder_media` 在 `commands.py:662-671`，实现在 `media/parser.py:27-76`）。

`resolveShare` 的完整轮询契约（`commands.ts:706-727`）：先拿 `task_id`，每 400ms 调一次 `get_resolve_result`，45 秒兜底；`status === 'done'` 返回 `result`，`status === 'error'` 抛 `entry.error`，超时抛 `解析超时，请稍后重试`。若首次返回里**没有** `task_id`（浏览器 mock / 旧版实现），直接把首包当结果用（`commands.ts:712-715`）。

解析成功时的结果形状（`media/share_resolver.py:461`、`661`、`740`）：`{...MediaInfo, source_url, via}`，`via` 取值 `playlist` / `direct` / 命中的规则 id。失败时抛出的字符串带可操作提示，并写入解析失败台账（`share_resolver.py:377-381`）：

```
无法解析出可播放地址 — <host>: <原因>；…（可为该站点在 <rules_dir> 添加解析规则，或在设置页安装规则包）
```

### 2.4 屏幕投屏与 FFmpeg 依赖（9 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `list_screen_sources` | `listScreenSources()` `commands.ts:1348` | 无 | `{ screens: ScreenSourceItem[], windows: ScreenSourceItem[], audio_inputs: AudioDeviceItem[], ffmpeg: {...}, platform: "darwin"\|"win32"\|"linux" }`（`ffmpeg_manager.py:486-508`） | Sidecar | 通用 |
| `start_screen_cast` | `startScreenCast(params)` `commands.ts:1340` | `params: ScreenCastStartParams`，**整个对象直接作为 Sidecar 的 params**（`lib.rs:759-767`，前端 `{ params }` → Rust 形参 `params` 原样下传） | 成功：dlna → `{ok:true, target:"dlna", device_id, session_id, url}`；browser → `{ok:true, target:"browser", session_id, watch_url, width, height, fps}`（`manager.py:322-339`）。失败：`{ok:false, error}` | Sidecar | **校验类错误全部走 `{ok:false,error}` 而不是异常**（`manager.py:92-169`）：`已有投屏正在进行，请先停止`、`未知目标类型: <t>`、FFmpeg 不可用原文、`窗口源缺少窗口信息`、`未选择系统声音设备（…）`、`未选择麦克风设备`、`选中的<麦克风/系统声音>设备已不可用：<名>；请重新选择（…）`、`麦克风采到的全是静音…`、`缺少目标设备 (device_id)`、`设备不存在或已离线: <id>`、`HTTP 服务启动失败`、`会话注册失败`、`屏幕捕获失败`、`电视拒绝播放（…），可换较低分辨率/码率后重试`、`启动失败: <异常>` |
| `stop_screen_cast` | `stopScreenCast()` `commands.ts:1358` | 无 | `{ok:true, control_failed: string[], reasons: Record<string,string>}`；本来就没在跑 → `{ok:true, already_stopped:true}`（`manager.py:204-229`） | Sidecar | 幂等，不抛错。dlna 目标会复用 `_stop_cast` 的 `control_failed` 语义 |
| `screen_cast_status` | `getScreenCastStatus()` `commands.ts:1363` | 无 | `_state` 全量 + `viewers`（仅 browser 通道）+ `pull`（有 session_id 时）+ `ffmpeg`（`manager.py:231-241`） | Sidecar | 通用 |
| `set_ffmpeg_path` | `setFfmpegPath(path)` `commands.ts:1355` | `path: string` | `{ok:true, path, version}` 或 `{ok:false, error}`（`ffmpeg_manager.py:448-462`）；**只能设不能清**，落盘键 `screencast.ffmpeg_path` | Sidecar | `缺少路径`（`manager.py:243-247`）；`文件不存在: <abs>`、`不是有效的 FFmpeg 可执行文件`（`ffmpeg_manager.py:450-454`） |
| `ffmpeg_status` | `ffmpegStatus(refresh)` `commands.ts:1361` | `refresh?: bool`（用户点「重新检测」时传 `true`） | 检测报告：`ok / path / version / source / ffprobe_path / items[] / missing[] / optional_missing[] / managed_dir / platform / arch / install_commands[] / download / error`（`ffmpeg_deps.py:189-252`） | Sidecar | 缺依赖不算异常：`ok:false` + 中文 `error`，前端据此弹窗 |
| `ffmpeg_download_start` | `ffmpegDownloadStart()` `commands.ts:1380` | 无 | `{ok:true, task:{status, stage, ...}}`；已在下 → `already_running` | Sidecar | 起不来后台线程时 `{ok:false,error}`；压缩包 >512MB 直接拒（`ffmpeg_deps.py:448-469`） |
| `ffmpeg_download_status` | `ffmpegDownloadStatus()` `commands.ts:1385` | 无 | 后台任务快照（`status/stage/received_bytes/total_bytes/speed/path/error`），`Command_TIMEOUT=60s` 所以下载绝不占住命令循环（`ffmpeg_deps.py:470-474`） | Sidecar | 通用 |
| `open_ffmpeg_dir` | `openFfmpegDir()` `commands.ts:1391` | 无 | `{ok:true, path}`（回显目录，成败由调用方按「有没有窗口」判断） | **壳层就地**（`lib.rs:832-859`） | `Sidecar 未返回 FFmpeg 目录`、`目录创建失败: <e>`、`无法打开目录: <e>`；路径由 Sidecar 的 `managed_dir` 给出，**不接受前端传入**，否则等于开放「用资源管理器打开任意路径」；**只 spawn 不 wait**——`xdg-open` 活到文件管理器关闭为止，等它等于把按钮按死在命令循环里 |
| `notify_os` | `notify()` `commands.ts:1447`（内部先 `raiseIfHidden()` `:1401`） | `title: string`、`body: string` | `Ok(())` 或 `Err(msg)` | **壳层就地**（`lib.rs:924`；macOS 实现在 `src-tauri/src/macos_notify.rs`，命令里 `run_on_main_thread` 排到主线程再调 UN） | macOS 用 `UNUserNotificationCenter`——插件那条路走已废弃的 `NSUserNotificationCenter`，实测只入通知中心不弹横幅；**非 macOS 一律回 `Err("本平台由前端走通知插件")`**，前端 catch 后改调 `tauri-plugin-notification`。通知失败从不影响投屏本身 |

壳层定义：`lib.rs:750-859`。Sidecar 注册 `commands.py:186-193`、处理函数 `commands.py:1053-1090`，投屏实现在 `screencast/manager.py`、依赖检测与下载在 `utils/ffmpeg_deps.py`。

边界：屏幕投屏是**把本机画面推给别的设备**，与「接收投屏」（本机作为 DLNA 渲染端，靠 MPV）完全是两套代码。dlna 通道与 browser 通道共用一份 HLS 输出，但分片时长的后台参数不同（`screencast.dlna_segment_seconds` 默认 2s，`screencast.browser_segment_seconds` 默认 1s，`params.py:43-53`）。每次 `start` 都生成新的 `session_id`（`secrets.token_hex(3)`，`manager.py:157`）和新的 URL，旧链接立即失效。

`_state` 的完整键集见 `manager.py:61-72`：`running / target / source / device_id / session_id / watch_url / url / viewers / error / started_at`。

### 2.5 投屏确认（Device Guard）（6 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `respond_cast_confirm` | `respondCastConfirm(requestId, approved, policy)` `commands.ts:1111` | `requestId: string`、`approved: boolean`、`policy: string`（`"once"`/`"always"`/`"blacklist"`） | `boolean`（壳层 `as_bool().unwrap_or(false)`，`lib.rs:653-655`） | Sidecar | 通用（`Missing field: 'request_id'/'approved'`）；请求已超时/不存在时返回 `false` 而非报错（`guard.py:301-314`） |
| `get_guard_devices` | `getGuardDevices()` `commands.ts:1275` | 无 | `{ trusted: GuardDevice[], blacklisted: GuardDevice[] }`（`commands.py:971-976`） | Sidecar | 通用 |
| `remove_guard_device` | `removeGuardDevice(deviceKey)` `commands.ts:1125` | `deviceKey: string`，UDN 或 IP 都认（`guard.py:322-344`） | `boolean` | Sidecar | 通用 |
| `set_guard_policy` | `setGuardPolicy(deviceKey, policy)` `commands.ts:1130` | `deviceKey: string`、`policy: "trusted" \| "blacklisted"`（`commands.py:986`） | `boolean` | Sidecar | 通用；非法 policy 由 `guard.set_policy` 判定后返回 `false` |
| `get_guard_settings` | `getGuardSettings()` `commands.ts:1293` | 无 | `{ enabled: boolean, confirm_timeout: number }`（`commands.py:989-994`，单位秒，默认 15，`guard.py:77`） | Sidecar | 通用 |
| `save_guard_settings` | `saveGuardSettings({enabled?, confirm_timeout?})` `commands.ts:1143` | `settings: { enabled?: boolean, confirm_timeout?: number }` | `void` | Sidecar | 通用。**注意壳层不解包**：`.send_command("save_guard_settings", settings)`（`lib.rs:706`），Python 直接读 `params["enabled"]` / `params["confirm_timeout"]`（`commands.py:996-1002`）—— 与 `save_settings`（会包一层 `{"settings": …}`，`lib.rs:577`）不一致 |

壳层定义：`lib.rs:641-711`。Sidecar 处理函数：`commands.py:955-1002`，实现在 `security/guard.py`。

Device Guard 管的是**接收投屏**方向：外部设备往本机推流时，未知来源要先经用户确认。识别走三层降级：SSDP 缓存直达 → SSDP 单播探测 + TCP 端口探测并行（`guard.py:9-11`、`36-40`）。确认等待用 `threading.Event`，超时默认拒绝（`guard.py:274-299`）；`wait_for_confirm` 只对 `'always'` / `'blacklist'` 两种策略做持久化落库，`'once'` 不落。

### 2.6 规则包管理（11 条）

规则包 = JS 插件 zip；声明式规则 = 放在 `share_rules/` 目录下的 JSON 站点规则。这一组把两者都覆盖了。

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `plugin_list` | `pluginList()` `commands.ts:979` | 无 | `{ plugins: PluginInfo[] }`，前端解包成数组（`commands.py:660-674`） | Sidecar | 通用 |
| `rule_inventory` | `ruleInventory()` `commands.ts:1026` | 无 | `{ rules_dir: string, script_packs: PluginInfo[], declarative: DeclarativeRuleInfo[] }`（`share_resolver.py:385-397`） | Sidecar | 通用。声明式规则 JSON 写坏时不报错，坏条目带 `error` 字段照样列出（`declarative.py:262-269`） |
| `failure_list` | `failureList()` `commands.ts:1037` | 无 | `{ enabled, path, hosts: FailureHost[], total }`（`failures.py:167-190`） | Sidecar | 通用 |
| `failure_delete` | `failureDelete(host?, url?)` `commands.ts:888` | `host?: string`、`url?: string`（前端把空串转成 `null`，`commands.ts:891`） | `{ removed: number, enabled, path, hosts, total }`（`commands.py:686-690`） | Sidecar | 通用 |
| `failure_clear` | `failureClear()` `commands.ts:1050` | 无 | `{ removed: number, enabled, path, hosts, total }`（`commands.py:692-694`） | Sidecar | 通用 |
| `plugin_check_updates` | `pluginCheckUpdates(indexUrl?)` `commands.ts:910` | `indexUrl?: string`（空 → 走后台参数 `rules.index_url`） | `{ index_url, updates: PackUpdateEntry[], available: PackIndexEntry[], installed: number }`（`jsplugins.py:448-470`） | Sidecar（**经 `plugin_task_start` 轮询**，`lib.rs:346-398`） | `索引地址必须是 http(s)（当前：…）`、`索引拉取失败: 镜像 HTTP <code>；直连 <异常>`（`jsplugins.py:392-432`）；`规则包操作忙，请稍后重试`、`规则包操作超时，已请求取消`；通用 |
| `plugin_pull` | `pluginPull(name, indexUrl?)` `commands.ts:927` | `name: string`、`indexUrl?: string` | `{ plugins: PluginInfo[] }`（`commands.py:761-772`） | Sidecar（**经 `plugin_task_start` 轮询**） | `缺少包名`（`commands.py:770`）；`官方索引不可用，「<name>」也没声明更新地址：…`、`「<name>」既不在官方索引里，也没声明 update_url`（`jsplugins.py:468-492`）；`包校验失败（sha256 不匹配，可能是镜像过期）: <前12位>…`（`jsplugins.py:687`）；下载类错误见下 |
| `plugin_import` | `pluginImportFile(filePath)` `:797` / `pluginImportData(dataBase64)` `:803` / `pluginImportUrl(url)` `:809` | 三选一：`filePath` / `dataBase64` / `url`（壳层合成一个对象下传，`lib.rs:429-448`） | `{ plugins: PluginInfo[] }`（`commands.py:774-789`） | Sidecar（**经 `plugin_task_start` 轮询**） | `需要 path / data_base64 / url 之一`；`插件包超过 2MB 上限`、`不是合法的 zip 包: …`、`插件包文件数超过 128 上限`、`插件包解压后超过 8MB 上限`、`zip 内路径不合法: …`、`zip 内不允许的文件（只收 manifest.json 和 .js）: …`、`单文件超过 2MB: …`、`zip 内不允许软链接: …`、`插件包必须恰好含一个 manifest.json`、`manifest.json 不是合法 JSON: …`、`manifest scripts 条目必须是 .js 文件名: …`、`manifest scripts 引用了不存在的文件: …`（`jsplugins.py:580-641`）；`只支持 http(s) 下载地址`、`HTTP <code>`（`jsplugins.py:64-89`） |
| `plugin_remove` | `pluginRemove(name)` `commands.ts:848` | `name: string` | `{ plugins: PluginInfo[] }`（`commands.py:729-731`） | Sidecar | `插件不存在: <name>`（`jsplugins.py:465`）；通用 |
| `plugin_set_rule_enabled` | `pluginSetRuleEnabled(plugin, rule, enabled)` `commands.ts:854` | `plugin: string`、`rule: string`、`enabled: boolean` | `{ plugins: PluginInfo[] }`（`commands.py:733-736`） | Sidecar | 通用 |
| `plugin_test` | `pluginTest(plugin, text)` `commands.ts:939` | `plugin: string`、`text: string` | `{ task_id, status: "pending" }`，之后走 `get_resolve_result` 轮询（与 `resolveShare` 完全同一套，`commands.ts:949-958`） | Sidecar | `插件不存在: <name>`（`jsplugins.py:465`）；`粘贴内容为空`（`commands.py:743`）、`已有 3 个解析任务进行中，请稍后再试`（`commands.py:630`） |
| `plugin_task_start` | 无前端封装（壳层 `run_plugin_task` 内部调用，`lib.rs:346-398`） | `operation: "plugin_check_updates" \| "plugin_pull" \| "plugin_import"`、`params: object` | `{ task_id, status: "pending" }`（`tasks.py:165-190`） | Sidecar | `不支持的规则包任务: <op>`、`规则包操作忙，请稍后重试`（单执行零队列）、`规则包任务服务已关闭`（`tasks.py:146-150`） |
| `plugin_task_result` | 同上（轮询） | `task_id: string` | `{ status: "pending" \| "done" \| "error" \| "cancelled", result?, error? }`（`tasks.py:251-253`） | Sidecar | `规则包任务不存在或已过期`（终态保留 600s、上限 16 条）；通用 |
| `plugin_task_cancel` | 同上（壳层超时兜底） | `task_id: string` | 取消后的当前状态（`tasks.py:255-260`） | Sidecar | 提交已赢的任务取消不掉：返回 `pending`/`done` 而非 `cancelled`（`tasks.py:72-95`） |

壳层定义：`lib.rs:297-422`。Sidecar 处理函数：`commands.py:660-746`。

约定：**所有会改动插件仓库的命令（import / remove / set_rule_enabled / pull）都返回全量 `{plugins: [...]}`**，前端拿它直接刷新列表，不需要再调一次 `plugin_list`（`commands.py:162` 的注释就是这个意思）。`rule_inventory` 是例外，它返回的是 `script_packs` 键。

规则包索引默认地址来自后台参数 `rules.index_url`（默认值 `DEFAULT_RULES_INDEX` `params.py:19-23`，登记表条目 `params.py:222-224`），`plugin_pull` 会按索引条目的 `sha256` 校验下载内容，**对不上就不落盘**（`jsplugins.py:682-688`）。

**GitHub 国内镜像开关**（`github.mirror_enabled`，`params.py:245-264`）：打开后，凡是
`github.com` / `raw.githubusercontent.com` / `api.github.com` / `codeload.github.com` /
`objects.githubusercontent.com` 这五个主机的地址，都会先经 `utils/ghmirror.py` 改写成
`<github.mirror_prefix><原始地址>` 的前缀形式去取（`ghmirror.py:1-21` 的模块说明就是这套规则）。
`candidates(url)` 返回「镜像 → 直连」两条路，调用点**逐条试、失败就退回直连**，且异常文案里
把两条路的失败原因都拼上（`jsplugins.py:332-369` 索引、`jsplugins.py:548-590` 包下载、
`ffmpeg_deps.py:_open_stream` 依赖下载、`commands.py:887-928` 更新检查）。
镜像只改**取字节的路径**：`plugin_pull` 仍按索引里的官方 sha256 校验，替换字节的镜像会被拒落盘
（`jsplugins.py:576-579`），退回直连重取一次才能把「镜像挂了」和「镜像给错东西」分开。
非 GitHub 主机（Gitee、站点自己的 CDN）原样通过。

### 2.7 设置与后台参数（5 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `get_settings` | `getSettings()` `commands.ts:1206` | 无 | `{ usn, friendly_name, version, media_port, default_device, network_interface, settings }`（`utils/config.py:94-104`） | Sidecar | 通用 |
| `save_settings` | `saveSettings(settings)` `commands.ts:1056` | `settings: Partial<AppSettings>`；壳层包成 `{"settings": …}`（`lib.rs:577`） | `void` | Sidecar | 通用（`Missing field: 'settings'`）。`friendly_name` 传空串会被静默改回当前值、不阻断其余项保存（`commands.py:764-767`） |
| `list_network_interfaces` | `listNetworkInterfaces()` `commands.ts:1216` | 无 | `{ interfaces: NetworkInterface[], selected: string }`（`commands.py:751-756`） | Sidecar | 通用 |
| `get_params` | `getParams()` `commands.ts:1222` | 无 | `{ specs: ParamSpec[] }`（**32 条**，`params.py:41-265`；`specs()` 给每条补 `value` 与 `overridden`，`params.py:271-279`），前端解包成数组 | Sidecar | 通用 |
| `save_params` | `saveParams(values)` `commands.ts:1074` | `values: Record<key, number\|boolean\|string>`；壳层包成 `{"values": …}`（`lib.rs:604`） | `{ ok: boolean, errors: string[], specs: ParamSpec[] }`（`commands.py:795-803`）。`ok = errors.length === 0` | Sidecar | **不抛错**，逐项校验失败写进 `errors`：`未知参数: <key>`、`<中文标签>: <原因>`（`params.py:332-340`）。成功项照常落库，是部分成功语义 |

壳层定义：`lib.rs:566-608`。Sidecar 处理函数：`commands.py:748-803`。

后台参数 = `params.py` 里 `PARAM_SPECS` 暴露的可调项，落盘位置是 `hancast_setting.json` 的 `advanced` 键（`params.py:17`，`ADVANCED_KEY`）。**只保存与默认值不同的覆盖项**（`params.py:342-345`），改回默认值即删除覆盖，这样将来版本调默认值时用户能跟着走。

32 条按 section 分布：`screencast` 9、`discovery` 4、`player` 3、`http` 3、`browser` 3、`failures` 3、`media` 2、`rules` 2、`network` 2、`logs` 1。设置页分组标题按 section **首次出现顺序**排（`SettingsView.vue:111-115`），所以新加 section 要同时在 `src/locales/zh-CN.json` 与 `en-US.json` 补一条 `settings.paramsGroup.<section>`，否则界面上露出的是 i18n 键名。

`network` 段那两条（`github.mirror_enabled` / `github.mirror_prefix`）是**开关 + 前缀**一对：
开关默认关，前缀默认 `https://gh-proxy.com/`（`params.py:28`，`DEFAULT_GITHUB_MIRROR`）。
它们的消费点不在设置页而在 `utils/ghmirror.py`，每次请求现读，所以改完不用重启也不影响
已经在跑的解析任务；改写范围与回退语义见 §2.6 的「GitHub 国内镜像开关」一段。

改 `network_interface` 会触发 SSDP 在后台线程重绑网卡（约 1-2 秒），命令本身立即返回（`commands.py:775-789`）—— 前端 `saveSettings` resolve 之后设备列表可能还是旧网卡的结果。

### 2.8 日志（3 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `get_logs` | `getLogs(query)` `commands.ts:1158` | `params: { limit?, module?, level?, keyword?, include_older? }`；壳层**不解包**，`params` 原样下传（`lib.rs:803`） | `{ lines: LogLine[], total, truncated, path, level, modules: LogModule[] }`（`utils/logger.py:207-215`）。`limit` 被夹到 1..5000（`logger.py:163`） | Sidecar | 通用。`level` 非法时不报错，等同于不过滤（`logger.py:166-167`） |
| `set_log_level` | `setLogLevel(level)` `commands.ts:1163` | `level: string` | `{ ok: true, level: string }`（`commands.py:1071-1075`） | Sidecar | `未知日志级别: <level>`（`logger.py:131`）；通用 |
| `export_logs` | `exportLogs()` `commands.ts:1308` | 无 | `boolean`：`true` = 已复制，`false` = 用户在保存对话框里取消（`lib.rs:852-860`） | **壳层** | `日志文件不存在`（`lib.rs:839`）；`无法获取 APPDATA 目录`（`lib.rs:824`）/ `无法获取 HOME 目录`（`lib.rs:829`、`819`）；`Invalid path: <e>`（`lib.rs:854`）；`复制日志文件失败: <e>`（`lib.rs:856`） |

壳层定义：`lib.rs:798-814`（前两条）、`lib.rs:816-861`（`export_logs`）。

日志过滤按**模块名前缀**匹配：选 `hancast.screencast` 时 `manager` / `httpd` / `capture` 一并命中（`logger.py:157-158`、`196-197`）。`modules` 由 11 个已知模块标签（`logger.py:114-126`）加上日志尾部里实际出现过的其他模块名合并而成（`logger.py:219-240`）。多行堆栈会挂到上一条的 `msg` / `raw` 上（`logger.py:189-193`）。

**两个 `hancast.log` 是不同的文件**（§6.7）：`export_logs` 与 `get_logs` 读的都是 Python 侧那份（macOS `~/Library/Application Support/HanCast/logs/hancast.log`，`lib.rs:826-830`、`logger.py:19-24`）；壳层 `SessionLogger` 自己写的是 `app_data_dir()/logs/hancast.log`（`lib.rs:1069-1074`），identifier 为 `com.han.hancast`（`tauri.conf.json:5`），路径不同，且**每次启动都会清空**（`lib.rs:131`）。

### 2.9 更新（2 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `check_update` | `checkUpdate(currentVersion, force?)` `commands.ts:1170` | `currentVersion: string`、`force?: boolean`。壳层**硬编码注入** `"sources": ["github","gitee"]`（`lib.rs:621`），前端无法指定源 | `{ has_update, current, latest, url, body, source }`；`body` 截断到 500 字符（`commands.py:963`）。**只有真的经镜像取到版本信息时才多带一个 `via: "镜像"`**（`commands.py:922-923`），直连命中则没有这个键，前端 `UpdateInfo` 也不读它——它是给日志和探针看的。全部源失败时额外带 `"error": "无法连接更新服务器"`（`commands.py:930-939`） | Sidecar | 不抛错，失败信息在返回体的 `error` 键里（前端 `UpdateInfo` 类型没有这个字段，见 §6.6）。单源异常被 `logger.warning` 吞掉后 continue（`commands.py:925-928`） |
| `ignore_update_version` | `ignoreUpdateVersion(version)` `commands.ts:1175` | `version: string` | `boolean`；空串返回 `false`（`commands.py:968-974`） | Sidecar | 通用 |

壳层定义：`lib.rs:610-639`。Sidecar 处理函数：`commands.py:872-974`。

版本号比较走 SemVer 元组，含预发布位（`commands.py:816-863`，`_parse_version`）。超时：GitHub API 1 秒、Gitee 3 秒（`commands.py:887-896`）—— 手动检查最坏情况约 4 秒返回。**GitHub 走镜像时那 1 秒预算换成 3 秒**（`commands.py:904-907`）：国内镜像的往返本来就在 1~2 秒，照抄 1 秒等于开关无效。

另有一条**不经过前端**的启动检查：壳层 spawn 一个后台任务，睡 3 秒等 Sidecar 就绪，然后只查 `["github"]` 一个源，命中就 `emit("update-available")`（`lib.rs:1216-1256`）。微软商店版本直接跳过（`lib.rs:1219-1222`）。

### 2.10 剪贴板（2 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `read_clipboard` | `readClipboardText()` `commands.ts:1122` | 无 | `string` | **壳层** | clipboard 插件错误原文（`lib.rs:431`）。封装层会 try/catch 掉，失败后回落 `navigator.clipboard.readText()`，再失败返回 `''`（`commands.ts:969-979`）——**调用方看不到异常** |
| `write_clipboard` | `writeClipboardText(text)` `commands.ts:983` | `text: string` | `void` | **壳层** | clipboard 插件错误原文（`lib.rs:438-440`）。这条**不降级**，失败直接抛给调用方 |

壳层定义：`lib.rs:429-441`。

存在理由写在两处文档注释里（`lib.rs:424-428`、`lib.rs:434-435`）：macOS 的 WKWebView 不授予页面 `clipboard-read` 权限，`navigator.clipboard.readText()` 会**静默 reject**，「粘贴」按钮因此失效；写入侧的 reject 也不受同步 `try/catch` 保护。这两条是纯壳层能力，Sidecar 完全不知道剪贴板的存在。

### 2.11 窗口与托盘（2 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `minimize_window` | `minimizeWindow()` `commands.ts:1246` | 无 | `void` | **壳层** | 无。`window.minimize().ok()` 把错误丢掉（`lib.rs:153`） |
| `close_window` | `closeWindow()` `commands.ts:1255` | 无 | `void` | **壳层** | 无。`window.hide().ok()`（`lib.rs:159`）——**不是退出**，是隐藏到托盘 |

壳层定义：`lib.rs:151-160`。

配套行为：原生关闭按钮走 `CloseRequested` → `prevent_close()` + `hide()`（`lib.rs:1138-1145`），与 `close_window` 效果一致。真正退出只有托盘菜单的 Quit：先 `sidecar.shutdown()` 再 `exit(0)`（`lib.rs:1039-1046`）。窗口最小尺寸 346×472（`lib.rs:1107-1108`）。启动参数带 `--autostart` 或 `--minimized` 时直接以隐藏状态启动（`lib.rs:1096`）。

托盘菜单项（复制投屏地址 / 显示窗口 / 开机自启 / 退出）由壳层 `update_tray_menu` 维护（`lib.rs:976-1038`），**前端没有任何 API 能读写托盘**。前端能感知托盘的唯一途径是 `autostart-changed` 事件（§3.1）。

### 2.12 其他（系统集成 / 无前端封装）（5 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `is_store_version` | `isStoreVersion()` `commands.ts:1337` | 无 | `boolean` | **壳层** | 无。判定纯靠可执行文件路径是否含 `windowsapps` / `553787e6.hancast` / `build-msix`（`lib.rs:713-727`）；非 Windows 恒为 `false`。封装层 catch 后返回 `false`（`commands.ts:1182-1189`） |
| `get_autostart` | `getAutostart()` `commands.ts:1235` | 无 | `boolean` | **壳层** | MSIX 路径：`获取启动任务失败: …` / `等待启动任务失败: …` / `获取启动任务状态失败: …`（`lib.rs:876-883`）；非 MSIX：`tauri-plugin-autostart` 的错误原文（`lib.rs:948`） |
| `set_autostart` | `setAutostart(enabled)` `commands.ts:1085` | `enabled: boolean` | `void`；成功后 emit `autostart-changed`（`lib.rs:970`） | **壳层** | MSIX 路径：`请求启用启动任务失败: …` / `等待启用结果失败: …` / `禁用启动任务失败: …`（`lib.rs:899-906`）；非 MSIX：autolaunch 错误原文（`lib.rs:960-966`）。非 Windows 的 `MSIX 开机自启仅在 Windows 上可用`（`lib.rs:927`）实际不可达 —— `is_store_version()` 在非 Windows 恒 `false`（`lib.rs:912-916` 的注释也这么说） |
| `check_mpv` | **无** | 无 | `{ status: "ready"\|"not_found"\|"error", path, version, source, error }`（`models/mpv.py:17-27`、`commands.py:1005-1008`） | Sidecar | 通用 |
| `set_mpv_path` | **无** | `path: string` | 同 `check_mpv`；成功时会热替换 MPV 渲染器（`commands.py:1010-1036`） | Sidecar | 通用 |

壳层定义：`lib.rs:713-727`（`is_store_version`）、`lib.rs:939-972`（自启）、`lib.rs:731-744`（MPV）。

开机自启有**两套实现**，按 `is_store_version()` 二选一（`lib.rs:931-937`、`925-935`、`938-957`）：MSIX 走 WinRT `StartupTask`（TaskId `HanCastStartup`，`lib.rs:868`，启动参数由 `AppxManifest.xml` 的 `uap10:Parameters="--autostart"` 提供，`lib.rs:889`），经典安装走 `tauri-plugin-autostart`。

`check_mpv` / `set_mpv_path` 已在 `generate_handler!` 里注册、Sidecar 也实现了，但 **`src/` 下没有任何封装或调用点** —— 属于「接收投屏」（本机作 DLNA 渲染端）方向的预留接口。

---

## 3. 事件全表

### 3.1 8 个事件

| 事件名 | 载荷字段（逐个） | 谁发出 | 谁订阅 | 触发时机 |
| --- | --- | --- | --- | --- |
| `ready` | 无（`data` 为 `{}`，`protocol_io.py:58`） | `hancast-backend/hancast_sidecar/main.py:110` | **无前端订阅者**（全仓 `listen(` 检索无命中） | Sidecar 启动完成、进入 `for line in sys.stdin` 主循环之前，握手用。壳层也不监听它 |
| `device_found` | `id: string`、`name: string`（已被 `display_name` 覆盖）、`device_type: string`、`ip: string`、`port: number`、`status: string`、`is_default: boolean`、`manufacturer: string\|null`、`model_name: string\|null`、`udn: string`、`custom_name: string\|null`、`location: string` | `commands.py:282`（`_on_device_found`，载荷是 `Device.to_dict()`，`models/device.py:26-29`） | `src/stores/device.ts:122` | SSDP 收到 NOTIFY/搜索响应并完成描述抓取时。同一设备重复发现也会发，前端做 upsert（`device.ts:123-134`）。**v2.3.13 起有准入门槛**：描述里必须声明 `AVTransport` 服务（`ssdp.py:1074`、`ssdp.py:1169-1175`），路由器/NAS 这类「有 UPnP 描述但投不了屏」的设备不再发事件、也不进列表 |
| `device_lost` | `id: string` | `commands.py:290`（`_on_device_lost`） | `src/stores/device.ts:139` | 收到 SSDP byebye（`ssdp.py:725-738`）或探活判定过期（`ssdp.py:722-723`、`discovery.device_stale_s`，`ssdp.py:747`） |
| `cast_state_changed` | `status: string`、`device_id: string\|null`、`position: string`（`HH:MM:SS`）、`duration: string`（`HH:MM:SS`，见下注）、`volume: number`、`is_muted: boolean`、`media?: MediaInfo`（有媒体时才带） | `commands.py:324`（DLNA `TransportState` 变化回调）、`commands.py:440`（`start_cast` 后立即补发）、`commands.py:470`（`stop_cast` 逐设备发）、`commands.py:518`（`_emit_session_state`，会话轮询回调） | `src/stores/cast.ts:653-724` | 投屏状态机变化。**注意 `commands.py:312-322` 那条路径不带 `duration`**，前端要容错 |
| `cast_confirm_request` | `request_id: string`、`device: { ip, udn, friendly_name, model_name }`、`timeout: number`（秒） | `commands.py:962`（`_on_guard_confirm_request`） | `src/stores/guard.ts:126` | 外部设备向本机推流、且该来源不在信任/黑名单里时（`guard.py:188-238` → `240-272`）。前端收到后弹确认框并把窗口拉到前台（`guard.ts:141-148`） |
| `screen_cast_state_changed` | 与 `screen_cast_status` 返回体**完全相同**：`running`、`target`、`source`、`device_id`、`session_id`、`watch_url`、`url`、`viewers`、`error`、`started_at`，外加 `pull`、`ffmpeg` | `screencast/manager.py:469`（`_emit()`，载荷就是 `self.status()`） | `src/stores/screencast.ts:285-300` | 屏幕投屏启动成功（`manager.py:167`）、停止（`manager.py:225`）、捕获出错、观众数变化时 |
| `update-available` | `has_update: boolean`、`current: string`、`latest: string`、`url: string`、`body: string`（≤500 字符）、`source: string` | `src-tauri/src/lib.rs:1229`（**壳层发出，不是 Python**） | `src/stores/update.ts:94` | 启动后台检查（`lib.rs:1216-1256`，睡 3s 后查 GitHub）命中新版本时。商店版本不会发 |
| `autostart-changed` | `boolean`（新的自启状态） | `src-tauri/src/lib.rs:955`（`set_autostart` 命令末尾）、`src-tauri/src/lib.rs:1048`（托盘菜单勾选回调） | `src/stores/settings.ts:32`；**另有壳层自监听** `src-tauri/src/lib.rs:1258`（同步托盘勾选态） | 开机自启状态被任何一侧改动时。前端据此更新设置页开关，避免与托盘状态打架 |

### 3.2 一个死监听

`src/stores/cast.ts:728` 注册了 `listen<{device_id?, message}>('cast_error', ...)`，但**整个 `hancast-backend/` 里没有任何 `emit_event("cast_error", ...)`**（全仓 `emit_event(` 检索只命中 `device_found` / `device_lost` / `cast_state_changed` / `cast_confirm_request` / `ready` / `screen_cast_state_changed`）。这个监听器永远不会被触发。投屏失败实际是通过 `cast_state_changed` 的 `status: "error"`（或 `start_cast` 的 invoke rejection）传出来的。

因为不存在发送方，`cast_error` **不计入 §0 的事件总数 8**。

### 3.3 前端订阅方式

所有 store 都用动态 import 拿 `listen`，并把返回的 `UnlistenFn` 收进数组以便卸载：

```ts
const { listen } = await import('@tauri-apps/api/event')
unlistenFns.push(await listen<Device>('device_found', (event) => { /* event.payload */ }))
```

外层一律包 `try/catch`，非 Tauri 环境（浏览器 dev）静默跳过（`device.ts:118-146`、`guard.ts:119-152`、`update.ts:88-101`、`settings.ts:28-38`、`screencast.ts:281-303`）。

---

## 4. 分组调用示例（摘自真实代码）

以下片段全部从仓库现有文件里截出，标注了来源；没有为文档新造的 API。

### 4.1 设备发现与管理 — `src/stores/device.ts:93-112`

```ts
/** 成功返回设备名，失败写入 addError 并返回 null */
async function addDevice(address: string): Promise<string | null> {
  adding.value = true
  addError.value = null
  try {
    const device = await apiAddDevice(address)
    // device_found 事件对已存在设备可能不再触发，直接并入列表
    const exists = devices.value.some((d) => d.id === device.id)
    devices.value = exists
      ? devices.value.map((d) => (d.id === device.id ? { ...d, ...device } : d))
      : [...devices.value, device]
    await fetchManualDevices()
    return device.name
  } catch (err) {
    addError.value = err instanceof Error ? err.message : String(err)
    return null
  } finally {
    adding.value = false
  }
}
```

要点：命令返回与 `device_found` 事件是**两条并行的更新通道**，必须做 upsert 而不是无脑 push。

### 4.2 投屏控制 — `src/stores/cast.ts:356-484`

```ts
async function stopCast(deviceId?: string) {
  loading.value = true
  stopWarning.value = null
  const ids = deviceId ? [deviceId] : Object.keys(sessions.value)
  const names = ids.map((id) => sessions.value[id]?.device_name ?? id)
  let failed: string[] = []
  let reasons: string[] = []
  try {
    const res = await apiStopCast(deviceId)
    failed = res?.control_failed ?? []
    const map = res?.reasons ?? {}
    reasons = failed.map((id) => map[id] ?? '')
  } catch (err) {
    // 命令层本身失败（sidecar 超时/异常）时无法确认电视收到 Stop
    failed = ids
    reasons = ids.map(() => String(err))
  } finally {
    // 本地会话一律先释放：留着它只会让界面停在「正在投屏」
    if (deviceId) { stopSim(deviceId); removeSession(deviceId) }
    else { stopAllSim(); sessions.value = {}; focusedDeviceId.value = null; showController.value = false }
    if (activeDeviceIds.value.length === 0) { stopPolling(); showController.value = false }
    if (failed.length) {
      stopWarning.value = {
        devices: failed.map((id) => names[ids.indexOf(id)] ?? id),
        reasons: reasons.filter(Boolean),
      }
    }
    loading.value = false
  }
}
```

要点：`stop_cast` 的「成功」只表示本地会话已释放。`control_failed` 必须一路传到 UI，否则「界面停了、电视还在播」会被当成一次成功的停止。

### 4.3 媒体解析与粘贴 — `src/stores/media.ts:115-182`

```ts
async function parseUrl(input?: string, quality = '') {
  const raw = (input ?? urlInput.value).trim()
  if (!raw) return
  state.value = 'parsing'
  error.value = null
  try {
    const info = await resolveShare(raw, quality)
    if (!info.uri) throw new Error('解析结果为空')
    mediaInfo.value = {
      media_type: 'url',
      uri: info.uri,
      title: info.title || raw,
      mime_type: info.mime_type ?? '',
      file_size: info.file_size ?? null,
      duration: info.duration ?? null,
      thumbnail: info.thumbnail ?? null,
      qualities: info.qualities ?? [],
      quality: info.quality ?? quality,
      // 切换画质要拿它再解一次（口令里的短链已展开，重解更快）
      source_url: info.source_url || raw,
    }
    if (info.source_url) urlInput.value = info.source_url
    state.value = 'ready'
    return
  } catch (err) {
    // 只有「地址本身就是媒体文件」（MEDIA_FILE_RE 白名单扩展名）才按扩展名推 MIME 透传；
    // 网页地址透传出去电视拉不到画面，所以如实报错，并置 ledgerHint 提示台账入口
    const fallbackUrl = extractFirstUrl(raw)
    if (!fallbackUrl || !isMediaFileUrl(fallbackUrl)) { error.value = String(err); state.value = 'idle'; return }
    /* ……媒体直链：按扩展名推 MIME 后透传 */
  }
}
```

要点：`resolveShare` 内部已经把 `task_id` 轮询封死了，调用方看到的就是一次普通的 `await`。
解析失败的口径（v2.3.9 之后收紧，尚未随任一 tag 发布）：**抠出的地址本身是媒体文件才降级透传，网页地址一律报错**——旧实现无条件透传，
表现为「随便粘一个 HTML 页面也能点投屏，点了却什么都没发生」（`media.ts:142-160`、白名单 `media.ts:15-19`）。
报错时 `ledgerHint` 置真，输入框下方就地给出台账入口：设置 → 解析插件 → 解析失败台账（`MediaInput.vue:610`）。

### 4.4 屏幕投屏 — `src/components/ScreenCastPanel.vue:131-155`

```ts
async function onStart() {
  if (micOn.value) await ensureMicAccess()
  const ok = await sc.start({
    target: target.value,
    ...(target.value === 'dlna' ? { device_id: deviceId.value } : {}),
    source: sourceType.value === 'window'
      ? { type: 'window', id: selectedWindow.value!.id, title: selectedWindow.value!.title }
      : { type: 'screen', id: screenId.value },
    audio: {
      mic: micOn.value,
      mic_device: micOn.value ? micDevice.value : undefined,
      system: systemOn.value,
      system_device: systemOn.value ? systemDevice.value : undefined,
    },
    video: { width: width.value, height: height.value, fps: fps.value, bitrate: bitrate.value },
  })
  if (ok) { /* 成功开始后保持面板打开展示邀请链接 */ }
}
```

store 侧的返回处理在 `src/stores/screencast.ts:189-211`：`result.ok === false` 时把 `result.error` 写进 `lastError` 并返回 `false`，**不抛异常**。

### 4.5 投屏确认（Device Guard）— `src/stores/guard.ts:72-86`

```ts
async function handleResponse(approved: boolean, policy: 'once' | 'always' | 'blacklist' | 'reject') {
  const request = pendingRequest.value
  if (!request) return
  stopCountdown()
  try {
    await respondCastConfirm(request.request_id, approved, policy)
  } catch (err) {
    console.error('Failed to respond cast confirm:', err)
  } finally {
    pendingRequest.value = null
    // Refresh device lists in case a new device was added
    fetchDevices()
  }
}
```

事件侧的自动决策在 `guard.ts:126-140`：Guard 关闭时自动 `approved: true, policy: 'once'`；已有 pending 时对新请求自动 `approved: false`。

### 4.6 规则包管理 — `src/components/PluginSettings.vue:152-166`

```ts
async function onPull(entry: PackIndexEntry) {
  if (pulling.value) return
  pulling.value = entry.name
  try {
    inv.value = { ...inv.value, script_packs: await pluginPull(entry.name) }
    packUpdates.value = packUpdates.value.filter((u) => u.name !== entry.name)
    packAvailable.value = packAvailable.value.filter((a) => a.name !== entry.name)
    pulled.value = t('settings.pluginPulled', { name: entry.name, version: entry.version })
    actionError.value = ''
  } catch (err: any) {
    actionError.value = String(err?.message ?? err)
  } finally {
    pulling.value = ''
  }
}
```

`pluginPull` 返回的就是全量插件清单，直接塞回 `rule_inventory` 结果的 `script_packs` 字段，省掉一次刷新。

### 4.7 设置与后台参数 — `src/views/SettingsView.vue:171-189`

```ts
async function submitParams(values: Record<string, number | boolean | string>) {
  if (!Object.keys(values).length) return
  paramsSaving.value = true
  try {
    const res = await apiSaveParams(values)
    params.value = res.specs ?? params.value
    paramDraft.value = Object.fromEntries(params.value.map((s) => [s.key, s.value]))
    paramErrors.value = res.errors ?? []
    if (res.ok) {
      paramsSaved.value = true
      setTimeout(() => { paramsSaved.value = false }, 2000)
    }
  } catch {
    paramErrors.value = [t('settings.paramsSaveFailed')]
  } finally {
    paramsSaving.value = false
  }
}
```

要点：`save_params` 是**部分成功**语义 —— `res.ok === false` 不代表什么都没存，成功项已落库，`res.specs` 就是最新全量。UI 必须逐项显示 `res.errors`。

### 4.8 日志 — `src/stores/logs.ts:32-63`

```ts
async function refresh() {
  // 追日志和改过滤条件可能撞车：丢掉请求会让界面停留在旧过滤结果上，排队重跑
  if (loading.value) { pendingRefresh = true; return }
  loading.value = true
  error.value = null
  try {
    const page = await getLogs({
      limit: limit.value,
      module: module.value || undefined,
      level: filterLevel.value || undefined,
      keyword: keyword.value.trim() || undefined,
      include_older: includeOlder.value,
    })
    lines.value = page.lines
    modules.value = page.modules
    total.value = page.total
    truncated.value = page.truncated
    path.value = page.path
    level.value = page.level
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
    if (pendingRefresh) { pendingRefresh = false; refresh() }
  }
}
```

面板打开时每 3 秒 tail 一次，页面不可见时跳过（`logs.ts:77-89`）。`level` 从返回体回读，不本地维护 —— `set_log_level` 的结果才是权威值。

### 4.9 更新 — `src/stores/update.ts:26-52`

```ts
async function check(currentVersion: string, force = true) {
  // 微软商店版本由 Store 自动管理更新，跳过检查
  if (storeVersion.value) { error.value = 'store_version'; return null }
  loading.value = true
  error.value = ''
  try {
    const result = await checkUpdate(currentVersion, force)
    if (result.has_update) { updateInfo.value = result; showModal.value = true }
    else if (force) { error.value = 'no_update' }  // UI 提示「已是最新版本」
    return result
  } catch (e) { error.value = String(e); return null }
  finally { loading.value = false }
}
```

`storeVersion` 来自 `isStoreVersion()`（`update.ts:20`，壳层命令）。商店版还会跳过 `update-available` 的监听注册（`update.ts:88-90`）。

### 4.10 剪贴板 — `src/components/MediaInput.vue:366-375`

```ts
async function onPasteFromClipboard() {
  const text = (await readClipboardText()).trim()
  if (!text) {
    pickerError.value = t('media.clipboardEmpty')
    return
  }
  pickerError.value = null
  mediaStore.setInputChange(text.replace(/\s+/g, ' '))
  mediaStore.parseUrl(text)
}
```

要点：`readClipboardText()` **不会抛异常**（封装层三级降级，最终返回 `''`，`commands.ts:967-980`），所以调用方只能靠空串判断失败。同文件 `onUrlPaste`（`MediaInput.vue:377`）走的是浏览器原生 `ClipboardEvent`，不经过壳层。

### 4.11 窗口与托盘 — `src/components/TitleBar.vue:6` + `:26`、`:74`

```ts
import { minimizeWindow, closeWindow } from '@/api/commands'
```

```html
<button class="title-bar__btn" @click="minimizeWindow" :title="t('controller.minimize')">
```

`closeWindow()` 在 `TitleBar.vue:26`（关闭确认弹窗的「确定」）和 `:37`（不再提示时直接调用）两处触发。两条都返回 `void` 且吞掉错误，前端不需要 try/catch。

### 4.12 其他（开机自启）— `src/stores/settings.ts:40-51`、`65-73`

```ts
async function fetchSettings() {
  loading.value = true
  try {
    settings.value = await getSettings()      // Sidecar
    autostart.value = await apiGetAutostart() // 壳层
    initAutostartListener()                   // 订阅 autostart-changed
  } catch (err) {
    console.error('Failed to fetch settings:', err)
  } finally {
    loading.value = false
  }
}

async function toggleAutostart() {
  const newVal = !autostart.value
  try {
    await apiSetAutostart(newVal)
    autostart.value = newVal
  } catch (err) {
    console.error('Failed to toggle autostart:', err)
  }
}
```

这一段是「一次设置页加载同时打两层」的典型：`get_settings` 走 Sidecar，`get_autostart` 走壳层。托盘那边改动自启时，`autostart-changed` 事件（`settings.ts:32`）会把 store 里的值同步回来。

`check_mpv` / `set_mpv_path` **没有调用示例** —— 前端不存在封装也不存在调用点（§2.12）。

---

## 5. 类型定义索引（`src/types/`）

| 文件 | 行数 | 负责什么 | 关键定义 |
| --- | --- | --- | --- |
| `index.ts` | 6 | 桶文件，统一再导出 | 只导出 `device` / `media` / `cast` / `settings` / `guard` / `update` 六个模块的类型。**`logs.ts` 与 `screencast.ts` 没有被它再导出**（`index.ts:1-6`），必须直接从各自路径 import |
| `device.ts` | 12 | 设备发现结果 | `Device`：`id` / `name` / `type` / `ip` / `port` / `status` / `is_default` / `manufacturer?` / `model_name?` / `udn`。`type` 联合是 `'tv'\|'speaker'\|'box'\|'unknown'`（与后端不一致，见 §6.1）；缺 `custom_name`、`location` 两个后端实际会发的字段 |
| `media.ts` | 23 | 媒体信息与画质档位 | `MediaQuality {id,label}`；`MediaInfo`：`media_type` / `uri` / `title` / `duration: number\|null` / `mime_type` / `file_size` / `thumbnail` / `qualities?` / `quality?` / `source_url?`；`MediaInputState = 'idle'\|'dragover'\|'input'\|'parsing'\|'ready'`。与后端 `MediaInfo.to_dict()`（`models/media.py:22-39`）字段一一对应，但 TS 侧少了 `via` / `author` / `bvid`（那三个在 `commands.ts:683-689` 的 `ShareResolveResult` 里补） |
| `cast.ts` | 53 | 投屏状态与会话 | `CastState`（**前端展示态**，`position: number` 秒 + `positionTime`/`durationTime` 字符串）、`CastSession`（绑定设备的会话）、`CastUrlInfo`（`get_cast_url` 返回）、`createSession(deviceId, deviceName)` 工厂函数（`:41-52`，初始 `status: 'connecting'`、`volume: 80`）。注意这里的 `CastState` **不是**后端 `CastState.to_dict()` 的形状（后端 `position`/`duration` 都是 `HH:MM:SS` 字符串，`models/cast.py:16-27`），转换在 `stores/cast.ts:190-236` |
| `settings.ts` | 58 | 应用设置、网卡、后台参数 | `AppSettings`（对应 `config.to_dict()`）、`NetworkInterface` / `NetworkInterfacesInfo`、`ParamType = 'number'\|'bool'\|'enum'\|'text'\|'string'`、`ParamSpec`（含 `label_zh`/`label_en`/`help_zh`/`help_en`/`min`/`max`/`unit`/`options`/`value`/`overridden`）、`SaveParamsResult {ok, errors, specs}`、`DeviceLostEvent {id}`、`CastErrorEvent {message}`（**后者对应的事件从未被发出**，§3.2） |
| `guard.ts` | 34 | 投屏确认 | `GuardDevice`（对应后端 `DeviceGuardEntry.to_dict()`，`guard.py:44-57`）、`GuardDevicesResponse {trusted, blacklisted}`、`GuardSettings {enabled, confirm_timeout}`、`CastConfirmRequest`（`cast_confirm_request` 事件载荷，`device` 只声明了 `ip`/`udn`/`friendly_name`/`model_name` 四个字段） |
| `logs.ts` | 33 | 日志面板 | `LogLine {ts, module, level, msg, raw}`、`LogModule {id, label}`、`LogPage {lines, total, truncated, path, level, modules}`、`LogQuery {limit?, module?, level?, keyword?, include_older?}`。与 `logger.read_logs()` 返回体（`logger.py:207-215`）逐字段对应 |
| `screencast.ts` | 107 | 屏幕投屏 + FFmpeg 依赖 | `ScreenSourceItem`（含显示器专用的 `width`/`height`/`primary`）、`AudioDeviceItem`（含 `tap`：macOS 自建 Audio Tap 环回；`loopback`：Windows WASAPI 环回；`default`）、`ScreenSources`（含 `platform`）、`ScreenCastState`、`ScreenCastStartParams`；v2.3.11 去捆绑后新增 `FfmpegDepItem`（单个依赖的状态）、`FfmpegStatus`（`ffmpeg_status` 返回体，含 `items/missing/install_commands/download`）、`FfmpegDownloadTask`（后台下载任务快照）——三者都是「需要下载运行依赖」弹窗的数据源。`ScreenCastState` **缺 `pull` 字段**（§6.5） |
| `update.ts` | 16 | 更新检查 | `UpdateInfo {has_update, current, latest, url, body, source}`（**缺后端会发的 `error`**，§6.6）、`UpdateModalState = 'hidden'\|'checking'\|'available'\|'latest'\|'error'` |

`src/api/commands.ts` 里还自带一批**没有放进 `src/types/`** 的接口定义，用规则包/台账时要从那里 import：`ShareResolveResult`（`:661`）、`PluginRuleInfo`（`:710`）、`PluginInfo`（`:718`）、`PackIndexEntry`（`:730`）、`PackUpdateEntry`（`:739`）、`PackUpdateCheck`（`:743`）、`DeclarativeRuleInfo`（`:751`）、`RuleInventory`（`:761`）、`FailureEntry`（`:768`）、`FailureHost`（`:776`）、`FailureLedger`（`:784`）、`StopCastResult`（`:963`）、`StopWarning`（`:972`）。

---

## 6. 坑（已验证的契约不一致）

### 6.1 `Device.type` 对不上后端的 `device_type`

后端 dataclass 字段名是 `device_type`（`models/device.py:9`），`to_dict()` 只做 `asdict()` 再把 `name` 换成 `display_name`，**不改键名**（`models/device.py:26-29`）。前端类型写的却是 `type`（`types/device.ts:4`）。

后果：`DeviceCard.vue:151` 的 `v-if="device.type === 'speaker'"` 恒为 `false`，音箱设备的同步控件永远不渲染。

同时联合类型两头都不对：`ssdp.py:1160-1167` 实际只会产出 `tv` / `speaker` / `server` / `unknown` 四种，**`box` 从来不会被赋值**，而 `server` 不在 TS 联合里。

### 6.2 `PluginInfo.broken` 后端从不产出

`commands.ts:746` 声明了 `broken?: boolean`，`PluginSettings.vue:279` 用它渲染「已损坏」标记。但后端 `plugin.info()`（`jsplugins.py:254-271`）只输出 `name` / `version` / `author` / `description` / `update_url` / `error` / `rules`，**没有 `broken`**（全 `jsplugins.py` 检索 `broken` 无命中）。坏包靠 `error` 字段体现。

### 6.3 `PackIndexEntry.sha256` 在 `check_updates` 里被丢掉

`fetch_index` 解析出了 `sha256`（`jsplugins.py:445`），但 `check_updates` 组装 `updates` / `available` 条目时只取 `name` / `version` / `url` / `description`（`jsplugins.py:459-460`）。前端 `PackIndexEntry.sha256?`（`commands.ts:757`）因此永远是 `undefined`。真实下载时 `pull` 会重新 `fetch_index` 拿摘要（`jsplugins.py:478`、`:490`），校验本身没被绕过，只是前端拿不到这个值。

### 6.4 `get_cast_state()` 不带参数时返回的不是 `CastState`

前端封装签名是 `getCastState(deviceId?): Promise<CastState>`（`commands.ts:1030`），但省略 `deviceId` 时后端返回 `{sessions, current}`（`commands.py:549-552`）。`stores/cast.ts:193` 只能 `as any` 绕开类型系统再手工解构。

同类问题：`get_cast_url` 的 `status` 大小写随分支变化 —— 带 `deviceId` 走 `session.get_cast_info()` 返回小写（`session.py:150`），不带则返回 DLNA 原生大写（`commands.py:569`）。前端用 `info.status.toUpperCase()` + 映射表统一（`cast.ts:196-208`）。

### 6.5 `ScreenCastState` 缺 `pull`

`manager.status()` 在有 `session_id` 时会补 `pull`（拉流统计，`manager.py:236-239`），`screen_cast_state_changed` 事件的载荷就是 `status()` 的输出（`manager.py:469`）。但 `types/screencast.ts:40-52` 没有这个字段 —— 电视黑屏时区分「没人来拉」和「拉了但跟不上码率」的关键证据在前端类型上是不可见的。

### 6.6 `UpdateInfo` 缺 `error`

`_check_update` 在所有源都失败时返回 `{"error": "无法连接更新服务器"}`（`commands.py:938`），`types/update.ts:4-11` 没有这个字段，`source` 也会被声明成 `'github'|'gitee'` 而实际会是 `''`。

### 6.7 两个 `hancast.log`

- 壳层 `SessionLogger` 写 `app_data_dir()/logs/hancast.log`（目录拼接在 `lib.rs:1069-1074`），且**每次启动清空**（`lib.rs:126-133`，`fs::write(&path, "")` 在 `:131`）。`app_data_dir` 由 identifier `com.han.hancast` 推导（`tauri.conf.json:5`）。
- Python 写 `~/Library/Application Support/HanCast/logs/hancast.log`（macOS，`logger.py:19-24`）。

`export_logs`（`lib.rs:965`）和 `get_logs`（`lib.rs:947`，取数走 `read_logs` `logger.py:156-218`）读的都是**后者**。前者里的壳层日志不会出现在日志面板和导出文件里。

> **例外（v2.3.23 起）**：macOS 通知的授权状态与投递错误由壳层 `macos_notify::log()` 直接追加到**后者**那份文件（路径拼接在 `macos_notify.rs:30-40`，行格式与 Python logger 一致：`[时间] hancast.notify INFO: …`），为的就是让后台日志面板当场读到——上一版把 add 的 completion handler 传成 `None`，等于亲手丢掉了唯一的直接证据。

### 6.8 Guard 的 `'reject'` 策略后端不认

前端确认弹窗提供 `'reject'`（`GuardConfirmModal.vue:14`），store 的类型联合里也有它（`guard.ts:72`），但后端 `wait_for_confirm` 只对 `'always'` 和 `'blacklist'` 做特殊处理（`guard.py:274-299`），其余策略一律按「本次拒绝、不落库」走。传 `'reject'` 不会报错，行为等价于 `'once'` + `approved: false`。

### 6.9 `save_guard_settings` 与 `save_settings` 的解包方式不一致

- `save_settings`：壳层包一层 → `json!({"settings": settings})`（`lib.rs:577`），Python 读 `params["settings"]`（`commands.py:759`）。
- `save_guard_settings`：壳层**原样下传** → `.send_command("save_guard_settings", settings)`（`lib.rs:706`），Python 直接读 `params["enabled"]`（`commands.py:998`）。

同样是「保存一个设置对象」，两条命令的信封结构不同。`start_screen_cast`（`lib.rs:820`）和 `get_logs`（`lib.rs:947`）也是原样下传。

### 6.10 `unhide_device` / `get_hidden_devices` 前端调不到

Sidecar 实现了（`commands.py:382-390`）并注册在 `_commands` 里（`commands.py:119-120`），但壳层没有对应的 `#[tauri::command]`，`generate_handler!` 里也没有。**设备一旦被隐藏，界面上没有任何途径恢复**，只能手改配置文件。

### 6.11 invoke rejection 是裸字符串

壳层命令一律 `Result<T, String>`，Tauri 把它作为 reject reason 直接传给 JS，**不是 `Error` 实例**。全仓统一用 `err instanceof Error ? err.message : String(err)` 兜（`device.ts:107`、`logs.ts:55`），但 `PluginSettings.vue:39` 用的是 `String(err?.message ?? err)` —— 两种写法都在流通。

### 6.12 浏览器 mock 静默返回空对象

`mockInvoke` 的 `default` 分支是 `return {} as T`（`commands.ts:622`）。未在 mock 里实现的命令在纯浏览器环境下**不会报错**，只会返回一个类型断言过的空对象。任何「浏览器里正常、Tauri 里异常」的报告都要先排除这一条。

---

## 7. 快速参考表（67 条）

执行层：**壳** = 壳层本地，**SC** = Sidecar。

| # | 命令名 | 一句话用途 | 执行层 |
| --- | --- | --- | --- |
| 1 | `minimize_window` | 最小化主窗口 | 壳 |
| 2 | `close_window` | 隐藏到托盘（不是退出） | 壳 |
| 3 | `get_devices` | 取当前在线设备列表（已剔除隐藏项） | SC |
| 4 | `refresh_devices` | 主动发一次 SSDP 搜索后取列表 | SC |
| 5 | `set_default_device` | 把某设备设为默认投屏目标 | SC |
| 6 | `rename_device` | 给设备起自定义名 | SC |
| 7 | `remove_device` | 从列表移除设备并清掉手动登记（前端无调用点） | SC |
| 8 | `hide_device` | 隐藏设备（单向，界面上无法恢复） | SC |
| 9 | `add_device` | 按 IP / `ip:port` / 描述 URL 手动添加设备 | SC |
| 10 | `get_manual_devices` | 取手动添加过的地址清单 | SC |
| 11 | `parse_media_file` | 解析本地媒体文件元信息 | SC |
| 12 | `parse_media_url` | 解析直链媒体元信息（前端无调用点） | SC |
| 13 | `resolve_share` | 从粘贴文本解析可播地址，返回异步任务 id | SC |
| 14 | `get_resolve_result` | 轮询解析任务结果（无独立封装） | SC |
| 15 | `plugin_list` | 列出已安装规则包 | SC |
| 16 | `rule_inventory` | 一屏列全规则包 + 声明式 JSON 规则 | SC |
| 17 | `failure_list` | 解析失败台账（按域名分组） | SC |
| 18 | `failure_delete` | 删台账里一个域名或一条地址 | SC |
| 19 | `failure_clear` | 清空解析失败台账 | SC |
| 20 | `plugin_check_updates` | 对照官方索引看哪些包可更新 / 可安装 | SC |
| 21 | `plugin_pull` | 按索引装或更新一个包（sha256 校验） | SC |
| 22 | `plugin_import` | 导入规则包 zip（本地路径 / base64 / URL 三选一） | SC |
| 23 | `plugin_remove` | 卸载一个规则包 | SC |
| 24 | `plugin_set_rule_enabled` | 启停规则包里的某条规则 | SC |
| 25 | `plugin_test` | 用一段分享文本试跑某规则包，返回任务 id | SC |
| 26 | `read_clipboard` | 读系统剪贴板文本 | 壳 |
| 27 | `write_clipboard` | 写系统剪贴板 | 壳 |
| 28 | `start_cast` | 向指定设备开始投屏 | SC |
| 29 | `stop_cast` | 停止投屏（可全停），报告电视是否真收到 Stop | SC |
| 30 | `pause_cast` | 暂停 | SC |
| 31 | `resume_cast` | 继续 | SC |
| 32 | `seek_cast` | 跳转到 `HH:MM:SS` | SC |
| 33 | `get_cast_state` | 取投屏状态（单设备 / 全部会话两种形状） | SC |
| 34 | `get_cast_url` | 取当前投屏元素的 URL 与进度 | SC |
| 35 | `set_volume` | 设音量，返回生效值 | SC |
| 36 | `set_mute` | 设静音 | SC |
| 37 | `get_settings` | 取应用设置 | SC |
| 38 | `save_settings` | 存应用设置（改网卡会后台重启 SSDP） | SC |
| 39 | `list_network_interfaces` | 枚举可选网卡及当前选定项 | SC |
| 40 | `get_params` | 取 32 条后台参数登记表 + 当前值 | SC |
| 41 | `save_params` | 存后台参数，逐项校验、部分成功 | SC |
| 42 | `check_update` | 检查新版本（壳层硬编码 github + gitee 双源） | SC |
| 43 | `ignore_update_version` | 记录「此版本不再提示」 | SC |
| 44 | `respond_cast_confirm` | 用户回应投屏确认弹窗 | SC |
| 45 | `get_guard_devices` | 取信任 / 黑名单设备 | SC |
| 46 | `remove_guard_device` | 从信任 / 黑名单移除（UDN 或 IP） | SC |
| 47 | `set_guard_policy` | 改某设备的信任策略 | SC |
| 48 | `get_guard_settings` | 取投屏确认开关与超时 | SC |
| 49 | `save_guard_settings` | 存投屏确认开关与超时（信封不解包） | SC |
| 50 | `check_mpv` | 检查本机 MPV 可用性（前端无封装） | SC |
| 51 | `set_mpv_path` | 手动指定 MPV 路径并热替换渲染器（前端无封装） | SC |
| 52 | `list_screen_sources` | 枚举屏幕 / 窗口 / 音频设备 + FFmpeg 状态 | SC |
| 53 | `start_screen_cast` | 开始屏幕投屏（dlna / browser 两路） | SC |
| 54 | `stop_screen_cast` | 停止屏幕投屏，报告电视是否真收到 Stop | SC |
| 55 | `screen_cast_status` | 取屏幕投屏状态（含拉流统计与观众数） | SC |
| 56 | `set_ffmpeg_path` | 手动指定 FFmpeg 路径 | SC |
| 57 | `ffmpeg_status` | FFmpeg / FFprobe 检测报告（缺什么、装法、下载进度）| SC |
| 58 | `ffmpeg_download_start` | 起一次 FFmpeg 后台下载，立即返回 | SC |
| 59 | `ffmpeg_download_status` | 轮询 FFmpeg 下载任务 | SC |
| 60 | `open_ffmpeg_dir` | 在系统文件管理器里打开程序自己的 FFmpeg 目录 | 壳 |
| 61 | `export_logs` | 弹保存对话框导出 Python 侧日志文件 | 壳 |
| 62 | `get_logs` | 读日志尾部并按模块 / 级别 / 关键字过滤 | SC |
| 63 | `set_log_level` | 运行时改日志级别 | SC |
| 64 | `is_store_version` | 判定是否微软商店（MSIX）版本 | 壳 |
| 65 | `get_autostart` | 读开机自启状态（MSIX / 经典两套实现） | 壳 |
| 66 | `set_autostart` | 写开机自启状态，成功后 emit `autostart-changed` | 壳 |
| 67 | `notify_os` | 发一条系统通知（macOS 走 `UNUserNotificationCenter`，其余平台回 `Err` 让前端退回插件） | 壳 |

**8 个事件**（详见 §3.1）：`ready`、`device_found`、`device_lost`、`cast_state_changed`、`cast_confirm_request`、`screen_cast_state_changed`、`update-available`、`autostart-changed`。

---

## 8. ⚠️ 未验证项

以下三条无法只靠读代码得出结论，标注原因：

1. **`ready` 事件是否有壳层消费者** —— ⚠️ 未验证。已确认 `src/` 下无 `listen('ready')`（§3.1），且 `sidecar.rs` 里事件是被无条件 `app.emit` 转发的（`sidecar.rs:358-361`），但壳层 `new()` 里是否用别的方式等待就绪（例如 `send_command` 重试）没有逐行追完；从 `lib.rs:1226` 那句「睡 3 秒等 sidecar 就绪」推断，握手事件在壳层大概率被忽略，但这是推断而非实证。
2. **`set_mpv_path` 热替换渲染器后的实际可用性** —— ⚠️ 未验证。`commands.py:1010-1036` 的实现完整（新建 renderer → `protocol.set_renderer` → `start()` → 关旧的），但前端没有任何调用点，无法确认这条路径在当前版本仍被使用还是已成为死代码。
3. **MSIX 分支的运行时行为** —— ⚠️ 未验证。`get_autostart_msix` / `set_autostart_msix`（`lib.rs:1022-1067`，非 Windows 的 stub 在 `:1053-1070`）依赖 WinRT `StartupTask` 与 `AppxManifest.xml` 里的 `uap10:Parameters`，本次校对在 macOS 上进行，未读 `AppxManifest.xml`、也未在 Windows 上实跑，只核对了 Rust 侧代码路径与错误字符串。
