# HanCast — 前端 API 参考

> 回答一个问题：前端 `invoke()` 出去的每一条命令，究竟由壳层（Rust/Tauri）就地执行、还是转发给 Sidecar（Python 后台进程）；它的入参、返回结构、可能错误，以及 Sidecar 主动推给前端的事件分别是什么。
>
> 最后校对：2026-11-06，对应版本 v2.4.13（本轮 v2.4.12 未发布改动见下）。本轮（应用内更新，v2.4.12 未发布）：**更新不再把用户打发到网页**。Sidecar 新增 `update/` 模块（`update/__init__.py:28-52` 转出 10 个名字，实现在 `update/manager.py`，884 行：Release 清单取数 → 本平台产物挑选 → 带 sha256 校验 / 断点续传 / 可取消的下载 → `install_plan`）；`check_update` 的返回体补 `error/ignored/via/platform/assets/selected/no_asset_for_platform`，老六键一个没动（§2.9）；新增 5 条 Sidecar 命令 `update_release_info`/`update_download_start`/`update_download_status`/`update_download_cancel`/`update_install_plan`（`commands.py:228-232`）＋ 7 条 Rust 命令（上面 5 条的转发 + `update_apply` + `open_update_dir`，后两条才真的动手装与开目录）；**gitee 那条更新源删除**、`check_update` 不再下发 `sources`；命令总数 88→**95**、Sidecar 注册 81→**86**、前端封装 87→**94**、Sidecar 发出的事件名 9→**10**（新增 `update_download_done`：转发前端 8→9、前端监听 9→10；顺带把上一版漏账的 `remote_command` 补回 §3.1 表里）；组件 24→23（`DownloadModal.vue` 删除）、locale 各 456→**492** 键、`commands.ts` 多出 `?mockUpdate=1|slow|fail` 三个 dev 开关。后台参数**没有新增**（应用内更新用的 `github.mirror_enabled` / `github.mirror_prefix` 是既有两条，仍在 network 分组，32 项不变）。上一轮（手机镜像，v2.4.12 未发布）：新增 6 条 `mirror_*` 命令（§7 #83-88）与 `mirror_state_changed` 事件（§3.1），命令总数 82→88、Sidecar 注册 75→81、事件 8→9（当时的口径，漏了 `remote_command`，本轮已按代码更正）、前端封装 81→87。上一轮（v2.4.11）：新增 `frontend_log`（前端错误写进同一份 `hancast.log`，§2.8 四条），`remote_status` 返回的 `state` 补 `devices` 全量设备表、页面由自动轮询改手动 `refresh_devices`，命令总数 81→82、Sidecar 注册 74→75。上一轮（v2.4.10）：§7 遥控器三条补 token 落盘复用语义（地址长期不变、stop 不删配置键、首页弹窗先 `remote_status` 再补 start）。

---

## 0. 统计

| 项 | 数量 | 出处 |
| --- | --- | --- |
| **命令总数** | **95** | src-tauri/src/lib.rs:2024-2120（`generate_handler!` 里的条目逐个点名数出来；本轮补进注册表的更新命令是：`update_release_info`(:2104) / `update_download_start`(:2105) / `update_download_status`(:2106) / `update_download_cancel`(:2107) / `update_install_plan`(:2108) / `update_apply`(:2109) / `open_update_dir`(:2110)，88→95） |
| ├─ 壳层就地完成主要动作 | 12 | **9 条完全不碰 Sidecar**：`minimize_window`(lib.rs:155-157) / `close_window`(:160) / `read_clipboard`(:567) / `write_clipboard`(:574) / `notify_os`(:1450，macOS 走 `UNUserNotificationCenter`，其余平台回 `Err` 让前端退回通知插件) / `export_logs`(:1516) / `is_store_version`(:922) / `get_autostart`(:1639) / `set_autostart`(:1652)。**3 条「先问 Sidecar 拿数据、动手在壳层」**：`open_ffmpeg_dir`(:1419-1445，下发 `ffmpeg_status`)、本轮新增 `update_apply`(:1334-1378，下发 `update_install_plan`)、`open_update_dir`(:1382-1413，下发 `update_download_status`)。⚠️ 上一版这里写「10 + 77 = 88」，两数相加只有 87，本来就对不上；本轮按「12 就地 + 80 直接转发 + 3 经 `run_plugin_task` = 95」重算，三种数法都写出口径 |
| ├─ 直接转发 Sidecar | 80 | 命令体内就是一句 `sidecar.send_command(...)`（本轮新增的 `update_release_info` / `update_download_start` / `update_download_status` / `update_download_cancel` / `update_install_plan` 这 5 条属于这里） |
| └─ 经辅助函数转发 | 3 | `plugin_check_updates` / `plugin_pull` / `plugin_import`：改调 `run_plugin_task`（lib.rs:423-473）下发 `plugin_task_start`，再轮询 `plugin_task_result`、超时补 `plugin_task_cancel`，命令体里不直接出现 `send_command` |
| **事件总数** | **12 个事件名** | 见 §3.1：Sidecar 发出 **10** 个名字，其中 **9** 条转发前端（`browser_request` 只到壳层，`sidecar.rs:361-370` 那条专分支 `return`，不 `app.emit`）；壳层自加 **2** 条 ⇒ 前端**能收到 11** 个名字，实际 `listen` **订阅 10** 个（唯一没人订阅的是 `ready`；本轮新增的 `update_download_done` 由 `stores/update.ts:397-398` 订阅，收到就 `resumeDownload()` 立刻与后端对齐，省掉最多 1 秒空等） |
| 前端封装函数 | **94** | `src/api/commands.ts` 里 `export async function` 计数（本轮新增 7 个，都写在 commands.ts:1828 那段「应用内更新（v2.4.12）」分组里：`updateReleaseInfo` 1834-1844、`updateDownloadStart` 1847-1851、`updateDownloadStatus` 1854-1857、`updateDownloadCancel` 1860-1863、`updateInstallPlan` 1866-1869、`updateApply` 1878-1881、`openUpdateDir` 1884-1887）。94 与命令总数 95 差 1：`get_resolve_result` 没有独立封装（内联在 `resolveShare` / `pluginTest` 的轮询里），而 `plugin_import` 是一对三封装 |
| └─ 一对多封装 | 1 | `plugin_import` → `pluginImportFile` / `pluginImportData` / `pluginImportUrl` |
| Sidecar 命令注册表条目 | **86** | `hancast-backend/hancast_sidecar/commands.py:159-283`（`self._commands` 那个 dict 的起止；条目本身 161-282，逐条点名 86 条。本轮新增 5 条 `update_*` 在 commands.py:228-232，81→86） |
| 后台参数（`params.py`）条目 | 32 | `hancast-backend/hancast_sidecar/params.py:41-265`（**本轮没有新增**：应用内更新用的 `github.mirror_enabled` / `github.mirror_prefix` 是既有两项，仍在 network 分组） |

**86 / 95 / 83 这三个数各自的口径**（别拿其中一个当另一个）：① **95** = Rust 命令数，`generate_handler!` 条目数（lib.rs:2024-2120）；拆法是 12 条就地（9 条不碰 Sidecar + 3 条混合）+ 80 条命令体内直接 `send_command` + 3 条经 `run_plugin_task`。② **83** = `src-tauri/src/` 全树 `send_command("…")` 字面量**去重**后的 Sidecar 命令名数 = 79（出现在命令体里的名：`parse_media_file` 与 `parse_media_url` 归并成一条 `parse_media`；`ffmpeg_status` / `update_download_status` / `update_install_plan` 被混合命令复用了一次）+ 3（`run_plugin_task` 里的 `plugin_task_start` / `plugin_task_result` / `plugin_task_cancel`）+ 1（`browser.rs:194` 的 `browser_result`）。③ **86** = Sidecar 注册表条目数（commands.py:159-283）。与 ② 的差 **3** 条 = `plugin_import` / `plugin_check_updates` / `plugin_pull`：自 v2.3.20 起改走 `plugin_task_start` 的 operation，注册项保留只为「直发仍可用」。83 + 3 = 86 ✓。此外 `exit` 既不在 86 也不在 83 里——它不走注册表，由主循环直接拦下（`main.py:126-128`）。gitee 那条源本轮从更新链路彻底删除（`gitee.com/buxiangqumingzi/han-cast` 是原作者那条线，`releases/latest` 停在 v2.1.0，GitHub 一不通就顶上来，用户于是被发到原作者的下载页）；壳层与 Sidecar 里现在都没有 gitee 字面量，更新链路只剩 GitHub 那几个主机（`update/manager.py:33-42`），取字节一律过 `ghmirror.candidates()`（清单 `:391`、下载 `:657`）。

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
src-tauri/src/lib.rs       壳层：#[tauri::command] 函数（**95 条**，lib.rs:2024-2120）
        │                   ├─ 12 条就地完成主要动作（剪贴板 / 窗口 / 托盘自启 / 日志导出 / 商店判定 / 通知 / `open_ffmpeg_dir` / 本轮新增 `update_apply`、`open_update_dir`：这两条先问 Sidecar 拿安装计划或目录，真的动手在壳层）
        │                   ├─ 80 条 → sidecar.send_command("cmd", json!({...}))
        │                   └─ 3 条（plugin_import / plugin_check_updates / plugin_pull）→ run_plugin_task → plugin_task_start
        ▼
src-tauri/src/sidecar.rs   SidecarManager：stdin 写一行 JSON，oneshot 等一行 JSON
        │  子进程 stdin / stdout（行分隔 JSON，UTF-8）
        ▼
hancast_sidecar/main.py    for line in sys.stdin → handler.execute(cmd, params)
        │
        ▼
hancast_sidecar/commands.py  CommandHandler._commands 注册表（**86 条**，commands.py:159-283）
```

Sidecar 的启动方式：开发态 `uv run python -m hancast_sidecar.main`（`sidecar.rs:155-172`），打包态用 Nuitka 产物 `hancast-sidecar*`（`sidecar.rs:173-213`，macOS 在 `Contents/MacOS/` 下找，Windows/Linux 在 `resource_dir` 下找，`sidecar.rs:44-129`）。找不到二进制时 `setup()` 直接中止启动（lib.rs:1848-1851，注释就写着「窗口一闪就退」的由来）。

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
| 前端 `resolveShare` / `pluginTest` 轮询 | 45s，每 400ms 一次 | `commands.ts:813-823`、commands.ts:1127-1128 |
| 并发解析任务 | 最多 3 个 pending | commands.py:710-736 |
| 解析任务结果保留 | 完成后 10 分钟过期清理 | commands.py:706-729 |
| 更新清单 HTTP | Release 列表单次 GET **8s** | `update/manager.py:324`（`_http_json` 默认值）；历史一句：v2.4.11 及之前这一行是「GitHub API 1 秒 / Gitee 3 秒」（`commands.py:1025-1043` 那份旧实现），1 秒只为「GitHub 不通时别拖住启动自检」，gitee 那条源本轮已删 |
| 安装包下载 | connect **15s** / 单次读 **60s**，单包上限 512 MB | `update/manager.py:47-50`、`:763` |
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

（src/stores/device.ts:149、`src/stores/logs.ts:55`；`src/components/PluginSettings.vue:39` 用的是另一种写法 `String(err?.message ?? err)`）

壳层自己也会在写入阶段失败，这类错误与 Sidecar 无关：

- `Sidecar process not running`（`sidecar.rs:390-392`，child 已被 take，通常是进程已退出）
- `Failed to write to sidecar: <e>`（`sidecar.rs:394-396`，stdin 管道断了）

超时与进程退出是两条独立的错误消息：

- `Sidecar did not respond to '<cmd>' within 60s`（`sidecar.rs:416-419`）
- `Sidecar process exited unexpectedly`（`sidecar.rs:409-413`，oneshot 发送端被 drop；进程退出时 `reader_loop` 还会 drain 掉所有 pending 并逐个送这条错误，`sidecar.rs:333-341`）

还有一类只出现在 Python 侧的错误：命令名不在注册表里 → `Unknown command: <cmd>`（commands.py:270-273）。正常路径不会触发，改协议时容易踩。

### 1.4 事件如何从 Python 走到前端

```
protocol_io.emit_event("device_found", {...})          # commands.py:401
  → stdout 一行 {"event":"device_found","data":{...}}
  → SidecarManager::handle_stdout_line                 # sidecar.rs:345-368
      if val["id"] 存在 → 派发给 pending oneshot（是响应）
      elif val["event"] 存在 → app.emit(event_name, data)   # sidecar.rs:358-371
  → Tauri 事件总线
  → 前端 const { listen } = await import('@tauri-apps/api/event')
     await listen<Device>('device_found', e => ...)     # stores/device.ts:163
