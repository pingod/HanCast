# HanCast 开发进度
## 2026-11-06 未发布轮：应用内更新 + 发布面对账（v2.4.12）

用户请求 user said: "github上的版本有些乱啊，release和tag都不一致，另外为啥release里为啥会有draft标签？ 另外，实现下应用内更新，而且应用内可以选择是否走国内镜像地址来进行更新，现在每次都要去页面下载好麻烦，另外，现在点更新的时候还会出gitee这类的原始作者的地址"。经 ask_user_question 选定四条：删废弃 draft + CI 不再留孤儿 / 彻底删 gitee 源 / 下载+校验+一键装（分平台尽力）/ 镜像开关就放在更新弹窗里。**⚠️ 产物层未验**（`update_apply` 的三条平台脚本没在装好的产物上真装过一次）。

- **发布面事实（对账当场数出来的）**：63 tag / 42 release，其中 **22 个 draft 从未 publish**、21 个 tag 无 release。draft 是 CI 写死 `draft: true` + 人工 `gh release edit --draft=false` 的产物；验收没做完就被下一版取代 → 永远烂着（1.67 GiB 没人能下载的产物）。`releases/latest` **不返回 draft** ⇒ 「draft 攒着不发」直接等于「线上更新检查永远报旧版本」。
- **⚠️ 本轮最贵的实测教训**：GitHub 的 `releases/latest` 把 "latest" 给的是**最近一次 publish** 的那条，不是版本号最大的那条。给 v2.3.8 补了一条无产物的说明 release 之后，`/releases/latest` 当场回指 v2.3.8，2.4.11 的客户端从此报「已是最新版本」且不报错。5 分钟内删掉恢复。**结论：永远不要为旧版补 publish release**；`{tag 有, release 无}` 用文档记着即可。代码侧相应地改为拉 `/releases?per_page=100` 自己按语义版本挑。
- **gitee 根因**：`check_update` 的 sources 硬编码 `["github","gitee"]`，而 `gitee.com/buxiangqumingzi/han-cast` 是**原作者（lanzeweie 线）**的仓库，它的 `releases/latest` 停在 **v2.1.0** 且正文是原作者 changelog。GitHub 不通时它就顶上来 → 用户被发到原作者下载页。前端 `DownloadModal.vue` 另有 GitHub/Gitee/商店三个外链按钮（`push-gitee.ps1` 早跑不通，gitee 从 v2.1.0 起没同步过）。
- **已做的 GitHub 侧动作**：`gh release delete <tag> --yes` 删掉 22 个 draft（**tag 一个没删**）；备份清单 `/tmp/hancast-release-backup/drafts_manifest.txt`（tag/id/资产名/字节/sha256/时间）。现状 20 条已发布、0 draft、latest=v2.4.11。
- **后端**：新增 `hancast_sidecar/update/`（`__init__.py` 转出 9 个名字 + `manager.py` 883 行）。要点：`parse_version` 从 `CommandHandler._parse_version` **整体搬来**当唯一实现（commands.py 里那份删了）；`_latest_release_raw` 翻页拉列表、排除 draft/prerelease、按语义版本挑；`classify_asset` 认 6 种安装包、排除规则包 zip；`describe_release` 只回本平台 + `arch_match`；Linux 默认包型按 `APPIMAGE` 环境变量 + dpkg/rpm/pacman 真问一遍；`check_for_update` 保持老字段不动、新增 `assets/selected/platform/no_asset_for_platform/via/error/ignored`；下载走单槽位任务表 + `.part` + `Range` 续传 + `_cancel_event`（取消是**请求**，终态由线程落）+ 整包 sha256 对照 asset `digest` + `_prune`（保住本次的 `.part`，只动 `hancast*` 命名形态）；`install_plan` 的 `kind` 决定壳层动作，deb/rpm/arch 只回 `command` 不代跑 sudo。
- **命令/事件**：Sidecar 新增 5 条（注册 81→86，`commands.py:159-283`）+ Rust 新增 7 条（88→95，`lib.rs:2024-2120`，含 `update_apply` / `open_update_dir` / `update_install_plan` 转发）+ 事件 `update_download_done`（发出 9→10、转发 8→9、前端监听 9→10）。`check_update` 不再下发 `sources`。
- **壳层**：`update_apply` 先向 Sidecar 取装法（路径不接受前端传参，同 `open_ffmpeg_dir`），再把四份 const 模板之一写进临时目录并 spawn 到**新进程组**（unix `process_group(0)` / Windows `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP`）——脚本必须活过本进程。macOS 是 attach → 旧 `.app` 挪 /tmp 回滚位 → `cp -R` → 失败回滚 → detach → `touch` + `open`；开发布局只 `open` dmg。日志 `<updates>/update-apply.log`。**600ms 后才 `app.exit(0)`**，让命令的回包先到前端。
- **前端**：**删除 `DownloadModal.vue`**（组件 24→23）；`UpdateModal.vue` 重写 593 行（挑产物 CustomSelect / 镜像开关与前缀 / 进度条 / 摘要校验 / 主按钮四态 + 提权产物给「复制安装命令」）；`stores/update.ts` 重写 451 行（1s 轮询 + `resumeDownload` + `epoch`/`starting` 代次守卫 + `refreshPlan` 本地兜底）；`types/update.ts` 补 `UpdateAsset/UpdateTask/UpdatePlan/UpdateApplyResult` 与 `error`/`ignored`/`via`；`SettingsView.vue` 不再把「没查到」说成「已是最新」；`commands.ts` 导出 87→94 + mock（`?mockUpdate=1|slow|fail`）。
- **本轮抓出来的四个真 bug**（都有回归位）：① 勾选镜像开关会把用户配好的前缀清掉（改为只提交 enabled）；② `update.cancel` 文案键漏加，按钮直接显示键名 → 于是**新建了 `tools/verify-locale-keys.mjs` 闸门**（本仓此前只核对 zh 键数这个硬数字）；③ `epoch` 竞态：弹窗打开那次状态查询的回包比 start 晚到，把 `downloading` 按回 false ⇒ 后端在正常下载而界面没有进度条；④ `update_install_plan` 前端有封装、**Rust 没有那条命令** ⇒ 打包版下载完拿不到 plan，「退出并安装」永久置灰（浏览器 mock 有 case 所以测不出来）。另外 `_prune` 曾把本次的 `.part` 一起清掉，等于断点续传永远从 0 开始。
- **收尾补的三处**：① CI deb 冒烟与 Arch 容器实跑都加到**三条命令**（第三条 `update_download_status` 断言返回含 `dir`，专抓 Nuitka 漏收 `hancast_sidecar/update/`——更新 handler 是函数内延迟 import，前两条照样绿）；② `download_status()` 空任务表也回 `dir`；③ `openReleasePage()` 改走 `plugin-shell.open`（Tauri 里 `window.open` 不交给系统浏览器，被删的 DownloadModal 用的就是它）。`build.yml` 因这三条新增变 **758 行**，全仓 `build.yml:` 行号引用已整体重数。
- **装端验收步骤已写成文档但尚未执行**：docs/构建与发布.md §6.12（七步表：检查更新→镜像开关+下载→退出并装→续传→取消→deb/rpm/Arch 只给命令→装后回归；每步给「失败先看哪」）。本轮**没有打 tag、没有 bump 版本号**（用户 2026-11-06 选「先不打 tag，改动留在工作区」）。
- **闸门**：新增 `tools/audit-releases.py`（12 项判据 + `--cleanup-commands` 只打印不执行 + `LEGACY_TAG_LINE=v2.2.19` / `DECLARED_UNRELEASED` 22 条 / `DECLARED_ASSETLESS` 留空并注明为什么空）与 `.github/workflows/release-audit.yml`（周一 03:10 UTC + 手动）。`build.yml` 改为 tag 即 publish、publish 前必须六种产物齐（`Require a complete artifact set before publishing`）。
- **测试**：`tests/test_update_manager.py` 47 例（含真 http.server 的截断续传、篡改丢弃、取消删半份、无摘要如实标、明文 HTTP 拒绝、清理不碰用户文件、命令注册与 `install_plan` 原样透传）；`tests/test_ghmirror.py` 的更新那组重写（含 `test_no_gitee_anywhere_in_the_update_chain` 直接源码断言 + 403 必须说成「限流」）。全量 pytest 首跑 **686 passed / 1 failed**，唯一 FAIL 是 `tests/test_remote_commands.py::test_上传无聚焦设备报错`——**红的是测试自己依赖环境**：它只清了 `cast_state.device_id`，没桩 `_remote_upload` 会退回去读的遥控器 `focus`，而本机局域网有设备在（SSDP 真发现了 TCL / AMD-YES），`focus` 非空 → DID NOT RAISE。已把该用例改成显式桩空 sessions（不是放宽判据，是让它真的只测那一条分支），改后全量 **695 passed / 0 failed（3 subtests）· exit 0**，复跑一次仍是 695/0。
- **验收**：`node tools/verify-update-ui.mjs` 32 项判据全绿（27 条断言 + 5 张截图落点）（真实 Chromium 驱动弹窗，含「弹窗内不得出现 gitee/原作者/商店地址」）；`node tools/verify-locale-keys.mjs` ALL PASS；`python3 tools/audit-releases.py` exit 0；`bash tools/rust-check.sh` 通过；`npm run build`（vue-tsc + vite）通过。
- **文档**：AGENTS.md（新增「应用内更新的三条硬边界」+ 全套计数）、`docs/构建与发布.md`（§4.5 重写 + 新增「发布面对账」+ §7.4 划清 gitee 只是代码镜像）、`docs/HanCast-Frontend-API.md`、`docs/HanCast-Frontend-Spec.md`、`README.md` / `README_en.md`、`tools/README.md`、`docs/README.md`、CHANGELOG 新节。
- **口径变化**：Rust 88→**95**、Sidecar 81→**86**、导出 87→**94**、组件 24→**23**、locale 456→**492**、顶层脚本 60→**63**、`test_*.py` 41→**42**、事件 10-9-10。
- **剩余**：① 装好的产物上真装一次（Windows 静默装 / macOS dmg 替换+回滚 / Linux AppImage 改起、`pacman -U` 那条在 CachyOS 复验）；② release 正文目前只有 compare 链接，想把 CHANGELOG 小节自动填进 CI 的 release body；③ **本轮改动救不了正在被 gitee 误导的老客户端**，只能靠他们手动装一次新版。


