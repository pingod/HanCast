# HanCast 开发进度

## 当前阶段：v2.3.19 工作区前端优化（尚未发布）

**最后更新**: 2026-10-28

本轮已安全拉取 `origin/feat/js-rule-plugins`，远端无新增，本地比 `origin/main` 多 5 笔既有提交。用户确认“克制精致的原生工具”风格：统一紫色亮暗主题、媒体/设备卡片、设置分组、弹窗边界及键盘操作；保持 Linux 不透明策略。修复解析结果竞态、App 路由切换丢失投屏监听、迟到监听清理、主题缓存、播控错误吞掉、进度锁定/旧轮询回写、视频被当图片加载；去掉无功能的多房同步装饰。

验证：媒体/主题 73 项、播控原 20 项及封面新增 1 项、真实 App 生命周期 2 项；类型检查及 Vite 构建通过。浏览器验 346×472 / 434×634 / 800×900、中英设置、日志/屏幕/控制器弹窗、解析/暂停/停止/清空（项目内置 mock，不是真机投屏）。未跑全量测试、未打三平台包、未提交或发布。界面截图在 `docs/images/ui-native-{light,dark}.png`。

## 2026-10-28 发布轮：v2.4.0（已发布 + 已装 macOS/Linux ✅）

v2.3.24 → **2.4.0**（minor，用户选）。合并两笔未发布改动：投 TCL「投不上」的后缀门修复 + 本地文件「整个文件夹」投屏 / 播放队列。`npm run version:set -- 2.4.0`；提交 `64a815c`；tag `v2.4.0`；Release 已 publish（draft=false，2026-10-28T11:21:19Z，body 已写）。收尾 CHANGELOG 验收证据提交 `a5eb00f`。

- **CI run 40650382734**：macos-arm64 首次因 GitHub 托管 runner 容量排队超时被**取消**（不是构建失败，`--job` 重跑后成功）；windows-x64、linux-x64 一次过；"Upload to draft Release" 需三平台全绿才跑。
- **macOS**：dmg（`e5ac4c1d…` 逐字节一致）装入 `/Applications`，`accept-macos-artifact.sh 2.4.0` → **85 PASS / 0 真 FAIL**（唯一 FAIL = 沙箱 osascript 退不掉 GUI，环境限制；FFmpeg 依赖用临时 HOME 探针手补：`ffmpeg_status` ok=True source=system ffmpeg 9.0.2）。产物内 `media/server.py` sha256 `be41bb3c…` 与仓库一致，后缀门代码在包内。已 `open -a` 拉起 2.4.0，sidecar 在 :8080 监听。
- **Linux（CachyOS 192.168.1.68，ssh 端口 10086 用户 pavia，sudo 免密）**：Arch 包 `96cafdef…` 两端一致，`sudo pacman -U` → `pacman -Q hancast = 2.4.0-1`，sidecar `self.version="2.4.0"`。装前用 `kill -TERM` 优雅退出 GUI+sidecar（沙箱 osascript 不可用）。
- **功能实测（装好的 macOS 产物）**：`list_folder_media`（参数名是 `path`）对 `第1集/第2集/第10集/花絮.mkv/readme.txt` 返回自然序 **第1集→第2集→第10集→花絮**、跳过 txt、mime 正确。
- **踩坑**：draft 资产 `gh release download` 会静默给 415KB 坏文件 ⇒ 必须 API + token 取签名 URL；GitHub 单连接 ~7-22KB/s、部分字节区间反复 stall ⇒ 12 段并发 `curl -r` + 分段 resume；分段拼接后必须逐段 sha 校验，本轮 part.4 曾因重复追加多 4096 字节导致整包 sha 不符，定位后单段重下修复。
- **未闭合**：Windows 那台（ssh `Host win`，`~/.ssh/config` → 192.168.1.68:22，但那是 Linux 的 IP 冲突/历史记录待核；用户此前描述为 `%OS%=Windows_NT` / `administrator`，现 2.3.15）待机器上线核实后再装；TCL 现场需用户在电视端真投一次确认可播。

## 2026-10-28 三续：本地文件投「整个文件夹」+ 播放队列（工作区，未发版 → 已由 v2.4.0 发布轮发布）