```

关键点：**事件名不被壳层白名单过滤**，Python 里 `emit_event` 写什么名字，前端就能 `listen` 到什么名字——唯一的例外是 `browser_request`，它在 `sidecar.rs:361-370` 被一条专门分支转给离屏渲染窗口并 `return`，**不 `app.emit`**（所以它不出现在前端能收到的清单里）。本轮新增的 `update_download_done` 因此不需要动壳层就到得了前端（`update/manager.py:708-713`）：`stores/update.ts:380-381` 收到它立刻 `resumeDownload()`，把「下载完了」这件事从「等下一次 1 秒轮询」提前到事件到达那一刻，轮询照旧兜底（进度、失败、取消都只认任务表）。壳层自己也会发事件（`autostart-changed` lib.rs:1654/1749、`update-available` lib.rs:1931），走的是同一个 `app.emit`，前端无法从名字上区分来源。

壳层还有一个**自监听**：`listen("autostart-changed", ...)`（lib.rs:1413），用于托盘菜单勾选态同步 —— 也就是这条事件同时有 Rust 和前端两个订阅者。

### 1.5 参数命名

Tauri 2 会把 JS 侧的 camelCase 自动映射到 Rust 的 snake_case 形参。约定：

- 前端传 `{ deviceId }`，Rust 形参写 `device_id: Option<String>`（例：lib.rs:541-542）。
- 但 **Rust → Sidecar 的 params 用 snake_case 原样传**：`json!({"device_id": device_id})`。Sidecar 里读的是 `params["device_id"]`（commands.py:578）。两套命名在壳层交接，改一处要跟着改另一处。
- `Option<T>` 形参在前端可以整个省略（`invoke('stop_cast', {})`，commands.ts:1216），Sidecar 用 `params.get(...)` 读，缺省即 `None`。

### 1.6 浏览器开发模式的 mock

`initInvoke()`（`commands.ts:32-48`）只有在 `window.__TAURI_INTERNALS__?.metadata` 存在时才用真 `invoke`，否则回落到 `mockInvoke`（`commands.ts:529-1110`）—— 后者是内存态假实现，**只覆盖部分命令**，其余走 `default: return {} as T`（`commands.ts:1107-1108`）—— 纯浏览器里任何未 mock 的命令都会「成功」返回空对象，不会报错。排查「浏览器里好好的、Tauri 里坏了」时先看这里。

v2.4.12 起更新链路自带三个 mock 开关（`commands.ts:337-346`）：默认 `mockUpdateMode = 'off'`，`check_update` 回「已是最新版」，与其他流程互不干扰；URL 加 **`?mockUpdate=1`**（或 `=available`）演「有新版」的整条链路（挑产物 → 下载进度 → 校验 → 装），假产物清单在 `commands.ts:359-380`（假任务表 `mockUpdateTask()` 在 `:382-426`），按 `navigator.userAgent` 猜平台——Linux 给 AppImage / deb / rpm / Arch 四选一，正好用来试 `kind: 'command'` 那条手动分支；**`?mockUpdate=slow`** 把进度上限压到 45%（`commands.ts:408-412`）好验「取消」；**`?mockUpdate=fail`** 让第 3 次轮询起回 `status: 'error'` 带截断原文（`commands.ts:400-406`）。假远端版本刻意写成 `MOCK_UPDATE_REMOTE = '2.5.0'`（`commands.ts:347-349`）而非常量内联，免得被 `scripts/sync-version.cjs` 当版本号刷掉。浏览器模式下没有壳层：`update_install_plan`（`commands.ts:894-905`）与 `update_apply`（`commands.ts:906-926`）都按「要提权就回 `manual`」那条形态演，**不当成安装成功**；`open_update_dir` 固定回 `{ok:true, path:'/mock/updates'}`（`commands.ts:927-928`）。另外「发布页」那颗按钮在浏览器里也验得到：`openReleasePage()` 先试 `plugin-shell.open`，动态 import 失败才退回 `window.open`（`stores/update.ts:357-370`）。各分支落点：`check_update` 836-862、`update_release_info` 863-872、`update_download_start` 873-883、`update_download_status` 884-888、`update_download_cancel` 889-893。

---

## 2. 命令全表

约定：
- 「执行层」= **壳层**（Rust 就地完成，Sidecar 不参与）/ **Sidecar**（转发 Python）。
- 「可能的错误」列里标 **通用** 的，指 §1.3 那三条：`Sidecar did not respond to '<cmd>' within 60s`、`Sidecar process exited unexpectedly`、`Missing field: '<key>'`。壳层命令不写「通用」。
- 「行号」列：壳层定义 / Sidecar 处理函数。

### 2.1 设备发现与管理（11 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `get_devices` | `getDevices()` commands.ts:1115 | 无 | `Device[]`；已剔除隐藏设备（commands.py:347-350） | Sidecar | 通用 |
| `refresh_devices` | `refreshDevices()` commands.ts:1120 | 无 | `Device[]`；先 `ssdp.scan()` 再取（commands.py:353-354） | Sidecar | 通用 |
| `set_default_device` | `setDefaultDevice(id)` `commands.ts:1125` | `id: string` | `void`（壳层丢弃 data，`lib.rs:179-180`） | Sidecar | 通用 |
| `rename_device` | `renameDevice(id, name)` `commands.ts:1130` | `id: string`、`name: string` | `void` | Sidecar | 通用 |
| `remove_device` | `removeDevice(id)` `commands.ts:1135` | `id: string` | `void`；同时从持久化的手动设备列表摘掉同 IP 条目（commands.py:369-378） | Sidecar | 通用。⚠️ 该封装在 `src/` 下**无调用点** |
| `hide_device` | `hideDevice(id)` `commands.ts:1140` | `id: string` | `void`；SSDP 仍能发现，只是不再推给前端（commands.py:397-400） | Sidecar | 通用 |
| `add_device` | `addDevice(address)` commands.ts:1168 | `address: string`，接受 `192.168.1.68` / `ip:port` / 完整描述 URL | `Device`（commands.py:439） | Sidecar | `请输入设备 IP 或描述地址`（commands.py:388、`ssdp.py:1246`）；`无法识别的地址：<target>`（`ssdp.py:1289`）；`该地址不能投屏：设备描述里必须声明 AVTransport 服务（电视/盒子/带投屏的音箱才有，路由器与 NAS 没有）`（`ssdp.py:1252-1256`，v2.3.13 起手动添加与自动发现走同一条准入）；`设备 <名称> 已被隐藏，请先取消隐藏`（`ssdp.py:1261`）；描述抓取失败原因（`HTTP <code>` / `响应中没有 UPNP 设备描述` / 异常原文，`ssdp.py:1310-1321`） |
| `add_local_device` | `addLocalDevice()` commands.ts:1174 | 无 | `Device`；地址由后端 `_get_local_ip()` + DLNA 实际端口拼出，并顺手把本机写进 Device Guard 信任列表（v2.4.7，commands.py:478） | Sidecar | 通用；自己投自己用，不写 `manual_devices` |
| `get_manual_devices` | `getManualDevices()` commands.ts:1180 | 无 | `string[]`（`config.manual_devices`，commands.py:394-394） | Sidecar | 通用 |
| `unhide_device` | `unhideDevice(id)` | `id: string`（UDN） | `void`；从 `hidden_devices` 摘掉该 UDN（v2.4.4，`commands.py` `_unhide_device`） | Sidecar | 通用 |
| `get_hidden_devices` | `getHiddenDevices()` | 无 | `{udn,name,ip,device_type,online,is_self}[]`（按 UDN 回查 SSDP 富化；查不到退回 UDN 尾段且 `online=false`，v2.4.4） | Sidecar | 通用 |
| `remove_manual_device` | `removeManualDevice(target)` | `target: string`（地址或 IP） | `void`；从 `manual_devices` 删一条，幂等（不存在只记 debug 不落盘，v2.4.4） | Sidecar | `要删除的地址不能为空` |

壳层定义：`lib.rs:164-271`。Sidecar 处理函数：commands.py:347-412。

`Device` 的键名坑见 §6.1。

### 2.2 投屏控制（9 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `start_cast` | `startCast(deviceId, mediaUri, mimeType?)` commands.ts:1608 | `deviceId: string`、`mediaUri: string`、`mimeType?: string` | `void`；成功后 Sidecar 立即补发一条 `cast_state_changed`（commands.py:694，就在 `_start_cast` 函数体里，def 在 commands.py:645） | Sidecar | `Device not found: <device_id>`（commands.py:541）；`session.play()` 抛出的控制点异常原文（`ControlUrlUnavailable` / `RendererUnresponsive`，`protocol/dlna.py:119-126`）。本地文件路径会被 Sidecar 换成媒体服务器 HTTP 地址（commands.py:520-545） |
| `stop_cast` | `stopCast(deviceId?)` commands.ts:1629 | `deviceId?: string`（省略 = 停全部会话） | `{ ok: true, stopped: string[], control_failed: string[], reasons: Record<string,string> }`（commands.py:585-611） | Sidecar | 命令本身**不抛错**（幂等，先摘会话再发 SOAP Stop，commands.py:574-585）；「电视没收到 Stop」体现在 `control_failed` / `reasons` 里，不是异常 |
| `pause_cast` | `pauseCast(deviceId?)` commands.ts:1634 | `deviceId?: string` | `void` | Sidecar | 通用；无会话时回落到主渲染器 `self.protocol.pause()`（commands.py:606-627） |
| `resume_cast` | `resumeCast(deviceId?)` commands.ts:1639 | `deviceId?: string` | `void` | Sidecar | 通用；同上回落逻辑（commands.py:614-635） |
| `seek_cast` | `seekCast(position, deviceId?)` commands.ts:1644 | `position: string`（`HH:MM:SS`）、`deviceId?: string` | `void` | Sidecar | 通用（`Missing field: 'position'`） |
| `get_cast_state` | `getCastState(deviceId?)` commands.ts:1649 | `deviceId?: string` | **两种形状**：带 `deviceId` → `CastState`；不带 → `{ sessions: Record<deviceId, CastState>, current: CastState }`（commands.py:632-651） | Sidecar | 通用 |
| `get_cast_url` | `getCastUrl(deviceId?)` commands.ts:1654 | `deviceId?: string` | **两种形状**：带 `deviceId` → `{device_id, device_name, url, title, duration, position, status, cast_state}`（`session.py:137-152`，`status` 小写）；不带 → `{url, title, duration, position, status}`（commands.py:650-671，`status` 是 DLNA 大写 `PLAYING`/`STOPPED`） | Sidecar | 通用 |
| `set_volume` | `setVolume(volume, deviceId?)` commands.ts:1659 | `volume: number`（u32）、`deviceId?: string` | `number`（壳层 `as_u64()` 兜底回入参值，lib.rs:612-618） | Sidecar | 通用 |
| `set_mute` | `setMute(muted, deviceId?)` commands.ts:1664 | `muted: boolean`、`deviceId?: string` | `void` | Sidecar | 通用 |

壳层定义：lib.rs:520-631。Sidecar 处理函数：commands.py:540-670。

多设备语义：每个设备一个 `DeviceCastSession`（`models/session.py:14-26`），会话内 1 秒轮询一次进度（`session.py:223-307`）；连续 10 次轮询失败即视为掉线（`session.py:25`，`POLL_FAIL_LIMIT`）。前端 `stores/cast.ts` 另有自己的 1 秒轮询（`cast.ts:211-269`），两条轮询链路并存。

壳层自己也在**每秒调一次 `get_cast_url`**（不带 `deviceId`），只为刷新托盘菜单「复制当前投屏地址」项（lib.rs:1404-1450）。这条流量不经过前端封装。

### 2.3 媒体解析与粘贴（4 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `parse_media_file` | `parseMediaFile(filePath)` `commands.ts:1192` | `filePath: string` | `MediaInfo`（`models/media.py:22-39`） | Sidecar | 通用；解析异常原文。壳层把它改写成 Sidecar 的 `parse_media` + `{"type":"file","path":...}`（lib.rs:274-276） |
| `parse_media_url` | `parseMediaUrl(url)` `commands.ts:1197` | `url: string` | `MediaInfo` | Sidecar | 同上，改写成 `{"type":"url","url":...}`（lib.rs:301-304）。⚠️ 该封装在 `src/` 下**无调用点** |
| `resolve_share` | `resolveShare(text, quality?)` `commands.ts:1237` | `text: string`、`quality?: string` | **异步任务**：`{ task_id: string, status: "pending" }`（commands.py:769） | Sidecar | `粘贴内容为空`（commands.py:737）；`已有 3 个解析任务进行中，请稍后再试`（commands.py:737）；通用 |
| `list_folder_media` | `listFolderMedia(folderPath)` commands.ts:1208 | `folderPath: string`（壳层 `folder_path`，lib.rs:292） | `{ folder: string, files: MediaInfo[] }`，按文件名**自然序**（`第2集` 在 `第10集` 前） | Sidecar | `文件夹路径为空`；`Not a folder: …`（`parser.py:37`）；`无法读取文件夹: …`。只列清单**不探时长**（避免上百集串行 ffprobe），只列当前层文件、跳过子目录/隐藏文件/`._` 资源叉/不支持扩展名（`parser.py:27-76`）。**新增于文件夹投屏**（播放队列初始顺序靠它） |
| `get_resolve_result` | 无独立封装，内联在 `resolveShare`（commands.ts:1237-1258）与 `pluginTest`（commands.ts:1558-1578） | `taskId: string` | `{ status: "pending" \| "done" \| "error", result?: ShareResolveResult, error?: string, ts: number }`（commands.py:737-757） | Sidecar | `解析任务不存在或已过期`（commands.py:762）；通用 |

壳层定义：lib.rs:273-302（含 `list_folder_media` lib.rs:292-302）。Sidecar 处理函数：commands.py:699-751（`_list_folder_media` 在 commands.py:1142，实现在 `media/parser.py:27-76`）。

`resolveShare` 的完整轮询契约（`commands.ts:802-823`）：先拿 `task_id`，每 400ms 调一次 `get_resolve_result`，45 秒兜底；`status === 'done'` 返回 `result`，`status === 'error'` 抛 `entry.error`，超时抛 `解析超时，请稍后重试`。若首次返回里**没有** `task_id`（浏览器 mock / 旧版实现），直接把首包当结果用（`commands.ts:808-811`）。

解析成功时的结果形状（`media/share_resolver.py:461`、`661`、`740`）：`{...MediaInfo, source_url, via}`，`via` 取值 `playlist` / `direct` / 命中的规则 id。失败时抛出的字符串带可操作提示，并写入解析失败台账（`share_resolver.py:377-381`）：

```
无法解析出可播放地址 — <host>: <原因>；…（可为该站点在 <rules_dir> 添加解析规则，或在设置页安装规则包）
```

### 2.4 屏幕投屏与 FFmpeg 依赖（9 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `list_screen_sources` | `listScreenSources()` commands.ts:1900 | 无 | `{ screens: ScreenSourceItem[], windows: ScreenSourceItem[], audio_inputs: AudioDeviceItem[], ffmpeg: {...}, platform: "darwin"\|"win32"\|"linux" }`（`ffmpeg_manager.py:486-508`） | Sidecar | 通用 |
| `start_screen_cast` | `startScreenCast(params)` commands.ts:1905 | `params: ScreenCastStartParams`，**整个对象直接作为 Sidecar 的 params**（lib.rs:834-834，前端 `{ params }` → Rust 形参 `params` 原样下传） | 成功：dlna → `{ok:true, target:"dlna", device_id, session_id, url}`；browser → `{ok:true, target:"browser", session_id, watch_url, width, height, fps}`（`manager.py:322-339`）。失败：`{ok:false, error}` | Sidecar | **校验类错误全部走 `{ok:false,error}` 而不是异常**（`manager.py:92-169`）：`已有投屏正在进行，请先停止`、`未知目标类型: <t>`、FFmpeg 不可用原文、`窗口源缺少窗口信息`、`未选择系统声音设备（…）`、`未选择麦克风设备`、`选中的<麦克风/系统声音>设备已不可用：<名>；请重新选择（…）`、`麦克风采到的全是静音…`、`缺少目标设备 (device_id)`、`设备不存在或已离线: <id>`、`HTTP 服务启动失败`、`会话注册失败`、`屏幕捕获失败`、`电视拒绝播放（…），可换较低分辨率/码率后重试`、`启动失败: <异常>` |
| `stop_screen_cast` | `stopScreenCast()` commands.ts:1910 | 无 | `{ok:true, control_failed: string[], reasons: Record<string,string>}`；本来就没在跑 → `{ok:true, already_stopped:true}`（`manager.py:204-229`） | Sidecar | 幂等，不抛错。dlna 目标会复用 `_stop_cast` 的 `control_failed` 语义 |
| `screen_cast_status` | `getScreenCastStatus()` commands.ts:1915 | 无 | `_state` 全量 + `viewers`（仅 browser 通道）+ `pull`（有 session_id 时）+ `ffmpeg`（`manager.py:231-241`） | Sidecar | 通用 |
| `set_ffmpeg_path` | `setFfmpegPath(path)` commands.ts:1920 | `path: string` | `{ok:true, path, version}` 或 `{ok:false, error}`（`ffmpeg_manager.py:448-462`）；**只能设不能清**，落盘键 `screencast.ffmpeg_path` | Sidecar | `缺少路径`（`manager.py:243-247`）；`文件不存在: <abs>`、`不是有效的 FFmpeg 可执行文件`（`ffmpeg_manager.py:450-454`） |
| `ffmpeg_status` | `ffmpegStatus(refresh)` commands.ts:1926 | `refresh?: bool`（用户点「重新检测」时传 `true`） | 检测报告：`ok / path / version / source / ffprobe_path / items[] / missing[] / optional_missing[] / managed_dir / platform / arch / install_commands[] / download / error`（`ffmpeg_deps.py:189-252`） | Sidecar | 缺依赖不算异常：`ok:false` + 中文 `error`，前端据此弹窗 |
| `ffmpeg_download_start` | `ffmpegDownloadStart()` commands.ts:1932 | 无 | `{ok:true, task:{status, stage, ...}}`；已在下 → `already_running` | Sidecar | 起不来后台线程时 `{ok:false,error}`；压缩包 >512MB 直接拒（`ffmpeg_deps.py:448-469`） |
| `ffmpeg_download_status` | `ffmpegDownloadStatus()` commands.ts:1937 | 无 | 后台任务快照（`status/stage/received_bytes/total_bytes/speed/path/error`），`Command_TIMEOUT=60s` 所以下载绝不占住命令循环（`ffmpeg_deps.py:470-474`） | Sidecar | 通用 |
| `open_ffmpeg_dir` | `openFfmpegDir()` commands.ts:1943 | 无 | `{ok:true, path}`（回显目录，成败由调用方按「有没有窗口」判断） | **壳层就地**（lib.rs:907-926） | `Sidecar 未返回 FFmpeg 目录`、`目录创建失败: <e>`、`无法打开目录: <e>`；路径由 Sidecar 的 `managed_dir` 给出，**不接受前端传入**，否则等于开放「用资源管理器打开任意路径」；**只 spawn 不 wait**——`xdg-open` 活到文件管理器关闭为止，等它等于把按钮按死在命令循环里 |
| `notify_os` | `notify()` commands.ts:2034（内部先 `raiseIfHidden()` `:1600`） | `title: string`、`body: string` | `Ok(())` 或 `Err(msg)` | **壳层就地**（lib.rs:999；macOS 实现在 `src-tauri/src/macos_notify.rs`，命令里 `run_on_main_thread` 排到主线程再调 UN） | macOS 用 `UNUserNotificationCenter`——插件那条路走已废弃的 `NSUserNotificationCenter`，实测只入通知中心不弹横幅；**非 macOS 一律回 `Err("本平台由前端走通知插件")`**，前端 catch 后改调 `tauri-plugin-notification`。通知失败从不影响投屏本身 |

壳层定义：lib.rs:825-926。Sidecar 注册 commands.py:192-198、处理函数 commands.py:1221-1231，投屏实现在 `screencast/manager.py`、依赖检测与下载在 `utils/ffmpeg_deps.py`。

边界：屏幕投屏是**把本机画面推给别的设备**，与「接收投屏」（本机作为 DLNA 渲染端，靠 MPV）完全是两套代码。dlna 通道与 browser 通道共用一份 HLS 输出，但分片时长的后台参数不同（`screencast.dlna_segment_seconds` 默认 2s，`screencast.browser_segment_seconds` 默认 1s，`params.py:43-53`）。每次 `start` 都生成新的 `session_id`（`secrets.token_hex(3)`，`manager.py:157`）和新的 URL，旧链接立即失效。

`_state` 的完整键集见 `manager.py:61-72`：`running / target / source / device_id / session_id / watch_url / url / viewers / error / started_at`。

### 2.5 投屏确认（Device Guard）（6 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `respond_cast_confirm` | `respondCastConfirm(requestId, approved, policy)` commands.ts:1730 | `requestId: string`、`approved: boolean`、`policy: string`（`"once"`/`"always"`/`"blacklist"`） | `boolean`（壳层 `as_bool().unwrap_or(false)`，lib.rs:722-728） | Sidecar | 通用（`Missing field: 'request_id'/'approved'`）；请求已超时/不存在时返回 `false` 而非报错（`guard.py:301-314`） |
| `get_guard_devices` | `getGuardDevices()` commands.ts:1739 | 无 | `{ trusted: GuardDevice[], blacklisted: GuardDevice[] }`（commands.py:1105-1127） | Sidecar | 通用 |
| `remove_guard_device` | `removeGuardDevice(deviceKey)` commands.ts:1744 | `deviceKey: string`，UDN 或 IP 都认（`guard.py:322-344`） | `boolean` | Sidecar | 通用 |
| `set_guard_policy` | `setGuardPolicy(deviceKey, policy)` commands.ts:1749 | `deviceKey: string`、`policy: "trusted" \| "blacklisted"`（commands.py:1142） | `boolean` | Sidecar | 通用；非法 policy 由 `guard.set_policy` 判定后返回 `false` |
| `get_guard_settings` | `getGuardSettings()` commands.ts:1757 | 无 | `{ enabled: boolean, confirm_timeout: number }`（commands.py:1123-1145，单位秒，默认 15，`guard.py:77`） | Sidecar | 通用 |
| `save_guard_settings` | `saveGuardSettings({enabled?, confirm_timeout?})` commands.ts:1762 | `settings: { enabled?: boolean, confirm_timeout?: number }` | `void` | Sidecar | 通用。**注意壳层不解包**：`.send_command("save_guard_settings", settings)`（lib.rs:914），Python 直接读 `params["enabled"]` / `params["confirm_timeout"]`（commands.py:1131-1152）—— 与 `save_settings`（会包一层 `{"settings": …}`，lib.rs:652）不一致 |

壳层定义：lib.rs:716-778。Sidecar 处理函数：commands.py:1111-1131，实现在 `security/guard.py`。

Device Guard 管的是**接收投屏**方向：外部设备往本机推流时，未知来源要先经用户确认。识别走三层降级：SSDP 缓存直达 → SSDP 单播探测 + TCP 端口探测并行（`guard.py:9-11`、`36-40`）。确认等待用 `threading.Event`，超时默认拒绝（`guard.py:274-299`）；`wait_for_confirm` 只对 `'always'` / `'blacklist'` 两种策略做持久化落库，`'once'` 不落。

### 2.6 规则包管理（11 条）

规则包 = JS 插件 zip；声明式规则 = 放在 `share_rules/` 目录下的 JSON 站点规则。这一组把两者都覆盖了。

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `plugin_list` | `pluginList()` commands.ts:1370 | 无 | `{ plugins: PluginInfo[] }`，前端解包成数组（commands.py:754-767） | Sidecar | 通用 |
| `rule_inventory` | `ruleInventory()` commands.ts:1417 | 无 | `{ rules_dir: string, script_packs: PluginInfo[], declarative: DeclarativeRuleInfo[] }`（`share_resolver.py:385-397`） | Sidecar | 通用。声明式规则 JSON 写坏时不报错，坏条目带 `error` 字段照样列出（`declarative.py:262-269`） |
| `failure_list` | `failureList()` commands.ts:1428 | 无 | `{ enabled, path, hosts: FailureHost[], total }`（`failures.py:167-190`） | Sidecar | 通用 |
| `failure_delete` | `failureDelete(host?, url?)` commands.ts:1434 | `host?: string`、`url?: string`（前端把空串转成 `null`，commands.ts:1069） | `{ removed: number, enabled, path, hosts, total }`（commands.py:770-793） | Sidecar | 通用 |
| `failure_clear` | `failureClear()` commands.ts:1441 | 无 | `{ removed: number, enabled, path, hosts, total }`（commands.py:774-799） | Sidecar | 通用 |
| `plugin_check_updates` | `pluginCheckUpdates(indexUrl?)` commands.ts:1529 | `indexUrl?: string`（空 → 走后台参数 `rules.index_url`） | `{ index_url, updates: PackUpdateEntry[], available: PackIndexEntry[], installed: number }`（`jsplugins.py:448-470`） | Sidecar（**经 `plugin_task_start` 轮询**，lib.rs:421-465） | `索引地址必须是 http(s)（当前：…）`、`索引拉取失败: 镜像 HTTP <code>；直连 <异常>`（`jsplugins.py:392-432`）；`规则包操作忙，请稍后重试`、`规则包操作超时，已请求取消`；通用 |
| `plugin_pull` | `pluginPull(name, indexUrl?)` commands.ts:1546 | `name: string`、`indexUrl?: string` | `{ plugins: PluginInfo[] }`（commands.py:901-917） | Sidecar（**经 `plugin_task_start` 轮询**） | `缺少包名`（commands.py:926）；`官方索引不可用，「<name>」也没声明更新地址：…`、`「<name>」既不在官方索引里，也没声明 update_url`（`jsplugins.py:468-492`）；`包校验失败（sha256 不匹配，可能是镜像过期）: <前12位>…`（`jsplugins.py:687`）；下载类错误见下 |
| `plugin_import` | `pluginImportFile(filePath)` `:797` / `pluginImportData(dataBase64)` `:803` / `pluginImportUrl(url)` `:809` | 三选一：`filePath` / `dataBase64` / `url`（壳层合成一个对象下传，lib.rs:504-515） | `{ plugins: PluginInfo[] }`（commands.py:918-930） | Sidecar（**经 `plugin_task_start` 轮询**） | `需要 path / data_base64 / url 之一`；`插件包超过 2MB 上限`、`不是合法的 zip 包: …`、`插件包文件数超过 128 上限`、`插件包解压后超过 8MB 上限`、`zip 内路径不合法: …`、`zip 内不允许的文件（只收 manifest.json 和 .js）: …`、`单文件超过 2MB: …`、`zip 内不允许软链接: …`、`插件包必须恰好含一个 manifest.json`、`manifest.json 不是合法 JSON: …`、`manifest scripts 条目必须是 .js 文件名: …`、`manifest scripts 引用了不存在的文件: …`（`jsplugins.py:580-641`）；`只支持 http(s) 下载地址`、`HTTP <code>`（`jsplugins.py:64-89`） |
| `plugin_remove` | `pluginRemove(name)` commands.ts:1394 | `name: string` | `{ plugins: PluginInfo[] }`（commands.py:811-836） | Sidecar | `插件不存在: <name>`（`jsplugins.py:465`）；通用 |
| `plugin_set_rule_enabled` | `pluginSetRuleEnabled(plugin, rule, enabled)` commands.ts:1400 | `plugin: string`、`rule: string`、`enabled: boolean` | `{ plugins: PluginInfo[] }`（commands.py:816-840） | Sidecar | 通用 |
| `plugin_test` | `pluginTest(plugin, text)` commands.ts:1558 | `plugin: string`、`text: string` | `{ task_id, status: "pending" }`，之后走 `get_resolve_result` 轮询（与 `resolveShare` 完全同一套，commands.ts:1127-1128） | Sidecar | `插件不存在: <name>`（`jsplugins.py:465`）；`粘贴内容为空`（commands.py:850）、`已有 3 个解析任务进行中，请稍后再试`（commands.py:737） |
| `plugin_task_start` | 无前端封装（壳层 `run_plugin_task` 内部调用，lib.rs:421-465） | `operation: "plugin_check_updates" \| "plugin_pull" \| "plugin_import"`、`params: object` | `{ task_id, status: "pending" }`（`tasks.py:165-190`） | Sidecar | `不支持的规则包任务: <op>`、`规则包操作忙，请稍后重试`（单执行零队列）、`规则包任务服务已关闭`（`tasks.py:146-150`） |
| `plugin_task_result` | 同上（轮询） | `task_id: string` | `{ status: "pending" \| "done" \| "error" \| "cancelled", result?, error? }`（`tasks.py:251-253`） | Sidecar | `规则包任务不存在或已过期`（终态保留 600s、上限 16 条）；通用 |
| `plugin_task_cancel` | 同上（壳层超时兜底） | `task_id: string` | 取消后的当前状态（`tasks.py:255-260`） | Sidecar | 提交已赢的任务取消不掉：返回 `pending`/`done` 而非 `cancelled`（`tasks.py:72-95`） |

壳层定义：lib.rs:342-489。Sidecar 处理函数：commands.py:767-826。

约定：**所有会改动插件仓库的命令（import / remove / set_rule_enabled / pull）都返回全量 `{plugins: [...]}`**，前端拿它直接刷新列表，不需要再调一次 `plugin_list`（commands.py:1205 的注释就是这个意思）。`rule_inventory` 是例外，它返回的是 `script_packs` 键。

规则包索引默认地址来自后台参数 `rules.index_url`（默认值 `DEFAULT_RULES_INDEX` `params.py:19-23`，登记表条目 `params.py:222-224`），`plugin_pull` 会按索引条目的 `sha256` 校验下载内容，**对不上就不落盘**（`jsplugins.py:682-688`）。

**GitHub 国内镜像开关**（`github.mirror_enabled`，`params.py:245-264`）：打开后，凡是
`github.com` / `raw.githubusercontent.com` / `api.github.com` / `codeload.github.com` /
`objects.githubusercontent.com` 这五个主机的地址，都会先经 `utils/ghmirror.py` 改写成
`<github.mirror_prefix><原始地址>` 的前缀形式去取（`ghmirror.py:1-21` 的模块说明就是这套规则）。
`candidates(url)` 返回「镜像 → 直连」两条路，调用点**逐条试、失败就退回直连**，且异常文案里
把两条路的失败原因都拼上（`jsplugins.py:332-369` 索引、`jsplugins.py:548-590` 包下载、
`ffmpeg_deps.py:_open_stream` 依赖下载，以及本轮新增的**应用内更新**两处：Release 清单 `update/manager.py:390`、安装包下载 `update/manager.py:656`）。
镜像只改**取字节的路径**：`plugin_pull` 仍按索引里的官方 sha256 校验，替换字节的镜像会被拒落盘
（`jsplugins.py:576-579`），退回直连重取一次才能把「镜像挂了」和「镜像给错东西」分开。
非 GitHub 主机（Gitee、站点自己的 CDN）原样通过。

### 2.7 设置与后台参数（5 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `get_settings` | `getSettings()` commands.ts:1670 | 无 | `{ usn, friendly_name, version, media_port, default_device, network_interface, settings }`（`utils/config.py:94-104`） | Sidecar | 通用 |
| `save_settings` | `saveSettings(settings)` commands.ts:1675 | `settings: Partial<AppSettings>`；壳层包成 `{"settings": …}`（lib.rs:652） | `void` | Sidecar | 通用（`Missing field: 'settings'`）。`friendly_name` 传空串会被静默改回当前值、不阻断其余项保存（commands.py:896-920） |
| `list_network_interfaces` | `listNetworkInterfaces()` commands.ts:1680 | 无 | `{ interfaces: NetworkInterface[], selected: string }`（commands.py:885-907） | Sidecar | 通用 |
| `get_params` | `getParams()` commands.ts:1686 | 无 | `{ specs: ParamSpec[] }`（**32 条**，`params.py:41-265`；`specs()` 给每条补 `value` 与 `overridden`，`params.py:271-279`），前端解包成数组 | Sidecar | 通用 |
| `save_params` | `saveParams(values)` commands.ts:1693 | `values: Record<key, number\|boolean\|string>`；壳层包成 `{"values": …}`（lib.rs:679） | `{ ok: boolean, errors: string[], specs: ParamSpec[] }`（commands.py:932-951）。`ok = errors.length === 0` | Sidecar | **不抛错**，逐项校验失败写进 `errors`：`未知参数: <key>`、`<中文标签>: <原因>`（`params.py:332-340`）。成功项照常落库，是部分成功语义 |

壳层定义：lib.rs:641-675。Sidecar 处理函数：commands.py:855-932。

后台参数 = `params.py` 里 `PARAM_SPECS` 暴露的可调项，落盘位置是 `hancast_setting.json` 的 `advanced` 键（`params.py:17`，`ADVANCED_KEY`）。**只保存与默认值不同的覆盖项**（`params.py:342-345`），改回默认值即删除覆盖，这样将来版本调默认值时用户能跟着走。

32 条按 section 分布：`screencast` 9、`discovery` 4、`player` 3、`http` 3、`browser` 3、`failures` 3、`media` 2、`rules` 2、`network` 2、`logs` 1。设置页分组标题按 section **首次出现顺序**排（`SettingsView.vue:111-115`），所以新加 section 要同时在 `src/locales/zh-CN.json` 与 `en-US.json` 补一条 `settings.paramsGroup.<section>`，否则界面上露出的是 i18n 键名。

`network` 段那两条（`github.mirror_enabled` / `github.mirror_prefix`）是**开关 + 前缀**一对：
开关默认关，前缀默认 `https://gh-proxy.com/`（`params.py:28`，`DEFAULT_GITHUB_MIRROR`）。
它们的消费点不在设置页而在 `utils/ghmirror.py`，每次请求现读，所以改完不用重启也不影响
已经在跑的解析任务；改写范围与回退语义见 §2.6 的「GitHub 国内镜像开关」一段。