## 2026-11-05 未发布轮：手机镜像接收（Android → 本机，v2.4.12）

用户请求 user said: "能添加手机及电脑屏幕镜像投屏的功能么"。经 ask_user_question 选定：方向＝手机/电脑屏 → 本机当接收端，平台＝Android。**⚠️ 真机未验（当前无 Android 设备）**。

- **方案**：v1 = 调用**外部 scrcpy 独立窗口**（画面/触摸/声音/旋转现成；不自研传输协议）。曾评估 scrcpy `--record=<FIFO>` → 自带 mpv（`app/src/recorder.c` 的 `file:` 前缀只能写文件/FIFO、不能直接 stdout；`Streamer.java` 每包 12 字节头自研可解析但无真机可验、风险高），结论是 v1 不做。iOS 走 AirPlay，本机也不做 Miracast sink，都不在本轮范围。
- **后端**：新增 `hancast-backend/hancast_sidecar/mirror/`（`__init__.py` 27 + `tools.py` 271 + `manager.py` 405）。`tools.py`：adb/scrcpy 定位（手填错不回退、`--version` 真跑才 found、30s 缓存）、版本解析、`SCRCPY_AUDIO_MIN_VERSION=(2,0,0)`、`install_commands`、`parse_adb_devices`。`manager.py`：`build_scrcpy_args`（夹 max_size≤4096 / max_fps≤240，老 scrcpy 不加 `--no-audio`）、`MirrorManager` 的 status/set_tool_path/devices/connect/start/stop/cleanup，Popen（POSIX `start_new_session=True`）、stderr 读线程、1s 启动宽限、`_watch` 退出发 `mirror_state_changed`、非主动非 0 退出写 `last_error`。
- **命令/事件**：Sidecar 注册 6 条 `mirror_*`（`commands.py:251-256`，注册 75→81）+ Rust 6 条同名（`lib.rs:1035-1095`，82→88）+ 事件 `mirror_state_changed`。
- **前端**：`src/types/mirror.ts`、`src/stores/mirror.ts`（309 行，`PREFS_KEY='hancast.mirror.settings'`）、`src/components/MirrorPanel.vue`（约 460 行，`mp-*`）、`HomeView.vue` 首页「手机镜像」按钮、`commands.ts` 6 封装（导出 81→87）+ 浏览器 mock、locale 各 403→456。
- **测试**：`hancast-backend/tests/test_mirror.py` 25 例全绿（`FakeProcess` 必须用 `threading.Event` 让 wait 阻塞）。修 `manager.py:211` 真 bug：`{"ok":False,"error":…, **self._state_locked()}` 里 state 的 `error` 键覆盖了失败原因 → 改成 `{**self._state_locked(), "ok":False, "error":…}`。
- **验证**：`hancast-backend/.venv/bin/python -m pytest tests/test_mirror.py` 25 passed；全量 pytest **647 passed**（318s，退出码 0）；`npx vue-tsc --noEmit` 0 错（曾修 TS2783 mock `ok` 重复 → `{ ...mockMirrorStatus(), ok: true }`，commands.ts:855/:859）；`bash tools/rust-check.sh` 通过。
- **文档**：新建 `docs/手机镜像.md`（148 行）；改 AGENTS.md / README.md / README_en.md / docs/README.md / docs/架构总览.md / docs/HanCast-Frontend-API.md（§0 摘要表、§3.1 事件表加行、§7 #83-88）/ docs/HanCast-Frontend-Spec.md（组件 24、store 12、locale 456、§7.3 语言键数更正）/ CHANGELOG.md（`playlist.ts` 那条补「v2.4.12 起共 12 个 store」保留历史证据）。校对戳一律改 2026-11-05。硬数字：Rust 88 / Sidecar 81 / 导出 87 / 组件 24 / store 12 / locale 456 / test_*.py 41 / 事件 8-7-9。
- **闸门**：`audit-doc-refs.py` 4180 处可疑 0；`check-doc-freshness.py` FAIL 0 / WARN 0（踩坑：`resolve("manager.py")` 会命中 `screencast/manager.py` 而非 mirror 的，必须写全路径 `hancast-backend/hancast_sidecar/mirror/manager.py`；`MirrorManager` 定义在 `manager.py:97`）。
- **坑**：mock 里形似真机序列号的连续字面量会被网关替换成 `{{SERIAL_*}}` 占位符，必须用 `'mock' + '-phone-online'` 拼接。`uv run pytest` 在本沙箱 cache 目录不可写，改用 `.venv/bin/python -m pytest`。
- **口径变化**：Sidecar 注册 75→81、Rust 82→88、导出 81→87、components 23→24、store 11→12、locale 403→456、`test_*.py` 40→41。
- **剩余（待真机）**：真机 E2E 全流程（见专文 §9 验收清单）。

