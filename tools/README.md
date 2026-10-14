# HanCast — `tools/` 验收与探针脚本索引

> 这份文档回答：`tools/` 下这些脚本各自验什么、怎么跑、需要什么环境，以及新增脚本要守哪些约定。

`tools/` 里放的是**可重复运行的验收 / 探针脚本**，不是一次性的 `/tmp` 手搓脚本。它们必须自适应当前机器——设备名、网卡、产物路径、参数条数都从现场现读，不写死。用法上的两条铁律：**修 bug 之前先跑它复现，修完之后再用它证明**。「证明」的对象是判据（退出码 / PASS-FAIL 行），不是截图，也不是「我点了一下看起来对」。

当前分类表收录 **41 个顶层脚本 + 1 个目录型验收包**（`browser-channel-livecheck/`，4 个文件合写成 §1.7 的一行）。核对计数用 `git ls-files tools/`：现在是 47 项 = 42 个顶层脚本文件 + 验收包 4 个 + 本 README，其中 `audit-doc-refs.py` 是全仓文档体检工具，用法记在 `AGENTS.md:598` 与 [docs/README.md](../docs/README.md) §4，不进本索引的分类表。`ls tools/` 还多一个不入库的 `__pycache__/`。

---

## 1. 分类索引

### 1.1 产物与安装

对**打包结果**而非源码做验证，是发布链路的闸门。

| 脚本 | 一句话用途 | 运行方式 | 前置环境 |
|------|-----------|---------|---------|
| `accept-macos-artifact.sh` | macOS 产物验收总入口：版本、Info.plist 三条声明、签名身份、产物内文件 sha256 对齐 tag、参数表逐键比对、SSDP 心跳通道、跨机入站、JS 插件宿主、窗口/音频契约，逐项打 PASS/FAIL | `tools/accept-macos-artifact.sh <version>`（例 `tools/accept-macos-artifact.sh 2.3.5`） | 仅 macOS；`/Applications/HanCast.app` 已装 CI 产物；仓库里能 `git show v<version>`（tag 已 fetch）；局域网可达 |
| `install-macos-artifact.sh` | 把 CI 的 dmg 原样装进 `/Applications`，不做任何补丁；装完直接接 `accept-macos-artifact.sh` | `tools/install-macos-artifact.sh <version> <dmg 路径>` | 仅 macOS；dmg 产物；当前用户在 `admin` 组（全程不提权，`:8-11`）；旧 sidecar 必须已自行退出（脚本只等不 `pkill`，`:31-34`） |
| `verify-windows-artifact.sh` | 验收装在 .68 上的 CI 产物**本身**：注册表 `DisplayVersion` == 要求版本；安装目录里那份源码级 `.py` 逐个与 `git show v<tag>` 比对（两边都 `tr -d '\r'`，Windows 包全是 CRLF，直接 certutil 比 tag 哈希会把装对的版本判成装错）；curl_cffi 三件套与主程序里的离屏窗口标签单独扫一遍，tag 里没这项就 SKIP 而不是假 FAIL | `tools/verify-windows-artifact.sh <version>`<br>`… <version> params.py utils/httpclient.py`（只比对指定文件） | 仅 macOS 侧发起；能 `ssh administrator@192.168.1.68`；本地有 `v<version>` tag；不碰 UI、不打补丁。任一项 FAIL = 这版产物在他手上不可信，别继续让他测 |
| `pull-rule-packs-installed.py` | 驱动**打包版** sidecar 走 `plugin_check_updates` → `plugin_pull`，证明已装的规则包真能升到官方索引版本 | `python3 tools/pull-rule-packs-installed.py`<br>`… --only hancast-douyin`<br>`… --no-relaunch`<br>`… --app /Applications/HanCast.app` | 仅 macOS（默认 `APP = /Applications/HanCast.app`，`:38`）；打包产物；能访问 `raw.githubusercontent.com`，走系统代理时带 `HTTPS_PROXY`；脚本会**整体退出并重启 GUI** |
| `cleanup-macos-aggregates.py` | 清掉探针残留在系统里的 HanCast CoreAudio 聚合设备（默认只打印计划） | `hancast-backend/.venv/bin/python tools/cleanup-macos-aggregates.py`<br>`… --apply`<br>`… --apply --product` | 仅 macOS；后端 venv（import `hancast_sidecar.screencast.macos_tap`） |
| `rust-check.sh` | 本地 `cargo check` 的前置：按 Tauri 配置建一次性空桩，让资源 glob 不落空；check 完只删自己建的文件 | `tools/rust-check.sh`<br>`tools/rust-check.sh --release`（参数透传给 `cargo check`） | 本地开发机；`rustc`/`cargo`；会**在 `src-tauri/` 下临时写文件**（`trap cleanup EXIT` 回收，已存在的路径绝不动） |
| `build-rule-packs.py` | 官方规则包的生产工具：`rule-packs/<name>/` → `rule-packs/dist/<name>-<version>.zip` + `index.json` | `python3 tools/build-rule-packs.py`（重建）<br>`… --check`（只校验，CI/验收用）<br>`… --clean`（删孤儿 zip） | 任意平台，纯 Python 标准库；详见 [rule-packs/README.md](../rule-packs/README.md) |

### 1.2 DLNA / SSDP 链路

