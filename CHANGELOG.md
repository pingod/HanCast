# Changelog

> 最后校对：2026-10-15，对应版本 v2.3.11（`package.json:4`）

本文件记录 HanCast 的版本历史。口径说明：

- **一个版本一笔发布提交**，提交信息自带该轮的完整契约说明；版本号与 tag 同名（`v2.3.6` ↔ 提交里的 `（v2.3.6）`）。
- 版本号唯一来源是 **`package.json` 的 `version`**（`scripts/sync-version.cjs:4,17`），`npm run version:set <x.y.z>` 把它刷进 7 处契约点：`src-tauri/tauri.conf.json`、`src-tauri/Cargo.toml`、`src-tauri/Cargo.lock`（本地包那条，v2.3.8 起收进来——此前每版都靠人肉补）、`hancast-backend/pyproject.toml`、`hancast_sidecar/utils/config.py:39`、`src/api/commands.ts`（mock）、`src/stores/settings.ts`。`scripts/check-version.cjs` 自称以 `tauri.conf.json` 为源（口径相反），但全仓无人调用，是未接线的孤儿脚本——判定以 `sync-version.cjs` 为准。详见 [docs/构建与发布.md](docs/构建与发布.md) §1。
- 下表「提交」列即 `git tag <版本>` 指向的提交；细节以 `git show <提交>` 为准，本文只留用户可感知的变化。
- 新加版本时**同一次提交**里补这里，并按 [docs/README.md](docs/README.md) §4 的约定同步对应文档。

---

## v2.3.11 — 2026-10-15（本轮发布提交）

**彻底不再随包 FFmpeg**：安装包里的 `ffmpeg/` 整个删掉，改为检测本机已装的 FFmpeg；确实没有时弹「需要下载运行依赖」，用户自己装、或者让程序下载一份。屏幕投屏是 FFmpeg 的唯一用途，所以这是它唯一的闸门入口。