## 2026-11-03 发布轮：v2.4.11（前端错误落日志 + 区块错误边界 + 遥控页全量设备/手动刷新）

用户报三件事 user said: "中间那个模块不知道为什么自己消失了,另外,手机控制页面无法准确列出局域网设备,而且你需要改成手动刷新方式"。

- **问题①：首页媒体输入区自己消失，且事后查无可查**。逐层排除后确认代码路径不可能渲染成空：`HomeView.vue` 无条件渲染 `<MediaInput />`；`MediaInput.vue` 的 `v-if="state !== 'ready'"` / `v-else`（预览卡）覆盖了全部取值；CSS 里只有 `<input type="file" style="display:none">` 一处隐藏，没有能把整块压成高度 0 的规则（用截图逐行亮度扫描确认那块是真的没渲染，不是颜色太暗）。**唯一剩的解释是渲染期抛异常被 Vue 整块卸掉**，而这一类异常在 v2.4.10 之前哪都不落盘（`app.config.errorHandler` 没注册，`hancast.log` 是 Python 侧的）。
- **根因仍未定位**：打包版 WKWebView 不便挂调试器（`safaridriver` 需用户手开 Safari「允许远程自动化」，`/status` ready 但 `POST /session` 500）；`npm run tauri dev` 试过，被四道构建门挡死（`externalBin` 缺 `hancast-sidecar-aarch64-apple-darwin`、以及后续三步）。**本轮做的是可观测性：让它下次能查、能看见、能重试**，不猜根因。
- **修法（可观测）**：① Sidecar 新增 `frontend_log` 命令（`_frontend_log`，`commands.py:1756`），前端错误写进**同一份** `hancast.log`，行首 `[webview/<source>]`，message 截 2000 字、stack 截 4000 字；② `src/api/commands.ts` 新增 `frontendLog()`（`:1523`，整段 try/catch 吞异常——它本身在错误处理里被调用）；③ `src/main.ts` 注册 `app.config.errorHandler`（`:50`）+ `unhandledrejection`/`error`，为此把 `createApp`/`use()` 提到前面（否则 vue-tsc 报 TS2448/TS2454）；④ 新增 `src/components/SectionBoundary.vue`（89 行）用 `onErrorCaptured` 把出错区块原地换成「这块内容没能加载出来 + 具体错误 + 重试」，`return false` 是刻意的（上面还有 errorHandler，重复记一遍没意义、继续上抛会整页白）；⑤ `HomeView.vue` 用它包住媒体输入区，重试 `mediaKey += 1` 重挂载。
- **问题②：遥控页设备列表只列「有活动会话」的**。`renderDevSel()` 吃 `state.sessions`。修法：`_remote_sessions_state()` 返回新增 `devices`（SSDP 全量设备表，跳过隐藏设备，逐台带 `online`/`session_status`；会话还在但 SSDP 已消失的标离线补进来）。
- **问题③：页面自动轮询** → 改手动：页面新增 `refresh()`（`remote/server.py:443`）走 `refresh_devices` → `ssdp.scan()`，等 1.8 秒再拉一次 state，**去掉 state 的 setInterval 轮询**。
- **全量测试抓到的自查回归（重要）**：`test_remote_commands.py` 两条用例单独跑绿、连跑红——`handler` fixture 会起真 SSDP 并发现局域网里真电视，于是 `state["devices"]` 非空、`state["focus"]` 退到「第一台真电视」，把测试里写死的 `cast_state.device_id = "d1"` 盖掉。三处取控制目标统一改成 `cast_state.device_id or state["focus"]`（`commands.py:1015/1046/1073`）——**权威聚焦点是 `cast_state.device_id`，`state["focus"]` 只是页面显示兜底**，反过来用会盖掉正在控制的那台。改后 26 passed、全量 **622 passed**。教训：涉 SSDP 的用例不能只单独跑，设备表内容取决于本机网络。
- **新增测试**：`hancast-backend/tests/test_remote_device_list_refresh.py`（10 条，`git stash` 后 9 failed / 1 passed 负向验证）、`tools/verify-section-boundary.cjs`（5 条，把 `return false`→`true` 或换裸 `<MediaInput />` → 4/5 变红）。`tools/verify-app-lifecycle.cjs` 叶子白名单补 `SectionBoundary`。
- **文档**：`docs/故障排查.md` 新增 **§5.9**「界面上有一块自己消失了」+ §5.8 补「设备下拉列的是谁」「改手动刷新」+ §2 索引两行；`docs/HanCast-Frontend-Spec.md` 组件 22→23 并重校 8 处 `HomeView.vue:NN`；`docs/开发指南.md` 测试地图 42→43 文件 10235→10401 行；`docs/HanCast-Frontend-API.md` 日志组 3→4 条并补 `frontend_log` 行；`docs/架构总览.md` 行数表（`commands.py` 1668→1734、`remote/server.py` 718→746）；`tools/README.md` 59→60。
- **闸门**：audit 4098 处可疑 0；freshness 数字 11 处一致 FAIL 0/WARN 0（自动重锚 44 处 `commands.py`/`commands.ts`/`HomeView.vue`/locale 漂移 + 手工 4 处 `frontendLog` 行号）。**坑**：`commands.py` 里三处 `did = …` 从 4 行注释改成 5 行 → 之后所有行号 +1，第二次重锚才收敛；批量改校对戳会把正文「本轮（v2.4.10）」留在原地，必须逐份重写头部说明（本次命中 3 份）。
- **口径变化**：`#[tauri::command]` 81→82、Sidecar 注册 74→75、`commands.ts` 导出 80→81、components 22→23、tools 顶层脚本 59→60、`test_*.py` 39→40、locale 各 400→403 键。