| 脚本 | 一句话用途 | 运行方式 | 前置环境 |
|------|-----------|---------|---------|
| `ssdp_probe.py` | SSDP 现场探针：设备「发现不到」时先看网络上到底有什么。每块网卡各开一个组播成员，能看出「某块网卡收不到组播」这类单向发现问题 | `python3 tools/ssdp_probe.py`（全部网卡，听 20 秒并主动搜一轮）<br>`… --seconds 60`<br>`… --st MediaRenderer --search-only` | 真机 + 局域网；与 HanCast 同网段；会**向 239.255.255.250:1900 发组播** |
| `verify-ssdp-offline-eviction.py` | 静默下线的设备必须被探活摘掉：起一台假 MediaRenderer 替身，`SIGKILL` 掉它（发不出 byebye），等 sidecar 连探两次无应答后在日志里摘除 | `python3 tools/verify-ssdp-offline-eviction.py`（用打包版日志）<br>`… --log /path/to/hancast.log`<br>`… --serve --port 49217`（只当替身，内部用） | 默认读**打包版**日志（macOS 路径）；替身监听 `127.0.0.1`，LOCATION 故意写 127.0.0.1 以绕过 `_is_own_location` 自通告回环过滤；等待时长受后台参数 `discovery.device_stale_s`（默认 90s）影响 |
| `verify-dlna-stop-chain.py` | 真机验证停止链路：用 CI 打进 `.app` 的 sidecar 把测试视频推给 Windows renderer，再从 renderer 自己那边确认 `TransportState` 真从 `PLAYING` 变回 `STOPPED`（只看本机状态会骗人） | `python3 tools/verify-dlna-stop-chain.py` | 仅 macOS 打包产物（`APP = /Applications/HanCast.app/Contents/MacOS`）；**必须先整体退出 HanCast**（sidecar 空载要占 8080）；`/tmp/hc_media/test.mp4` 存在（docstring 给了用 .app 自带 ffmpeg 生成的命令）；局域网里要有一台在跑的 Windows renderer（默认认 `display_name` 含 `AMD-YES` 或 IP `192.168.1.68`） |
| `verify-dlna-volume-chain.py` | 离线验证音量键链路（手机作控制端 → RenderingControl → MPV）：直接给 `DLNAProtocol.call()` 喂 SOAP 信封，用假 renderer / 截获的 IPC 命令做判据 | `hancast-backend/.venv/bin/python tools/verify-dlna-volume-chain.py` | **不需要真机**；必须用后端 venv 的解释器（系统 `python3` 没有 `requests`/`lxml`）；判据全部 PASS 才退出 0 |

### 1.3 屏幕投屏 — macOS

| 脚本 | 一句话用途 | 运行方式 | 前置环境 |
|------|-----------|---------|---------|
| `probe-macos-systap.py` | macOS 系统声音（CoreAudio Audio Tap）真机探针：`ensure_device()` → 设备出现在 avfoundation 列表 → 抓 3 秒里真带 440Hz → `destroy_device()` 后设备消失 | `hancast-backend/.venv/bin/python tools/probe-macos-systap.py [ffmpeg 路径]` | 仅 macOS；后端 venv；判据是 440Hz ±2% 带内能量显著高于静置基线（不是「有数据」就算过）——本机播放/采集链有约 −0.8% 固定音高偏差 |
| `run-macos-tap-probe.sh` | 把上面那个探针装进一个最小 `.app` 再跑，以复现产品路径的 TCC 身份 | `bash tools/run-macos-tap-probe.sh [ffmpeg 路径]` | 仅 macOS；`hancast-backend/.venv/bin/python` 存在；CoreAudio 采集是 TCC 权限，判给**负责进程**的 bundle 且要求 `NSAudioCaptureUsageDescription`——裸 python 没 bundle，`AudioHardwareCreateProcessTap` 照样返回 noErr 但环回永不产出数据。输出落 `$TMPDIR/hancast-tap-probe.log`；临时 app 建在 `$TMPDIR/HanCast Tap Probe.app`，bundle id 可用 `HANCAST_TAP_BUNDLE_ID` 覆盖 |
| `probe-macos-audio-pitch.py` | 「音高是否跑掉」的对照实验：拿已在售的 BlackHole 走同一条测量链，判断 436.3Hz（−0.84%）是新环回路径的缺陷还是这台机器上任何进程内环回都有的表现 | `hancast-backend/.venv/bin/python tools/probe-macos-audio-pitch.py [ffmpeg 路径]` | 仅 macOS；**需装 BlackHole**；⚠️ 会**临时把系统默认输出改成 BlackHole 2ch**，结束（含异常）一定还原并复核；不写任何文件 |
| `probe-window-picker.py` | 验证「投屏指定窗口」的枚举与裁剪数学：CoreGraphics 显示器顺序 vs avfoundation `Capture screen N` 顺序、两套坐标系（CGDisplayBounds y 向上 / 窗口列表 y 向下）与 Retina 点→像素换算 | `hancast-backend/.venv/bin/python tools/probe-window-picker.py`（只枚举）<br>`… --capture`（抽样真投 2 秒） | 仅 macOS；后端 venv（import `screencast.ffmpeg_manager`）；多屏环境才有意义；坐标错了不会报错、画面照样出，所以必须真跑捕获 |
| `dump-cgwindowlist.py` | 原样 dump `CGWindowListCopyWindowInfo`，用来核对 `darwin_windows()` 的过滤条件 | `python3 tools/dump-cgwindowlist.py` | 仅 macOS；**故意不用 pyobjc**——这台机器没装 Quartz 绑定，而打包 sidecar 走的就是 ctypes + CoreFoundation，必须复刻同一条读取路径，否则差异说明不了问题 |
| `verify-browser-segment-live.py` | 真机验证「浏览器通道分片时长可配置且不必重编译」：默认必须是 1 秒分片；`save_params` 把 `screencast.browser_segment_seconds` 改成 4 后同一份二进制停开一次必须变 4 秒；最后改回默认并清掉覆盖项 | `python3 tools/verify-browser-segment-live.py` | 仅 macOS 打包产物（`.app` 内的 sidecar）；**会整体退出 HanCast**（GUI 与 sidecar 抢 8080），结束把应用开回来；屏幕捕获依赖父进程的「录屏」权限，被拒时 start 直接报错、判据如实 FAIL |
| `verify-watch-page-live.mjs` | 浏览器通道观看页真机验收（1 秒分片）：驱动打包版 sidecar 起真实屏幕投屏，再用真实 Chromium（headless shell + CDP）打开观看页，看画面是否真在动、有没有卡死、端到端延迟多少 | `node tools/verify-watch-page-live.mjs [segmentSeconds]`（默认 1 = 出厂契约；传 4 会先 `save_params` 覆盖再测） | 仅 macOS 打包产物；`~/.cache/puppeteer/chrome-headless-shell` 下有 `mac_arm-*` 构建；**会整体退出 HanCast**，结束自动打开回来；截图落 `/tmp/hancast-watch-live.png`；退出码 0 = 全部断言通过 |

### 1.4 屏幕投屏 — Windows（.68 测试机）

`.68` = `192.168.1.68`，项目的 Windows 真机测试机。**gdigrab 需要交互桌面，ssh 会话在 session 0 抓不到桌面**，所以凡是真跑捕获的 `.cmd` 都必须由计划任务丢进交互会话，不能直接 ssh 执行。

`.cmd` 的通用投递方式（各脚本头部都写了）：