改 `network_interface` 会触发 SSDP 在后台线程重绑网卡（约 1-2 秒），命令本身立即返回（commands.py:918-931）—— 前端 `saveSettings` resolve 之后设备列表可能还是旧网卡的结果。

### 2.8 日志（4 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `get_logs` | `getLogs(query)` commands.ts:1777 | `params: { limit?, module?, level?, keyword?, include_older? }`；壳层**不解包**，`params` 原样下传（lib.rs:878） | `{ lines: LogLine[], total, truncated, path, level, modules: LogModule[] }`（`utils/logger.py:207-215`）。`limit` 被夹到 1..5000（`logger.py:163`） | Sidecar | 通用。`level` 非法时不报错，等同于不过滤（`logger.py:166-167`） |
| `set_log_level` | `setLogLevel(level)` commands.ts:1782 | `level: string` | `{ ok: true, level: string }`（commands.py:1216-1239） | Sidecar | `未知日志级别: <level>`（`logger.py:131`）；通用 |
| `export_logs` | `exportLogs()` commands.ts:1772 | 无 | `boolean`：`true` = 已复制，`false` = 用户在保存对话框里取消（lib.rs:927-927） | **壳层** | `日志文件不存在`（lib.rs:914）；`无法获取 APPDATA 目录`（lib.rs:929）/ `无法获取 HOME 目录`（lib.rs:904、`819`）；`Invalid path: <e>`（lib.rs:929）；`复制日志文件失败: <e>`（lib.rs:931） |
| `frontend_log` | `frontendLog(entry)` commands.ts:1795 | `level?: "error" \| "warn" \| "info"`、`message: string`、`stack?: string`、`source?: string`；默认 `level=error`、`source=webview` | `{ ok: true }`（commands.py:1653 `_frontend_log`）。`message` 截 2000 字、`stack` 截 4000 字，落盘行首统一是 `[webview/<source>]`（v2.4.11） | Sidecar | **没有**——前端侧整段 `try/catch` 吞掉异常：这个通道是在错误处理里被调用的，它自己再抛就会引发新错误 |