## 2026-11-03 发布轮：v2.4.10（手机遥控器地址可见 + 令牌落盘复用）——已发布

用户问 user said: "你好像还没告诉我手机端怎么访问页面,界面上我也没看到任何提示"。查下去发现是两个问题叠在一起。

- **问题一：入口不存在**。手机遥控器服务 v2.4.6 就有，但界面上**没有任何地方**显示地址——只有开发者知道去 `remote_status` 里捞。用户从界面上根本无从得知。
- **问题二：地址不稳定却宣称可加书签**。`remote/server.py` 每次进程启动都随机生成 token，而文档/文案写着「地址固定不变，可加书签」。昨天存的链接今天就是 404。
- **修法（入口）**：新增 `src/components/RemoteControlPanel.vue`（139 行，弹窗展示地址、整块可点复制、走 `writeClipboardText`），`HomeView.vue` 首页加常驻「手机遥控」按钮（与屏幕投屏并列，`.home__actions` 容器）；i18n 加 `home.remote`（两份各 400 键）。
- **修法（令牌）**：`RemoteControlServer.__init__` 加第 4 个参数 `token`，由 `CommandHandler` 从配置键 `remote_control_token` 读出来注入；新增 `_remember_remote_token()`（幂等，只在变化时 `config.save()`），首启生成后回写，之后所有启动复用。`stop_remote_control` **不删**落盘 token。
- **新增测试**：`hancast-backend/tests/test_remote_token_persistence.py`（8 条，含「构造即可落盘」「重启复用同一令牌」「令牌未变不重复写盘」「真起服务后地址里的令牌仍是注入的那个」），并做 `git stash` 负向验证（8 failed → 恢复后 8 passed）。`tools/verify-remote-entry.cjs`（3 条，仿 compiler-sfc + createRenderer + node:vm，负向验证「删掉首页按钮 → 用例 1 变红」）。三个坑写进注释：`vue.createApp` 走 runtime-dom 会 `ReferenceError: window is not defined` 必须用 `renderer.createApp`；Proxy 作 imports 必须加 `has() { return true }` 否则 `name in imports` 短路成 `Unexpected import`；断言输出要打 `error.stack`。
- **文档**：`docs/故障排查.md` 新增 **§5.8**「手机遥控器页：地址在哪、手机怎么连、连不上怎么办」（地址在界面哪、手机侧三件事、token 稳定性、连不上判据表 6 行），§5.7 按新行为重写并删掉「每次启动新 token」的谎话，§2 症状索引加两行；`docs/HanCast-Frontend-Spec.md` 组件清单 21→22；`docs/HanCast-Frontend-API.md` §7 三条遥控命令补 token 语义；`docs/架构总览.md` 行数表（`commands.py` 1668、`remote/server.py` 718，顺带校准 `security/guard.py` 701→714）；`docs/开发指南.md` 测试地图 41 文件 10127 行 → 42 文件 10235 行；`tools/README.md` 58→59 并登记新脚本。
- **验证**：`npx vue-tsc --noEmit` 0 错、`npm run build` 通过（主包 374.08 kB）；verify-remote-entry **3/3**、verify-cast-ui **30/30**、verify-device-list-empty **4/4**、verify-app-lifecycle **2/2**；两道闸门 audit 4042 可疑 0、freshness 数字 11 处一致 FAIL 0/WARN 0（自动重锚 20 处 `commands.py`/locale 漂移 + 2 处手工：`HomeView.vue:72-76`→`:73-77`、API.md 把 `start_remote_control` 改成 TS 侧真名 `startRemoteControl`）。
- **口径变化**：components 22、tools 顶层脚本 59、`test_*.py` 39、locale 各 400 键。
- **发版**：commit `f91cdfd` → tag `v2.4.10` → CI **41621247311** 三平台全绿；dmg 85,661,674 字节 sha256 `c1269129284de9e30213214612e87c958b1d945f483f016fc695ee01f783efe8`；`install-macos-artifact.sh 2.4.10 <dmg>` + `accept-macos-artifact.sh 2.4.10` → ALL PASS；发布记录 commit `201ddd7`；`gh release edit --draft=false` → publishedAt 2026-11-03T11:09:43Z。后端全量 **612 passed / 0 failed**（290s）。
- **真机复核（核心承诺）**：装好的 2.4.10 首启后配置里出现 `remote_control_token`，`curl http://127.0.0.1:8899/r/<token>/` → 200；quit 后重开读回**同一个 token**，同一地址仍 200。这是脚本测不到的那条线（进程重启）。
- **未做**：首页按钮的像素截图复核——本机 HanCast 启动即隐藏到菜单栏，当时用户正在通话，不便抢焦点；该入口由 `tools/verify-remote-entry.cjs`（3 条断言 + 负向验证）与类型检查兜底。

## 2026-11-03 发布轮：v2.4.9（设备列表空状态误显示 + 遥控器镜像定时器泄漏）——已发布

用户贴截图质问「这么明显的bug没发现??」：设备列表已渲染两台 LAN 设备，下方同时又显示「还没有发现设备」空状态块。

- **真因**：`src/components/DeviceList.vue` 的 `v-else` 空状态块贴在「隐藏设备按钮」的 `v-if` 上，而不是设备列表的 `v-if` 上 ⇒ 渲染条件是「没有隐藏设备」，与有没有设备无关。`git log` 追溯到 **v2.4.4（`e1d4922`）引入**，跨三个版本没被发现。修法：空状态块移回 `device-list__items` 之后、隐藏按钮移到空状态之后改独立 `v-if`；现结构 items(v-if) → empty(v-else) → hidden(v-if)，`git diff --stat` 11/11 纯重排。
- **顺带两处（同源：v2.4.8 加了新依赖，验收脚本没跟上）**：
  - `src/stores/cast.ts` 的 `setupRemoteUiSync()` 里 300ms 去抖 timer 是**函数局部变量**，`onUnmounted` 只置 `disposed = true` 从不 `clearTimeout`，回调体也不挡 `disposed` ⇒ 组件死后仍发一次 `remote_update_ui`。修法：timer 提到 store 作用域 `remoteUiTimer`，回调开头清引用并挡 `disposed`，卸载时 `clearTimeout`。
  - `tools/verify-cast-ui.cjs` 桩缺 `remoteUpdateUi` 与 `playlist.playableItems`（v2.4.8 起 cast.ts 要它们）⇒ 从 v2.4.6 的 30/30 **静默退化到 0/30**，发布时没察觉；`tools/verify-app-lifecycle.cjs` 的 `leafNames` 漏 `HiddenDevicesModal`（v2.4.4 起 App.vue 已挂）导致整脚本加载失败、`responses` 缺 `getHiddenDevices`。