```bash
perl -pe 's/\r?\n/\r\n/' tools/<name>.cmd > /tmp/<name>.cmd          # 必须 CRLF；仓库副本是 LF
scp /tmp/<name>.cmd administrator@192.168.1.68:D:/Temp/<name>.cmd
ssh administrator@192.168.1.68 "schtasks /create /f /tn <TASK> /tr D:\Temp\<name>.cmd /sc once /sd 2999-01-01 /st 00:00 /ru Administrator /it"
ssh administrator@192.168.1.68 "schtasks /run /tn <TASK>"
ssh administrator@192.168.1.68 "type D:\Temp\<out>.txt"              # 取结果
ssh administrator@192.168.1.68 "schtasks /delete /tn <TASK> /f"      # 收尾
```

`.cmd` 必须**保持 ASCII-only**：cmd 读带 UTF-8 中文的批处理会按字节偏移错位解析（2026-10-14 实测，见 `verify-screencast-audio-order.cmd:8-9`、`gen-win-screencast-live-req.py:14-16`）。

| 脚本 | 一句话用途 | 运行方式 | 前置环境 |
|------|-----------|---------|---------|
| `probe-wasapi-loopback.py` | WASAPI 环回真机探针，验三件事：默认渲染设备的 loopback 能否开到 int16/48k/立体声；真在放声音时能否读到非零采样；**静音期** PortAudio 是照实时节奏交付零块还是阻塞（第三点决定 ffmpeg 侧要不要在 Python 里按单调时钟补零） | `python tools/probe-wasapi-loopback.py` | 仅 Windows（.68）；需 `pyaudiowpatch` |
| `probe-win-audio-pipe.py` | Windows 系统声音链路真机探针，用仓库里的真实代码覆盖最容易在打包后才崩的那段：`loopback.open_stream` → `build_audio_input` 的 `pipe:0` 与 amix 图 → ffmpeg stdin → HLS 里真带一条可解码音频流 | `set "PYTHONIOENCODING=utf-8"& set "HANCAST_SRC=D:\Temp\pkg"& D:\Temp\pa-probe\Scripts\python.exe D:\Temp\probe-win-audio-pipe.py` | 仅 Windows（.68）；`HANCAST_SRC` 指向被测代码根（缺省回落到仓库的 `hancast-backend`）；不碰 gdigrab，视频输入用 `lavfi testsrc` 顶替；判据是播 440Hz 时 HLS 解出的该频点分量显著高于不播时的基线 |
| `probe-win-window-picker.py` | Windows「投屏指定窗口」真机探针，验四件事：`_list_windows()` 交出来的窗口任务栏里是否真有（裸 `EnumWindows` 会混进 `CHeadBarWindow`、`com.han.hancast-siw` 这类影子输入窗口，选到就是黑屏）；中文/冒号/与号/引号/括号逗号能否交给 gdigrab；抓到的尺寸 == 客户区且非全黑；不存在的标题必须让 ffmpeg 快速失败 | `run-winpick.cmd --survey`（只枚举并打印属性表，用来定过滤判据）<br>`run-winpick.cmd`（完整跑） | 仅 Windows（.68）**桌面会话**；必须用 `schtasks … /it` 投递（脚本头给了 `HANCAST_WINPICK` 的完整命令）；窗口由探针自己 `RegisterClass` + `CreateWindowExW` 开出来，标题直接进 Win32 API 不经 shell；探针会把自己置顶——gdigrab 抓的是屏幕上那块矩形，不是窗口自己的 buffer |
| `verify-screencast-audio-order.cmd` | 验证「屏幕 + 麦克风/系统声音」的 ffmpeg **参数顺序**：`-vf` 夹在两个 `-i` 之间会被判给下一个输入（麦克风）并以 `Option vf cannot be applied to input url audio=…` 死掉。三个 case：OLD 必须 FAIL（复现 v2.2.22 及以前）、A（仅麦克风）必须 PASS、B（麦克风 + 环回 s16le + `-filter_complex amix`）必须 PASS | 按上面的通用投递方式，任务名 `HANCAST_VFT`，脚本落 `D:/Temp/hcvf.cmd` | 仅 Windows（.68）交互桌面；打包版 ffmpeg 6.1.1（`C:\Users\Administrator\AppData\Local\HanCast\ffmpeg\ffmpeg.exe`）；结果看 `D:\Temp\hcvf-out.txt`：`OLD_EXIT` 非 0、`A_EXIT`/`B_EXIT` 为 0，且 `a.m3u8`/`b.m3u8` 各报一条视频 + 一条 aac |
| `verify-screencast-audio-order.ps1` | 上面那三个 case 的结果读取器：数每个 `m3u8` 的分片条数，并用 ffmpeg 打印 `Input #` / `Stream #` | `powershell -File verify-screencast-audio-order.ps1`（在 .68 上） | 仅 Windows（.68）；`D:\Temp\hcvf\{old,a,b,c}.m3u8` 已由 `.cmd` 那轮产出；缺文件打 `MISSING` 后跳过 |
| `verify-win-loopback-feed.cmd` | 分离「stdin 静默但打开」与「实时涓流」两种喂料方式：产品命令在 .68 上 8 秒不落播放列表（2.2.23），而同一套参数布局在 `verify-screencast-audio-order.cmd` 里能跑，唯一差别就是 `pipe:0` 的字节从哪来 | 按通用投递方式，任务名 `HANCAST_FEED`，脚本落 `D:/Temp/hcfeed.cmd` | 仅 Windows（.68）交互桌面；结果看 `D:\Temp\hcfeed-out.txt`：`D_*` 是空 stdin 下播放列表是否出现，`E_*` 是实时涓流基线；这里 loglevel 用 `info` 而非 `error`，好让 stall 原因可见 |
| `verify-win-screencast-audio-live.cmd` | Windows 屏幕投屏音频的**产品链路**验收：按真实 stdin/stdout JSON 协议驱动**已发布的** sidecar，让 ffmpeg 命令行由产品代码（`screencast/capture.py`）自己拼——手写 ffmpeg 命令只能证明某种参数布局，这条证明的是应用真正吐出来的那一份 | 按通用投递方式，任务名 `HANCAST_HCSL`，脚本落 `D:/Temp/hcsl.cmd` | 仅 Windows（.68）交互桌面；打包版 sidecar 在 `C:\Users\Administrator\AppData\Local\HanCast`；请求文件由 `gen-win-screencast-live-req.py` 生成（纯 ASCII）；结果看 `D:\Temp\hcsl\out.txt`：三轮（3/6/9 = 麦克风+系统声音 / 只系统声音 / 只麦克风）每个 start `"ok":true`、每个 status `"running":true`、`SIDECAR_EXIT=0`（停止路径无 `0xc0000005`）、无 `Option vf cannot be applied to input url`；`D:\Temp\hcsl\seg.txt` 是每轮运行**中途**抓的分片快照；收尾 `schtasks /delete /tn HANCAST_HCSL /f` + `rmdir /s /q D:\Temp\hcsl` |
| `gen-win-screencast-live-req.py` | 生成上面那条验收的请求行：把中文设备名转成纯 ASCII（`json.dumps(ensure_ascii)` 再把 `( ) & < > \| %` 也转成 `\uXXXX`），sidecar 的 `json.loads` 照样还原，批处理里一个特殊字符都不剩 | `python3 tools/gen-win-screencast-live-req.py phase1 -o req.txt`（只枚举设备，第一轮跑这个拿设备名）<br>`… devices <out.txt>`（列出 phase-1 响应里的音频输入设备）<br>`… req <out.txt> --mic I [--sys J] [--second mic\|system]… [-o req.txt]`（按序号取设备名拼请求文件，`--second` 可重复以把多路组合排进一次运行） | 任意平台（只是生成文本）；配套 `verify-win-screencast-audio-live.cmd`；里面的 ffmpeg 路径硬编码为 .68 的 `C:\Users\Administrator\AppData\Local\HanCast\ffmpeg\ffmpeg.exe` |
| `verify-win-screencast-dwell.sh` | 跑批的同时从本机拉产品自己的 HTTP 通道 `http://<host>:8765/hls/<session>/live.m3u8`，按秒记录最大分片号。协议里的 `screen_cast_status` 只说「进程还在」，说不出「画面还在往前走」；喂料线程/环流饿死时进程可以活着但分片不再增长 | `tools/verify-win-screencast-dwell.sh [host] [时长秒]`（默认 `192.168.1.68` / 60 秒） | 从 macOS/Linux 跑；能 `ssh administrator@<host>`；.68 上正有一路屏幕投屏在跑；`curl` |
| `verify-win-screencast-tone-content.sh` | 内容级判据：start 返回 ok、status 返回 running、退出码 0 全都只说明「FFmpeg 还活着」，声音到底有没有进分片只能把分片拿回来量。让 .68 循环播「440Hz 与 3500Hz 同时发声」的提示音，用产品协议起一路投屏，从产品 HTTP 通道拉回两段中途分片，用打包内同一份 ffmpeg 做 420–460Hz / 3300–3700Hz 带通 + `volumedetect` | `tools/verify-win-screencast-tone-content.sh [phase-1输出] [系统声音名字正则] [麦克风名字正则]`<br>环境变量 `HOST`（默认 `192.168.1.68`）<br>特殊值 `MIC=off`（只起「单独系统声音」那一路，判据最干净）、`TONE=off`（静音对照，量同一台机器同一副麦克风同一路投屏的地板） | 从 macOS/Linux 跑；能 ssh 到 .68；默认**自适应选设备**：系统声音 = 枚举里标着「当前出声」(default) 的那块环回，麦克风 = `麦克风 .*Realtek`；不给 phase-1 输出就自己跑一轮 `list_screen_sources` 枚举。判据：3.5kHz 带通有能量 → 提示音真被混进输出；3.5kHz = −91dB（数字静音）→ 输出里一点提示音都没有。为什么是 3.5kHz：单频 440Hz 判不准，风扇与麦克风底噪的能量就堆在低频（静音对照的 420–460Hz 带通已有 −35dB，跟放音时只差 3dB），3.5kHz 麦克风给不出来 |
| `verify-dshow-parse-offline.py` | 离线回放 Windows dshow 设备枚举，验 `_list_audio_devices` 的解析。判据是「认出的音频设备集合 == 原始文本里带 `(audio)` 后缀的行」，且 `Alternative name` 行一条都不能混进来（它同样带引号，早期只按分段头切段的实现会把它当设备） | `tools/verify-dshow-parse-offline.py <ffmpeg -list_devices 抓到的原始输出文件>` | **不需要 Windows**（设计上就是在 Mac 上跑）；后端源码可 import（`sys.path` 插入 `hancast-backend`）；输入文件需事先从 .68 抓来。为什么离线：.68 的麦克风名是中文、设备列表随插拔变化，UI 里点一遍既慢又不可复现 |