- **删掉的东西**：仓库根 `ffmpeg/README.txt`、四个平台 `bundle.resources` 里的 `ffmpeg/*` glob、CI 的「Download & unpack FFmpeg」步骤（曾从 `eugeneware/ffmpeg-static` 的 `b6.1.1` 取静态单文件、Windows 改名 `ffmpeg.exe`），以及 `ffmpeg_manager.py` 里夹在中间的「随包目录」那两层（`_get_base_path()` / `_find_in_program_dir()`）。
- **检测链**（新增 `hancast-backend/hancast_sidecar/utils/ffmpeg_deps.py`，524 行；`status()` 在 `:189-252`）：**手动指定 → 系统已装 → 本程序下载的那份**。② 是 `PATH` + 平台常见目录（`search_dirs()` `:104-117`），Windows 额外往 winget 包目录里下钻一层 `bin/`（`_walk_dirs()` `:120-139`）；③ 落在 `<SETTING_DIR>/bin/`（`managed_dir()` `:51-53`，命中则 `source="downloaded"`）。三层共同的判据是**真的跑一次 `-version`**（`read_version()` `:142-162`）——「文件在」不等于「能用」，装坏的 exe、平台不匹配的构建都会被跳过继续往下找，配置里失效的手填路径同样降级（`ffmpeg_manager.py:425-427`），不会把功能锁死。
- **ffprobe 是可选依赖**（`REQUIRED=("ffmpeg",)`、`OPTIONAL=("ffprobe",)` `:43-44`）：macOS 那条 ffmpeg-static 产物本来就只有一个二进制，缺 ffprobe 只影响本地文件的时长探测（`media/parser.py` 用 `probe_path() or "ffprobe"`）。手动指定 ffmpeg 而系统里没有 ffprobe 时，会自动取**同目录**那份当搭档（`:219-225`）——gyan/BtbN 这类包就是两个二进制同目录分发。
- **命令面**：Rust `#[tauri::command]` 62 → **66**（`lib.rs:1373-1440`）、Sidecar 注册 55 → **59**（`commands.py:110-197`）、`commands.ts` 导出 **65**。新增 `ffmpeg_status`（检测报告，同时是弹窗的数据源）、`ffmpeg_download_start`、`ffmpeg_download_status`、`open_ffmpeg_dir`。最后一条是「就地执行但要先问 Sidecar」：它下发 `ffmpeg_status` 拿 `managed_dir` 再调系统文件管理器（`lib.rs:817-844`），**路径不接受前端传入**——否则等于开放「用资源管理器打开任意路径」；它只 spawn 不 wait，因为 `xdg-open` 会活到文件管理器被关闭为止，等它等于把一个按钮按死在命令循环里，而 `explorer` 常返回非 0 却照样开窗，退出码本来就不是判据。
- **下载走后台任务表 + 前端 1s 轮询**（`screencast.ts:114-128`、`:143-157`），理由与解析异步化同一条：归档几百 MB，而 Rust 侧 `COMMAND_TIMEOUT = 60s`（`src-tauri/src/sidecar.rs:18`）等不起。阶段 ∈ `connecting / downloading / extracting / verifying / done / failed`，进度带 `received/total/percent/speed/elapsed`；源按 `(sys_platform, machine)` 选（`_SOURCES` `:264-296`：win32/amd64 → gyan `ffmpeg-release-essentials.zip`，win32/arm64 → BtbN，darwin → ffmpeg-static `.gz`，linux → johnvansickle `tar.xz`）；`MAX_ARCHIVE_BYTES = 512MB`（`:32`）拦 Content-Length 撒谎的响应；解压只按 **basename** 匹配想要的名、不照归档里的路径落盘、`chmod 0o755`，归档无论如何用完即删；收尾必须 `verifying` 再跑一次 `-version`，跑不起来报的是「下载并解包成功，但这个 ffmpeg 跑不起来……多半是当前平台用不了这份构建」而不是假成功。
- **失败给用户看底层异常原文**：`format_error()`（`:308-324`）沿 `__cause__`/`__context__` 逐层收集、去重、用 ` ← ` 连接（最多 3 段），所以弹窗里是 `连接下载源失败（https://…）— ConnectionError: …` 这种能据以判断的句子，不是一句「下载失败」。
- **UI（截图里的功能全部落地）**：第 20 个组件 `src/components/FfmpegDepsModal.vue`（418 行）——缺失清单、按平台的**可复制**安装命令（Windows `winget install --id Gyan.FFmpeg -e` / `choco install ffmpeg -y`，macOS `brew install ffmpeg` / `port install ffmpeg`，Linux `apt install ffmpeg` / `dnf install ffmpeg`，`install_commands()` `:178-188`）、手动指定路径、重新检测、打开安装目录、「下载并继续」+ 进度条与实时速度、失败原文。两个入口：`ScreenCastPanel.vue:43-50`（面板加载后 `ffmpeg.ok=false` 自动弹一次，`depsPrompted` 保证关掉不追着弹）与 `:198-201`（错误条里的「打开依赖」按钮）。下载 `done` → 弹窗自动关 → 投屏闸门随即放开；旧的「手填 ffmpeg 路径」输入框由弹窗接管（手填本身还在，`onSetManual` 走 `set_ffmpeg_path`）。
- **dev mock 补上缺失态**：`src/api/commands.ts:235-246` 新增 `?mockFfmpeg=missing|fail` 两条开关。此前 mock 无条件报「本机已装」，浏览器模式下永远走不到报错分支——这类功能根本没法在 dev 里验收。
- **脚本与打包入口不再假设随包 ffmpeg**：`windows-build.ps1`（删掉「Step 4.6 同步 FFmpeg」与绿色版的 ffmpeg 复制）、`tools/gen-win-screencast-live-req.py`、`verify-screencast-audio-order.{cmd,ps1}`、`verify-win-loopback-feed.cmd`、`verify-win-screencast-tone-content.sh`、`verify-dlna-stop-chain.py`、`verify-proxy-segment-decodable.py`、`tools/rust-check.sh`（本地资源桩删掉 `ffmpeg/ffmpeg`，只剩 mpv + sidecar）共 9 处改为现读本机 ffmpeg。
- **顺带修掉一条测试隔离 bug**：`tests/test_ffmpeg_deps.py` 的 `StatusContractTest.setUp` 把 `managed_dir` patch 起来却没 `addCleanup(...stop)`，mock 泄漏到后续用例——`tests/test_ffmpeg_download_e2e.py::test_gz_download_end_to_end` **只有两个文件同跑时**才失败（它的托管目录被指进了一个已删除的临时目录）。逐类 bisect 定位后补上 cleanup，产品代码无辜（曾怀疑的下载任务表跨线程竞争在生产路径上不成立：`start_download()` 在 `pending` 期间直接早退）。
- **数字口径更正**（本轮没动参数表，是补文档滞后）：`PARAM_SPECS` 27 → **30**（`params.py:35-236`）。
- **验收（代码层）**：定向 `pytest tests/test_screencast.py tests/test_commands.py tests/test_imports.py tests/test_ffmpeg_deps.py tests/test_ffmpeg_download_e2e.py` → **117 passed**。其中 e2e 那组是**真跑下载管线**：本地 HTTP 源 → 流式下载 → gz / zip / tar.xz 三种解包 → 落盘后真执行 `-version` → `status(refresh=True)` 立刻认出 `source="downloaded"`，另含两条失败分支（跑不起来的构建报「跑不起来」、端口没人听时错误里留着 `ConnectionError` 原文）。`vue-tsc --noEmit` rc=0。前端在 **Vite dev server + 浏览器 mock** 实测：缺失态自动弹窗 → 下载进度 → verifying → done 自动关窗 → 闸门放开；`?mockFfmpeg=fail` 的错误块渲染出网络异常原文；手填路径被采纳、「重新检测」、复制安装命令的「已复制」反馈。
- **验收层级要说清**：上面全是源码层 + dev server 的证据，**不含装好的产物**。打包链目前只证明到「配置里再没有 ffmpeg glob、CI 不再有下载步骤、`node scripts/check-resources.cjs` 不再报 ffmpeg 相关缺失」（本机那次剩下的 MISSING 全是还没跑 `npm run build:sidecar` 的 Nuitka 产物）。三件事必须等 CI 产物与 .68 真机：① bundle 里确实不含 ffmpeg；② Windows 上能检出系统已装那份（`source="system"`）；③ 没有 ffmpeg 的机器上弹窗与「让程序下载」真的可用。产物层判据见 [docs/屏幕投屏.md](docs/屏幕投屏.md) §5.8 与 [docs/故障排查.md](docs/故障排查.md) §6.6，本轮发布后由 CI 产物 + .68 真机补证。
- **已知边界**：`set_ffmpeg_path` 只能设不能清（§屏幕投屏 5.1），靠失效降级兜住；流式下载经 curl_cffi 时会打 `CurlCffiWarning: chunk_size is ignored`（curl 没有分块大小的概念），只影响进度粒度不影响字节；Linux 与 macOS 的产物路径本轮只在源码层验过，`_SOURCES` 的每条源没逐条真下载验证过（gyan 与 ffmpeg-static 两条在 CI/真机里验）。