壳层定义：lib.rs:873-881（前两条）、lib.rs:891-928（`export_logs`）。

日志过滤按**模块名前缀**匹配：选 `hancast.screencast` 时 `manager` / `httpd` / `capture` 一并命中（`logger.py:157-158`、`196-197`）。`modules` 由 11 个已知模块标签（`logger.py:114-126`）加上日志尾部里实际出现过的其他模块名合并而成（`logger.py:219-240`）。多行堆栈会挂到上一条的 `msg` / `raw` 上（`logger.py:189-193`）。

**两个 `hancast.log` 是不同的文件**（§6.7）：`export_logs` 与 `get_logs` 读的都是 Python 侧那份（macOS `~/Library/Application Support/HanCast/logs/hancast.log`，lib.rs:897-901、`logger.py:19-24`）；壳层 `SessionLogger` 自己写的是 `app_data_dir()/logs/hancast.log`（lib.rs:1206-1209），identifier 为 `com.han.hancast`（`tauri.conf.json:5`），路径不同，且**每次启动都会清空**（`lib.rs:131`）。

### 2.9 更新（9 条，v2.4.12 起含应用内下载与安装）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `check_update` | `checkUpdate(currentVersion, force?)` commands.ts:1816 | `currentVersion: string`、`force?: boolean`。**`sources` 入参已废弃**：v2.4.11 及之前壳层在这里硬编码注入 `"sources": ["github", "gitee"]`，gitee 那条源本轮从 Sidecar 删除后壳层不再下发（lib.rs:821-825 的注释记着原因），旧壳层继续传也只是被忽略（commands.py:1430-1449） | `{ has_update, current, latest, url, body, source, error?, ignored?, via?, platform{os,arch}, assets[], selected, no_asset_for_platform }`；老字段一个没动，`body` 仍截 500 字符（update/manager.py:454）；`source` 只可能是 `'github'` 或 `''`（update/manager.py:402、:433） | Sidecar | **不抛错**：取清单失败把原因放进 `error` 键（update/manager.py:424-434），前端类型现在有 `error?`（§6.6） |
| `ignore_update_version` | `ignoreUpdateVersion(version)` commands.ts:1821 | `version: string` | `boolean`；空串返回 `false`（commands.py:1450-1457） | Sidecar | 通用 |
| `update_release_info` | `updateReleaseInfo(version?)` commands.ts:1834 | `version: string`（壳层 `Option<String>`，空串 = 语义最新的已发布版，lib.rs:1107-1118） | `describe_release` 的产物清单：`tag / version / release_url / published_at / body(≤2000) / platform / assets[] / selected / no_asset_for_platform / via / source`（update/manager.py:174-217、:375-411） | Sidecar | `取版本清单失败：<镜像/直连两条路各自的原因>`（update/manager.py:406-410）；GitHub 限流原文 |
| `update_download_start` | `updateDownloadStart(asset?, version?)` commands.ts:1847 | `asset: string`（本平台清单里的名字，空 = 用 `selected`）、`version: string`（lib.rs:1120-1135） | 任务表快照（`UpdateTask`：`status/stage/task_id/version/asset/kind/url/dest/total/received/percent/speed/elapsed/via/resumed_from/sha256_expected/sha256_actual/verified/note/error`，update/manager.py:520-540）；已有 `pending` 任务时**幂等返回当前任务**（update/manager.py:491-493） | Sidecar | `<ver> 没有本平台（<os>）的安装产物`（update/manager.py:497-499）；`该版本不含产物 <name>`（:504-505）；`产物地址不是 http(s)：…` / `产物地址是明文 HTTP，已拒绝：…`（`_check_asset_url`（:585-602），两条原文在 :596 与 :600）；`创建下载目录失败：…`（:513-516） |
| `update_download_status` | `updateDownloadStatus()` commands.ts:1854 | 无 | 任务表快照；**空表也带 `dir`**（`update_dir()`，update/manager.py:549-560 的 docstring 就解释了为什么：「打开所在目录」在还没下载时就得能点），此时是 `{ "status": "idle", "dir": … }` | Sidecar | 通用 |
| `update_download_cancel` | `updateDownloadCancel()` commands.ts:1860 | 无 | 取消**请求**后的当前任务：进行中的写 `stage: "cancelling"`，已结束的按原样返回（update/manager.py:562-578）。终态由下一次 `update_download_status` 给出——真停的是下载线程（:758-759 每块检查一次取消位） | Sidecar | 通用 |
| `update_install_plan` | `updateInstallPlan()` commands.ts:1866 | 无 | `{ok,kind,asset_kind,path,asset,version,sha256,verified,auto}`，`kind:"command"` 时另带 `command / note_zh / note_en`（update/manager.py:834-875） | Sidecar | `还没有下载过安装包`、`安装包尚未就绪（当前状态：…）`、`下载记录还在，但文件已不在磁盘上…`、`认不出这个产物的安装方式：…`（update/manager.py:848-856） |
| `update_apply` | `updateApply()` commands.ts:1878 | 无 | 能自动装的产物：`{ ok: true, kind, path, log, helper }`，随后**本进程 600ms 后退出**（lib.rs:1367-1370）；要提权的产物：`{ ok: false, manual: true, plan }`（lib.rs:1354-1356） | **混合**：先下发 `update_install_plan` 拿计划（lib.rs:1339-1341），写脚本 / spawn 活过本进程的脚本 / 退出都在壳层 | `更新产物路径为空`（lib.rs:1352）；`写安装脚本失败: …` / `安装脚本不可执行: …`（lib.rs:1211-1216）；`启动安装脚本失败: …`（unix，lib.rs:1326）/ `启动安装程序失败: …`（windows，lib.rs:1315） |
| `open_update_dir` | `openUpdateDir()` commands.ts:1884 | 无 | `{ ok: true, path }`（回显目录；成败由调用方按「有没有开窗」判断） | **混合**（lib.rs:1381-1413）：目录**优先取 `update_download_status` 任务表的 `dir`**，只剩 `dest` 的旧 Sidecar 才退回它的父目录（lib.rs:1389-1401）；**不接受前端传路径**；`create_dir_all` 后 `open` / `explorer` / `xdg-open` 只 spawn 不 wait | `Sidecar 未返回更新产物目录`（lib.rs:1402）；`目录创建失败: …`、`无法打开目录: …` |

`check_update`（lib.rs:816-834）、`ignore_update_version`（lib.rs:837-845）、`update_release_info`（lib.rs:1108-1118）、`update_download_start`（lib.rs:1121-1135）、`update_download_status`（lib.rs:1138-1144）、`update_download_cancel`（lib.rs:1147-1153）、`update_install_plan`（lib.rs:1162-1168）、`update_apply`（lib.rs:1334-1378）、`open_update_dir`（lib.rs:1382-1413）。

**`update_install_plan` 这条曾经只有 Sidecar 侧、壳层漏了转发**（Sidecar 注册 commands.py:232、处理函数 commands.py:1599-1603）。后果按代码顺序推一遍：下载完 → `pollDownload` 调 `refreshPlan()`（`stores/update.ts:198`、`:278-298`）→ `invoke('update_install_plan')` 在打包版里「Command not found」→ `plan=null` → 弹窗主按钮在 `done` 分支按 `!plan.value` 置灰（`UpdateModal.vue:212-217）→ **「退出并安装」永远点不动**，而浏览器 mock 有这条 case，测试全绿看不出来。现在两头都补上了：① 壳层加了纯转发的 `update_install_plan`（lib.rs:1162-1168，lib.rs:1155-1159 那段注释就是这条坑的原文）；② 前端仍保留本地兜底——取装法失败时按任务表自己拼一份够用的计划（`stores/update.ts:283-293`，判据是 `AUTO_KINDS` = `nsis`/`dmg`/`appimage`，:24）。**两条都在，别再删其中一条**：删兜底等于把这条链交回一次网络往返，删转发则 `update_apply` 之外的路径再也拿不到 `command` 原文。

安装脚本模板在 lib.rs 里是四个 const：`WIN_APPLY`（lib.rs:1224-1229，`timeout /t 3` → `start "" /wait … /S` → 装成再拉起 → `del "%~f0"` 自删）、`MAC_APPLY`（lib.rs:1231-1271：`hdiutil attach` → 先 `mv` 旧 `.app` 到 `/tmp/HanCast_prev_<ts>.app` **备份** → `cp -R` 新包 → 任一步失败就 `mv` **回滚**并改为 `open` dmg 让用户手动拖 → 成功后 `touch "$DST"` 让 Launch Services 重新登记新 inode → `open`）、`DEV_FALLBACK`（lib.rs:1274-1278，倒推不出 `.app` 布局（`cargo run` 的 target/debug）时**只 `open` dmg，不替换任何东西**）、`LINUX_APPLY`（lib.rs:1280-1287，`chmod +x` + `nohup` 起新 AppImage，不需要 root）。写脚本 `write_apply_helper`（lib.rs:1177-1219，Windows 用 `hancast-apply.cmd`、其余 `hancast-apply.sh` 并 `chmod 0o755`），`.app` 路径倒推 `macos_bundle_path`（lib.rs:1291-1300），起进程 `spawn_survivor`（lib.rs:1305-1329：unix 侧 `process_group(0)`、Windows 侧 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`）——用 `app.shell().command()` 那条插件路径不行，壳层退出时插件会收拾它 spawn 的东西，而这里要的恰恰是「我们走了它还得继续」。日志落 `update-apply.log`，就在安装包旁边（lib.rs:1358-1360）。