### 1.5 解析与规则包

| 脚本 | 一句话用途 | 运行方式 | 前置环境 |
|------|-----------|---------|---------|
| `verify-rule-packs-live.py` | 用源码 sidecar 的真实解析管线跑规则包，打印每条链接是哪条规则命中的。不点 GUI、不起整个应用：只把 `ShareResolver` 拎出来配一个假媒体服务器（注册代理只记地址、不落流） | `hancast-backend/.venv/bin/python tools/verify-rule-packs-live.py <链接或口令> …`<br>`… --rules-dir <dir>`（用指定规则目录，内含 `plugins/` 与 `*.json`）<br>`… --no-packs`（不装官方包，只跑 `<dir>` 里已有的规则）<br>`… --keep`（保留临时目录，方便看插件缓存/落盘文件） | 后端 venv；默认把仓库根 `rule-packs/dist/` 的包当**用户导入的 zip** 装进临时目录，装前先比 `index.json` 的 sha256；需要外网访问站点。判据看输出的 `via`：`<包名>/<规则 id>` = 走了 JS 规则包，`<规则名>` 不带斜杠 = 声明式 JSON 规则。程序里没有编译进去的站点规则，所以「没装包 ⇒ 什么站点都解析不了」是**预期行为不是 bug**。证明的是「规则能不能取出可播地址」，不含电视端真实播放 |
| `share_resolve_probe.py` | 粘贴投屏解析现场复现器：把一段分享文案喂给真实解析管线，并验证播放器能不能解出画面 | `python tools/share_resolve_probe.py "<粘贴的链接或口令文案>"`<br>`… --play "<url>"`（解析完用 ffmpeg 实解一段）<br>`… --mpv "<url>"`（用打包版 mpv 实解）<br>`… --serve "<url>"`（起真实媒体代理）<br>`… --log "<url>"`（只看决策日志）<br>`… --rules-dir /tmp/r "<url>"`<br>`… --mpv --segment "<分片/流地址>" --referer "<页面地址>"`（跳过解析，只验代理+播放器）<br>`… --keep-ledger "<url>"`（失败条目写进**用户真实台账**，默认不写） | 后端源码可 import；**默认替身代理不占端口、不起服务**；`--serve`/`--play` 会**真的起 MediaServer**（占端口、写临时文件），于是「伪装分片走本机代理」这类结论可以直接看到播放器视角的结果。**默认还接管解析失败台账**：解不出来时 `resolve()` 会记一条，而那份台账是设置页给用户看的真实失败清单——探针调试用的假地址混进去会把真实失败冲淡，所以不带 `--keep-ledger` 时把 `failures.SETTING_DIR` 指到临时目录、退出即删 |
| `verify-proxy-live.py` | 端到端验收「粘贴投屏」的代理链路：真起一个 MediaServer，把远端播放列表挂成本机代理，再用 `ffprobe` 冒充播放器逐片检查字节能不能解出画面/声音 | `tools/verify-proxy-live.py <m3u8_url> [--referer U] [--picks 0,300,699]` | 后端源码可 import；`ffprobe` 在 PATH；**会真起 MediaServer**（本机端口）；需外网。退出码 0 = 每个抽样分片都被识别为 `mpegts` 且有视频流 |
| `verify-proxy-segment-decodable.py` | 产品代理链路验收：给一条需要防盗链的 m3u8，看本机代理转出来的分片能不能真解码。判据是播放器视角的三件事——首字节是 TS 同步字节 `0x47` 且按步长游走时绝大多数是 188；转发时 `Content-Type` 不再是 `image/*`、`text/html`；打包版 ffmpeg 用**播放器的方式**（不指定 `-f`）探这段字节，解封装器不许是 `png_pipe`/`image2` 且要有真视频流 | `hancast-backend/.venv/bin/python3.12 tools/verify-proxy-segment-decodable.py <m3u8_url> --referer <页面或源站地址> [--ffmpeg <路径>]`（验工作树）<br>`… --root /Applications/HanCast.app/Contents/Resources --ffmpeg /Applications/HanCast.app/Contents/MacOS/ffmpeg/ffmpeg`（验**已装上的那份产物**）<br>`… --probe-only /path/to/skinned.ts`（反例自检：喂一段「皮没剥」的字节，工具必须判 FAIL） | 后端 venv；外网；**只读上游、只在本机 `127.0.0.1` 起一个临时端口，不碰任何产品配置**。退出码不算判据——带皮的字节 ffmpeg 自己会重同步，`-f mpegts -c copy` 照样 rc=0，拿它判等于没判 |
| `probe-hls-antileech.py` | 诊断 HLS 分片的防盗链/伪装行为：同一分片用不同请求头形状各拉一次首块，打印魔数、PNG 头长度、TS 同步偏移，用来决定代理该走哪套头、要不要剥皮 | `tools/probe-hls-antileech.py <m3u8_url> [--index N] [--cookie "k=v; k2=v2"]` | `requests`；外网；只读 |
| `probe-ts-skin-offset.py` | 定位「PNG 皮 + TS」的精确剥离偏移，并验证剥完能否被 ffmpeg 解封装 | `tools/probe-ts-skin-offset.py <m3u8_url> [--index N]` | `requests` + `ffmpeg`；外网；只读 |
| `probe-dyrs-headers.sh` | dyrs 采集站分片防盗链矩阵：同一分片换不同的头/Range/Cookie，看到底哪种给真 TS（要搞清它认的是页面 Referer、Set-Cookie 会话、`Sec-Fetch-*` 还是 Range） | `tools/probe-dyrs-headers.sh <m3u8_url> [页面 referer]` | `curl` + `/usr/bin/python3`；外网；在 `mktemp -d` 里写临时文件；全程 `--noproxy '*'` |
| `probe-dyrs-skin.sh` | 一次性复现：dyrs 采集站的分片到底是不是「披着 PNG 皮的 TS」，皮有多长（三种 Range 各拉一次，打头 32 字节 hex、是否 PNG、TS 同步偏移） | `tools/probe-dyrs-skin.sh <m3u8_url> [referer]` | 同上 |

