# Macast-Han 已知问题与解决方案

**最后更新**: 2026-11-06

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

### COMP-009: 本机无法作为投屏目标（自己投自己）——设备列表只认 SSDP 发现的机器
- **现象**：HanCast 本身能当 DLNA 渲染端，但设备列表只列 SSDP 发现的局域网设备；本机把自己广播出去的那台被当成「另一台设备」且常被隐藏/忽略，用户没法「投给本机」（全屏本机看 / 另一台手机遥控）。
- **方案**：`add_local_device` 按本机 LAN IP + DLNA 端口显式登记本机，顺手写进 Device Guard 信任列表（避免自己弹确认），不写 `manual_devices`（IP 每次启动重算）。前端 `AddDeviceModal.vue` 一键按钮。
- **状态**：🟢 v2.4.7 已发布（tag `v2.4.7`，CI 41585153134 三平台全绿，macOS 装端 89 PASS / 0 FAIL；产物上 `add_local_device` 端到端跑通）。真机「真投一集」未手动复测。

### COMP-010: 手机遥控器页能力弱（需先投屏、不能选片）——v2.4.8 升级
- **现象**：v2.4.6 的遥控器页要手动 `start_remote_control` 先投屏才有用，只有播放/暂停/音量/进度几个按钮；选片还得回电脑操作，手机上不能直接挑视频投。
- **方案**：`remote/server.py` 重写为随应用常驻（`_start_services` 启动，8899 占用回退）；新增 `/api/upload`（相册上传即投）、`/api/paste`（粘链接转解析投）、`/api/resolve`（轮询）；队列/历史/多设备/进度条/防息屏/横屏；`remote_update_ui` 命令让前端把队列/历史/模式同步给后端读取。
- **状态**：🟢 v2.4.8 已发布（commit `38b6aab`、tag `v2.4.8`、CI 41598558501 三平台全绿、macOS 装端 90 PASS/0 FAIL；产物上遥控器页随应用常驻、/api/state 与 /api/cmd 端到端通）。真机手机浏览器真开/相册真上传未手动复测。

### COMP-011: 设备列表有设备却又显示「还没有发现设备」空状态块（v2.4.9 已修）
- **现象**：首页已渲染出 LAN 设备卡片，下方同时叠着「还没有发现设备 / 组播可能被防火墙拦截」的提示块。用户截图质问「这么明显的bug没发现??」。
- **根因**：`src/components/DeviceList.vue` 的 `v-else` 空状态块贴在「隐藏设备」按钮的 `v-if` 上（Vue 的 v-else 只认**紧邻上一个** v-if/v-else-if），于是空状态的渲染条件是「没有隐藏设备」，与有没有设备无关。
- **引入版本**：v2.4.4（commit `e1d4922` 加隐藏设备按钮时顺手把空状态挤成了 v-else），跨 v2.4.5/v2.4.6/v2.4.7/v2.4.8 四个版本未被发现——因为只有「有设备 + 无隐藏设备」这一种组合才暴露，而那正是最常见的使用状态。
- **方案**：空状态块移回 `device-list__items` 之后（紧跟它的 v-else），隐藏按钮移到最后改独立 `v-if`。新增 `tools/verify-device-list-empty.cjs` 4 条用例钉住「列表与空状态互斥」，已做负向验证（还原旧结构即红）。
- **教训**：多个相邻 `v-if` 共存时，`v-else` 绑的是**紧邻的上一个**而不是你以为的那个；改这块布局必须跑 `tools/verify-device-list-empty.cjs`。同类静默退化还有 `tools/verify-cast-ui.cjs`（v2.4.8 加新依赖后从 30/30 掉到 0/30 未被察觉）——验收脚本一旦不再被 CI 卡住，绿变红没人知道。

