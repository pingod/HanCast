# Macast-Han 已知问题与解决方案

**最后更新**: 2026-10-31

---

## 技术债务

### TD-001: 原始代码依赖过时
- **来源**: Macast-main/
- **问题**: 依赖库版本老旧（CherryPy、pystray 等）
- **影响**: 安全风险、兼容性问题
- **解决**: 全面升级依赖或替换为现代方案

### TD-002: 原始 GUI 代码废弃
- **来源**: macast/gui.py
- **问题**: 使用 pystray 实现系统托盘，与 Tauri 架构冲突
- **影响**: 需要完全重写前端
- **解决**: 由 Tauri 前端替代，gui.py 仅作参考

### TD-003: Python 进程管理
- **来源**: 架构设计
- **问题**: Rust 需要管理 Python 子进程生命周期
- **影响**: 崩溃恢复、资源清理
- **解决**: 实现 PythonBridge 模块，包含健康检查和自动重启

---

## 兼容性问题

### COMP-001: WebKit CSS 兼容性
- **平台**: macOS
- **问题**: 某些 CSS 特性在 WebKit 中不支持或表现不同
- **解决**:
  - 使用 `@supports` 检测
  - 避免最新 CSS 特性
  - 跨平台测试

### COMP-002: 媒体格式支持差异
- **平台**: macOS vs Windows
- **问题**: WebKit 对某些视频格式解码支持有限
- **解决**: 前端不负责解码，全部交给后端处理

### COMP-003: 系统特效 API 差异
- **平台**: 全平台
- **问题**: 每个平台的窗口特效 API 不同
- **解决**: 使用 tauri-plugin-vibrancy 抽象层

### COMP-004: 入站 SOAP 信封带前导空白 → 投屏被回裸 HTTP 500
- **平台**: 全平台（接收投屏侧）
- **发现日期**: 2026-10-25
- **状态**: 🟢 已解决并发布（v2.3.24，2026-10-25）
- **描述**: 某控制点把 `<?xml version="1.0"?>` 前面垫 16 个空格再 POST。libxml 严格按
  XML 规范拒绝，`DLNAProtocol.call()` 当时的 `etree.fromstring` 没有 try，异常冒到
  `server.py` 兜底 catch 回裸 500 —— 动作分发没走到，Device Guard 弹框也就没机会出现，
  用户只看到「投不上」。
- **取证要点**: lxml 该错误的**列号能反推报文形态**（列 = 声明真实起始列 + 5，
  `line 1, column 22` 即前面正好 16 个字符）。当时 SOAP Action/Body 只在 debug 级、
  面板默认 INFO，日志里既无来源 IP 也无 body，只能靠列号猜。
- **解决方案**: 前导空白/BOM 剔除后继续处理（`dlna.py:48-68`、`dlna.py:628-632`）；
  剔完仍不可解析的回 SOAP Fault 402（`dlna.py:633-641`）；兜底 catch 补 IP/Action/path/
  body 前 200 字符的取证（`protocol/server.py:178-192`）。详见 docs/故障排查.md §5.4。
- **验证方法**: `tests/test_dlna_soap_envelope_leniency.py`（**14 项**，tag 后补了 2 项边界：
  空 Body 的 `IndexError` 路径、Fault 自身必须可解析）+ 真起 DLNA server 喂现场报文，断言
  `CurrentURI` 落到动作处理器；**发布后又对装好的产物真验一次**——对 `/Applications/HanCast.app`
  的 2.3.24 打 16 前导空格报文回 200 + `GetTransportInfoResponse`（动作分发正常、日志落剔除告警），
  纯废文回 200 + Fault `402`。
- **发布**: tag `v2.3.24`（提交 `1cac337`），Release 已 publish：
  https://github.com/pingod/HanCast/releases/tag/v2.3.24

---

### COMP-008: 队列/定时/遥控器/字幕缺失（v2.4.6 已补齐）
- **现象**：投屏只能一批替换、没有队列追加与跨重启记忆；播完自动下一集无法关；没有手机遥控；外挂字幕不能内封投屏。4.69GB 抖音文案一类场景尤其明显。
- **方案**：playlist 持久化 + 追加、cast 播完即停/睡眠定时、`remote/server.py` 手机遥控页（一次性 token）、`media/subtitles.py` `-c copy` 内封 + 重投续播。
- **状态**：🟢 v2.4.6 工作区已修未发版（待装端）。

### COMP-007: 出站投屏失败无痕（SOAP 出站仅 DEBUG，电视不动时查不到断在哪）

- **状态**: 🟢 已解决（v2.4.5 工作区，未发版）
- **现象**: 解析成功、点投屏无报错，电视不动；面板默认 INFO 看不到我们发给谁、电视回了什么。COMP-005 因此排查大半天。
- **根因**: `protocol/dlna.py` 的 `_send_action` 成功只 `logger.debug`；失败散落、无台账。
- **方案**: ① 出站动作成功收 INFO 一行 `SOAP OUT ← <status> | <action> | <device> | <ms>ms`（`dlna.py:1256-1258`）；② 新增 `media/cast_failures.py` 投屏失败台账 + `cast_failure_list`/`cast_failure_clear` + `LogsPanel` 展示；③ `probe_url`（`commands.py:826`）历史条目重投前预检直链、url 条目自动回来源页重解。
- **判据**: 面板「投屏失败台账」有条目 = 断在某阶段（connect/set_uri/play/stop/media_ready/cast）；契约测试 `tests/test_cast_failures.py`。

## 架构风险

### RISK-001: Python 启动延迟
- **描述**: Python 进程启动可能较慢
- **缓解**:
  - 预启动 Python 进程
  - 显示加载动画
  - 异步初始化