> 关于代理：macOS 上 `requests` 会继承系统代理（`127.0.0.1:9565`），代理端口一失效解析就全挂（见 `docs/分享解析规则.md:156`）。需要显式走代理时用
> `HTTPS_PROXY=http://127.0.0.1:9565 python3 tools/<script>.py …`；
> `probe-dyrs-*.sh` 反向操作，用 `curl --noproxy '*'` 明确绕开代理。

### 1.6 后台参数与协议

| 脚本 | 一句话用途 | 运行方式 | 前置环境 |
|------|-----------|---------|---------|
| `verify-params-ui.mjs` | 设置页「后台参数」区块的浏览器验收：用真实 Chromium（headless shell + CDP）渲染，不靠快照推断布局。走 vite dev 的 mock 通道，只验 UI 行为（展开 / 三类控件 / 待保存计数 / 保存回落 / 恢复默认） | `node tools/verify-params-ui.mjs [base-url]`（默认 `http://localhost:5199`） | 先起 vite：`node node_modules/vite/bin/vite.js --port 5199`；`~/.cache/puppeteer/chrome-headless-shell` 下有 `mac_arm-*` 构建；CDP 端口 9337；截图落 `/tmp/hancast-params-ui-*.png`；退出码 0 = 全部断言通过。**后端契约不由它负责**——那是 `hancast-backend/tests/test_params*.py` 与 `tools/accept-macos-artifact.sh` 的活 |
| `verify-sidecar-utf8-protocol.py` | 验证 sidecar 的 stdin/stdout 协议在「父进程没有 locale」时仍走得通。登记表/错误文案全是中文，而 stdout 就是与 Rust 的通信通道；父进程没给 `LANG`/`LC_*` 时 Python 的 stdout 会退回 ASCII，`emit_response(..., ensure_ascii=False)` 直接抛 `UnicodeEncodeError`，表现为「命令永远失败」而不是「编码不对」 | `python3 tools/verify-sidecar-utf8-protocol.py [python 二进制]`（缺省用 `sys.executable`；可指向打包版 sidecar） | 后端源码可 import；无需外网、无需真机。判据：hostile env（`LC_ALL=C` 等）下 `get_params` 必须成功、条数不少于 18、中文标签原样回来；`stop_screen_cast`（空载）这种带中文提示的响应也必须回来。v2.2.18 用打包版实测撞到过 `{"id":2,"success":false,"error":"'ascii' codec can't encode characters …"}` |

### 1.7 反爬：TLS 指纹与浏览器通道

