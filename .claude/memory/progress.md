# HanCast 开发进度

## 当前阶段：v2.3.19 工作区前端优化（尚未发布）

**最后更新**: 2026-10-24

本轮已安全拉取 `origin/feat/js-rule-plugins`，远端无新增，本地比 `origin/main` 多 5 笔既有提交。用户确认“克制精致的原生工具”风格：统一紫色亮暗主题、媒体/设备卡片、设置分组、弹窗边界及键盘操作；保持 Linux 不透明策略。修复解析结果竞态、App 路由切换丢失投屏监听、迟到监听清理、主题缓存、播控错误吞掉、进度锁定/旧轮询回写、视频被当图片加载；去掉无功能的多房同步装饰。

验证：媒体/主题 73 项、播控原 20 项及封面新增 1 项、真实 App 生命周期 2 项；类型检查及 Vite 构建通过。浏览器验 346×472 / 434×634 / 800×900、中英设置、日志/屏幕/控制器弹窗、解析/暂停/停止/清空（项目内置 mock，不是真机投屏）。未跑全量测试、未打三平台包、未提交或发布。界面截图在 `docs/images/ui-native-{light,dark}.png`。

## 2026-10-25 发布轮：v2.3.24（tag 已打，CI 产物与 publish 待验）

用户授权：「以后你自己来管理这个项目，像版本、打 tag、changelog 等等都由你来定，不用等我授权」
——口径与三条边界写进 `decisions.md` **DEC-006**（跨 minor 先问、不许放宽验收判据、
publish 要装端验收）。

补齐与修正：

1. **`test_ghmirror.py` 三条长期红**修好（根因在桩不在产品：`FakeNet` 对 dict 路由只喂
   `resp.json()`，而索引那条路 v2.3.20 起经 `_download()` 流式取字节，桩给空 body →
   `json.loads` 抛 ValueError 被当成镜像失败多退一次直连）。**后端全量 522 passed / 0 failed**。
2. `npm run version:set -- 2.3.24` 刷 7 处契约点；AGENTS.md 概览版本行与
   `hancast-backend/README.md` 的「当前 v2.3.2x」两处手写表述同步（sync 脚本不管这些）。
3. CHANGELOG 新增 v2.3.24 一节（含取证方法「列号 = 声明真实起始列 + 5」与四条未闭合口子）。
   两处自订数字实测校正：行号引用重校是 **52 处/44 个不同引用**（先写成 50，改）；
   校对戳 **19 份**。
4. 全仓 19 处 md 校对戳盖到 v2.3.24（闸门 `--max-lag 0` 要求等于当前版）。

验收（tag 前做完，项目纪律）：后端全量 522 passed；`npm run build`（vue-tsc + vite）通过；
`tools/verify-cast-ui.cjs` 23/23；`tools/build-rule-packs.py --check` 4 包同步；
两道文档闸门绿（3674 处引用可疑 0 / FAIL 0 WARN 0）；
`tools/verify-params-ui.mjs` 需先起 `vite --port 5199`，且脚本启动 Chrome 时没带
`--disable-gpu`/`--no-sandbox`，在受限环境里 Chrome 直接崩
（`GPU process isn't usable. Goodbye.`）——本轮用**临时副本**补这两个参数跑出 ALL PASS，
**没有改脚本本体**（改验收工具=放宽判据，留给用户定）。临时副本与 vite 进程都已清理。

待办：CI 产物到手后跑 `tools/install-macos-artifact.sh` + `accept-macos-artifact.sh` 并
**真投一次**入站 SOAP 那条例外路径，才 `gh release edit v2.3.24 --draft=false`。
本机 /Applications 仍是 2.3.23，装新版前现场仍会复现旧报错。
## 2026-10-25 续：投屏「投不上」——入站 SOAP 信封容错（未发版）

用户报「客户端投屏投不上」。日志（`~/Library/Application Support/HanCast/logs/hancast.log`）
当天唯一异常是 6 次 `hancast.dlna.server ERROR: SOAP error: XML declaration allowed only
at the start of the document, line 1, column 22`，时间点正对投屏那一刻。定位为**入站**方向
（别的设备投给本机），不是本机投电视；另需注意 12:14:58 两台 TCL 已 byebye 下线，
若用户其实想投电视则是另一码事（设备列表里没有它）。

用打包同款 lxml 复现：**16 个前导空格 + `<?xml version="1.0"?>`** 精确产生该列号
（BOM、重复声明、DIDL 内嵌声明各自的报错都不同，据此排除）。两处一起改：

1. `protocol/dlna.py`：新增 `strip_soap_leading_junk()`（:48-68），`call()` 先剔前导
   空白/BOM 再解析（:628-632 打 WARNING 点名对端与字节数）；解析仍失败回
   **SOAP Fault 402** 而非冒异常（:633-641，UPnP 未为「信封本身不合法」定义错误码，
   取最贴近的 402）。
2. `protocol/server.py`：`do_POST` 的兜底 catch 改为自带取证（:178-192，IP + SOAPAction +
   path + 字节数 + body 前 200 字符；正文不整段落盘），`caller_ip` 提到 try 之前（:143）。

行号漂移处理：dlna.py +51 行、server.py +17 行，按 diff 映射把全仓 50 处
`dlna.py|server.py:行号` 引用换算重校（含 AGENTS.md、架构总览、投屏确认、故障排查、
开发指南、Frontend-API），并手改 3 处 `caller_ip` 指向。文档同轮补齐：
故障排查新增 §5.4 + 症状索引一行 + §11 关键字表三行；投屏确认 §2.2 图示与步骤 1。