### RISK-002: 端口冲突
- **描述**: DLNA 服务需要固定端口，可能与其他应用冲突
- **缓解**:
  - 端口自动检测
  - 配置文件可修改
  - 启动时检查端口可用性

### RISK-003: 防火墙拦截
- **描述**: SSDP 多播可能被防火墙拦截
- **缓解**:
  - 启动时检测网络环境
  - 提供防火墙配置提示
  - 文档说明

---

### COMP-005: TCL 电视只按 URL 后缀决定拉不拉流 → 无后缀直链「投不上」
- **平台**: 全平台（发出投屏侧；接收端 = 只认后缀的渲染器，实测 TCL 85T8G Max）
- **发现日期**: 2026-10-28
- **状态**: 🟢 已解决并发布（v2.4.0，2026-10-29；后缀门 + 交付代理，本机已装到 2.4.3）
- **描述**: 抖音分享文案解析出直链、`Created cast session` 也打了，但 TCL 电视一次画面都没有；
  同一时刻投给局域网另一台 Linux HanCast（mpv 接收端）能正常播。全程 0 ERROR/WARNING——
  因为旧的出站 `_send_action` 成功只打 debug 级，面板默认 INFO 看不到。
- **根因**: 这台 TCL **完全无视 DIDL 的 `protocolInfo`，只按 URL 路径末尾的扩展名决定要不要发起拉流**；
  无后缀就连一次 GET 都不发。抖音 CDN 直链是 `…/video/tos/cn/tos-cn-ve-15/<hash>/`（末尾斜杠、无后缀）。
  B 站直链带 `.mp4` 所以正常；Linux 那台 mpv 不看后缀所以也能播。
- **取证要点（真机矩阵，判据 = 服务端有没有出现电视的 GET）**: 本机可控信源同字节 MP4 对比——
  `/ext/test.mp4`、`/ext/test.mp4?vid=1`、`/proxy/a1b2c3.mp4`、MP4 字节挂 `/proxy/a1b2c3.ts` 都能拉；
  同字节 `/noext/stream?vid=1` 带满 DIDL **零连接**。被证伪并排除：**不是 IPv6**（本机 IPv6-only 服务电视用
  GUA 正常拉）、**不是空 DIDL**。⚠️ 同一条抖音签名地址反复投喂约 10 次后被 CDN 判废，读数作废——
  排障必须换可控信源、一次只动一个变量。
- **解决方案**: `player_ext_ok()` 后缀白名单扩为「分片 + 交付直链」同一把尺（无后缀算不合格）；
  `player_safe_suffix(url, mime="")` 按 MIME 给代理路径补后缀（未知兜 `.ts`，字节仍上游原样）；
  `_tv_can_fetch()` 末尾加后缀门，不合格改走本机代理（脏 MIME 的播放列表例外，维持直链）；
  顺带修 `models/session.py` `play()` 丢 `mime_type`（媒体投屏 `CurrentURIMetaData` 此前恒为空）。
  详见 docs/故障排查.md §5.5。
- **验证方法**: `tests/test_tv_url_suffix.py`；`pytest tests/test_tv_url_suffix.py tests/test_share_resolver.py`
  **81 passed**，后端全量 **543 passed / 0 failed**；真机端到端——真起 MediaServer + 真 ShareResolver 解析
  现场文案 → 交付 `http://192.168.1.5:<port>/proxy/…mp4` → 裸 SOAP 投 TCL → `Proxy response: status=206`、
  电视 `Dur=00:04:55` 且 `AbsTime` 1→13 秒前进 ⇒ 投得动了。
- **待办（发布轮）**: `npm run version:set` bump、打 tag、重建装机，CHANGELOG 已留「未发布 — 2026-10-28」节。

### COMP-006: 后端测试套件会覆盖用户真实 hancast_setting.json（跑一次测试污染一次配置）
- **平台**: 全平台（开发/CI）
- **发现日期**: 2026-10-30（v2.4.4 开工时顺带发现）
- **状态**: 🟢 已解决（v2.4.4 工作区，已发版）
- **描述**: 本机 `~/Library/Application Support/HanCast/hancast_setting.json` 被写坏几十轮：
  `default_device` 变成 `"test-id"`、多了 `test_key:"test_value"`，用户真实选择被覆盖。
- **根因**: `hancast-backend/tests/test_commands.py` 的 `setUp` 直接构造 `CommandHandler()`，
  `tearDown` 调 `cleanup()` → `config.save()` 写回真实路径；而 `utils/config.py` 的 `SETTING_DIR`、
  `identity.py`、`logger.py` 的 `LOG_DIR` 都是 **import 时**定值，所以事后 patch `config.SETTING_DIR` 无效。
- **解决方案**: `tests/conftest.py` 在 import 任何 `hancast_sidecar` 模块之前把
  `HOME`/`USERPROFILE`/`APPDATA`/`LOCALAPPDATA` 重定向到一次性临时目录（`tempfile.mkdtemp`），
  并在三道启动自检里断言配置目录确实落在临时 HOME 下；新增 `tests/test_config_isolation.py` 钉死。
- **验证方法**: `pytest tests/test_config_isolation.py` 5 passed；全量 563 passed；跑完再查真实
  `hancast_setting.json` 的 mtime 不再变化。现场已清理被污染的 `default_device`/`test_key`（备份在 /tmp）。

## 解决方案模板

```markdown
### ISSUE-XXX: [标题]
- **发现日期**: YYYY-MM-DD
- **严重程度**: 高/中/低
- **状态**: 🔴 未解决 / 🟡 进行中 / 🟢 已解决
- **描述**: [问题描述]
- **复现步骤**: [步骤]
- **解决方案**: [方案]
- **验证方法**: [如何验证修复]
```