## v2.3.10 — 2026-10-15（本轮发布提交）

修的是**「解析失败」的判定口径**：随便粘一个视频站的 HTML 播放页（例：`https://www.lvsc168.com/play/14439-1-0.html`）也能点投屏，点了却毫无动作。

- **根因不在后端**：`ShareResolver.resolve()` 对这类页面已经如实抛「页面中未找到可播放的视频源」并记进台账（`share_resolver.py:680-683`、`353-354`）。吞掉失败的是前端 `src/stores/media.ts` 的 catch —— 旧口径「只要从文本里抠得出链接就降级透传原始地址」，于是网页地址被当成可播媒体交给电视，电视拉到的是 HTML；顺带这条失败也再没人看得见。
- **收紧**：新增媒体扩展名白名单 `MEDIA_FILE_RE`（`media.ts:15-19`，mp4/mkv/m3u8/mpd/音频图片类），catch 里只有**地址本身就是媒体文件**才透传，其余如实报错、投屏按钮保持禁用（`media.ts:142-182`）。
- **台账入口就地可见**：报错下方新增一行提示「这条已记入解析失败台账：设置 → 解析插件 → 解析失败台账（按域名分组）」（`media.resolveLedgerHint`，`MediaInput.vue:522`；中英双份文案）。台账本身 v2.3.6 就有，只是入口藏在设置页折叠区底部，用户不知道去哪找他粘过的失败地址。
- **dev mock 保真**：`src/api/commands.ts` 的 `resolve_share` 对网页地址同样抛「解不出可播源」。此前 mock 是无条件成功，浏览器模式下永远走不到真实报错分支，这类口径问题也就永远测不出来。
- **测试不再污染用户数据**：新增 `hancast-backend/tests/conftest.py`（session 级 fixture 把 `failures.SETTING_DIR` 指进临时目录），`tools/share_resolve_probe.py` 默认同样重定向、`--keep-ledger` 才写真实台账。此前跑一次测试就往用户设置页那份台账里塞进 `b23.tv/AbCdEf`、`ads.example.com/lp` 之类的假域名，把真实失败冲淡。
- **验收**：本机浏览器实测两条分支——网页地址 → 报错 + 台账提示、三台设备的投屏按钮全禁用；`…/movie.mp4` 直链 → 出预览卡片、按钮可用且不再误显示台账提示。`vue-tsc --noEmit` rc=0；定向 `pytest tests/test_share_resolver.py tests/test_jsplugins.py` 99 passed，且 `parse_failures.json` 跑前后 md5 逐字节一致。文档同步：[docs/HanCast-Frontend-API.md](docs/HanCast-Frontend-API.md) §4.3、[docs/HanCast-Frontend-Spec.md](docs/HanCast-Frontend-Spec.md) 链路 1 要点 3、[docs/故障排查.md](docs/故障排查.md) §7.4/§7.5、[docs/开发指南.md](docs/开发指南.md) §5.1、[tools/README.md](tools/README.md)。
- **验收层级要说清**：上面那两条分支是在 **Vite dev server + 浏览器 mock** 上实测的，不是装好的 `.app`。Tauri 把前端资源压缩后内嵌进主二进制，`strings` 在 `Contents/MacOS/HanCast` 里搜 `m2ts`/`ledgerHint`/提示文案全是 0 命中，所以 `tools/accept-macos-artifact.sh` 这次**证明不了前端那一半**（它只覆盖 sidecar 与打包资源；2.3.10 三平台 CI 全绿 + 该脚本 ALL PASS + `probe-installed-ja3.py` PASS 说明的是后端与二进制）。产物内前端的真实判据只能在装好的 GUI 里粘一次网页地址看报错与按钮状态。
- **没动的地方**：`share_resolver.py:695-699` 候选地址验证遇网络层异常时「按原样交付」是刻意取舍（本机网络判定不稳，电视端反而可能拉得通），本轮不改；它和 `_publish` 预算见底、`_media_ok` 对未知 Content-Type 判 OK 一起，是后端侧仅剩的三类假成功来源。