- **新增回归测试** `tools/verify-device-list-empty.cjs`（4 条用例钉住「列表与空状态互斥」，仿 verify-cast-ui 的 compiler-sfc + createRenderer + node:vm 手法），并做了**负向验证**（还原旧结构 → 4/4 变红）。两个坑写进注释：① `compileScript` 走 CommonJS 取 `.default`，imports 必须传 `{ __esModule: true, default: Stub }`；② 查找必须遍历挂载根 `container` 不能遍历 `body`（body 只有 teleport 才有内容）。
- **验证**：`npx vue-tsc --noEmit` 0 错、`npm run build` 通过；verify-cast-ui **30/30**、verify-app-lifecycle 2/2、verify-device-list-empty 4/4、verify-playlist-advance 10/10；后端全量 **604 passed**；两道文档闸门绿（audit 3994 可疑 0、freshness FAIL 0/WARN 0，顺手修 5 处既有漂移：cast.ts 843→847/794→798、CHANGELOG cast.ts 531→535、CHANGELOG 里 57→58）。
- **发版**：`npm run version:set -- 2.4.9`（7 处契约点）、CHANGELOG v2.4.9 节、`tools/` 顶层脚本 57→58（`tools/README.md` §1 + AGENTS.md）、19 份 .md 校对戳 → v2.4.9、commit `0e7571f`、tag `v2.4.9`。
- **环境性失败（非回归）**：`verify-params-ui.mjs` / `verify-error-wrap-ui.mjs` / `verify-watch-page-live.mjs` / `verify-watch-page-sound.mjs` 均因 `~/.cache/puppeteer/chrome-headless-shell` 不存在而 ENOENT。

## 2026-11-03 发布轮：v2.4.8（手机遥控器页升级——常驻 + 媒体选择 + 功能加强）——已发布

在 v2.4.7 之上把 v2.4.6 的手机遥控器页从「先投屏才能用、只有几个按钮」升级成真正的遥控器：应用一启动就常驻（手机浏览器打开即用），能选视频/粘链接/选目录投屏，有队列/历史/多设备/进度条/防息屏/横屏。

- **后端 `remote/server.py` 重写**（343 → 713 行）：`RemoteControlServer(get_state, action, local_ip)` + `set_content_hooks(on_upload, on_paste, on_resolve_poll)`；`start(port=8899)` 占用回退 0；新增 `/api/paste`、`/api/resolve`、`/api/upload`（multipart，≤4GB，落临时文件保扩展名）；页面内联 UI 升级（封面卡片、拖动进度条、快进快退 ±5/±10、队列/历史面板、多设备 select、Wake Lock、横屏媒体查询）。
- **后端 `commands.py`**（1519 → 1652 行）：新增 `remote_update_ui` 注册项同步 `_remote_ui` 镜像；`_remote_sessions_state()` 扩 queue/history/play_mode/stop_after_current；`_remote_action` 扩 next/prev/mode/stop_after/cast_history/load_folder 走 `remote_command` 事件委派前端；新增 `_remote_upload`/`_remote_paste`/`_remote_resolve_poll`；`_start_services()` 随应用常驻启动遥控器页。`_start_cast` 接受可选 `title` 覆盖 DIDL 标题（手机上传哈希文件名不再难看）。
- **前端**：`lib.rs` 新增 `remote_update_ui`（81 条 `#[tauri::command]`）；`commands.ts` 导出 `remoteUpdateUi`/`RemoteUiSnapshot`（80 个）；`cast.ts` 监听 `remote_command`、新增 `setupRemoteUiSync()` 去抖推 UI 快照；`CastController.vue` 弹窗改常驻地址展示；locale 各 399 键。
- **测试**：`test_remote_control.py` 68→153 行（4→10 项）、新增 `test_remote_commands.py`（125 行 8 项）；后端全量 **604 passed**；vue-tsc 0 错、build 通过。
- **文档**：AGENTS/架构总览/Frontend-API/Frontend-Spec/故障排查/开发指南 + CHANGELOG v2.4.8 节；两道闸门绿（audit 3949 可疑 0、freshness FAIL 0/WARN 0）；59 处行号漂移按 freshness「实际在[N]」重锚。
- **发布**：commit `38b6aab`、tag `v2.4.8`、CI 41598558501 三平台全绿、dmg sha256 `5acff302…b889` 装端 90 PASS/0 FAIL、产物内 remote/server.py+commands.py sha256 与仓库一致、装好的产物上遥控器页随应用常驻并 /api/state /api/cmd 端到端通；publish `gh release edit --draft=false`。
- **未闭合**：真机手机浏览器真开/相册真上传/多设备真投未手动复测；iOS Safari 超大文件未做分片。

## 2026-11-03 发布轮：v2.4.7（添加本机——自己投自己）

在 v2.4.6 之上补「添加本机」能力：HanCast 自己也是 DLNA 渲染端，但设备列表只认 SSDP 发现的机器，本机被当「另一台设备」还常被隐藏。本版让用户一键把本机加进设备列表。

- **Sidecar**：新增 `add_local_device`（`commands.py:451 _add_local_device`）——地址=本机 LAN IP + 自己的 DLNA 端口（`_get_local_ip` + `dlna_server.get_port()`），顺手 `guard.trust_device` 进信任列表，不写 `manual_devices`（本机 IP 每次启动重算，存下来会变死记录）。
- **Guard**：新增 `trust_device(ip, device_info, friendly_name)`（`security/guard.py`，直接 `_add_trusted`，friendly_name 可覆盖）。
- **Rust**：`#[tauri::command] add_local_device`（`lib.rs:260`）+ generate_handler 登记。
- **前端**：`AddDeviceModal.vue` 加「添加本机（自己投自己）」按钮（`addSelf`，成功 900ms 关弹窗）；`commands.ts` 导出 `addLocalDevice` + mock；`device.ts` `addLocalDevice()` 去重并入列表；locale 各 +2 键（`devices.addSelf`/`devices.addSelfHint`）。
- **数字**：`#[tauri::command]` 79→**80**（lib.rs:1668-1752）、Sidecar 72→**73**（commands.py:144-264）、commands.ts 导出 78→**79**、locale 395→**397**；commands.py 1519 行、lib.rs 1687、commands.ts 1643、guard.py 714、device.ts 248、AddDeviceModal.vue 273。
- **验证**：后端全量 **590 passed**；`npx vue-tsc --noEmit` exit 0；`npm run build` 通过（主包 369.21 kB）。两道文档闸门绿：audit 3942 处可疑 0；freshness FAIL 0/WARN 0、数字 13 处一致。
- **文档坑（本轮返工教训）**：两遍 difflib remap 用「起点取新、终点取旧」混出 **85 处反向行号范围**（a>b），audit 报「行号越界」，用 `path` basename 全局 rglob 解析唯一路径后批量 swap 修好；freshness 的 `实现 A path:N、B path:M` 并列写法会把行号 N 的 right 窗口抓到 B → 假 FAIL，改用「`A` 见 path:N，`B` 见 path:M」隔断。
- **发布**：提交 `7ace465`、tag `v2.4.7` 已推；CI run 41585153134 三平台全绿；dmg sha256 逐字节一致（`2c8cbb7f…08b2`）、`hdiutil verify` VALID；`install-macos-artifact.sh 2.4.7` 装好、`accept-macos-artifact.sh 2.4.7` **89 PASS / 0 FAIL / ALL PASS**；在装好的产物上喂 `add_local_device` 真返回本机设备、`get_devices` 查得到（端到端通）。Release 已 publish。
- **未闭合**：真机「自己投自己」真投（本机渲染端真放一集 + 另一台手机遥控）尚未手动点一遍；多网卡/VPN 下 `_get_local_ip` 选哪张卡未加选择入口。