### COMP-012: 手机遥控器页地址在界面上无处可见 + 令牌每次启动都换（v2.4.10 已修）
- **现象**：用户原话「你好像还没告诉我手机端怎么访问页面,界面上我也没看到任何提示」。遥控器服务从 v2.4.6 就有，但界面上没有任何地方显示地址，只有开发者知道去 `remote_status` 里捞。
- **叠加问题**：`remote/server.py` 每次进程启动都随机生成 token，而文案写着「地址固定不变、可加书签」——昨天存的链接今天就是 404。**文档与实现互相矛盾**。
- **方案**：①新增 `src/components/RemoteControlPanel.vue`（弹窗展示地址、整块可点复制），`HomeView.vue` 首页加常驻「手机遥控」按钮；②`RemoteControlServer.__init__` 加 `token` 参数，由 `CommandHandler` 从配置键 `remote_control_token` 注入，`_remember_remote_token()` 首启生成后幂等回写，`stop_remote_control` 不删该键。
- **判据**：`docs/故障排查.md` §5.8「手机遥控器页：地址在哪、手机怎么连、连不上怎么办」有 6 行连不上判据表。
- **教训**：功能存在 ≠ 用户找得到。凡是要用户手抄/记忆的信息（地址、端口、口令），必须在界面上有一处可见入口，否则等于没做。

### COMP-013: 首页媒体输入区整块消失、且日志里查不到任何痕迹（v2.4.11 已加可观测性，根因未定位）
- **现象**: 2026-11-03 用户截图显示首页「局域网设备」表头上方整块空白——拖拽区/链接输入栏都不见了，设备列表本身正常。
- **排查结论**: 代码路径不可能渲染成空（`HomeView.vue` 无条件渲染 `<MediaInput />`；`MediaInput.vue` 的 `v-if="state !== 'ready'"`/`v-else` 覆盖全部取值；CSS 无隐藏规则）；截图逐行亮度扫描确认那块真没渲染。**只剩「渲染期抛异常被 Vue 卸掉整块」一种解释**，而这类异常此前哪都不落盘。
- **为什么当初查不到**: 前端没有 `app.config.errorHandler`、没有 `unhandledrejection` 监听，`hancast.log` 只收 Python 侧日志；打包版 WebView 不便挂调试器（`safaridriver` 要用户手开「允许远程自动化」）。
- **修法**: ① `frontend_log` Sidecar 命令 + `commands.ts:1620` `frontendLog()` + `main.ts:50` errorHandler ⇒ 前端错误进**同一份** `hancast.log`（行首 `[webview/<source>]`）；② `SectionBoundary.vue` 区块错误边界 ⇒ 出错时原地显示「没能加载 + 错误 + 重试」，不再是一块空白。
- **判据**: 再出现时按 `docs/故障排查.md` §5.9 第 1 步筛 `[webview/` 日志；`node tools/verify-section-boundary.cjs` 验证边界本身没坏。

### COMP-008: 队列/定时/遥控器/字幕缺失（v2.4.6 已补齐）
- **现象**：投屏只能一批替换、没有队列追加与跨重启记忆；播完自动下一集无法关；没有手机遥控；外挂字幕不能内封投屏。4.69GB 抖音文案一类场景尤其明显。
- **方案**：playlist 持久化 + 追加、cast 播完即停/睡眠定时、`remote/server.py` 手机遥控页（一次性 token）、`media/subtitles.py` `-c copy` 内封 + 重投续播。
- **状态**：🟢 v2.4.6 已发布产物（tag `v2.4.6`，CI 41163007137 全绿，macOS 装端 89 PASS / 0 FAIL / 0 SKIP；Release 仍 draft，等用户统一测）。

### COMP-007: 出站投屏失败无痕（SOAP 出站仅 DEBUG，电视不动时查不到断在哪）

- **状态**: 🟢 已解决（v2.4.5 工作区，未发版）
- **现象**: 解析成功、点投屏无报错，电视不动；面板默认 INFO 看不到我们发给谁、电视回了什么。COMP-005 因此排查大半天。
- **根因**: `protocol/dlna.py` 的 `_send_action` 成功只 `logger.debug`；失败散落、无台账。
- **方案**: ① 出站动作成功收 INFO 一行 `SOAP OUT ← <status> | <action> | <device> | <ms>ms`（`dlna.py:1256-1258`）；② 新增 `media/cast_failures.py` 投屏失败台账 + `cast_failure_list`/`cast_failure_clear` + `LogsPanel` 展示；③ `probe_url`（commands.py:867）历史条目重投前预检直链、url 条目自动回来源页重解。
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

