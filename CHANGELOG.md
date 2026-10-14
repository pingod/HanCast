# Changelog

> 最后校对：2026-10-15，对应版本 v2.3.9（`package.json:4`）

本文件记录 HanCast 的版本历史。口径说明：

- **一个版本一笔发布提交**，提交信息自带该轮的完整契约说明；版本号与 tag 同名（`v2.3.6` ↔ 提交里的 `（v2.3.6）`）。
- 版本号唯一来源是 **`package.json` 的 `version`**（`scripts/sync-version.cjs:4,17`），`npm run version:set <x.y.z>` 把它刷进 7 处契约点：`src-tauri/tauri.conf.json`、`src-tauri/Cargo.toml`、`src-tauri/Cargo.lock`（本地包那条，v2.3.8 起收进来——此前每版都靠人肉补）、`hancast-backend/pyproject.toml`、`hancast_sidecar/utils/config.py:39`、`src/api/commands.ts`（mock）、`src/stores/settings.ts`。`scripts/check-version.cjs` 自称以 `tauri.conf.json` 为源（口径相反），但全仓无人调用，是未接线的孤儿脚本——判定以 `sync-version.cjs` 为准。详见 [docs/构建与发布.md](docs/构建与发布.md) §1。
- 下表「提交」列即 `git tag <版本>` 指向的提交；细节以 `git show <提交>` 为准，本文只留用户可感知的变化。
- 新加版本时**同一次提交**里补这里，并按 [docs/README.md](docs/README.md) §4 的约定同步对应文档。

---

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