`kind` 决定壳层怎么动（update/manager.py:834-844 的 docstring 就是这套分工）：`nsis` / `dmg` / `appimage` 在 `AUTO_APPLY`（update/manager.py:90）里，能自动装；`deb` / `rpm` / `arch` / `msi` 一律回 `kind: "command"` + 命令原文（update/manager.py:869-872、:877-884：`sudo apt install`、`sudo rpm -Uvh --replacepkgs`、`sudo pacman -U`、`msiexec /i`），**Sidecar 与壳层都不代跑 sudo**，由界面给复制按钮（`UpdateModal.vue:99-100`、:168-183、:309-312）。

版本号比较是**全项目唯一一处**实现，SemVer 元组含预发布位（update/manager.py:226，`parse_version`）：原来住在 `commands.CommandHandler._parse_version`，本轮随「检查」一起搬进 `update/`，`commands.py` 那边只剩一条调用。历史一句：更早的实现是 `re.findall(r'\d+', v)`，把 `2.1.0-beta.1` 解析成比正式版还大的元组，于是装了正式版的用户被推向 beta——这条教训写在 `update/manager.py:239-241`。

超时与预算（括号里是被本轮改掉、必须留痕的旧数字）：

| 环节 | 预算 | 出处 |
| --- | --- | --- |
| Release 清单单次 GET | 8 秒 | update/manager.py:324-339（`_http_json` 默认 `timeout=8.0`） |
| Release 列表翻页 | 最多 6 页 × 100 条 | update/manager.py:43、:350-372 |
| release 清单内存缓存 | 60 秒（`invalidate_release_cache()` 可清；手动「重新检查」也走这份缓存，与旧实现不同） | update/manager.py:55、:381-404、:467-470 |
| 安装包下载 | connect **15s** / 单次读 **60s** | update/manager.py:48-50（`TIMEOUT = (15, 60)`） |
| 单包体积上限 | 512 MB，超了立即断 | update/manager.py:45-47、:762-763 |
| 下载进度写入节流 | 0.4 秒 | update/manager.py:51、:765-771 |
| 前端进度轮询 | 1 秒一次（`POLL_MS`），与 FFmpeg 依赖下载同节奏 | `stores/update.ts:21`，两处起表在 :224（`startDownload`）与 :268（`resumeDownload`） |

（历史一句：这一节在 v2.4.11 及之前写的是「GitHub API 1 秒、Gitee 3 秒，手动检查最坏约 4 秒；GitHub 走镜像时那 1 秒换成 3 秒」（旧 `commands.py:1025-1043`）。1 秒那条预算是为「GitHub 不通时别拖住启动自检」设的；gitee 兜底源停在原作者的 v2.1.0，两条都随源删除——见 lib.rs:821-825 的注释。）

**版本清单来自 GitHub Releases 的列表接口**（`api.github.com/repos/pingod/HanCast/releases?per_page=100&page=`，update/manager.py:34-39），**不是 `/releases/latest`**：后者按「最近一次 publish」而不是语义版本给结果，2026-11-06 对账时实测踩中——为 v2.3.8 补一条无产物的说明 release，`/releases/latest` 当场回指 2.3.8，跑着 2.4.11 的客户端从此只报「已是最新版本」，界面一行错都不报。列表侧自己挑语义版本最大的**已发布且非 prerelease** 条目（update/manager.py:350-372，draft 由 GitHub 直接不给未登录用户）。下载产物按 GitHub 声明的 asset `digest`（`sha256:…`，update/manager.py:188-193 剥前缀）校验：续传可能跨两条路（镜像半份 + 直连半份），所以是**对最终字节整包重算**（update/manager.py:680-693），不符就丢弃半份文件并把两条路各自的原因写进 `error`；只有「取消」与「摘要不符」才真删 `.part`，断流/超时**留着**给下一次续传（update/manager.py:673-676 的注释解释了这条，代价是「每次都白下一半」）。GitHub 没声明摘要时不判死，但 `verified=false` + `note` 如实写「未做摘要校验」（update/manager.py:701-706）。

时序（三层，行号是 2026-11-06 校对时读数）：

```
前端 check_update / update_release_info   → commands._check_update（薄壳，commands.py:1430-1449）
   → update.check_for_update(update/manager.py:413) → get_release(:375) → ghmirror.candidates(:391)
       → _latest_release_raw(:351) 翻页取列表 → describe_release(:175) 挑本平台产物 / 架构
   ← { has_update, latest, url, body, source:'github', assets[], selected, platform,
       no_asset_for_platform, via?, error?, ignored? }
前端 update_download_start(asset)         → manager.start_download(:486)
   → _prune() 清旧安装包并保住本次的 .part(:604-637) → 起 UPDATE_DOWNLOAD 线程(:546)
   → _fetch() 带 Range 续传写到 .part(:727-783)，每 0.4s 写一次进度（:765-771）
   → 整包 sha256 比对(:681-694) → os.replace(:696) → status=done(:702-707)
   → protocol_io.emit_event("update_download_done", {version, asset, path, verified})  # update/manager.py:708-713
   ↳ 前端两条路同时收敛：1s 轮询（stores/update.ts:224）与这条事件（:397-398 → resumeDownload）
前端 update_apply（Rust）                 → update_install_plan(update/manager.py:834) → 写脚本 → spawn_survivor → 600ms 后 exit(0)
```

`update_download_done` 载荷只有 `version / asset / path / verified` 四个键；**失败与取消不发事件**，只在任务表的 `status/error` 里，界面靠轮询读到。

另有一条**不经过前端**的启动检查：壳层 spawn 一个后台任务（lib.rs:1917-1958），睡 3 秒等 Sidecar 就绪（lib.rs:1927），然后调一次 `check_update`（**不带 `sources`**，lib.rs:1938-1941 的注释就是这件事），命中就 `emit("update-available")`，载荷是整个返回体（lib.rs:1946）。微软商店版本直接跳过（lib.rs:1921-1925）。


### 2.10 剪贴板（2 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `read_clipboard` | `readClipboardText()` commands.ts:1586 | 无 | `string` | **壳层** | clipboard 插件错误原文（lib.rs:506）。封装层会 try/catch 掉，失败后回落 `navigator.clipboard.readText()`，再失败返回 `''`（commands.ts:1147-1175）——**调用方看不到异常** |
| `write_clipboard` | `writeClipboardText(text)` commands.ts:1602 | `text: string` | `void` | **壳层** | clipboard 插件错误原文（lib.rs:507-513）。这条**不降级**，失败直接抛给调用方 |

壳层定义：lib.rs:504-508。

存在理由写在两处文档注释里（lib.rs:495-499、lib.rs:502-509）：macOS 的 WKWebView 不授予页面 `clipboard-read` 权限，`navigator.clipboard.readText()` 会**静默 reject**，「粘贴」按钮因此失效；写入侧的 reject 也不受同步 `try/catch` 保护。这两条是纯壳层能力，Sidecar 完全不知道剪贴板的存在。

### 2.11 窗口与托盘（2 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `minimize_window` | `minimizeWindow()` commands.ts:1710 | 无 | `void` | **壳层** | 无。`window.minimize().ok()` 把错误丢掉（`lib.rs:153`） |
| `close_window` | `closeWindow()` commands.ts:1719 | 无 | `void` | **壳层** | 无。`window.hide().ok()`（`lib.rs:159`）——**不是退出**，是隐藏到托盘 |

壳层定义：`lib.rs:151-160`。

配套行为：原生关闭按钮走 `CloseRequested` → `prevent_close()` + `hide()`（lib.rs:1277-1278），与 `close_window` 效果一致。真正退出只有托盘菜单的 Quit：先 `sidecar.shutdown()` 再 `exit(0)`（lib.rs:1178-1179）。窗口最小尺寸 346×472（lib.rs:1240-1247）。启动参数带 `--autostart` 或 `--minimized` 时直接以隐藏状态启动（lib.rs:1236）。

托盘菜单项（复制投屏地址 / 显示窗口 / 开机自启 / 退出）由壳层 `update_tray_menu` 维护（lib.rs:1116-1170），**前端没有任何 API 能读写托盘**。前端能感知托盘的唯一途径是 `autostart-changed` 事件（§3.1）。

### 2.12 其他（系统集成 / 无前端封装）（5 条）

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `is_store_version` | `isStoreVersion()` commands.ts:1889 | 无 | `boolean` | **壳层** | 无。判定纯靠可执行文件路径是否含 `windowsapps` / `553787e6.hancast` / `build-msix`（lib.rs:788-794）；非 Windows 恒为 `false`。封装层 catch 后返回 `false`（commands.ts:1408-1409） |
| `get_autostart` | `getAutostart()` commands.ts:1699 | 无 | `boolean` | **壳层** | MSIX 路径：`获取启动任务失败: …` / `等待启动任务失败: …` / `获取启动任务状态失败: …`（lib.rs:950-951）；非 MSIX：`tauri-plugin-autostart` 的错误原文（lib.rs:1023） |
| `set_autostart` | `setAutostart(enabled)` commands.ts:1704 | `enabled: boolean` | `void`；成功后 emit `autostart-changed`（lib.rs:1110） | **壳层** | MSIX 路径：`请求启用启动任务失败: …` / `等待启用结果失败: …` / `禁用启动任务失败: …`（lib.rs:973-974）；非 MSIX：autolaunch 错误原文（lib.rs:1098-1100）。非 Windows 的 `MSIX 开机自启仅在 Windows 上可用`（lib.rs:1002）实际不可达 —— `is_store_version()` 在非 Windows 恒 `false`（lib.rs:983-987 的注释也这么说） |
| `check_mpv` | **无** | 无 | `{ status: "ready"\|"not_found"\|"error", path, version, source, error }`（`models/mpv.py:17-27`、commands.py:1137-1161） | Sidecar | 通用 |
| `set_mpv_path` | **无** | `path: string` | 同 `check_mpv`（commands.py:1510-1514）；成功时会热替换 MPV 渲染器（commands.py:1515-1526） | Sidecar | 通用 |

壳层定义：lib.rs:788-794（`is_store_version`）、lib.rs:1014-1039（自启）、lib.rs:806-811（MPV）。

开机自启有**两套实现**，按 `is_store_version()` 二选一（lib.rs:1004-1006、`925-935`、`938-957`）：MSIX 走 WinRT `StartupTask`（TaskId `HanCastStartup`，lib.rs:943，启动参数由 `AppxManifest.xml` 的 `uap10:Parameters="--autostart"` 提供，lib.rs:964），经典安装走 `tauri-plugin-autostart`。

`check_mpv` / `set_mpv_path` 已在 `generate_handler!` 里注册、Sidecar 也实现了，但 **`src/` 下没有任何封装或调用点** —— 属于「接收投屏」（本机作 DLNA 渲染端）方向的预留接口。

### 2.13 投屏失败台账与地址预检（3 条，v2.4.5）

投屏方向（出站）过去失败只在 DEBUG 级留痕，面板默认 INFO 一个字看不到——`COMP-005`（无后缀直链电视不拉流）就是这样查了大半天。v2.4.5 把出站动作收成 INFO 一行，并把失败写进本机台账（`media/cast_failures.py`，与解析失败台账 `media/failures.py` 对称）。

| 命令名 | 前端封装 | 入参 | 返回 | 执行层 | 可能的错误 |
| --- | --- | --- | --- | --- | --- |
| `cast_failure_list` | `castFailureList()` | 无 | `{ path, entries: [{at, device_id, device_name, stage, stage_label, error, url, count}], total }`；按时间倒序，`stage` ∈ `connect`/`set_uri`/`play`/`stop`/`media_ready`/`cast`（`media/cast_failures.py` `STAGES`） | Sidecar | 通用。台账读不出来时返回空 `entries`，不影响投屏 |
| `cast_failure_clear` | `castFailureClear()` | 无 | `{ removed, ...snapshot() }`（删文件并回最新快照） | Sidecar | 通用 |
| `probe_url` | `probeUrl(url, timeout=1.0)` | `url: string`、`timeout?: number`（秒，夹到 0.3–5） | `{ ok, status, ms, final_url?, error? }`；先 `HEAD` + `Range: bytes=0-0`，CDN 不给 HEAD（405/501/空 Content-Length）时退回区段 GET 取首字节；任何异常都当「探不动」返回 `ok:false`，**绝不抛给上层** | Sidecar | 非 http(s) 地址回 `{ok:false,error:"不是 http(s) 地址"}`；网络异常回 `{ok:false,error:"<类型>: <原文>"}` |

壳层定义：`lib.rs`（`cast_failure_list` / `cast_failure_clear` / `probe_url` 三个 `#[tauri::command]`，紧挨 `failure_clear`）。前端 `commands.ts` 对应 `castFailureList` / `castFailureClear` / `probeUrl`（`initInvoke` + `normalizeCastLedger`）。后台日志面板（`LogsPanel.vue`）打开时拉一次台账并可清空；投屏历史面板（`MediaInput.vue`）打开时用 `probe_url` 并发 3 路探 `url` 条目的直链，死链灰掉（「探不动」不算死），点「直接投回」时 url 条目先回来源页 `resolveShare` 重解一次、解不出再退回旧直链。

---

## 3. 事件全表

### 3.1 事件总账：Sidecar 10 个名字 / 转发前端 9 条 / 壳层自加 2 条

三个数别混（口径照 AGENTS.md 那条「事件是三个数」写，本轮按代码更正）：

- **Sidecar 发出的事件名 10 个** —— `grep -rn "emit_event(" hancast_sidecar/ | grep -v "def emit_event"` 逐条点名：`ready`、`device_found`、`device_lost`、`cast_state_changed`、`cast_confirm_request`、`screen_cast_state_changed`、`remote_command`、`browser_request`、`mirror_state_changed`，以及本轮新增的 `update_download_done`（`update/manager.py:708`）。
- **转发到前端 9 条** —— 上面这 10 个里，除 `browser_request` 之外全部由 `sidecar.rs:358-371` 无条件 `app.emit`；`browser_request` 走 `:361-370` 那条专门分支转给离屏渲染窗口并 `return`，不向前端发。
- **壳层自加 2 条** —— `update-available`（lib.rs:1946）、`autostart-changed`（`set_autostart` 里发在 lib.rs:1669，托盘菜单勾选回调里发在 :1764）。
- ⇒ 前端**能收到 11 个名字**，实际 `listen` **订阅 10 个**：唯一没人订阅的是 `ready`（只作握手痕迹）。本轮新增的 `update_download_done` **有人订阅**——`src/stores/update.ts:397-398` 收到就 `resumeDownload()`，与 1 秒轮询互为兜底。