## v2.3.9 — 2026-10-15（本轮发布提交）

修的是**发布链路自己**：v2.3.8 的 `windows-x64` 作业在 `Build frontend` 步就炸，Windows 安装包从未产出，连带 `Upload to draft Release` 被 skip——功能代码一行没错，错在上一轮刚收进契约点的 `Cargo.lock` 正则。

- **根因**：`scripts/sync-version.cjs:57-68` 的 `read`/`replace`/`expect` 三条正则写死了 `\n`。仓库里 `Cargo.lock` 是 LF，但 Windows runner 的 `actions/checkout` 把文本刷成 CRLF，于是三条同时落空；`expect` 闸门如实判「找不到应被刷新的版本字段，脚本正则已失效」并 `exit 1`。macOS/Linux 检出是 LF，所以只有 windows 一路红——**这条 FAIL 是真的，不是抖动**。
- **复现**：把 `src-tauri/Cargo.lock` 临时转成 CRLF 再跑 `node scripts/sync-version.cjs 2.3.8`，本机输出与 CI 日志逐字一致（`⏭️ …（已是最新）` + `❌ …` + `💥 1 个版本契约点没匹配上`，退出码 1）。
- **修法**：三处正则改 `\r?\n`；`scripts/check-version.cjs:18` 同一形状一起改。判据补了两条：CRLF + 滞后值注入时 `✅ src-tauri/Cargo.lock` 必须改写成功，且改写后仍是纯 CRLF（5618 行 CRLF、0 行混入裸 LF），不污染行尾；两轮结束后 `Cargo.lock` 与改前 md5 逐字节一致。
- **契约文档**：[docs/构建与发布.md](docs/构建与发布.md) §1.2 记下这第二课——契约点的正则必须容忍 CRLF，闸门的表现是「宁可毫秒级炸掉也不安静发一版没人知道装的是哪版」。
- **与 v2.3.8 的关系**：功能内容与 v2.3.8 完全相同，只差这个脚本修复。v2.3.8 的 macOS 产物已验收通过（`tools/accept-macos-artifact.sh 2.3.8` 全绿 + `tools/probe-installed-ja3.py` 判 PASS，产物 JA3 与源码级 `curl_cffi[chrome124]` 逐字节同一个哈希），已装在本机。