顺带校准此前**就已**过期的硬数字（非本次引入）：架构总览模块行数表 6 行
（params 338→402、ssdp 1264→1322、parser 159→163、share_resolver 830→805、
rules/base 253→272、rules/declarative 281→289）、AGENTS.md 的 test_ 数 23→29、
ffmpeg_manager 1156→1243、ffmpeg_deps 524→541、开发指南 test_jsplugins 747→746（既有 off-by-one）。

验证：`tools/audit-doc-refs.py` 3647 处可疑 0；`tools/check-doc-freshness.py` FAIL 0 WARN 0；
相关用例 65 passed；全量后端 **519 passed / 3 failed**——3 条失败在 `test_ghmirror.py`，
已用 stash 回退到基线复跑确认**先于本次改动就坏**（`tests/test_ghmirror.py:136` KeyError：
假注册表缺直连 URL 那条键），未在本次修。

当时未做（**同日晚些已由 v2.3.24 发布轮做完**，见下面那一节）：未 bump 版本、未打 tag、
未补 CHANGELOG；未改 /Applications 里已装的 2.3.23 产物，所以本机现场当时仍会复现该报错。
## 2026-10-24 续：后端四项复查问题已修

同一工作区（未提交、未 bump、未打 tag）。四项全部落地并有离线并发用例：

1. 规则包三条慢操作任务化（`media/rules/tasks.py` 新增；`commands.py` 注册
   `plugin_task_start/result/cancel`，Sidecar 注册表 59 → **62**，`commands.py:131-222`）；
   壳层 `run_plugin_task` 轮询（`lib.rs:324-390`），前端封装与 Tauri 命令名不变。
   安装改为「暂存目录 → rename」，失败退回旧包；取消与提交互斥。
2. 轮询每代独立取消令牌 + `callback(session, event_type, generation)`，
   处理器在 `_cast_lock` 下校验实例身份与代次，旧回调不再误删新会话。
3. 会话带 `owner`，屏幕通道只停自己那份；媒体投屏开播前 `stop_for_device` 让屏幕让位。
4. 启动期间采集错误记 `_pending_error` 并统一回滚，不再「返回成功却没画面」。

验证：`test_plugin_tasks` 10、`test_session_poll_generation` 与 `test_stop_cast_delivery`
合计 42、`test_screencast` 全量、`test_jsplugins` 43 全绿；`cargo check` 通过。
契约同步：AGENTS.md、hancast-backend/README.md、docs/{架构总览,HanCast-Frontend-API,
HanCast-Frontend-Spec,README,开发指南}.md 的计数与 §11 状态。
**未做**：真机复现这些失败、三平台打包、发布。

---

## 历史记录：2026-07-05 项目初始化

以下保留当时进度，不代表当前实现状态。

---

## 总体进度

| 阶段 | 状态 | 进度 |
|------|------|------|
| Phase 0: 项目初始化 | 🔄 进行中 | 30% |
| Phase 1: 基础架构 | ⏳ 待开始 | 0% |
| Phase 2: 前端 UI | ⏳ 待开始 | 0% |
| Phase 3: 后端功能 | ⏳ 待开始 | 0% |
| Phase 4: 集成优化 | ⏳ 待开始 | 0% |

---

## Phase 0: 项目初始化

### 已完成
- [x] 分析原始 Macast 1.x 代码结构
- [x] 阅读并理解功能规格文档
- [x] 创建 CLAUDE.md 项目指南
- [x] 创建 .claude/memory 记忆系统
- [x] 识别可复用代码模块
- [x] 拆解前后端设计文档
  - [x] `docs/Macast-Frontend-Spec.md` — 前端 UI/UX 方案
  - [x] `docs/Macast-Backend-Spec.md` — 后端 Rust+Python 方案

### 进行中
- [ ] 初始化 Tauri 2.0 项目骨架
- [ ] 配置 Python 后端虚拟环境
- [ ] 设计 Rust ↔ Python 通信协议

### 待开始
- [ ] 配置开发环境文档
- [ ] 创建 CI/CD 配置

---

## 关键里程碑

| 里程碑 | 目标日期 | 状态 |
|--------|----------|------|
| M1: 项目骨架完成 | TBD | ⏳ |
| M2: 基础 UI 可运行 | TBD | ⏳ |
| M3: SSDP 发现可用 | TBD | ⏳ |
| M4: DLNA 投屏可用 | TBD | ⏳ |
| M5: 首个 Beta 版本 | TBD | ⏳ |

---

## 会话记录

### 2026-07-05 — 首次会话
- 完成项目结构分析
- 确定技术栈：Tauri 2.0 + Python
- 创建项目文档体系
- 原始代码最后更新于 2022-01，需要大量现代化改造

### 2026-07-05 — 前后端拆解
- 将原始功能规格文档拆解为两个独立方案
- **前端方案** (Macast-Frontend-Spec.md):
  - Vue 3 + Vite + TypeScript + Pinia
  - Tauri invoke() 命令层定义
  - 组件规格、样式系统、国际化
  - WebView 兼容性约束
- **后端方案** (Macast-Backend-Spec.md):
  - Rust SidecarManager (stdin/stdout IPC)
  - Python Sidecar 入口 + 命令路由器
  - 原始代码复用映射表 (ssdp/protocol/renderer/mpv)
  - 新增 MediaParser + MediaServer
  - 完整通信协议定义
  - PyInstaller 打包流程