> 历史一句：上一版这张表只有 9 行、标题写「9 个事件」，账上漏了 `remote_command`（手机遥控器页面的 next/prev/mode/stop_after/cast_history/load_folder 六个动作全靠它回推，`commands.py:1008-1030`）与 `browser_request`；本轮按代码实际补齐，并注明「9 条转发前端」这个口径与 AGENTS.md 里那句「Sidecar 发出 9 条」为何差一（那边同样没把 `remote_command` 记进账）。

| 事件名 | 载荷字段（逐个） | 谁发出 | 谁订阅 | 触发时机 |
| --- | --- | --- | --- | --- |
| `ready` | 无（`data` 为 `{}`，`protocol_io.py:56-58`） | `hancast-backend/hancast_sidecar/main.py:110` | **无前端订阅者**（全仓 `listen(` 检索无命中） | Sidecar 启动完成、进入 `for line in sys.stdin` 主循环之前，握手用。壳层也不监听它 |
| `device_found` | `id`、`name`（已被 `display_name` 覆盖）、`device_type`、`ip`、`port`、`status`、`is_default`、`manufacturer`、`model_name`、`udn`、`custom_name`、`location` | commands.py:391（`_on_device_found`，发出在 :401；载荷是 `Device.to_dict()`，见 `models/device.py:26-29`） | src/stores/device.ts:182 | SSDP 收到 NOTIFY/搜索响应并完成描述抓取时；同一设备重复发现也发，前端 upsert。**v2.3.13 起有准入门槛**：描述里必须声明 `AVTransport` 服务，路由器/NAS 不发事件也不进列表 |
| `device_lost` | `id: string` | commands.py:407 的 `_on_device_lost`（发出在 :409） | src/stores/device.ts:199 | 收到 SSDP byebye（`ssdp.py:628-629` → `:746`）或探活判定过期（`ssdp.py:761-777`、`discovery.device_stale_s`） |
| `cast_state_changed` | `status`、`device_id`、`position`（`HH:MM:SS`）、`duration`（`HH:MM:SS`，见下注）、`volume`、`is_muted`、`media?`（有媒体时才带） | commands.py:415 的 `_on_protocol_state_change`（发出在 :443）、:642（`_on_session_event`，def 在 :614）、:694（`_start_cast` 内补发，def 在 :645）、:740 的 `_emit_session_state`（发出在 :751，会话轮询回调） | `src/stores/cast.ts:908` | 投屏状态机变化。`_on_protocol_state_change` 那条路径不带 `duration`，前端要容错 |
| `cast_confirm_request` | `request_id`、`device: { ip, udn, friendly_name, model_name }`、`timeout`（秒） | commands.py:1460 的 `_on_guard_confirm_request`（发出在 :1467） | `src/stores/guard.ts:126` | 外部设备向本机推流、且该来源不在信任/黑名单里时。前端收到后弹确认框并把窗口拉到前台（`guard.ts:141-148`） |
| `remote_command` | `action: string`、`value?: any`、`device_id: string\|null` | commands.py:1008 的 `_remote_action`（发出在 :1029-1030） | `src/stores/cast.ts:1008` | 手机遥控器页里点了后端不自己处理的那六个动作（`next`/`prev`/`mode`/`stop_after`/`cast_history`/`load_folder`）时；命令立即回 `{action, device_id, delegated: true}`，实际执行在前端。`refresh_devices` 由后端自己扫（:1024-1027），**不发这条事件** |
| `browser_request` | `request_id`、`url`、`options` | `utils/browser_channel.py:131` | **不转发前端**：`sidecar.rs:361-370` 把它交给离屏渲染窗口，渲染完由 `browser_result` 命令（commands.py:1420-1428）把结果送回等待中的解析线程 | 声明式/JS 规则需要「用浏览器通道取一次页面」时（反爬那条链路，见 docs/反爬实现取证与浏览器通道.md） |
| `screen_cast_state_changed` | 与 `screen_cast_status` 返回体**完全相同**：`running`、`target`、`source`、`device_id`、`session_id`、`watch_url`、`url`、`viewers`、`error`、`started_at`，外加 `pull`、`ffmpeg` | `screencast/manager.py:560`（`_emit()`，def 在 :557，载荷就是 `self.status()`） | `src/stores/screencast.ts:285-300` | 屏幕投屏启动成功、停止、捕获出错、观众数变化时 |
| `mirror_state_changed` | 与 `mirror_status` 的会话部分相同：`running`、`serial`、`model`、`started_at`、`pid`、`window`（恒为 `external`）、`error`、`ok`、`last_error` | `mirror/manager.py:403`（`_emit()`，def 在 :401） | `src/stores/mirror.ts:265` | 外部 scrcpy 进程退出（用户关窗 / 手机拔线 / 异常崩溃）时。非主动退出且退出码非 0 时 `last_error` 带 stderr 末行，前端据此发系统通知（v2.4.12 未发布） |
| `update_download_done` | `version: string`、`asset: string`（文件名）、`path: string`（落盘的绝对路径）、`verified: boolean` | **本轮新增**：`update/manager.py:708-713`，在安装包下载完成**且 sha256 整包校验通过**、`os.replace(part, dest)`（:696）之后发；任务表在 :702-707 先写成 `done` | `src/stores/update.ts:397-398`（回调就一句 `resumeDownload()`：查一次状态、`done` 则接着取装法） | 一次下载走到 `status: "done"` 时。**注意**：失败与取消**不发事件**，只在任务表的 `status/error` 里——所以这条只是把「下完了」提前 1 秒送到，界面状态的真值仍然只有 `update_download_status` 那一份 |
| `update-available` | 就是 `check_update` 的整个返回体：`has_update`、`current`、`latest`、`url`、`body`（≤500 字符）、`source`，有新版时另带 `platform`、`assets[]`、`selected`、`no_asset_for_platform`（可能带 `via`、`error`） | src-tauri/src/lib.rs:1946（**壳层发出，不是 Python**） | `src/stores/update.ts:387-393`（收到即读镜像开关 + `resumeDownload()` 向后端对齐一次） | 启动后台检查（lib.rs:1917-1958，睡 3s 后查一次 GitHub）命中新版本时。商店版本不会发（lib.rs:1921-1925） |
| `autostart-changed` | `boolean`（新的自启状态） | src-tauri/src/lib.rs:1652（`set_autostart` 命令，发出在 :1669）、src-tauri/src/lib.rs:1764（托盘菜单勾选回调） | `src/stores/settings.ts:32`；**另有壳层自监听** src-tauri/src/lib.rs:1975（同步托盘勾选态） | 开机自启状态被任何一侧改动时。前端据此更新设置页开关，避免与托盘状态打架 |

### 3.2 一个死监听

`src/stores/cast.ts:992` 注册了 `listen<{device_id?, message}>('cast_error', ...)`（类型 `CastErrorEvent` 也还在 `src/types/settings.ts:56`、由 `src/types/index.ts:4` 再导出），但**整个 `hancast-backend/` 里没有任何 `emit_event("cast_error", ...)`**——全仓 `emit_event(` 检索只命中这 10 个名字：`ready`、`device_found`、`device_lost`、`cast_state_changed`、`cast_confirm_request`、`remote_command`、`screen_cast_state_changed`、`browser_request`、`mirror_state_changed`、`update_download_done`。这个监听器永远不会被触发。投屏失败实际是通过 `cast_state_changed` 的 `status: "error"`（或 `start_cast` 的 invoke rejection）传出来的。

因为不存在发送方，`cast_error` **不计入 §3.1 的任何一条口径**（Sidecar 发出 10 / 转发前端 9 / 壳层 2 / 前端订阅 9）。

### 3.3 前端订阅方式

所有 store 都用动态 import 拿 `listen`，并把返回的 `UnlistenFn` 收进数组以便卸载：

```ts
const { listen } = await import('@tauri-apps/api/event')
unlistenFns.push(await listen<Device>('device_found', (event) => { /* event.payload */ }))
```

外层一律包 `try/catch`，非 Tauri 环境（浏览器 dev）静默跳过（`device.ts:195-225`、`guard.ts:119-152`、`update.ts:384-402`、`settings.ts:28-38`、`screencast.ts:281-303`、`mirror.ts:264-278`）。

---

## 4. 分组调用示例（摘自真实代码）

以下片段全部从仓库现有文件里截出，标注了来源；没有为文档新造的 API。

### 4.1 设备发现与管理 — src/stores/device.ts:135-153

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

### 4.2 投屏控制 — `src/stores/cast.ts:365-518`

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
报错时 `ledgerHint` 置真，输入框下方就地给出台账入口：设置 → 解析插件 → 解析失败台账（`MediaInput.vue:808`）。

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

### 4.9 更新 — `src/stores/update.ts:75-107`（检查）与 `:188-298`（下载状态机，含 `:64`/`:66` 的代次与在途标记）

```ts
// 检查：商店版跳过；失败原因从返回体的 error 键里读，不再一律报「检查失败」（update.ts:75-107）
async function check(currentVersion: string, force = true) {
  if (storeVersion.value) { error.value = 'store_version'; return null }   // :77-80
  loading.value = true; error.value = ''
  try {
    const result = await checkUpdate(currentVersion, force)
    if (result.has_update) {
      updateInfo.value = result; showModal.value = true
      void loadMirrorSettings()          // :89 —— 弹窗一开就能看到镜像开关的现值
    } else {
      if (result.error) error.value = result.error       // :94-97：把「根本没查到」与「确实没新版」分开
      else if (force) error.value = 'no_update'          // :98-100
    }
    return result
  } catch (e) { error.value = String(e); return null } finally { loading.value = false }
}

// 下载：起任务 = 先领一个代次号，再起 1 秒轮询；终态由轮询给出，取消只是「请求」（:209-234）
async function startDownload(asset?: UpdateAsset | string) {
  if (downloading.value) return                          // :210
  const gen = ++epoch                                    // :212 代次号：迟到的那一发从此写不动 store
  downloading.value = true; starting = true              // :213-214
  downloadTask.value = { status: 'pending', stage: 'connecting', asset: name }  // :218 先占位
  try {
    const task = await updateDownloadStart(name, updateInfo.value?.latest || '')
    if (gen !== epoch) return                            // :221
    downloadTask.value = task
    stopPolling(); pollTimer = window.setInterval(() => void pollDownload(gen), POLL_MS)  // :224，POLL_MS=1000（:21）
  } catch (e) { /* :226-233：写 error、停轮询；starting 在 finally 放 */ }
}

async function pollDownload(gen: number) {               // :188-206
  const task = await updateDownloadStatus()
  if (gen !== epoch) return                              // :191
  downloadTask.value = task
  if (task.status === 'idle' && starting) return         // :194 —— 起下载的请求还在路上，idle 不是终态
  if (task.status !== 'pending') {
    stopPolling(); downloading.value = false
    if (task.status === 'done') void refreshPlan()       // :198 立刻取装法，按钮才知道该「退出并安装」还是「复制命令」
  }
}
```

要点三条，都是「界面说的必须与后端一致」：

- **`resumeDownload()`（`update.ts:258-276`）在弹窗每次打开时向后端对齐一次**（`UpdateModal.vue:150-157` 的 watcher），Sidecar 的 `update_download_done` 事件也调它（`update.ts:397-398`）。定时器归 store 不归组件，但用户关掉弹窗再打开时界面是全新的，不对齐就会停在「下载中」而后端早完成了——与 `stores/screencast.ts:153` 那个坑同源。**这一轮还多了一道代次守卫**（`epoch`/`starting`，`update.ts:64-66`、`:191`、`:195`）：`resumeDownload` 与 `startDownload` 会同时往 store 里写，慢回来的那一发带着旧 `idle` 会把刚点下去的下载覆盖成「没在下」，界面于是没有进度条、主按钮也不切成「取消」——浏览器验收（`tools/verify-update-ui.mjs` 场景 A）第一次就跑红了这三条。
- **`cancelDownload()`（`update.ts:236-250`）不谎报已取消**：只有后端返回的 `status` 不是 `pending` **且代次没变**时才写终态（`:242`，代次在 `:238` 领），否则继续等轮询把 `cancelled` 落下来。
- **`applyUpdate()`（`update.ts:307-323`）成功即进程退出**，所以 `commands.ts:1878-1881` 的注释明确要求「调用前把话说完」；只有回 `manual: true`（deb / rpm / Arch / msi）时才再 `refreshPlan()`，命令原文由 `UpdateModal.vue:99-100`、`:168-183`、`:309-313` 那三处提供复制（`writeClipboardText` → `navigator.clipboard` 两级降级）。

商店版还会跳过这一组事件监听（`update.ts:381`，`setupListeners` 里 `await init()` 之后直接 `return`）；`storeVersion` 来自 `isStoreVersion()`（`update.ts:68-70`，壳层命令）。

### 4.10 剪贴板 — `src/components/MediaInput.vue:375-385`

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