| 脚本 | 一句话用途 | 运行方式 | 前置环境 |
|------|-----------|---------|---------|
| `probe-tls-ja3.py` | **源码级** TLS 指纹取证：本机起一个只读 ClientHello 的裸 TLS 服务（不做握手、不需要证书、不出网），逐个客户端握手后算 JA3 并标出 GREASE。`--client hancast` 走 `utils/httpclient.make_session()`，证明的是**接线**而不是库能用 | `hancast-backend/.venv/bin/python tools/probe-tls-ja3.py`（四个客户端对照跑）<br>`… --client hancast`（单探针，只报本次指纹）<br>`… --target chrome124`（impersonate 目标）<br>`… --ua "…Chrome/124…"` | 后端 venv（那里装了 `curl_cffi`）；只在 `127.0.0.1` 起临时端口；无需外网。判据只认 GREASE（`0x?a?a`）：OpenSSL/SecureTransport 发不出，Chrome 系必发。**别拿「与裸 ssl 基线不同」当生效证据**——urllib3 v2 自己会加 ALPN `http/1.1`，两者 JA3 本来就不逐字相同 |
| `probe-installed-ja3.py` | **产物级**同款取证：起 `.app` 里那个冻结 sidecar，喂一条真实 `resolve_share`（地址带一次性标记），看它自己发出去的 ClientHello。存在的理由：v2.3.7 接线是对的、`bundle.resources` 白名单漏了 `curl_cffi/`，import 失败按设计静默降级回 requests——**只有装好的包才暴露得出来** | `python3 tools/probe-installed-ja3.py`（默认 `/Applications/HanCast.app/Contents/MacOS/hancast-sidecar`）<br>`… --binary <sidecar 路径>`<br>`… --timeout 60` | 仅 macOS 打包产物；不需要外网、不需要 GUI 起着、不锁屏也能跑。同一份 JA3 解析代码由 `importlib` 直接复用 `probe-tls-ja3.py`，两套判据不会走偏。⚠️ 会**写用户的解析失败台账**（收尾用 `failure_delete{url}` 只删自己那一条，绝不调 `failure_clear`） |
| `browser-channel-livecheck/`（`run.sh` + `serve.py` + `pack/`） | 浏览器通道（`host.render` / `host.sniff` / WebView Cookie）真机验收包：把测试包装进应用的规则目录 → 起一个假站点（播放地址**只由页面 JS 现挂**、并下发一枚 HttpOnly Cookie，HTTP 抓页永远看不到）→ 等人工把地址粘进主界面点「粘贴」→ 从应用日志读四条判据 | `bash tools/browser-channel-livecheck/run.sh [--port 8099] [--host 192.168.1.7] [--wait 180] [--no-serve] [--keep]` | 仅 macOS/Linux；应用启动过（规则目录得存在）；**必须人工粘贴**（`resolve_share` 只是 sidecar 命令，DLNA 入站不走解析链）。四条判据：`sniff=.*master\.m3u8`、`httponly=1`、`uaMatchesHost=true`、`render title=HanCast 浏览器通道验收页`，全中才 `exit 0`。细节见 [docs/反爬实现取证与浏览器通道.md](../docs/反爬实现取证与浏览器通道.md) |

---

## 2. 使用纪律

### 2.1 会真的动设备 / 真的写文件的脚本

跑之前先确认目标不是真实路径、不是用户正在用的设备。

| 脚本 | 动了什么 | 安全措施 |
|------|---------|---------|
| `cleanup-macos-aggregates.py` | **删除系统级 CoreAudio 聚合设备**（「音频 MIDI 设置」里可见） | 默认只打印计划，必须 `--apply` 才真删；`--product` 才会连产品设备「HanCast 系统声音」一起删，而正在跑的 sidecar 会拒绝，避免把用户的投屏采集清掉 |
| `probe-macos-audio-pitch.py` | **临时修改系统默认音频输出**（改成 BlackHole 2ch） | 结束（含异常路径）一定还原并复核；改动只在这台机器的音频路由上，不写任何文件 |
| `probe-macos-systap.py` / `run-macos-tap-probe.sh` | 在系统里**创建**聚合设备与 process tap | 脚本自己 `destroy_device()`；进程异常退出会留残留，用 `cleanup-macos-aggregates.py` 收 |
| `install-macos-artifact.sh` | **覆写 `/Applications/HanCast.app`** | 先校验 dmg 内版本 == 要求的版本，不符即 FAIL 退出；不提权（当前用户在 `admin` 组即可）；不 `pkill` 旧 sidecar（会留下没有子进程的空壳 GUI），只 `osascript quit` 后等它自己退 |
| `pull-rule-packs-installed.py` | **写用户插件目录** `<share_rules>/plugins/`，并**退出/重启 GUI** | 先干净退出 GUI，否则会同时跑两个 sidecar，1900 端口与媒体服务互相干扰、验收日志被上一轮假象污染；`--no-relaunch` 可只拉不重启 |
| `verify-browser-segment-live.py` / `verify-watch-page-live.mjs` | **整体退出 HanCast**（抢 8080），并**写 `hancast_setting.json`** 覆盖后台参数 | 脚本结束把应用开回来；`verify-browser-segment-live.py` 最后把参数改回默认，且覆盖项应当被**清掉**，不是留一个 `== 默认值` 的脏键 |
| `verify-dlna-stop-chain.py` | 向局域网真实 renderer **推流并下发停止** | 要求先整体退出 HanCast；测试媒体固定在 `/tmp/hc_media/test.mp4`（不是用户路径） |
| `verify-ssdp-offline-eviction.py` | 在本机**起一台假 MediaRenderer**并监听端口 | LOCATION 故意写 `127.0.0.1`；替身端口可指定（`--port`） |
| `share_resolve_probe.py --serve/--play`、`verify-proxy-live.py` | **真起 MediaServer**，占本机端口、写临时文件 | 默认模式（不带 `--serve`/`--play`）用替身代理，不占端口不起服务 |
| `share_resolve_probe.py`（解析失败时） | 往**用户解析失败台账** `parse_failures.json` 追加一条 | 默认把 `failures.SETTING_DIR` 重定向到临时目录并在退出时删除，真实台账一个字节都不动；只有显式 `--keep-ledger` 才写用户数据。pytest 同理由 `tests/conftest.py` 的 session fixture 隔离，测试用的假地址不得出现在用户设置页 |
| `verify-proxy-segment-decodable.py` | 在 `127.0.0.1` 起一个临时端口 | 明确声明「只读上游、不碰任何产品配置」 |
| `probe-installed-ja3.py` | 起**打包版 sidecar** 进程并向用户的配置目录**写一条解析失败台账** | 只在 `127.0.0.1` 起取证端口；地址带一次性标记 `hancast-ja3-probe`，收尾用 `failure_delete{url}` 精确删自己那一条（绝不调 `failure_clear`，那是用户的数据）；`close()` 只收尾自己 spawn 的那个 pid——**绝不 `pkill` sidecar**，那会杀掉正在投屏的实例 |
| `browser-channel-livecheck/run.sh` | 往应用的**用户规则目录**装一个测试包，并起 HTTP 服务 | `trap cleanup EXIT` 只删本次自己建的那一个目录（`--keep` 才留）；服务是脚本自己起的子进程，退出即 `kill` |
| `rust-check.sh` | 在 `src-tauri/` 下**创建桩文件与目录** | `stub()` 遇已存在的路径直接跳过（打印「已存在，跳过」）；`trap cleanup EXIT` 逆序回收，先删文件再 `rmdir -p` 空目录，绝不动仓库里原有的东西 |
| `build-rule-packs.py`（无 `--check`） | **写 `rule-packs/dist/*.zip` 与 `rule-packs/index.json`** | `--check` 是只读模式；`--clean` 才删文件，且只删「源目录已不存在」的孤儿 zip |
| Windows `.cmd` 全家 | 在 .68 上**创建计划任务**、写 `D:\Temp\*`、起真实屏幕捕获 | 每个脚本头部都写了收尾命令（`schtasks /delete /tn <TASK> /f` + `rmdir /s /q <dir>`）；`gen-win-screencast-live-req.py` 里的 ffmpeg 路径与输出目录都是 .68 专属硬编码 |

