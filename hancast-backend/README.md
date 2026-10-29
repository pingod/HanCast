# HanCast Python Sidecar

> 最后校对：2026-10-29，对应版本 v2.4.3（`package.json:4`）。深度内容见 [../docs/README.md](../docs/README.md)。

`hancast-backend/` 是 HanCast 的 Python 后端，以 **Sidecar 子进程**形式被 Tauri 壳层拉起，
通过 stdin/stdout 的行分隔 JSON 与 Rust 通信。它负责 DLNA/SSDP、媒体服务、mpv 渲染、
屏幕投屏采集，以及粘贴链接的站点解析。

版本：与主程序同步（当前 v2.4.1，`hancast_sidecar/utils/config.py:39`；由 `npm run version:set` 统一刷入），
Python `>= 3.11`（`pyproject.toml:13`）。

> 深入内容不在此文：分层与端口/线程/落盘状态见 [../docs/架构总览.md](../docs/架构总览.md)，
> 命令与事件契约见 [../docs/HanCast-Frontend-API.md](../docs/HanCast-Frontend-API.md)，
> 跑法/测试/调试见 [../docs/开发指南.md](../docs/开发指南.md)，出问题先看
> [../docs/故障排查.md](../docs/故障排查.md)。

## 目录

```
hancast-backend/
├── hancast_sidecar/
│   ├── main.py          # 入口：UTF-8 stdio 包裹 → ready 事件 → 命令主循环
│   ├── commands.py      # 命令路由与 Sidecar 侧服务装配（注册 62 条，注册表 `commands.py:131-221`）
│   ├── params.py        # 后台参数登记表 PARAM_SPECS（32 项）
│   ├── ssdp.py          # SSDP 发现（M-SEARCH / NOTIFY alive+byebye / 陈旧摘除；准入=描述声明 AVTransport）
│   ├── protocol/        # dlna.py（SOAP 控制与描述）、server.py（UPnP HTTP 服务）
│   ├── renderer/        # mpv.py（接收端播放与 IPC）、base.py
│   ├── media/           # parser.py、server.py（本地文件 HTTP + 链路内代理）、
│   │                    # share_resolver.py（粘贴解析管线）、failures.py（解析失败台账）、
│   │                    # rules/（声明式 JSON + JS 包管理 jsplugins + QuickJS 沙箱 jsruntime
│   │                    #        + 慢操作后台任务 tasks.py）
│   ├── screencast/      # 屏幕投屏：ffmpeg 采集（可执行文件由 utils/ffmpeg_deps.py 定位）、
│   │                    # HLS 分片、aiohttp 供流、www 观看页；
│   │                    # Wayland 走原生 helper wlgrab.py（二进制 hancast-wlgrab，源码在仓库根
│   │                    # linux-capture/），Linux 音频枚举在 pulse.py
│   ├── security/        # guard.py（投屏确认）
│   ├── models/ utils/   # 数据结构、日志、身份（UA/设备号）、协议 IO、FFmpeg 依赖检测与按需下载
│   └── xml/             # UPnP 描述文件（随包数据）
├── scripts/
│   ├── run_sidecar.py   # 本地直跑（等价于 -m hancast_sidecar.main）
│   └── build_sidecar.py # Nuitka 打包成平台二进制
├── tests/               # pytest：28 个 test_*.py + 2 个 smoke_*.py（清单见 docs/开发指南.md §5）
├── pyproject.toml       # 依赖与 dev extras（uv 管理）
└── requirements.txt     # 运行依赖的 pip 视角镜像
```

## 安装与运行

```bash
uv sync                                  # 推荐：读 pyproject + uv.lock
uv run python -m hancast_sidecar.main    # 只调后端（Rust 不在场时它照常跑 stdin 循环）
uv run python scripts/run_sidecar.py     # 同上，脚本入口
```

pip 用户：`pip install -r requirements.txt`。屏幕投屏还需要系统里有 **ffmpeg**，
接收端播放需要 **mpv**——两者都是外部二进制，不打进 Python 依赖里。
mpv 随包（开发时从 `src-tauri/mpv/` 取，见 [../docs/开发指南.md](../docs/开发指南.md) §1）；
**ffmpeg 自 v2.3.11 起不再随包**，由 `hancast_sidecar/utils/ffmpeg_deps.py` 在运行时定位：
手动指定路径 → `PATH` + 各平台常见安装目录 → 本程序下载到 `<SETTING_DIR>/bin/` 的那份。

从 `npm run tauri dev` 起的全栈开发不需要单独跑这里。

## 通信协议

请求（Rust → stdin）：

```json
{"id": 5, "cmd": "start_cast", "params": {"device_id": "uuid-xxx", "media_uri": "http://192.168.1.5:52341/video.mp4"}}
```

响应（stdout，与 `id` 配对）：

```json
{"id": 5, "success": true,  "data": null}
{"id": 5, "success": false, "error": "Device not found"}
```

事件（stdout，无 `id`，主动推送）：

```json
{"event": "device_found", "data": {"id": "...", "name": "..."}}
```

三条铁律（改代码前必读）：

1. **stdout 只能有一个写者**。响应与事件都必须走 `hancast_sidecar/utils/protocol_io.py`，
   直接 `print` 会和响应交错，Rust 侧解析出半行 JSON。