## v2.3.8 — 2026-10-15（`2562d9f`，修复本体在 `9e3f9c9`）

修 v2.3.7 的一个**产物级**缺陷：源码里的 TLS/JA3 指纹伪造在打包版里根本没生效。

- **根因**：`bundle.resources` 是白名单，v2.3.7 只加了 `--include-package=curl_cffi`，没往三份平台 conf 里加 `curl_cffi/**/*`，于是 Nuitka 冻结出的 `curl_cffi/` 目录在打包时被整体丢弃，`make_session()` 的 `import curl_cffi` 当场失败并静默回落到 `requests`——指纹照样是 python-requests，且日志里一句错都不报。
- **修法**：三端 conf 各补 `curl_cffi/**/*`（`_cffi_backend*.so/.pyd` 由既有的 `*.so`/`*.pyd` 顶层 glob 接住）；`scripts/check-resources.cjs` 新增预检，把 `build_sidecar.py` 里每条 `--include-package` 与 resources 白名单逐条对齐，产出有该目录而白名单没列就 exit 1；`uv.lock` 补齐 curl-cffi 0.16.3 / cffi 2.1.1 / pycparser 3.11。
- **契约**：`src-tauri/Cargo.lock` 收进版本契约点（第 7 处），`version:set`、无参数同步、`check-version.cjs` 三处同时覆盖；`tools/rust-check.sh` 的桩列表补 `curl_cffi`，本地 `cargo check` 不会因新 glob 落空而失败。
- **验收判据**：`tools/accept-macos-artifact.sh` / `tools/verify-windows-artifact.sh` 按 tag 探测 `curl_cffi` 与 `browser.rs`，产物里缺二进制即 FAIL。另加产物级闭环探针 `tools/probe-installed-ja3.py`：直接起 `.app` 里那个冻结 sidecar，喂一条带一次性标记的 `resolve_share`，在同一次运行里与 stdlib ssl 基线对照，**判据只认 GREASE**（源码级 `probe-tls-ja3.py --client hancast` 走 `sys.path` 里的源码，测不到这一层）。
- **实测**：v2.3.7 产物 JA3 `4f05321b…`、零 GREASE（判 FAIL，正是降级形状）；装好 v2.3.8 后 `tools/accept-macos-artifact.sh 2.3.8` 全绿（含 curl_cffi 目录 / `_wrapper` 二进制 / 顶层 `_cffi_backend` 三项），同一条探针判 PASS，产物 JA3 `73e88c3c…` 与源码级 `curl_cffi[chrome124]` **逐字节同一个哈希**。

## v2.3.7 — 2026-10-15（`2a93873`）

- **浏览器通道**：规则包可借宿主 WebView 真渲染一次（macOS WKWebView / Windows WebView2），离屏加载播放页、静音起播、嗅探 `master.m3u8` 一类流地址，并把含 HttpOnly 的 Cookie 一起搬回给规则包。串行一把锁、90s 熔断，渲染失败规则包照旧走自己的路。
- **TLS/JA3 指纹伪造**（源码级）：`utils/httpclient.py` `make_session()` 按 UA 反推 curl_cffi 的 impersonate 目标，后台参数 `http.tls_impersonate` 可关。**注意：这一版打包产物里 curl_cffi 被打包白名单漏掉，指纹伪造在打包版没生效，v2.3.8 才修好**——三端 CI 产物由 `tools/accept-macos-artifact.sh` / `tools/verify-windows-artifact.sh` 抓出来的。
- 取证记录见 [docs/反爬实现取证与浏览器通道.md](docs/反爬实现取证与浏览器通道.md)。

## v2.3.6 — 2026-10-15（`efa3fec`）

解析链「规则包自己走完一条路」的四个缺口。