用户反馈："能否对投屏本地文件时候再丰富下场景…可以投屏整个文件夹，而且可以调整文件夹中文件的列队顺序"。四项全选（文件夹队列+排序+上下一集 / 顺序记忆 / 勾选子集 / 随机+单曲循环）。

实现：后端 `list_folder_media`（`media/parser.py:27` `MediaParser.list_folder`，自然序、只列当前层、跳过隐藏/`._`/非媒体、不探时长）；壳层 `lib.rs:247`；Sidecar `commands.py:662`。前端新增 `src/stores/playlist.ts`（Pinia 第 10 个）、`MediaInput.vue` 的📁入口 + 队列面板（拖拽/↑↓排序/勾选/全选/播放模式切换）、`CastController.vue` 上/下一集按钮、`cast.ts` 的 `advancePlaylistIfActive`/`skipToNext`/`skipToPrev`/`castQueueItem`（`queueAdvancing` Set + 1.5s 窗口吞 STOPPED 抖动防重复起播）。i18n 加一组 `media.*`（两语言各 346 键），清掉脚手架期未引用 key。

测试：新增 `tests/test_folder_playlist.py` 6 项；后端全量 **549 passed**；vue-tsc / vite build 通过；`tools/verify-cast-ui.cjs` 补 `@/stores/playlist` 空队列桩后 **23/23**。命令数 67→68（壳层）/ 62→63（Sidecar）。

文档同轮：AGENTS.md（命令数/store 数/测试数/命令清单）、Frontend-API（`list_folder_media` 行 + §4 计数）、Frontend-Spec（§6.5 新节 + store 表 + cast.ts 行数 + §8.3 key 数）、README（68 条 invoke）、CHANGELOG「未发布」节加子节；全仓 `commands.ts/.py`、`MediaInput.vue`、`lib.rs`、zh-CN.json、parser.py 的行号引用按 diff 重映射。两道闸门绿（audit 3767 可疑 0、freshness FAIL 0/WARN 0、defscan 仅 7 条既有 SOFT）。

未做：未 bump 版本、未打 tag、未重装 `/Applications`。Windows 装 2.3.24 任务（m01252）仍挂着未完成（`/tmp/chunks_keep` 分块未拼装）。

## 2026-10-28 续：投 TCL 电视「投不上」——渲染端只认 URL 后缀（工作区，未发版）

用户反馈：抖音分享文案 `https://v.douyin.com/cAS0o-smMyw/` 解析后投 TCL 电视没反应，投另一台 Linux 的 HanCast 正常。定位到根因不是网络/格式，而是**这台 TCL 85T8G Max 只按 URL 路径末尾扩展名决定要不要发起拉流**——无后缀连一次 GET 都不发。抖音 CDN 直链 `…/tos-cn-ve-15/<hash>/`（斜杠结尾、无后缀）永远不被拉；B 站直链带 `.mp4` 一直正常；mpv 接收端不看后缀所以 Linux 那台也能播。

改动：`media/server.py` 的 `player_ext_ok` / `player_safe_suffix(url, mime="")`、`media/share_resolver.py` 的 `_tv_can_fetch` 末尾后缀门、`models/session.py` `play()` 保留 `mime_type`。新增 `tests/test_tv_url_suffix.py`，相关 81 passed，后端全量 **543 passed / 0 failed**。真机端到端：真起 MediaServer+ShareResolver 解析现场文案 → 交付 `/proxy/…mp4` → 投 TCL → status=206、电视 `Dur=00:04:55` 且 `AbsTime` 前进 ⇒ 修好了。

文档/记忆同轮：故障排查 §5.5 + §11 两行、分享解析规则 §1、架构总览行数表 3 行、全仓 15 处 `media/server.py|share_resolver.py|session.py:行号` 重校；7 份文档校对戳补本轮说明；CHANGELOG 加「未发布 — 2026-10-28」节；issues.md 新增 COMP-005。两道闸门绿（audit 3735 可疑 0、freshness FAIL 0/WARN 0）。

未做：未 bump 版本、未打 tag、未重装 `/Applications`（仍是 2.3.24）→ 发布轮再走。Windows 那台装 2.3.24 的任务（m01252）此前被这个新问题打断，分块下载仍在 `/tmp/chunks_keep`，未完成。