## 当前阶段：v2.3.19 工作区前端优化（尚未发布）

**最后更新**: 2026-11-03

本轮已安全拉取 `origin/feat/js-rule-plugins`，远端无新增，本地比 `origin/main` 多 5 笔既有提交。用户确认“克制精致的原生工具”风格：统一紫色亮暗主题、媒体/设备卡片、设置分组、弹窗边界及键盘操作；保持 Linux 不透明策略。修复解析结果竞态、App 路由切换丢失投屏监听、迟到监听清理、主题缓存、播控错误吞掉、进度锁定/旧轮询回写、视频被当图片加载；去掉无功能的多房同步装饰。

验证：媒体/主题 73 项、播控原 20 项及封面新增 1 项、真实 App 生命周期 2 项；类型检查及 Vite 构建通过。浏览器验 346×472 / 434×634 / 800×900、中英设置、日志/屏幕/控制器弹窗、解析/暂停/停止/清空（项目内置 mock，不是真机投屏）。未跑全量测试、未打三平台包、未提交或发布。界面截图在 `docs/images/ui-native-{light,dark}.png`。

## 2026-10-31 发布轮：v2.4.6（队列持久化 + 播完即停/睡眠定时 + 手机遥控器页 + 外挂字幕内封）

用户说「你先把所有开发任务做完，后面再统一测吧」——把优化清单剩下的三批（原 v2.4.6/2.4.7/2.4.8）一次做完，一个补丁版发布（2.4→2.5 属 minor，DEC-006 要求先问，故以补丁号走）。

- **投屏队列**：`src/stores/playlist.ts` 重写（390 行），`localStorage['hancast.playlist.queue']`；新增 `appendItems`/`appendFolder`/`setPlayingId`/`stopAfterCurrent`/`setStopAfterCurrent`；`MediaInput.vue` 队列面板加「追加文件夹」。
- **播完即停 / 睡眠定时**：`cast.ts` 收到 `stopped` 且到结尾时若 `stopAfterCurrent` 就停队列；`CastController.vue` 开关 + 睡眠下拉（15/30/60/90/取消），deadline 持久化，秒级 tick 用 `setInterval` 并随卸载清理。
- **手机遥控器页**：新增 `remote/server.py`（343 行），每会话一次性 token `/r/<token>/`，页面 + `api/state` + `api/cmd`，token 不符 404；命令 `start_remote_control`/`stop_remote_control`/`remote_status`。
- **外挂字幕内封**：新增 `media/subtitles.py`（173 行），`-c copy` 不重编码、mp4→`mov_text`、其它→`srt`，产物按内容哈希缓存 `subtitle_cache/`；命令 `subtitle_apply`/`subtitle_status`；`CastController.vue`「💬 字幕」按钮，完成后重投同设备并从原进度继续。
- **数字**：`#[tauri::command]` 74→**79**（lib.rs:1668-1740）、Sidecar 67→**72**（`commands.py:144-263`）、`commands.ts` 导出 73→**78**、locale 375→**395**；`commands.py` 1492 行、`lib.rs` 1678、`commands.ts` 1635、tests 39 文件 9813 行。
- **验证**：后端全量 **586 passed**（新增 `test_remote_control.py` 68、`test_subtitles.py` 76）；`npm run build` 通过；`tools/verify-cast-ui.cjs` **30/30**（队列桩补齐新 API + 修 `window.setInterval` 桩环境崩溃）；两道文档闸门绿（audit 3920 处可疑 0；freshness FAIL 0/WARN 0、数字 13 处一致）。
- **文档**：CHANGELOG v2.4.6 节；故障排查新增 §5.7 + §11 三行 + §2 索引；架构总览行数表加 subtitles.py/remote/server.py；Frontend-API §0/§7（79 行）/§2 新增 5 条命令；Frontend-Spec §8.3 locale 395；全仓校对戳 2026-10-31/v2.4.6。
- **发布**：提交 `46d2ee0`（43 files）→ tag `v2.4.6` → 推 main；CI run 41163007137 全绿。dmg `HanCast_2.4.6_aarch64.dmg` 85,600,687 字节、sha256 `a1fe8f71…55b8` 与 GitHub 一致、`hdiutil verify` VALID；`install-macos-artifact.sh 2.4.6` 装好；`accept-macos-artifact.sh 2.4.6` **89 PASS / 0 FAIL / 0 SKIP**。产物上实测：`start_remote_control` 起 `http://<ip>:8899/r/<token>/`，页面 200、`api/state` 200、`stop_remote_control` success；`subtitle_status` idle；`cast_failure_list` 正常。
- **未 publish**：Release 仍 draft（用户 m06236「后面再统一测」；DEC-006 边界③需真机验收）。真机投屏/字幕真机内封、Linux/Windows 装端均未做（TCL 192.168.1.13、Windows 192.168.1.68 离线）。

## 2026-10-31 发布轮：v2.4.5（出站可观测 + 投屏失败台账 + probe_url 预检）

清单第二批（第一批 v2.4.4）。动因 COMP-005：出站整条链路只有 DEBUG，电视不拉流时面板默认 INFO 无痕。