### 2.2 必须用打包产物、不能用源码冒充的脚本

「产物验收」的定义就是**对打包结果而非源码的验证**。下面这些脚本的判据只有在产物上才成立：

- `accept-macos-artifact.sh` — 读的是 `/Applications/HanCast.app/Contents/Resources/hancast_sidecar/`（`:8`），并把里面的源文件与 `git show <tag>` 逐文件比 sha256（`:34-42`）。跑源码等于没跑。
- `install-macos-artifact.sh` — 输入就是 dmg。
- `pull-rule-packs-installed.py` — 存在的理由原话：「手改 `share_rules/plugins/` 里的文件则是拿补丁冒充验收，产物里根本没跑过 `plugin_pull`」（`:5-8`）。
- `verify-browser-segment-live.py`、`verify-watch-page-live.mjs`、`verify-dlna-stop-chain.py` — 都硬编码 `/Applications/HanCast.app/...` 下的 sidecar。
- `verify-win-screencast-audio-live.cmd` — 驱动 `C:\Users\Administrator\AppData\Local\HanCast` 下**已发布的** sidecar；docstring 明写「a hand-written ffmpeg line can only prove an argument layout; this proves the layout the app actually emits」。
- `verify-proxy-segment-decodable.py` — 提供 `--root` / `--ffmpeg` 两个开关专门指向已装产物，注释写着「禁止拿工作树冒充验收」。
- `verify-ssdp-offline-eviction.py` — 默认读打包版日志。
- `probe-installed-ja3.py` — 跑的是 `.app` 里的冻结 sidecar（`DEFAULT_BIN`，`:47`）。它存在的理由就是源码级探针测不到这一层：`probe-tls-ja3.py --client hancast` 走 `sys.path` 里的源码，只能证明接线对，v2.3.7 恰恰是接线对、白名单漏包。
- `verify-windows-artifact.sh` — 读的是 .68 安装目录里那份 `.py` 与二进制，与 `git show v<tag>` 逐文件比哈希；本地工作树跟它无关。

反过来，这些是**源码级/离线**的，不需要产物：`verify-dlna-volume-chain.py`、`verify-dshow-parse-offline.py`、`verify-sidecar-utf8-protocol.py`、`verify-params-ui.mjs`、`probe-hls-antileech.py`、`probe-ts-skin-offset.py`、`probe-dyrs-*.sh`、`ssdp_probe.py`、`dump-cgwindowlist.py`、`rust-check.sh`、`build-rule-packs.py`。

### 2.3 必须在 .68 Windows 测试机上跑的脚本

`192.168.1.68`（脚本里简称 `.68`）：

- **必须在 .68 的交互桌面会话里跑**（gdigrab 抓不到 session 0，所以必须经 `schtasks … /it` 投递）：`verify-screencast-audio-order.cmd`、`verify-win-loopback-feed.cmd`、`verify-win-screencast-audio-live.cmd`、`probe-win-window-picker.py`
- **必须在 .68 上跑但不需要桌面**：`probe-wasapi-loopback.py`、`probe-win-audio-pipe.py`（后者显式避开 gdigrab，视频输入用 `lavfi testsrc` 顶替）、`verify-screencast-audio-order.ps1`（读已产出的 m3u8）
- **从 macOS/Linux 远程打 .68**：`verify-windows-artifact.sh`（产物验收）、`verify-win-screencast-dwell.sh`、`verify-win-screencast-tone-content.sh`（两者 `HOST` 默认 `192.168.1.68`，走 ssh + 产品的 `http://<host>:8765/hls/<session>/` 通道）
- **不需要 .68**：`verify-dshow-parse-offline.py`（离线回放 .68 抓来的文本，设计上就是在 Mac 上跑）、`gen-win-screencast-live-req.py`（只生成 ASCII 请求文本）
- `verify-dlna-stop-chain.py` 需要局域网里有一台在跑的 Windows renderer，默认认 `display_name` 含 `AMD-YES` 或 IP `192.168.1.68`

### 2.4 解释器选择

| 需求 | 用什么 |
|------|--------|
| 只 import `hancast_sidecar` 且用到 `requests`/`lxml` | `hancast-backend/.venv/bin/python`（系统 `python3` 没有这些依赖——`verify-dlna-volume-chain.py` 的 docstring 明写这条） |
| 纯标准库 / 只用 `requests` | `python3` |
| Node 脚本（CDP 驱动 Chromium） | `node`（`verify-params-ui.mjs`、`verify-watch-page-live.mjs`） |
| `.mjs` 的 Chrome 定位 | 两个脚本都从 `~/.cache/puppeteer/chrome-headless-shell/mac_arm-*/` 现找，不写死版本号 |

---

## 3. 新增脚本的约定

### 3.1 命名前缀的语义差别