要点：`readClipboardText()` **不会抛异常**（封装层三级降级，最终返回 `''`，commands.ts:1145-1176），所以调用方只能靠空串判断失败。同文件 `onUrlPaste`（`MediaInput.vue:382`）走的是浏览器原生 `ClipboardEvent`，不经过壳层。

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
| `media.ts` | 23 | 媒体信息与画质档位 | `MediaQuality {id,label}`；`MediaInfo`：`media_type` / `uri` / `title` / `duration: number\|null` / `mime_type` / `file_size` / `thumbnail` / `qualities?` / `quality?` / `source_url?`；`MediaInputState = 'idle'\|'dragover'\|'input'\|'parsing'\|'ready'`。与后端 `MediaInfo.to_dict()`（`models/media.py:22-39`）字段一一对应，但 TS 侧少了 `via` / `author` / `bvid`（那三个在 `commands.ts:779-785` 的 `ShareResolveResult` 里补） |
| `cast.ts` | 53 | 投屏状态与会话 | `CastState`（**前端展示态**，`position: number` 秒 + `positionTime`/`durationTime` 字符串）、`CastSession`（绑定设备的会话）、`CastUrlInfo`（`get_cast_url` 返回）、`createSession(deviceId, deviceName)` 工厂函数（`:41-52`，初始 `status: 'connecting'`、`volume: 80`）。注意这里的 `CastState` **不是**后端 `CastState.to_dict()` 的形状（后端 `position`/`duration` 都是 `HH:MM:SS` 字符串，`models/cast.py:16-27`），转换在 `stores/cast.ts:287-333` |
| `settings.ts` | 58 | 应用设置、网卡、后台参数 | `AppSettings`（对应 `config.to_dict()`）、`NetworkInterface` / `NetworkInterfacesInfo`、`ParamType = 'number'\|'bool'\|'enum'\|'text'\|'string'`、`ParamSpec`（含 `label_zh`/`label_en`/`help_zh`/`help_en`/`min`/`max`/`unit`/`options`/`value`/`overridden`）、`SaveParamsResult {ok, errors, specs}`、`DeviceLostEvent {id}`、`CastErrorEvent {message}`（**后者对应的事件从未被发出**，§3.2） |
| `guard.ts` | 34 | 投屏确认 | `GuardDevice`（对应后端 `DeviceGuardEntry.to_dict()`，`guard.py:44-57`）、`GuardDevicesResponse {trusted, blacklisted}`、`GuardSettings {enabled, confirm_timeout}`、`CastConfirmRequest`（`cast_confirm_request` 事件载荷，`device` 只声明了 `ip`/`udn`/`friendly_name`/`model_name` 四个字段） |
| `logs.ts` | 33 | 日志面板 | `LogLine {ts, module, level, msg, raw}`、`LogModule {id, label}`、`LogPage {lines, total, truncated, path, level, modules}`、`LogQuery {limit?, module?, level?, keyword?, include_older?}`。与 `logger.read_logs()` 返回体（`logger.py:207-215`）逐字段对应 |
| `screencast.ts` | 107 | 屏幕投屏 + FFmpeg 依赖 | `ScreenSourceItem`（含显示器专用的 `width`/`height`/`primary`）、`AudioDeviceItem`（含 `tap`：macOS 自建 Audio Tap 环回；`loopback`：Windows WASAPI 环回；`default`）、`ScreenSources`（含 `platform`）、`ScreenCastState`、`ScreenCastStartParams`；v2.3.11 去捆绑后新增 `FfmpegDepItem`（单个依赖的状态）、`FfmpegStatus`（`ffmpeg_status` 返回体，含 `items/missing/install_commands/download`）、`FfmpegDownloadTask`（后台下载任务快照）——三者都是「需要下载运行依赖」弹窗的数据源。`ScreenCastState` **缺 `pull` 字段**（§6.5） |
| `update.ts` | 102 | 应用内更新（v2.4.12 重写） | `UpdateAsset`（`:11-24`，一个 Release 产物：`name/url/size/sha256/kind/os/arch/arch_match/priority`）、`UpdateInfo`（`:27-46`，老六键 + `error?`/`ignored?`/`via?`/`platform`/`assets?`/`selected?`/`no_asset_for_platform?`，`source` 收窄为 `'github' \| ''`）、`UpdateTask`（`:49-72`，下载任务表，`percent: number\|null` = 无 Content-Length 时不定长）、`UpdatePlan`（`:75-88`，`kind: 'dmg'\|'nsis'\|'appimage'\|'command'` + `auto` + `command/note_zh/note_en`）、`UpdateApplyResult`（`:91-99`，壳层 `update_apply` 回执，`manual?` 为真要手动）、`UpdateModalState`（`:102`）。§6.6 那条「缺 `error`」的坑本轮已修 |

`src/api/commands.ts` 里还自带一批**没有放进 `src/types/`** 的接口定义，用规则包/台账时要从那里 import：`ShareResolveResult`（`:661`）、`PluginRuleInfo`（`:710`）、`PluginInfo`（`:718`）、`PackIndexEntry`（`:730`）、`PackUpdateEntry`（`:739`）、`PackUpdateCheck`（`:743`）、`DeclarativeRuleInfo`（`:751`）、`RuleInventory`（`:761`）、`FailureEntry`（`:768`）、`FailureHost`（`:776`）、`FailureLedger`（`:784`）、`StopCastResult`（`:963`）、`StopWarning`（`:972`）。

---

## 6. 坑（已验证的契约不一致）

### 6.1 `Device.type` 对不上后端的 `device_type`

后端 dataclass 字段名是 `device_type`（`models/device.py:9`），`to_dict()` 只做 `asdict()` 再把 `name` 换成 `display_name`，**不改键名**（`models/device.py:26-29`）。前端类型写的却是 `type`（`types/device.ts:4`）。

后果：`DeviceCard.vue:151` 的 `v-if="device.type === 'speaker'"` 恒为 `false`，音箱设备的同步控件永远不渲染。

同时联合类型两头都不对：`ssdp.py:1181-1188` 实际只会产出 `tv` / `speaker` / `server` / `unknown` 四种，**`box` 从来不会被赋值**，而 `server` 不在 TS 联合里。

### 6.2 `PluginInfo.broken` 后端从不产出

commands.ts:890 声明了 `broken?: boolean`，`PluginSettings.vue:279` 用它渲染「已损坏」标记。但后端 `plugin.info()`（`jsplugins.py:254-271`）只输出 `name` / `version` / `author` / `description` / `update_url` / `error` / `rules`，**没有 `broken`**（全 `jsplugins.py` 检索 `broken` 无命中）。坏包靠 `error` 字段体现。

### 6.3 `PackIndexEntry.sha256` 在 `check_updates` 里被丢掉

`fetch_index` 解析出了 `sha256`（`jsplugins.py:445`），但 `check_updates` 组装 `updates` / `available` 条目时只取 `name` / `version` / `url` / `description`（`jsplugins.py:459-460`）。前端 `PackIndexEntry.sha256?`（commands.ts:901）因此永远是 `undefined`。真实下载时 `pull` 会重新 `fetch_index` 拿摘要（`jsplugins.py:478`、`:490`），校验本身没被绕过，只是前端拿不到这个值。

### 6.4 `get_cast_state()` 不带参数时返回的不是 `CastState`

前端封装签名是 `getCastState(deviceId?): Promise<CastState>`（commands.ts:1234），但省略 `deviceId` 时后端返回 `{sessions, current}`（commands.py:632-656）。`stores/cast.ts:290` 只能 `as any` 绕开类型系统再手工解构。

同类问题：`get_cast_url` 的 `status` 大小写随分支变化 —— 带 `deviceId` 走 `session.get_cast_info()` 返回小写（`session.py:150`），不带则返回 DLNA 原生大写（commands.py:676）。前端用 `info.status.toUpperCase()` + 映射表统一（`cast.ts:214-226`）。

### 6.5 `ScreenCastState` 缺 `pull`

`manager.status()` 在有 `session_id` 时会补 `pull`（拉流统计，`update/manager.py:235-238`），`screen_cast_state_changed` 事件的载荷就是 `status()` 的输出（`update/manager.py:468`）。但 `types/screencast.ts:40-52` 没有这个字段 —— 电视黑屏时区分「没人来拉」和「拉了但跟不上码率」的关键证据在前端类型上是不可见的。

### 6.6 `UpdateInfo` 缺 `error`（**已于 v2.4.12 修掉，留作历史**）

v2.4.11 及之前：`_check_update` 在所有源都失败时返回 `{"error": "无法连接更新服务器"}`，而 `types/update.ts` 没有这个字段，`source` 还声明成 `'github' | 'gitee'`，而实际可能是 `''`——界面因此只能一律报「检查失败」，说不出「根本没查到」与「确实没新版」的区别。

本轮补齐：`UpdateInfo.error?` / `ignored?` / `via?`（`src/types/update.ts:33-45`），`source` 收窄成 `'github' | ''`（`src/types/update.ts:34`，gitee 那条源已删），`stores/update.ts:94-97` 在读到 `error` 时把它直接当失败原因显示。后端仍然**不抛错**：取清单失败把原因放进返回体（`update/manager.py:424-434`），这是旧契约的一部分，别当成 bug 去「修」。

### 6.7 两个 `hancast.log`

- 壳层 `SessionLogger` 写 `app_data_dir()/logs/hancast.log`（目录拼接在 lib.rs:1206-1209），且**每次启动清空**（`lib.rs:126-133`，`fs::write(&path, "")` 在 `:131`）。`app_data_dir` 由 identifier `com.han.hancast` 推导（`tauri.conf.json:5`）。
- Python 写 `~/Library/Application Support/HanCast/logs/hancast.log`（macOS，`logger.py:19-24`）。

`export_logs`（lib.rs:1516-1560）和 `get_logs`（lib.rs:1473-1478，取数走 `read_logs` `logger.py:156-218`）读的都是**后者**。前者里的壳层日志不会出现在日志面板和导出文件里。

> **例外（v2.3.23 起）**：macOS 通知的授权状态与投递错误由壳层 `macos_notify::log()` 直接追加到**后者**那份文件（路径拼接在 `macos_notify.rs:30-40`，行格式与 Python logger 一致：`[时间] hancast.notify INFO: …`），为的就是让后台日志面板当场读到——上一版把 add 的 completion handler 传成 `None`，等于亲手丢掉了唯一的直接证据。

### 6.8 Guard 的 `'reject'` 策略后端不认

前端确认弹窗提供 `'reject'`（`GuardConfirmModal.vue:14`），store 的类型联合里也有它（`guard.ts:72`），但后端 `wait_for_confirm` 只对 `'always'` 和 `'blacklist'` 做特殊处理（`guard.py:274-299`），其余策略一律按「本次拒绝、不落库」走。传 `'reject'` 不会报错，行为等价于 `'once'` + `approved: false`。

### 6.9 `save_guard_settings` 与 `save_settings` 的解包方式不一致

- `save_settings`：壳层包一层 → `json!({"settings": settings})`（lib.rs:782），Python 读 `params["settings"]`（commands.py:915）。
- `save_guard_settings`：壳层**原样下传** → `.send_command("save_guard_settings", settings)`（lib.rs:781），Python 直接读 `params["enabled"]`（commands.py:1154）。

同样是「保存一个设置对象」，两条命令的信封结构不同。`start_screen_cast`（lib.rs:966-973）和 `get_logs`（lib.rs:1473-1478）也是原样下传。

### 6.10 隐藏/手动地址已从「单向门」补成双向（v2.4.4）

v2.4.3 及以前 `unhide_device` / `get_hidden_devices` 只有 Sidecar 实现、壳层没有 `#[tauri::command]`，**设备一旦被隐藏就只能手改配置文件**。v2.4.4 补了三个壳层命令：`unhide_device`、`get_hidden_devices`（设备列表底部「已隐藏 N 台 · 管理」→ `HiddenDevicesModal.vue` 逐台恢复）、`remove_manual_device`（手动添加的地址可在弹窗里删掉）。`get_hidden_devices` 的返回是富化过的（名称/IP/在线/是否本机），不是裸 UDN 列表。

### 6.11 invoke rejection 是裸字符串

壳层命令一律 `Result<T, String>`，Tauri 把它作为 reject reason 直接传给 JS，**不是 `Error` 实例**。全仓统一用 `err instanceof Error ? err.message : String(err)` 兜（device.ts:149、`logs.ts:55`），但 `PluginSettings.vue:39` 用的是 `String(err?.message ?? err)` —— 两种写法都在流通。

### 6.12 浏览器 mock 静默返回空对象

`mockInvoke` 的 `default` 分支是 `return {} as T`（`commands.ts:1107-1108`；本轮新增的 7 条更新 mock 分支在 `commands.ts:836-928`，未列出的命令仍走这条兜底）。未在 mock 里实现的命令在纯浏览器环境下**不会报错**，只会返回一个类型断言过的空对象。任何「浏览器里正常、Tauri 里异常」的报告都要先排除这一条。

### 6.13 `update_install_plan` 曾漏了壳层转发，打包版「退出并安装」因此永久置灰（v2.4.12 本轮内发现并修掉）

**当时的形状**：前端 `updateInstallPlan()`（现 `src/api/commands.ts:1866-1869`）发 `invoke('update_install_plan')`，而 `generate_handler!` 里只登记了 6 条更新命令，**没有** `update_install_plan`——那条命令当时只存在于 Sidecar 注册表（`commands.py:232`），由 `update_apply` 在壳层内部下发。后果按代码顺序推一遍：下载完成 → `pollDownload` 调 `refreshPlan()` → Tauri 侧「Command not found」→ `plan` 为空 → 弹窗主按钮在 `done` 分支按 `!plan.value` 置灰（`src/components/UpdateModal.vue:212-217`）⇒ **安装包下好了、校验过了，就是装不了**。浏览器 mock 有 `case 'update_install_plan'`（`commands.ts:894-905`），所以本地一路绿灯，只有装好的产物上才暴露——正是 §6.12 那类样本。

**现在两头都补上了**（保留这段是因为它解释了为什么代码里有看着多余的兜底）：
- 壳层加了纯转发的 `update_install_plan`（lib.rs:1162-1168，进注册表 lib.rs:2108；lib.rs:1155-1159 那段注释就是这条坑的原文，别删）；
- 前端 `refreshPlan()` 在取装法失败时按任务表自己拼一份够用的计划（`src/stores/update.ts:278-298`，兜底那一段在 `:282-294`，`auto` 用本地 `AUTO_KINDS`（`update.ts:24`）判，与 Sidecar 的 `AUTO_APPLY`（`hancast-backend/hancast_sidecar/update/manager.py:90`）同一份名单）。

**契约**：`update_apply` 的 `manual` 分支本来就把整份 `plan` 回带（lib.rs:1355），所以「要提权」那条路不依赖单独取装法；能自动装的那条依赖 `plan` 存在（按钮文案 `UpdateModal.vue:205-210` 靠它区分「退出并安装」与「复制安装命令」）。
---