### ISSUE-021: 更新检查把用户打发到原作者的 gitee 仓库
- **发现日期**: 2026-11-06（用户报「点更新的时候还会出 gitee 这类的原始作者的地址」）
- **严重程度**: 高
- **状态**: 🟢 已解决（v2.4.12，未发布）
- **描述**: `check_update` 的 `sources` 由壳层硬编码成 `["github","gitee"]`，gitee 那条指向
  `gitee.com/buxiangqumingzi/han-cast`——那是**原作者**（lanzeweie 线）的镜像，
  它的 `releases/latest` 停在 **v2.1.0**、正文是原作者的 changelog，且自 v2.1.0 起从未同步过
  （仓库里没有 gitee remote，`push-gitee.ps1` 推的是不存在的 `master` 分支）。
  于是 GitHub 一不通（墙内常态）gitee 就顶上来：装了 2.4.x 的用户看到「有新版 v2.1.0」，
  点下载按钮（`DownloadModal.vue` 的三颗外链按钮之一）被送到原作者页面。
- **复现步骤**: 断掉 GitHub 可达性（或把 `api.github.com` 指向黑洞），点设置页「检查更新」，
  返回体 `source` 为 `gitee`、`latest` 为 `2.1.0`。
- **解决方案**: gitee 这条源**从更新链路彻底删除**（`_fetch_github_release` 与 `source_configs` 一起没了，
  `_check_update` 改为薄壳调 `update/manager.check_for_update`）；壳层不再下发 `sources`；
  `DownloadModal.vue` 删除，更新改为应用内「挑产物 → 下载 → 校验 → 装」；
  README / 推文里那两条 gitee 链接标注过期。国内下载能力改由既有后台参数
  `github.mirror_enabled` / `github.mirror_prefix` + 更新弹窗里的开关承担。
- **验证方法**: `tests/test_ghmirror.py::TestUpdateCheckMirror::test_no_gitee_anywhere_in_the_update_chain`
  （既断言请求 URL 里没有 gitee，也直接读源码断言 `commands.py` / `update/manager.py` 里不含 `gitee.com`）；
  `node tools/verify-update-ui.mjs` 的「弹窗内无 gitee / 原作者 / 商店地址」判据。

### ISSUE-022: GitHub 的 `releases/latest` 按「最近 publish」给结果，不是按版本号
- **发现日期**: 2026-11-06（对账时踩中，5 分钟内发现并回滚）
- **严重程度**: 高
- **状态**: 🟢 已解决（代码不再依赖该接口，并加了闸门）
- **描述**: 发布面对账时为产物从未产出的 `v2.3.8` 补建了一条**无产物**的说明性 release 并 publish。
  GitHub 把 `/releases/latest` 的 "latest" 判给**最近一次 publish** 的那条，于是它当场从 v2.4.11
  回指 v2.3.8——而更新检查用的就是这个接口：正在跑 2.4.11 的客户端从此一律报「已是最新版本」，
  界面上一个错误都不报。
- **复现步骤**: `gh release create <旧 tag>`（或把任一旧 draft 转成 publish），随后
  `gh api repos/pingod/HanCast/releases/latest --jq .tag_name` 立刻变成那个旧版本。
- **解决方案**: ① 那条 v2.3.8 release 已删（`/releases/latest` 恢复指向 v2.4.11）；
  ② 更新检查改为拉 **Release 列表**接口（`/releases?per_page=100`）自己按语义版本挑，
  排除 draft/prerelease，**不再使用 `/releases/latest`**；
  ③ `tools/audit-releases.py` 新增「latest 指针 == 语义最新版」判据，`release-audit.yml` 每周跑；
  ④ 纪律写进文档：**永远不要为旧版补 publish 任何 release**，`{tag 有, release 无}` 用文档记着。