- **出站 INFO**：`protocol/dlna.py` `_send_action` 成功分支打 `SOAP OUT ← <status> | <action> | <device> | <ms>ms`（`dlna.py:1256-1258`），请求侧仍 DEBUG（`:1184`）。
- **投屏失败台账**：新增 `media/cast_failures.py`（132 行，阶段 connect/set_uri/play/stop/media_ready/cast；只写本机 `cast_failures.json`，上限 100 条/保留 14 天）；Sidecar `cast_failure_list`/`cast_failure_clear`；`LogsPanel.vue` 内「投屏失败台账」区（locale `logs.castFailures*`）。
- **预检 + 自动重解**：Sidecar `probe_url`（commands.py:867）；`MediaInput.vue:601` 历史条目重投前探直链；url 条目重投自动回来源页重解（`media.historyDead`/`media.historyDeadHint`）。
- **数字**：命令 71→74（lib.rs:1612-1680）、Sidecar 64→67（`commands.py:132-232`）、`commands.ts` 导出 70→73、locale 369→375；`commands.py` 1320 行、`dlna.py` 1310 行、`lib.rs` 1617、`commands.ts` 1591。
- **验证**：后端全量 `pytest` **577 passed**（新增 `test_cast_failures.py` 214 行）；`npm run build` 通过；`tools/verify-cast-ui.cjs` 30/30；两道文档闸门绿。
- **文档**：CHANGELOG v2.4.5 节；故障排查 §5.6（出站可观测/台账/判据）+ §11 两行 + §2 索引；架构总览行数表加 `media/cast_failures.py`；Frontend-API §0/§7（74 行）/§2 新增命令；Frontend-Spec §8.3 locale 375；全仓 `文件:行号` 按 diff 重映射（脚本 `/tmp/remap_v245.py` + `/tmp/remap_v245b.py`），全仓校对戳 2026-10-31/v2.4.5。
- **发布**：提交 `c783437`（39 files, +1212/−363）→ tag `v2.4.5` → 推 main；CI run 41159185441 全绿（macos 10m54s / linux 17m36s / windows 13m48s）。dmg sha256 `fc123f3a…07030` 已校验、`hdiutil verify` VALID；`install-macos-artifact.sh 2.4.5` 装好；`accept-macos-artifact.sh 2.4.5` **88 PASS / 1 FAIL / 0 SKIP**（唯一 FAIL = SSDP 心跳采样窗口落在网卡组播暂不可用的广播兜底期，20s 后自行恢复 multicast，非回归）。新命令在产物上实测 probe_url / cast_failure_list / cast_failure_clear 均正常。docs 跟进提交 `e16c51b`（装端记录 + README_en 计数）。
- **未 publish**：Release 仍 draft（DEC-006 边界③：需真机投一次；TCL 192.168.1.13、Windows 192.168.1.68 均离线）。Linux/Windows 装端亦未做（主机离线）。
- **用户决定（m06231，2026-10-31）**：选「等真机投一次再公开」——保留 draft，等 TCL/Windows 上线真机投一次确认后再 `gh release edit v2.4.5 --draft=false`。

## 2026-10-30 发布轮：v2.4.4（补齐两条单向门 + 测试隔离 + 卫生类 + 后缀门真 bug）

用户：m04982「还有哪里可以优化和场景增强」→ m05073「全部做下，顺序你自己决定」。本轮先做清单首段 + 开工时发现的重问题。

**代码**
- 新增 `src/components/HiddenDevicesModal.vue`（组件 21 个）+ `stores/device.ts` 的 `hiddenDevices`/`showHiddenModal`/`fetchHiddenDevices`/`openHiddenModal`/`closeHiddenModal`/`unhide`/`removeManualAddress`；`DeviceList.vue` 底部「已隐藏 N 台 · 管理」。
- 壳层 + Sidecar 新增 3 条命令：`unhide_device`、`get_hidden_devices`（返回富化 `{udn,name,ip,device_type,online,is_self}`）、`remove_manual_device`（幂等）。命令总数 68→71，Sidecar 注册 63→64。
- 严重坑：`tests/test_commands.py` 的 setUp/tearDown 会写用户真实 `hancast_setting.json`（本机被污染几十轮）。修法 `tests/conftest.py` 在 import 前重定向 HOME/USERPROFILE/APPDATA/LOCALAPPDATA 到临时目录 + 三道自检；新增 `test_config_isolation.py`。详见 issues COMP-006。
- 卫生：SSDP 组播不可用网卡一次性禁用（不再每 30s 刷 Errno 65）+ 跳过点对点/链路本地网卡；MPV `quit` 不可用降 debug；检查更新改走 `httpclient.make_session()`（代理用户能查到更新）。
- 真 bug：`media/server.py` `player_safe_suffix` 里 `_looks_like_playlist("", url)` 丢 mime ⇒ mpegurl 无后缀被兜成 `.ts`；改为传 mime。
- 验收脚本：`tools/verify-params-ui.mjs` spawn 补 `--no-sandbox --disable-gpu`；新增 `tools/verify-tv-url-suffix.py`。

**验证**：后端全量 `pytest` **563 passed**；`npm run build`（vue-tsc+vite）通过；`verify-cast-ui.cjs` 30/30；`verify-playlist-advance.cjs` 10/10；`verify-tv-url-suffix.py` ALL PASS；`build-rule-packs.py --check` 4 包同步；两道文档闸门 FAIL 0/可疑 0。

**文档**：全仓行号按 diff equal 段重映射；locale 引用 `zh-CN.json:348→357`；模块行数表校准（commands 1257、ssdp 1343、mpv 662、media/server 706，并顺带修 parser 163→214、jsruntime 761→845）；Frontend-API §0 统计表 71/61/70/64、§2.1 补 3 条命令、§6.10 重写、§7 快速表 67→71 行；Frontend-Spec §4 组件 20→21、device.ts 179→228、locale 358→369；19 份校对戳 → 2026-10-30/v2.4.4；CHANGELOG 新增 v2.4.4 节。

**装端**：CI run 41052359324 macos 首轮 `hdiutil create failed - Resource busy`（runner 抖动），rerun --failed 后全绿；dmg sha256 3d178a1f… 与 GitHub 一致、hdiutil verify VALID；install 2.4.4 成功（adhoc, com.han.hancast），accept 88 PASS/0 FAIL/1 SKIP（SKIP 为 FFmpeg 下载源需代理）；产物内 4 份 sidecar 源 sha256 与仓库一致；实装探针三条新命令 success。

**发布**：用户 m05719「继续 不用等linux windows了」→ 2026-10-31T01:54:15Z `gh release edit v2.4.4 --draft=false` 已 publish（https://github.com/pingod/HanCast/releases/tag/v2.4.4），DEC-006 边界③的「真机投一次」由用户本人豁免；CHANGELOG 已记录该决定（提交 d53aa22）。

**未做**：Linux(192.168.1.68)/Windows(Host win) 仍未装端（多轮复查 ssh/ping 全超时），用户已表示不再等待，上线时按需补装。

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