## 7. 快速参考表（95 条）

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
| 8 | `hide_device` | 隐藏设备（v2.4.4 起可在「已隐藏设备」弹窗恢复） | SC |
| 9 | `unhide_device` | 取消隐藏一台设备，让它重新回到设备列表（v2.4.4） | SC |
| 10 | `get_hidden_devices` | 取被隐藏设备的富化清单（名称/IP/在线/是否本机，v2.4.4） | SC |
| 11 | `add_device` | 按 IP / `ip:port` / 描述 URL 手动添加设备 | SC |
| 12 | `add_local_device` | 把「本机」加进设备列表，用于自己投自己（v2.4.7） | SC |
| 13 | `get_manual_devices` | 取手动添加过的地址清单 | SC |
| 14 | `remove_manual_device` | 从手动添加的地址清单里删掉一条（v2.4.4，幂等） | SC |
| 15 | `parse_media_file` | 解析本地媒体文件元信息 | SC |
| 16 | `list_folder_media` | 列一个文件夹里的媒体文件清单（自然序，不探时长） | SC |
| 17 | `parse_media_url` | 解析直链媒体元信息（前端无调用点） | SC |
| 18 | `resolve_share` | 从粘贴文本解析可播地址，返回异步任务 id | SC |
| 19 | `get_resolve_result` | 轮询解析任务结果（无独立封装） | SC |
| 20 | `plugin_list` | 列出已安装规则包 | SC |
| 21 | `rule_inventory` | 一屏列全规则包 + 声明式 JSON 规则 | SC |
| 22 | `failure_list` | 解析失败台账（按域名分组） | SC |
| 23 | `failure_delete` | 删台账里一个域名或一条地址 | SC |
| 24 | `failure_clear` | 清空解析失败台账 | SC |
| 25 | `plugin_check_updates` | 对照官方索引看哪些包可更新 / 可安装 | SC |
| 26 | `plugin_pull` | 按索引装或更新一个包（sha256 校验） | SC |
| 27 | `plugin_import` | 导入规则包 zip（本地路径 / base64 / URL 三选一） | SC |
| 28 | `plugin_remove` | 卸载一个规则包 | SC |
| 29 | `plugin_set_rule_enabled` | 启停规则包里的某条规则 | SC |
| 30 | `plugin_test` | 用一段分享文本试跑某规则包，返回任务 id | SC |
| 31 | `read_clipboard` | 读系统剪贴板文本 | 壳 |
| 32 | `write_clipboard` | 写系统剪贴板 | 壳 |
| 33 | `start_cast` | 向指定设备开始投屏 | SC |
| 34 | `stop_cast` | 停止投屏（可全停），报告电视是否真收到 Stop | SC |
| 35 | `pause_cast` | 暂停 | SC |
| 36 | `resume_cast` | 继续 | SC |
| 37 | `seek_cast` | 跳转到 `HH:MM:SS` | SC |
| 38 | `get_cast_state` | 取投屏状态（单设备 / 全部会话两种形状） | SC |
| 39 | `get_cast_url` | 取当前投屏元素的 URL 与进度 | SC |
| 40 | `set_volume` | 设音量，返回生效值 | SC |
| 41 | `set_mute` | 设静音 | SC |
| 42 | `get_settings` | 取应用设置 | SC |
| 43 | `save_settings` | 存应用设置（改网卡会后台重启 SSDP） | SC |
| 44 | `list_network_interfaces` | 枚举可选网卡及当前选定项 | SC |
| 45 | `get_params` | 取 32 条后台参数登记表 + 当前值 | SC |
| 46 | `save_params` | 存后台参数，逐项校验、部分成功 | SC |
| 47 | `check_update` | 检查新版本（v2.4.12 起返回体带本平台产物清单；壳层**不再**下发 `sources`，gitee 那条源已删） | SC |
| 48 | `ignore_update_version` | 记录「此版本不再提示」 | SC |
| 49 | `respond_cast_confirm` | 用户回应投屏确认弹窗 | SC |
| 50 | `get_guard_devices` | 取信任 / 黑名单设备 | SC |
| 51 | `remove_guard_device` | 从信任 / 黑名单移除（UDN 或 IP） | SC |
| 52 | `set_guard_policy` | 改某设备的信任策略 | SC |
| 53 | `get_guard_settings` | 取投屏确认开关与超时 | SC |
| 54 | `save_guard_settings` | 存投屏确认开关与超时（信封不解包） | SC |
| 55 | `check_mpv` | 检查本机 MPV 可用性（前端无封装） | SC |
| 56 | `set_mpv_path` | 手动指定 MPV 路径并热替换渲染器（前端无封装） | SC |
| 57 | `list_screen_sources` | 枚举屏幕 / 窗口 / 音频设备 + FFmpeg 状态 | SC |
| 58 | `start_screen_cast` | 开始屏幕投屏（dlna / browser 两路） | SC |
| 59 | `stop_screen_cast` | 停止屏幕投屏，报告电视是否真收到 Stop | SC |
| 60 | `screen_cast_status` | 取屏幕投屏状态（含拉流统计与观众数） | SC |
| 61 | `set_ffmpeg_path` | 手动指定 FFmpeg 路径 | SC |
| 62 | `ffmpeg_status` | FFmpeg / FFprobe 检测报告（缺什么、装法、下载进度） | SC |
| 63 | `ffmpeg_download_start` | 起一次 FFmpeg 后台下载，立即返回 | SC |
| 64 | `ffmpeg_download_status` | 轮询 FFmpeg 下载任务 | SC |
| 65 | `open_ffmpeg_dir` | 在系统文件管理器里打开程序自己的 FFmpeg 目录 | 壳 |
| 66 | `export_logs` | 弹保存对话框导出 Python 侧日志文件 | 壳 |
| 67 | `get_logs` | 读日志尾部并按模块 / 级别 / 关键字过滤 | SC |
| 68 | `set_log_level` | 运行时改日志级别 | SC |
| 69 | `is_store_version` | 判定是否微软商店（MSIX）版本 | 壳 |
| 70 | `get_autostart` | 读开机自启状态（MSIX / 经典两套实现） | 壳 |
| 71 | `set_autostart` | 写开机自启状态，成功后 emit `autostart-changed` | 壳 |
| 72 | `notify_os` | 发一条系统通知（macOS 走 `UNUserNotificationCenter`，其余平台回 `Err` 让前端退回插件） | 壳 |
| 73 | `cast_failure_list` | 取投屏（出站）失败台账：设备 / 阶段 / 错误 / 地址 / 次数（v2.4.5） | SC |
| 74 | `cast_failure_clear` | 清空投屏失败台账（v2.4.5） | SC |
| 75 | `probe_url` | 1 秒预算探一次地址是否还活着（历史直链重投前预检，v2.4.5） | SC |
| 76 | `start_remote_control` | 启动手机遥控器页（v2.4.8 起随应用常驻，幂等；已在跑返回同一地址），返回带 token 的局域网地址（v2.4.6；**v2.4.10 起 token 首启生成后落盘复用**，键 `remote_control_token`，所以同一台机器上的地址长期不变） | SC |
| 77 | `stop_remote_control` | 关闭遥控器页（v2.4.6）。**不删**落盘的 `remote_control_token`——下次启动还是同一段（v2.4.10） | SC |
| 78 | `remote_status` | 遥控器页运行状态与访问地址（v2.4.6）；首页「手机遥控」弹窗打开时先调它，`ok` 为假才补一次 `startRemoteControl`（`src/components/RemoteControlPanel.vue:28-30`）。返回的 `state.devices` 是**全量局域网设备**（v2.4.11，逐台带 `online` / `session_status`，隐藏设备不进），页面上的 `⟳` 走 `refresh_devices` 手动重扫、不再自动轮询 | SC |
| 79 | `subtitle_apply` | 把外挂字幕封进当前视频并重投同一台电视（v2.4.6） | SC |
| 80 | `subtitle_status` | 查询字幕内封任务进度（v2.4.6） | SC |
| 81 | `remote_update_ui` | 前端把播放队列 / 历史 / 播放模式 / 播完即停同步给后端，供遥控器页读取（v2.4.8） | SC |
| 82 | `frontend_log` | 前端把错误（含渲染期异常与未捕获 rejection）写进同一份 `hancast.log`，行首 `[webview/<source>]`（v2.4.11）；`SectionBoundary` 与 `app.config.errorHandler` 都走它 | SC |
| 83 | `mirror_status` | 手机镜像接收：返回 adb/scrcpy 工具链探测（路径/版本/是否支持音频）、当前会话状态、平台与安装命令（v2.4.12 未发布） | SC |
| 84 | `mirror_devices` | 列 `adb devices -l` 的在线设备（serial / state / online / model / wireless） | SC |
| 85 | `mirror_connect` | 无线调试：对 `ip:port` 发 `adb connect`，返回结果地址与错误 | SC |
| 86 | `mirror_start` | 用外部 scrcpy 打开镜像窗口（画质/选项/serial），进程退出时发 `mirror_state_changed` | SC |
| 87 | `mirror_stop` | 停止当前镜像会话（terminate → 5s → kill） | SC |
| 88 | `mirror_set_tool_path` | 手动指定 adb / scrcpy 路径（空串恢复自动检测，错路径不静默回退） | SC |
| 89 | `update_release_info` | 某版本的产物清单（空 = 语义最新的已发布版；Linux 换安装包用，不经 `check_update` 重判版本，v2.4.12） | SC |
| 90 | `update_download_start` | 起一次安装包下载：立即返回任务表，Range 续传 + sha256 整包校验，同任务幂等（v2.4.12） | SC |
| 91 | `update_download_status` | 轮询下载任务表（`status/stage/percent/received/speed/resumed_from/verified/error`） | SC |
| 92 | `update_download_cancel` | **请求**取消下载；`stage` 先转 `cancelling`，终态由下一次 status 给出（v2.4.12） | SC |
| 93 | `update_install_plan` | 已下好的产物该怎么装：`kind` ∈ `dmg`/`nsis`/`appimage`/`command` + `auto` + 命令原文（v2.4.12；这条转发**本轮补过**，见 §6.13） | SC |
| 94 | `update_apply` | 退出并安装：NSIS 静默装 / macOS 挂载 dmg 换 `.app`（先备份、失败回滚）/ AppImage chmod+exec；deb·rpm·Arch·msi 回 `manual:true` + 命令原文，不代跑 sudo（v2.4.12） | 壳（先下发 `update_install_plan`） |
| 95 | `open_update_dir` | 在系统文件管理器里打开安装包所在目录；目录**优先取 Sidecar 任务表的 `dir`**，旧 Sidecar 才退回 `dest` 的父目录（v2.4.12） | 壳（先下发 `update_download_status`） |

**更新链路 9 条**（§2.9）：`check_update` / `ignore_update_version` / `update_release_info` / `update_download_start` / `update_download_status` / `update_download_cancel` / `update_install_plan` / `update_apply` / `open_update_dir`。Sidecar 侧的 `update_install_plan`（注册 commands.py:232、处理函数 commands.py:1599-1603）除了被 `update_apply` 内部下发（lib.rs:1339-1341），现在也有一条**同名 Rust 命令**做纯转发（lib.rs:1161-1168）——这条不能省，前端在 `status=done` 之后要单独取一次装法，缺它「退出并安装」就永久置灰（§6.13）。

**事件（详见 §3.1，三个口径都写出来）**：Sidecar 发出 **10** 个事件名 —— `ready`、`device_found`、`device_lost`、`cast_state_changed`、`cast_confirm_request`、`remote_command`、`screen_cast_state_changed`、`browser_request`、`mirror_state_changed`、`update_download_done`（v2.4.12 新增）；其中 **9** 条转发前端（`browser_request` 只到壳层，`sidecar.rs:361-370`）；壳层自加 **2** 条 —— `update-available`、`autostart-changed` ⇒ 前端**能收到 11 个名字、实际 `listen` 订阅 10 个**（只有 `ready` 没人订阅）。另有一条前端死监听 `cast_error`（§3.2），不计入任何口径。

---

## 8. ⚠️ 未验证项

以下三条无法只靠读代码得出结论，标注原因：

1. **`ready` 事件是否有壳层消费者** —— ⚠️ 未验证。已确认 `src/` 下无 `listen('ready')`（§3.1），且 `sidecar.rs` 里事件除 `browser_request` 外是被无条件 `app.emit` 转发的（`sidecar.rs:358-371`），但壳层 `new()` 里是否用别的方式等待就绪（例如 `send_command` 重试）没有逐行追完；从 lib.rs:1927 那句「睡 3 秒等 sidecar 就绪」（`tokio::time::sleep(Duration::from_secs(3))`）推断，握手事件在壳层大概率被忽略，但这是推断而非实证。
2. **`set_mpv_path` 热替换渲染器后的实际可用性** —— ⚠️ 未验证。commands.py:1515-1526 的实现完整（新建 renderer → `protocol.set_renderer` → `start()` → 关旧的），但前端没有任何调用点，无法确认这条路径在当前版本仍被使用还是已成为死代码。
3. **MSIX 分支的运行时行为** —— ⚠️ 未验证。`get_autostart_msix` / `set_autostart_msix`（lib.rs:1590-1610，非 Windows 的 stub 在 `:1619-1628`）依赖 WinRT `StartupTask` 与 `AppxManifest.xml` 里的 `uap10:Parameters`，本次校对在 macOS 上进行，未读 `AppxManifest.xml`、也未在 Windows 上实跑，只核对了 Rust 侧代码路径与错误字符串。
> ✅ **本轮已经跑过的**（2026-11-06，代码冻结点之后，父代理侧执行，结果记在这里当作「已验证」依据）：`node tools/verify-update-ui.mjs` 31 项判据全绿（含「弹窗内不得出现 gitee / 原作者地址 / 商店地址」那条）；`node tools/verify-locale-keys.mjs` ALL PASS（zh / en 各 492 键、键集逐键一致）；`hancast-backend` 定向 `tests/test_update_manager.py`（49 例）+ `tests/test_ghmirror.py`（18 例）全绿；`bash tools/rust-check.sh` 通过；`npm run build`（vue-tsc + vite）通过；`python3 tools/audit-releases.py` exit 0。⚠️ 全量 pytest 里 `tests/test_remote_commands.py::test_上传无聚焦设备报错` 一条失败，原因是本机正跑着一份打包版 HanCast 占住 8080/8899，与本轮改动无关。
4. **应用内安装的三平台实跑** —— ⚠️ 未验证。`update_apply` 的三条自动路径（NSIS `/S` 静默装完重启，lib.rs:1224-1229；macOS `hdiutil attach` → `mv` 备份旧 `.app` → `cp -R` 新包 → 任一步失败回滚并改为 `open` dmg → 成功才 `touch` + `open`，lib.rs:1231-1271；AppImage `chmod +x` + `nohup`，lib.rs:1280-1287）、开发布局兜底（只 `open` dmg，lib.rs:1274-1278）与`write_apply_helper` / `spawn_survivor`（lib.rs:1305-1329、:1305-1329）本轮**只逐行读过**，没有在任何真机上跑过一次「下载完 → 退出并安装 → 新版起来」；`update-apply.log` 的实际落点、SmartScreen 拦截下的表现、Windows 上「安装程序等进程退出」是否真等到，都要等实跑。摘要校验那条路同理：整包 sha256 比对（update/manager.py:680-693）只有 `tests/test_update_manager.py` 的离线用例覆盖，没在线上产物上验过。