- **验证方法**: `python3 tools/audit-releases.py`（exit 0，含该判据）；
  `tests/test_update_manager.py::TestLatestSelection`（列表挑选、draft/prerelease 排除、翻页）。

### ISSUE-023: 22 个从未 publish 的 draft 让发布面与更新检查同时失真
- **发现日期**: 2026-11-06（用户报「release 和 tag 都不一致，为啥 release 里会有 draft 标签」）
- **严重程度**: 中
- **状态**: 🟢 已解决
- **描述**: CI 的 release job 历史上写死 `draft: true`（按 DEC-006「先装端验收、后 publish」），
  publish 靠人点。验收没做完就被下一版取代，draft 就永远烂着：实测 63 tag / 42 release 里
  **22 个是 draft**（约 1.67 GiB 产物没有任何用户能下载），另有 21 个 tag 完全没有 release。
  副作用不止"看起来乱"：`releases/latest` 不返回 draft ⇒ draft 攒着不发等于线上更新检查永远报旧版本。
- **解决方案**: ① 22 个已被取代的 draft 全部 `gh release delete <tag> --yes`（**tag 一个没删**，
  备份清单含资产名/字节/sha256）；② build.yml 改为**推 tag 即 publish**，手动 dispatch 才留 draft，
  publish 前加「六件产物齐不齐」闸门；③ 新增 `tools/audit-releases.py` + `release-audit.yml` 每周对账；
  ④ DEC-006 边界③的执行点从「publish 前」移到「打 tag 前」（验收没过就别打 tag）。
- **验证方法**: `python3 tools/audit-releases.py` 现报 `tag 63 / release 20（已发布 20、draft 0）`、全 PASS。

### ISSUE-024: 前端有封装、壳层没登记 —— 「退出并安装」在打包版永久置灰
- **发现日期**: 2026-11-06（写前端文档时对照 `generate_handler!` 发现）
- **严重程度**: 高
- **状态**: 🟢 已解决
- **描述**: `commands.ts` 有 `updateInstallPlan()` → `invoke('update_install_plan')`，Sidecar 也注册了该命令，
  但 **Rust 侧没有这条 `#[tauri::command]`**：打包版下载完成后 `refreshPlan()` 每次 reject，
  `plan` 为 null，弹窗主按钮按 `!plan` 判定禁用 ⇒ **没有任何入口能装**。
  浏览器 mock 里有 `case 'update_install_plan'`，所以 `?mockUpdate=1` 的验收全绿——
  「mock 掩盖壳层缺命令」这一类缺陷与 v2.3.8 的 `curl_cffi` 白名单漏项同构。
- **解决方案**: 补 Rust 转发（`generate_handler!` 88→95）；前端再退一步：取装法失败时按任务表
  的 `kind`/`dest` 兜底拼一份计划，不把这条路锁死；CI 的 deb 冒烟加第三条 `update_download_status`
  断言（`update/manager.py` 是函数内延迟 import，Nuitka 漏收时只有它会炸）。
- **验证方法**: `bash tools/rust-check.sh`；`node tools/verify-update-ui.mjs`（下载完必须能切到
  「退出并安装」并出现「正在退出并安装…」）；`python3 - <<'PY'` 逐条比对 `commands.ts` 里的
  invoke 名与 `generate_handler!` 名单（建议下轮做成 `tools/` 闸门）。

### ISSUE-025: 更新弹窗打开时的那次状态查询会掐掉刚开始的下载（竞态）
- **发现日期**: 2026-11-06（`tools/verify-update-ui.mjs` 首次运行即红）
- **严重程度**: 中
- **状态**: 🟢 已解决
- **描述**: 弹窗打开会 `resumeDownload()` 查一次任务表；用户手快紧接着点「立即下载并安装」，
  那条**更早发出、更晚返回**的查询回包带着旧的 `idle` 落回 store，把 `downloading` 按回 false
  并覆盖任务表。后端确实在下载（下一轮进度照常往上涨），界面却没有进度条、主按钮也不切成「取消」。