2. **每个请求必须有响应**。命令抛异常时主循环仍回 `success:false`（`main.py:138-140`）；
   长任务改成「立即返回 `task_id` + 轮询出口」（`resolve_share` / `get_resolve_result`，
   规则包慢操作走 `plugin_task_start` / `plugin_task_result` / `plugin_task_cancel`，
   见 `media/rules/tasks.py`）。
3. **stdio 一律 UTF-8**，且包裹点在 logger 初始化之前（`main.py:26-28`）。
   少了这一层，父进程没给 locale 时中文响应全部变 `success:false`（v2.2.19 实测）。

启动时发一条 `{"event":"ready"}` 作为握手信号，但**目前没有任何一侧监听它**
（`sidecar.rs` 里没有 `ready` 分支，前端也没订阅；壳层是照 timeout 兜底投请求的），
留着的价值在日志里看得清「后端到底起没起来」。详见
[../docs/HanCast-Frontend-API.md](../docs/HanCast-Frontend-API.md) §3.1。
用户直接双击打包后的 sidecar 二进制时（stdin 是 tty，`main.py:55` 判出），它会转而拉起主程序然后自己退出（`main.py:52-78`，`Popen` 在 `:71`）。

## 命令一览

完整表（含参数、返回、执行层）在 [../docs/HanCast-Frontend-API.md](../docs/HanCast-Frontend-API.md)，
这里只给分组印象：

| 组 | 代表命令 |
| --- | --- |
| 设备 | `get_devices` `refresh_devices` `add_device` `hide_device` `remove_device` `get_manual_devices` `list_network_interfaces` |
| 投屏控制 | `start_cast` `stop_cast` `pause_cast` `resume_cast` `seek_cast` `get_cast_state` `get_cast_url` `set_volume` `set_mute` |
| 媒体解析 | `parse_media`（**注册表里只有这一条**；`parse_media_file` / `parse_media_url` 是**壳层命令名**，由 Rust 归一后下发）、`resolve_share` `get_resolve_result` |
| 屏幕投屏 | `start_screen_cast` `stop_screen_cast` `screen_cast_status` `list_screen_sources` `set_ffmpeg_path` |
| 运行依赖 | `ffmpeg_status` `ffmpeg_download_start` `ffmpeg_download_status`（FFmpeg 检测与按需下载，`utils/ffmpeg_deps.py`） |
| 投屏确认 | `get_guard_devices` `get_guard_settings` `save_guard_settings` `set_guard_policy` `respond_cast_confirm` `remove_guard_device` |
| 规则包与失败台账 | `plugin_list` `plugin_import` `plugin_pull` `plugin_check_updates` `plugin_remove` `plugin_set_rule_enabled` `plugin_test`、**三条后台任务出口** `plugin_task_start` `plugin_task_result` `plugin_task_cancel`（v2.3.20 起，慢操作不再同步占住命令通道）、`rule_inventory` `failure_list` `failure_delete` `failure_clear` |
| 配置与运维 | `get_settings` `save_settings` `get_params` `save_params` `get_logs` `set_log_level` `check_update` `exit`（`exit` 由主循环拦下，`export_logs` **不在注册表里**——它是 Rust 就地实现的 `lib.rs:950`） |

`grep -rn "emit_event(" hancast_sidecar/` 能数出 **7 个事件名**：`ready`、`device_found`、`device_lost`、
`cast_state_changed`、`cast_confirm_request`、`screen_cast_state_changed`，外加 `browser_request`
（`utils/browser_channel.py:131`）——**最后这条只到壳层**，由 `sidecar.rs:361` 的专分支转给离屏渲染窗口，
不转发前端。所以**回推前端的是 6 条**，加上壳层自己发的 `update-available`、`autostart-changed`，
前端合计监听 **8 条**，逐条见 [../docs/HanCast-Frontend-API.md](../docs/HanCast-Frontend-API.md) §3。

## 打包

```bash
uv run python scripts/build_sidecar.py            # 当前平台
uv run python scripts/build_sidecar.py --help     # 平台/输出参数
```

产物是 Nuitka standalone 目录，由 `npm run tauri build` 前的 `sync-sidecar.cjs` 放进壳层资源，
最终随安装包发布。三平台流程与产物验收见 [../docs/构建与发布.md](../docs/构建与发布.md)。

> ⚠️ 直接双击打包后的 sidecar 二进制**会**把主程序拉起来然后自己退出（见上面「通信协议」末尾那条），
> 但它跑不起来是因为缺 stdin 管道；而主程序缺了 Sidecar 会闪退——本地自测请用 `npm run tauri dev`。

## 测试

```bash
uv run pytest tests/test_share_resolver.py -q          # 定向（默认口径）
uv run pytest tests/ -q                                 # 全量，仅发版前跑一次
```

`smoke_*.py` 需要真实 ffmpeg/mpv 环境，不在 CI 里跑。
**别默认全量**——套件的多数用例要起 socket 与线程，慢且噪音大（见 [../docs/开发指南.md](../docs/开发指南.md) §5.2）。

## 许可证

GPL-3.0，继承 [xfangfang/Macast](https://github.com/xfangfang/Macast) 原协议。