- **B 站多画质**：默认投最高档，投屏中还能切。档位不是缓存直链（源站签名带时效），切档 = 带 `ctx.quality` 重新解析再 `SetAVTransportURI`；规则包回报实际交付档，避免「UI 写 1080P、电视收到 720P」。切完 Stop→SetURI→Play 后用 `restorePosition` 重试 Seek 续进度。
- **解析失败台账** `media/failures.py`：按域名记录「粘了但解不出来」的地址与原因次数，设置页可看可删；只写本机配置目录，不外发、不进日志。这是后续挑站点写规则包的清单来源。
- **沙箱扩接口**：签名原语一套（md5/sha1/sha256/sha512、HMAC、AES-ECB/CBC、RSA PKCS#1 v1.5、hex/base64）＋身份/Cookie（`host.deviceId()`、`uuid`/`randomHex`、`cookies`/`setCookie`/`clearCookies`，罐子落在各包自己的 `cookies.json`）。见 [docs/解析插件开发指南.md](docs/解析插件开发指南.md) §3.5-3.6。
- **身份收口** `utils/identity.py`：桌面/移动 UA 单点定义并走后台参数；此前规则里写 Chrome/124、本机代理另写 Chrome/120，同一次投屏向源站报两个浏览器。设备号改为恒定 32-hex 存盘复用。

## v2.3.5 — 2026-10-14（`87b109b`）

- **投屏不再被「挖字节」**：链路内代理只在探测首块时多读，之后原样转发（`media/server.py` `_peek_upstream`）。
- **封面走 https**：B 站缩略图统一升级安全域，电视端不再拿到会被重定向的 http 图链。
- **静默下线的设备会消失**：SSDP 侧新增陈旧摘除（`discovery.device_stale_s`，默认 90s），不再靠 byebye 才能移除。
- **抖音包 v1.1.0**：领券口令/链接解析补齐。
- 验收：静默下线用替身设备端到端验过（死亡→摘除 147s）。

## v2.3.4 — 2026-10-14（`47acc76`）

- **规则包索引超时改为后台参数** `rules.index_timeout_s`（默认 40s）——之前硬编码，慢网络直接整体失败。
- **版本 SSOT 装滞后闸门**：`check-version.cjs` 会拦下「只改了产物、源码版本号滞后」这类漏改。
- 参数表验收脚本改为与 tag 源码逐键比对，不再写死条数（`9787f2b`）。

## v2.3.3 — 2026-10-14（`9227adf`）

**解析规则彻底包化，程序不再携带任何内置规则。**

- 删除编译进程序的站点规则（`bili.py` / `douyin.py` / `bili_resolver.py`）与随包播种的「内置规则包」概念：站点解析能力只能来自用户安装的规则包，全新安装就是不解析。
- 新增官方规则包索引与一键更新：`rule-packs/index.json` + zip 产物 + `plugin_pull`（sha256 校验）。见 [rule-packs/README.md](rule-packs/README.md)。

## v2.3.2 — 2026-10-14（`60f8432`）

- 内置规则包随包播种（`seed_bundled()` 按版本播种进用户插件目录）＋设置页「规则总览」统一管理。
- 该轮的播种机制在 v2.3.3 已整体退役。

## v2.3.1 — 2026-10-14（`0bc4983`）

- chore 发版：把 JS 解析插件与系统声音「当前出声」标记（`1a2f862`）带进第一个正式产物。
- 验收补 JS 解析插件的产物级判据（`9787f2b` 一线）。

## v2.3.0 — 2026-10-14（`3bb1cca`）

**JS 规则包解析插件。**

- QuickJS 沙箱（`media/rules/jsruntime.py`）＋ ZIP 导入（`media/rules/jsplugins.py`）＋设置页管理界面。
- `host.*` 宿主接口：fetch、DOM/CSS 选择、日志、超时预算；脚本拿不到 Python 世界。
- 声明式 JSON 规则与 JS 包共用同一条 `share_resolver` 管线。见 [docs/解析插件开发指南.md](docs/解析插件开发指南.md)。

## v2.2.25 — 2026-10-14（`c97ff52`）

- **环回按单调时钟补静音**：WASAPI 环回在静音渲染端点可能整场零字节，把 `amix`/HLS 饿死成「超时未生成」。改成按单调时钟节流补帧。对照脚本 `tools/verify-win-loopback-feed.cmd`。

## v2.2.24 — 2026-10-14（`05c1033`）

- 环回流改由读线程关闭，停止链路不再打崩 sidecar。

## v2.2.23 — 2026-10-14（`68bbb29`）

- 视频滤镜挪到所有输入之后：一选麦克风/系统声音就投崩（滤镜链引用了尚未建立的输入）。