- **解决方案**: `stores/update.ts` 引入代次号 `epoch` 与在途标记 `starting`：只有仍属当代的调用
  才允许写状态；起下载在途时把 `idle` 视为「后端还没收到」而不是终态。
- **验证方法**: `tools/verify-update-ui.mjs` 场景 A（进度条宽度必须增长、按钮必须切态）+
  场景 D（慢源上点「取消下载」必须落 `cancelled` 且不再显示进度条）。

### ISSUE-026: 投整个文件夹只出了封面图，后面 39 集一集都不续
- **发现日期**: 2026-11-06（用户报「Windows 投整个文件夹到 macOS 不成功」，怀疑是本轮投屏确认改动）
- **严重程度**: 高（文件夹投屏这个功能在此类目录上等于完全不可用）
- **状态**: 🟢 已解决
- **描述**: 队列自动续播**只有一个**触发点——设备自报 `STOPPED`/`IDLE`（`cast.ts` 的
  `cast_state_changed` 监听）。图片永远等不到那一刻：后端对图片直接跳过进度轮询
  （`models/session.py:88-91`），接收端 mpv 挂静帧也不发 `end-file`。而 `list_folder` 认
  `.jpg`（`media/parser.py:23`），自然序又把 `Cover.jpg` 排在 `《某剧…` 前面（`c` 的码位低于
  `《`），于是队列第 1 项就是封面图，投出去后状态永久停在 playing。
  现场目录：`H:\视频\动漫\JOJO的奇妙冒险1-6季\JOJO的奇妙冒险 S05 黄金之风`（39 个 mp4 + Cover.jpg，
  同级 6 季里 5 季都带封面图，只有 S06 没有——所以「有时候又是好的」）。
- **取证路径**: 发送侧 `%APPDATA%\HanCast\logs\hancast.log` 在 `Play` 之后那句
  `Image cast, skip progress polling: image/jpeg` 是决定性证据；接收侧同一时刻是
  `file-loaded` + `update duration 0:00:00`、只有 video-reconfig 没有 audio-reconfig。
  排查时要否掉「投屏确认」：接收端 `hancast_setting.json` 的 `device_guard.enabled=false`
  时 `guard.check()` 直接 return "trusted" 且不打日志，闸门根本没参与（guard.py/dlna.py 本轮也没改）。
- **复现步骤**: 任一带 `Cover.jpg` 的剧集目录 → Windows 端「📁 选文件夹」→ 投给在线的 HanCast
  接收端 → 观察到只显示封面图、队列面板一直「第 1 / 共 40 项」、日志里不再出现第二次 `SetAVTransportURI`。
- **解决方案**: 发送端计时。`castQueueItem` 投完一项后按 `isImageMedia` 分流，图片项走
  `armImageDwell`（`cast.ts:820`）到点调同一条 `advancePlaylistIfActive`，所以播放模式/播完即停/
  勾选子集语义一律不变；档位 5/8/15/30 秒在队列面板切换并随队列落盘
  （`hancast.playlist.queue` 的 `imageDwellSeconds`）。计时器三条撤除路径：停止（`cast.ts:587`）、
  卸载（`cast.ts:1182`）、被新起播取代（靠 `castOp` 代次）。两个特例：暂停时到点不跳走而是重新挂起，
  `repeat-one` 不计时（否则每 N 秒把同一张图重推一遍只会闪）。
- **验证方法**: `tools/verify-playlist-advance.cjs` 22 项（新增⑥～⑨图片组：到点续播、视频不被提前跳走、
  停止必须撤掉在途计时器、暂停/恢复、repeat-one、档位落盘、纯视频队列不该出现档位按钮）。
  判据做过负向对照：撤掉 `armImageDwell` 调用 → 「停留计时到点自动投下一项」FAIL；
  撤掉 `clearImageDwell(id)` → 「点停止必须撤掉在途计时器」FAIL。
  另修 `tools/verify-cast-ui.cjs`：它的 `@/stores/playlist` 桩没跟住 cast.ts 新增的具名 import
  （`isImageMedia`），会炸在 `castQueueItem` 里——「桩要跟着 import 清单走」这个坑本仓库已踩三次。

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