## 2026-10-25 发布轮：v2.3.24（已发布 ✅）

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

### 收尾：CI、装端验收与 publish（已完成）

- **CI run 40172640774 全绿**：macos-arm64 12m32s / linux-x64 19m14s / windows-x64 21m28s /
  Resolve 5s / Upload to draft Release 19s。Linux 那条含 Arch `.pkg.tar.zst` 打包与容器内
  `pacman -U` 实装冒烟。
- **dmg 取回的坑**：85,272,728 字节走 GitHub 直连只有 ~22 KB/s；`gh release download` 拿到的是
  **415 KB 的坏文件**（draft 资产的公开 URL 是 404，回的是错误页），`hdiutil verify` 直接不认。
  正解：取 asset 签名 URL 后**分 12 段并发** `curl -r`（单连接限速约 22 KB/s，12 路并发约 10 分钟拉完），
  拼接后 sha256 与 GitHub 声明 `ca192f85…` 逐字节一致、`hdiutil verify` CRC VALID 才算数。
  教训：`gh release download` 对 **draft** 资产的失败态是「静默给个坏文件」，必须自己校验摘要。
- **装端验收**：`install-macos-artifact.sh 2.3.24` → `accept-macos-artifact.sh 2.3.24`
  **85 PASS / 0 真 FAIL**；产物内 20 份 sidecar 源码 sha256 与仓库逐个对齐（含本轮
  `protocol/dlna.py` `dfcd668ab262…`、`protocol/server.py` `6d3869cdadfe…`）。
  其中「FFmpeg 依赖实测」那条曾报 FAIL，根因是验收脚本自己 `osascript` 退 GUI 失败（见下），
  绕开后单独跑探针：**四项全过**（检出系统 ffmpeg 9.0.2 且真能跑、托管目录落在临时 HOME 内不污染本机、
  ffmpeg+ffprobe 两条齐、缺失时清单/安装命令/下载源齐全）。
- **在装好的产物上真验容错**（不只有单测）：对 `127.0.0.1:8080` 发现场那条 16 前导空格报文
  （`GetTransportInfo`）→ HTTP 200 + `GetTransportInfoResponse`、无 Fault、动作正常分发，
  日志落 `WARNING SOAP body from 127.0.0.1 带 16 字节前导空白/BOM，已剔除后继续处理`；
  对照组纯废文 `not xml at all` → HTTP 200 + `<s:Fault>` `errorCode=402`，不再是裸 500。
- **已 publish**：`gh release edit v2.3.24 --draft=false` → https://github.com/pingod/HanCast/releases/tag/v2.3.24
  （2026-10-25T12:12:33Z），10 项资产齐全，不带 token 的公开 URL 回 200。
  Release 正文按用户可感知口径重写（修了什么 / 验证 / 已知限制），没照搬提交信息。

### 两个脚本坑（已修，提交 `4eaa35d`）

- **`install-macos-artifact.sh:64` 在 UTF-8 locale 下必崩**：`echo "已安装: $OUT（原 …"` 里变量紧跟
  全角左括号，bash 把 `（` 的字节吞进变量名 → `set -u` 报 `OUT�: unbound variable`。
  `LANG=en_US.UTF-8` / `zh_CN.UTF-8` 必现，`LC_ALL=C` 不报（本机 `LANG=""`，所以只在装包链路上现形）。
  危害：崩在 `mv`/`ditto` **之后**，包其实装好了，只是尾部 `codesign` 自检没跑，容易被误读成「安装失败」。
  修法 `$VAR` → `${VAR}`；顺带扫出同类隐患 **15 处**（`accept-macos-artifact.sh` 3、
  `verify-linux-window-opacity.sh` 8、`verify-windows-artifact.sh` 4），全在
  `say "FAIL … $cmd（…"` 这种**报 FAIL 的行**上——验收脚本会在最该出声时哑掉。
- **「`/Applications/HanCast.app` 当前用户不可写（root:wheel 遗留）」是误报**：实测归属一直
  `pavia:staff drwxr-xr-x`，是 agent 沙箱拦工作区外写入（连 `sudo` 都 `Operation not permitted`）。
  **别照提示 `sudo chown`**，那会把归属改坏。

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