## v2.2.22 — 2026-10-14（`a9dc541`）

- 图片皮判型窗口按上游类型分档，投哑原因写进日志。
- dshow 分段头兜底：别把 `Alternative name` 当设备名（`ff4a208`）。

## v2.2.21 — 2026-10-13（`f7da157`）

- **投屏指定窗口**改成真·选窗口列表；**系统声音与麦克风真正能采**（Windows 走 pyaudiowpatch 环回，macOS 走 CoreAudio Audio Tap）。
- 前提事实：FFmpeg 没有 WASAPI 输入（上游 ticket #9408 未合并）。

## v2.2.20 — 2026-10-13（`dd3c271`）

- sidecar 自报版本补齐版本契约点（`config.py` 的 `self.version` 纳入同步链）。

## v2.2.19 — 2026-10-13（`12d6196`）

- **stdio 一律强制 UTF-8**（不再只在 Windows 包裹）：没给 locale 时 macOS/Linux 退回 ASCII，所有带中文的响应变成 `success:false`，中文日志被编码器吞掉。表现为「设置页读不到后台参数」。

## v2.2.18 — 2026-10-13（`3181082`）

- 浏览器观看通道改 **1 秒分片**（DLNA 侧仍 2s），首屏等待砍半。
- **后台参数设置页**：26 项可调参数集中管理（`params.py` `PARAM_SPECS`）。
- 修手机音量键（DLNA `RenderingControl` 按键链路）。

## v2.2.17 — 2026-10-13（`6b9c84b`）

- 采集站（JS 渲染页）粘贴投屏可用：规则里补 `render`/多阶段候选。

## v2.2.16 — 2026-10-13（`4fadddd`）

- **粘贴解析改成站点规则管线**：站点专路从 `share_resolver` 里拆出来，一站一段 JSON（`media/rules/declarative.py`），加站点不再需要改代码重打包。
- 规则解析失败只让路并记日志，不冒泡成用户看到的解析失败。

## v2.2.15 — 2026-10-13（`99260b5`）

- **控制端 SOAP 信封补 `s:encodingStyle`**：真机二分锁定 TCL 85T8G Max 只接受带 SOAP 1.1 encodingStyle 的信封，同一份 URI/DIDL 带上稳定 200、去掉稳定 500。此前那条 500 被误读成「电视端投屏服务已退出」，即用户口中的「投屏只成功过一次」。

## v2.2.14 — 2026-10-13（`c983c4e`）

- macOS 产物补 **adhoc 深签**，解锁打包版组播：linker-signed 身份每版换 cdhash，TCC「本地网络」门按身份判定会静默拒绝；重签后同一份字节从「通道 broadcast / Errno 65」变「通道 multicast / 24 条」。

## v2.2.13 — 2026-10-13（`bd847da`）

- **停止链路如实回报**：`AVTransport Stop` 控制地址解析失败时以前只记 warning 就返回成功，本机拆了会话、UI 关掉，电视一条指令都没收到 →「怎么点都停不掉」。现在 `stop()` 返回是否送达，未送达原因随 `control_failed` 传到前端并给明确提示。
- macOS `Info.plist` 补 `NSBonjourServices`。

## v2.2.12 — 2026-10-13（`8eaca95`）

- **macOS 声明「本地网络」权限**：macOS 15+ 把本地网络做成 TCC 权限，缺 `NSLocalNetworkUsageDescription` 的应用不弹窗、直接静默拒绝——出站组播 `[Errno 65] No route to host`、入站 LAN 连接握手后零字节。修掉「发现不了 Windows 设备」「浏览器分享不可用」两条同源缺陷。

## v2.2.11 — 2026-10-12（`65ad943`）

- **DLNA 控制地址按描述基准解析**：设备描述并不固定在 `/description.xml`（TCL 只在根路径提供），拿到 404 HTML 后每条 SOAP 被静默跳过、界面报「推送成功」而电视一次都没收到。改为以 SSDP LOCATION／手动添加命中的 URL 为基准解析并回退穷举；解析不出抛 `ControlUrlUnavailable`，电视返回 5xx 抛 `RendererUnresponsive`。
- **发现链路取证**（NOTIFY 心跳条数/网卡/通道）与 **链路内 HTTP 绕系统代理**。