| 前缀 | 语义 | 硬性要求 | 例子 |
|------|------|---------|------|
| `verify-` | **验收**：有明确判据，能回答「这版对不对」 | 必须逐项打 `PASS`/`FAIL`，全部通过才 `exit 0`；判据要有力气——必须能通过「反例自检」（喂坏输入时它得判 FAIL），否则等于没判 | `verify-proxy-segment-decodable.py`（docstring 里专门给了 `--probe-only` 反例自检用法）、`verify-dlna-volume-chain.py`（「判据全部 PASS 才退出 0」） |
| `probe-` | **探针 / 现场取证**：回答「现场到底是什么样」，为决策提供观测值 | 打印观测数据与解释，不假设有唯一正确答案；可以有软判据（如能量是否显著高于基线），但主要产出是数据 | `probe-hls-antileech.py`（「用来决定代理该走哪套头、要不要剥皮」）、`probe-win-window-picker.py --survey`（「把每个窗口的 style/exstyle/矩形全打出来，判据从实测数据里挑，不靠猜」） |
| `dump-` | **原样倾倒**：不做任何判断，只把原始结构打出来 | 必须复刻被测代码的**同一条读取路径**，否则差异说明不了问题 | `dump-cgwindowlist.py`（故意不用 pyobjc，因为打包 sidecar 走的是 ctypes + CoreFoundation） |

其它动词前缀是工具类，不属判据脚本：`accept-`（产物验收总入口）、`install-`、`cleanup-`、`gen-`（生成别的脚本的输入）、`run-`（把某脚本包进必要的环境再跑）、`build-`、`rust-check`。

历史遗留：`ssdp_probe.py` 与 `share_resolve_probe.py` 用后缀 `_probe` + snake_case，是早期命名。新增一律用 `probe-` 前缀 + kebab-case。

### 3.2 自适配要求

**别写死机器上会变的东西。** 三条已付过学费的具体要求：

1. **别写死设备名。** `verify-win-screencast-tone-content.sh:3-8` 记着这条：系统声音不再按名字硬挑，因为「装了虚拟音效设备（FxSound 等）的机器上，默认渲染端点根本不是 Realtek，按 `Realtek.*Loopback` 挑中的是静音镜像，分片里只剩补出来的数字静音，会被读成『系统声音没采到』」。现在默认认枚举里标着「当前出声」(`default`) 的那块环回（提交 `bfe127a`）。名字只作为**可覆盖的默认值**暴露成位置参数/环境变量。
2. **别写死条数。** 提交 `9787f2b`（`fix(tools): 参数表验收改为与 tag 源码逐键比对，不再写死条数`）的原话：写死 `"18"` 的断言在加了 `rules.index_url`（第 19 条）之后必然 FAIL，而**这条 FAIL 只证明脚本过期，证明不了产物有问题**——每轮加参数都得同步改脚本。改法是从**被测 tag 的 git 树现读基准**（`git show <tag>:hancast-backend/hancast_sidecar/params.py`），逐 key 逐 default 比对，这样能同时抓住产物**多带 / 漏带 / 带旧值**三种情况（四种变异体验证过）。实现见 `accept-macos-artifact.sh:108-121`。
   - 顺带一个实现坑，同处 `:111` 注释：临时文件**必须叫 `params.py`**——importlib 按后缀选 loader，无后缀的 `mktemp` 文件会静默读不出表。
   - 下界断言（`len(specs) < 18` 这种「不少于」）不会因新增参数而假红，可以接受，但 docstring 里的数字要跟着更新——`verify-sidecar-utf8-protocol.py:11` 写的「18 项」已滞后于当前的 27 条（`params.py:35-209`），`:81` 的实际断言仍是 `< 18` 下界所以没坏。
3. **别写死路径/版本号当唯一入口。** 产物路径要做成可覆盖参数（`pull-rule-packs-installed.py --app`、`verify-proxy-segment-decodable.py --root/--ffmpeg`、`verify-ssdp-offline-eviction.py --log`）；Chrome 从 `~/.cache/puppeteer/` 现找而非钉版本；triple 用 `rustc -vV` 现取（`rust-check.sh:38`）。

### 3.3 其它约定

- **一次性脚本不进 `tools/`。** 真机取证的临时脚本放 `.tmp-win/`，该目录已被 `.gitignore:106` 忽略（注释：「真机取证脚本（本地临时，不入库）」）。要沉淀成可重复判据时才搬进 `tools/`——`install-macos-artifact.sh` 就是「固化替代每轮 `/tmp` 一次性脚本」的产物（提交 `e1ee651`）。
- **注释里写清「为什么是这个判据」。** 现有脚本的头部注释普遍记录了三件事：症状（用户看到什么）、根因（哪一层错了）、以及为什么这条判据能区分二者。`verify-win-screencast-tone-content.sh:19-25` 解释「为什么是 3.5kHz 而不是 440Hz」、`verify-proxy-segment-decodable.py:14-16` 解释「为什么退出码不算判据」，都是范例。
- **协议级判据优先于 UI 级。** `accept-macos-artifact.sh:3` 的「全程 CLI，不碰 UI 坐标」是硬要求：UI 坐标随版本漂移，协议与文件字节不会。
- **别拿本地补丁冒充验收。** 提交 `76bba31`（`chore(tools): 打包产物验收脚本，禁止手工补丁冒充验收`）。v2.2.13 就是靠这套检查才发现「plist 齐全但组播仍被拒」，而当时的**本地重签把现场掩盖了**。
- **`.cmd` 保持 ASCII-only，投递用 CRLF。** 见 §1.4 开头。
- **跑完要能收场。** 起替身/占端口/建计划任务/改系统音频路由的脚本，必须在头部注释里写出收尾命令，或像 `rust-check.sh` 那样用 `trap … EXIT` 自动回收。

---

## 4. 相关文档

- [docs/构建与发布.md](../docs/构建与发布.md) — 版本号 SSOT、三平台构建、CI 逐步骤、发布 runbook、产物验收判据在流程里的位置
- [rule-packs/README.md](../rule-packs/README.md) — 官方规则包的目录结构、`index.json` schema、打包与发布
- [docs/解析插件开发指南.md](../docs/解析插件开发指南.md) — 规则包作者视角：`manifest.json` 字段、`host.*` API、沙箱边界、打包约束
- [docs/分享解析规则.md](../docs/分享解析规则.md) — 声明式 JSON 站点规则
- [docs/HanCast-Backend-Spec.md](../docs/HanCast-Backend-Spec.md) — Sidecar 模块与协议规格