实现：后端 `list_folder_media`（`media/parser.py:27` `MediaParser.list_folder`，自然序、只列当前层、跳过隐藏/`._`/非媒体、不探时长）；壳层 `lib.rs:247`；Sidecar commands.py:703。前端新增 `src/stores/playlist.ts`（Pinia 第 10 个）、`MediaInput.vue` 的📁入口 + 队列面板（拖拽/↑↓排序/勾选/全选/播放模式切换）、`CastController.vue` 上/下一集按钮、`cast.ts` 的 `advancePlaylistIfActive`/`skipToNext`/`skipToPrev`/`castQueueItem`（`queueAdvancing` Set + 1.5s 窗口吞 STOPPED 抖动防重复起播）。i18n 加一组 `media.*`（两语言各 346 键），清掉脚手架期未引用 key。

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
   `plugin_task_start/result/cancel`，Sidecar 注册表 59 → **62**，`commands.py:132-227`）；
   壳层 `run_plugin_task` 轮询（lib.rs:332-390），前端封装与 Tauri 命令名不变。
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

## 2026-10-29 发布轮：v2.4.3（投屏历史可回到投屏流程，tag 已推，CI 跑着）

用户原话：「刚才那个'历史'功能有点废啊，只是个记录列表，能不能给加上更丰富的功能，比如支持点击对应的历史条目然后重新回到投屏流程」。

- **历史从流水账变入口**：点条目走 `reuseFromHistory()` 回到投屏流程（本地文件按原始路径重新解析、链接回来源页重新解、文件夹重读队列并摊开面板），行内新增 `▶` 走 `castFromHistory()` 直接投回当初那台设备——两种意图分开，「再投一次」和「换个设备」不该共用一个按钮。
- **记账口径修正**：`MediaInfo` 加 `file_path`（由 `mediaStore.parseFile()` 补，后端不回），本地文件不再记 media server 临时 URL（重启即死链）；`HistoryEntry` 加 `sourceUrl`，分享直链带签名几小时过期，重投优先回来源页；同设备同内容改 `castCount` +1 不堆行；面板底部加「清空全部历史」。
- **测试** `tools/verify-cast-ui.cjs` 25 → **30**：三条钉入账口径、两条直接测真 `history.ts`（次数累加、单设备 20 条上限）。夹具加了 `evaluate(..., globals)` 覆盖点，真 store 才能拿到可控 `localStorage`。
- **文档同轮**：spec §6.6 按实测重写（旧稿把 `entries` 写成按设备分组的 Record、字段名写 `playedAt`、API 写 `entriesFor/remove`，全与代码不符）；顺带校准 tests 清单 32 → **34 个文件 / 8931 → 9247 行**（补 `test_folder_playlist.py`、`test_tv_url_suffix.py`，`test_ghmirror.py` 330 → 335）。
- **验收**：`vue-tsc` 0、`npm run build` 通过、verify-cast-ui 30/30、后端 `pytest` **549 passed**、`audit-doc-refs` 3824 处可疑 0、`check-doc-freshness` FAIL 0 / WARN 0。
- 提交 **341902b**、tag **v2.4.3**、CI run **40794894598**（macos-arm64 首轮抢不到托管机被 cancelled，`gh run rerun --failed` 补跑才绿）。
- **装端**：dmg sha256 `1f25a558…`、pkg sha256 `68664886…` 均与 GitHub 一致；macOS 2.4.2→**2.4.3**（accept 89 PASS / 0 FAIL / ALL PASS、GUI + sidecar 8080 正常）；Linux cachyos `pacman -U` 到 **2.4.3-1**（sidecar 回 `version=2.4.3`、`list_screen_sources` 正常）。
- **下载坑（第二次）**：release-assets 某些字节区间反复 stall，整段重试无效——必须把停滞段再切成 4–6 个子区间单独拉。追加式续传（v2.4.1 那次）会污染字节，别再用。
- **已 publish**：`gh release edit v2.4.3 --draft=false` → draft=false（2026-10-29T02:04:42Z），Release 正文写了改动/验证/边界。v2.4.1、v2.4.2 的 draft 不再单独发布（被 v2.4.3 覆盖）。
- 未做真机复核：全是前端状态机 + localStorage，电视侧行为与 v2.4.2 一致。

## 2026-10-29 晨：v2.4.1 装端（macOS + Linux 完成，Windows 离线）
- CI run 40688622534 首轮 macos-arm64 三次抢不到 GitHub 托管机（cancelled，"job was not acquired by Runner"），第 4 次 rerun 成功；最终三平台全绿，draft Release 10 项资产齐。
- 下载踩坑：release-assets 限速 ~60-150KB/s 且易 stall；pdl.sh（12-14 段并发、全段覆盖重试）最终拿到——pkg sha256 9ba7db…✓、dmg sha256 196b28…✓ 且 hdiutil VALID。中途 resume1.sh 追加式续传导致过 pkg 字节损坏（sha 对不上），已弃用。
- macOS：/Applications 从 2.4.0 → 2.4.1（adhoc），已启动（sidecar 运行）。
- Linux cachyos(192.168.1.68:10086)：pacman -U 2.4.0-1 → 2.4.1-1，sidecar smoke 正常（version 2.4.1，get_settings success，wlgrab 在 /usr/lib/HanCast/）。
- Windows（Host win 192.168.1.68:22）：连接拒绝，机器离线，未装（仍 2.3.15）。
- Release 仍是 draft：按 DEC-006 边界③，publish 前需真机 TCL 确认两处修复（选设备弹窗 + 连续续播）。

## 2026-10-29 v2.4.2 已发布构建并装到两台机器（待用户真机确认）
- CI run 40789901148 全绿（windows 19m34s / macos 8m8s / linux 17m43s / upload 38s），draft Release 10 项资产。
- 下载：pdl.sh 16 段并发，本轮 GitHub 很快（dmg 1 分钟、pkg 2 分钟）；pkg 的 c7 段卡住，手工用新签名补拉剩余 range 后拼装。
- 校验：dmg sha256 35a0936004044da5… ✓、pkg sha256 2510329440d4b4aa… ✓，均与 GitHub 声明一致。
- macOS：/Applications 2.4.1 → 2.4.2（旧包留 /tmp/HanCast_pre_241_081116.app），已启动，description.xml 200。
- Linux cachyos：pacman -U 2.3… → 2.4.2-1；GUI 需重启才生效——SSH 里 hyprctl 缺 HYPRLAND_INSTANCE_SIGNATURE，
  从 /run/user/1000/hypr/ 读出签名 + wayland-1 后用 `setsid nohup /usr/bin/HanCast` 重启成功，
  sidecar get_settings 返回 success。
- 现场取证（用户报「还没解决」时的 Linux 日志）：20 秒内 4 次发往 Mac 的 SetAVTransportURI，中间无用户操作；
  每次都是「用户 Stop → 24ms 后第二次 Stop（play() 自带的清场）→ SetURI → Play」，且 4 次投的是**同一个文件**——
  停止时清空 playingId 让续播找不到当前项，只能重投。两台机器当时都还是 2.4.1（修复未装机），故仍复现。
- 遗留：Release v2.4.1/v2.4.2 都是 draft，待真机确认后 publish；Windows（Host win）仍 2.3.15 且离线。