## v2.2.10 — 2026-10-12（`304a1fa`，未打 tag）

- 屏幕投屏改 **HLS 双供**（DLNA 电视 + 浏览器观看页）。
- **手动添加设备**（多播被拦时的兜底）＋**日志面板**。
- 粘贴解析增强。
- 该轮做了版本 bump 与发布提交，但没有对应 tag；下一个 tag 是 v2.2.11。

## v2.2.9 — 2026-10-12（`7413de6`）

- 复制改三级降级：`navigator.clipboard.writeText()` 是 promise，同步 `try/catch` 抓不到 reject，原降级路径永远不触发。现在 Rust `write_clipboard` → Web API → `execCommand`。

## v2.2.8 — 2026-10-12（`d7a7567`）

- **「粘贴」按钮改由 Rust 读剪贴板**：WKWebView 不给页面 clipboard-read 权限，`readText()` 在 macOS 打包版静默 reject，点一次输入框仍为空。新增 `read_clipboard` 命令，浏览器 dev 模式再回退 Web API。

## v2.2.7 — 2026-10-12（`ca3e558`）

- NOTIFY 组播被阻断时回退子网广播；发送失败打印 errno 且去重（此前上千条无 errno 告警刷屏，本机 renderer 对手机完全不可见）。

## v2.2.6 — 2026-10-12（`2e8990c`）

- **解析任务化**：`resolve_share` 立即返回 `task_id`，后台线程执行，新增 `get_resolve_result` 轮询（pending 上限 3、完成 10min TTL）；解析期间投屏状态轮询不再被阻塞最长 25s。
- 候选地址按媒体似然度重排（已知平台域名优先）；快手短链、口令粘贴即解析。

## v2.2.5 — 2026-10-12（`7c40157`）

- **粘贴投屏增强**：新增 `resolve_share`，从任意粘贴文本提取地址（http(s) 直链、`b23.tv`/抖音/小红书裸短链、BV/av 号兜底），短链自动展开，B 站取流＋本地代理，抖音走 iesdouyin 分享页 JSON，其余按 `og:video`/`<video>`/内嵌 m3u8・mp4 提取，HLS 优先以适配电视 DLNA。
- 解析跑在单线程命令循环内，全部请求共享 25s 总预算（该限制在 v2.2.6 解除）。

## v2.2.4 — 2026-10-11（`1a19f0c`）

全项目复查的 9 处修复：

- MediaServer 固化默认路由 IP：选定网卡后投本地文件电视拉不到流。
- `start_raw` 首帧校验重构：先读帧再起读取线程（此前双 reader 抢管道致帧撕裂），读取限时 10s（此前 macOS 权限弹窗会永久卡死命令循环）。
- `RawCaptureThread` EOF 不再静默：非 stop 路径退出上报错误并回收子进程。

## v2.2.3 — 2026-10-11（`9ebfa79`）

- chore：版本同步到 2.2.2，修安装包版本号不一致。

## v2.2.2 — 2026-10-11（`6dd2e9f`）

- CI：macOS FFmpeg 改用 `ffmpeg-static` 静态构建（BtbN 不提供 macOS 资产）。

## v2.2.1 — 2026-10-11（`97835de`）

- 修托盘菜单 `update_tray_menu` 类型错误；CI 增加 FFmpeg 下载准备步骤。

## v2.2.0 — 2026-10-11（`933b7bb`）

- **添加电脑投屏**：本机屏幕/指定窗口推到 DLNA 大屏，并提供浏览器免安装观看页。
- 首版走 WebRTC，随后（v2.2.10）改为 HLS 双供，`aiortc` 依赖移除。

## v2.1.0 — 2026-10-11（`feec282`）

- **组播不可用时回退子网广播发现设备**：部分系统禁止 UDP 组播发送（实测 macOS 叠加代理/网络扩展后所有 `sendto` 返回 `EHOSTUNREACH`，连 lo0 也失败），M-SEARCH 完全发不出去、设备列表始终为空，而单播与子网广播正常。`_send_msearch` 失败即按各接口子网广播重发。

---

## 更早

v2.1.0 之前的历史在仓库首次打 tag 之前（基于 [xfangfang/Macast](https://github.com/xfangfang/Macast) 二次开发的 Tauri 2 + Python Sidecar 重构期），本文件不追溯；如需考古用 `git log --until=2026-10-11`。
