# HanCast — `tools/` 验收与探针脚本索引

> 这份文档回答：`tools/` 下这些脚本各自验什么、怎么跑、需要什么环境，以及新增脚本要守哪些约定。
>
> 最后校对：2026-10-29，对应版本 v2.4.2（分类表按当前工作区重数；新增 §1.8 文档体检）

`tools/` 里放的是**可重复运行的验收 / 探针脚本**，不是一次性的 `/tmp` 手搓脚本。它们必须自适应当前机器——设备名、网卡、产物路径、参数条数都从现场现读，不写死。用法上的两条铁律：**修 bug 之前先跑它复现，修完之后再用它证明**。「证明」的对象是判据（退出码 / PASS-FAIL 行），不是截图，也不是「我点了一下看起来对」。

当前分类表收录 **56 个顶层脚本 + 1 个目录型验收包**（`browser-channel-livecheck/`，4 个文件合写成 §1.7 的一行）。口径为当前工作区（含 v2.3.23 新加的 `check-doc-freshness.py`）：顶层 `.py/.sh/.cmd/.ps1/.mjs/.cjs` 共 56 个，全部进分类表；两份文档体检脚本单独立在 §1.8，用法与放行口径见 [docs/README.md](../docs/README.md) §4。历史 v2.3.19 已提交快照为 50 个分类脚本，`git ls-files` 在新增脚本提交前不会包含它们；目录和 `__pycache__/` 不计入脚本数。

---

## 1. 分类索引

### 1.1 产物与安装

对**打包结果**而非源码做验证，是发布链路的闸门。

| 脚本 | 一句话用途 | 运行方式 | 前置环境 |
|------|-----------|---------|---------|
| `accept-macos-artifact.sh` | macOS 产物验收总入口：版本、Info.plist 三条声明、签名身份、产物内文件 sha256 对齐 tag、参数表逐键比对、SSDP 心跳通道（窗口内出现过 multicast 即通过并打印 multicast/broadcast 分布——本机 5 块网卡会让每轮通道轮换，只取最后一行等于抛硬币）、跨机入站、JS 插件宿主、窗口/音频契约、**FFmpeg 去捆绑与检测链**（`.app` 内 `ffmpeg`/`ffprobe` 零命中；一次性 `HOME` + 干净 PATH 起打包 sidecar 跑完「检出系统那份 → 真下载 → 复检归位 → 手填优先」四步），逐项打 PASS/FAIL | `tools/accept-macos-artifact.sh <version>`（例 `tools/accept-macos-artifact.sh 2.3.5`）<br>下载那一步要线路配合：`HC_FFMPEG_PROXY=http://127.0.0.1:9565 tools/accept-macos-artifact.sh <version>` | 仅 macOS；`/Applications/HanCast.app` 已装 CI 产物；仓库里能 `git show v<version>`（tag 已 fetch）；局域网可达；连不上下载源判 **SKIP**（环境限制）而不是 FAIL，但失败原文照进载荷那条仍算 PASS |
| `install-macos-artifact.sh` | 把 CI 的 dmg 原样装进 `/Applications`，不做任何补丁；装完直接接 `accept-macos-artifact.sh` | `tools/install-macos-artifact.sh <version> <dmg 路径>` | 仅 macOS；dmg 产物；当前用户在 `admin` 组（全程不提权，`:8-11`）；旧 sidecar 必须已自行退出（脚本只等不 `pkill`，`:31-34`） |
| `verify-windows-artifact.sh` | 验收装在 .68 上的 CI 产物**本身**：注册表 `DisplayVersion` == 要求版本；安装目录里那份源码级 `.py` 逐个与 `git show v<tag>` 比对（两边都 `tr -d '\r'`，Windows 包全是 CRLF，直接 certutil 比 tag 哈希会把装对的版本判成装错）；curl_cffi 三件套与主程序里的离屏窗口标签单独扫一遍，tag 里没这项就 SKIP 而不是假 FAIL | `tools/verify-windows-artifact.sh <version>`<br>`… <version> params.py utils/httpclient.py`（只比对指定文件） | 仅 macOS 侧发起；能 `ssh administrator@192.168.1.68`；本地有 `v<version>` tag；不碰 UI、不打补丁。任一项 FAIL = 这版产物在他手上不可信，别继续让他测 |
| `verify-linux-window-opacity.sh` | Linux(Wayland + Hyprland) 窗口不透明判据：光标蛇形扫过整个客户区后，抓一张窗口图、杀掉 GUI 再抓同一矩形当裸桌面，逐行比 RGB 平均绝对差，**与桌面一致的行 = 那块 surface 从没提交过（透明洞）**。v2.3.18 的「鼠标晃一晃出现透明洞」就是它复现/证明的（机制见 [docs/故障排查.md](../docs/故障排查.md) §8.4） | `tools/verify-linux-window-opacity.sh`（默认 3 轮、上限 0%）<br>`… --sweeps 5`<br>`… --max-pct 2`<br>`… --workspace auto`（切到一个空 workspace 再量）<br>`… --no-relaunch`<br>`HC_BIN=/usr/bin/HanCast HC_CLASS=HanCast THRESHOLD=6.0 …` | **只能在装了 Hyprland 的 Linux 真机上跑**（CachyOS .68:10086）；`hyprctl`/`grim`/`ffmpeg`/`python3`；要**真的启动并杀掉 GUI**、并切换工作区（结束还回原工作区），跑前先确认这台机器空闲、没有投屏在跑；背景必须是裸桌面——同一 workspace 上有别的窗口压住矩形时脚本 exit 2，而不是拿它当背景量出假 PASS；启动前会先 `kill -TERM` 已在跑的实例（Tauri single-instance 只让新进程去聚焦旧窗口，不杀就会量到旧窗口所在的工作区）；退出码 0=PASS / 1=有洞 / 2=环境不满足 |
| `verify-linux-wayland-cast.py` | **Linux/Wayland 屏幕投屏验收（v2.3.19）**：按产品代码路径（`FfmpegCapture.start_hls`）真起一次会话，逐条断言 helper 可用 → `wl:<output>` 单屏条目在列 → rawvideo 命令形状 → 双路 pulse + `amix` → **切出来的分片不是黑的**（`signalstats` 亮度）→ 每片都带 aac 流 → 停止后无残留 `hancast-wlgrab` 进程、`running` 归位。带 `--with-tone` 时再证 `<sink>.monitor` 抓到的确实是系统在放的声音（真机：monitor RMS −16.3 dB，空闲 HDMI monitor −91 dB）。它存在的理由是 v2.3.18 那条**无声故障**：x11grab 在 Hyprland 上投得起来、帧率正常、每帧纯黑、日志一个字都不提（机制见 [docs/故障排查.md](../docs/故障排查.md) §6.7，产物层判据见 [docs/构建与发布.md](../docs/构建与发布.md) §6.8） | `python3 tools/verify-linux-wayland-cast.py`<br>`… --output DP-2`<br>`… --seconds 8`<br>`… --width 1280 --height 720`<br>`… --with-tone`<br>`HANCAST_WLGRAB=/path/to/hancast-wlgrab …` | 需要**真的 Wayland 会话**（`$WAYLAND_DISPLAY`）+ 本机 `ffmpeg` 与 `pactl`，即 CachyOS `.68:10086` 那台；helper 缺省按打包/开发布局自动找。退出码 0=全 PASS / 1=有 FAIL / 2=环境不满足（环境不满足不算产品问题）。⚠️ 亮度判据不能量 `seg000.ts`：产品参数 `delete_segments` + `hls_list_size=6` 早把它裁了，脚本是会话存活期间边跑边抄当下完整分片（`sweep()`） |
| `pull-rule-packs-installed.py` | 驱动**打包版** sidecar 走 `plugin_check_updates` → `plugin_pull`，证明已装的规则包真能升到官方索引版本 | `python3 tools/pull-rule-packs-installed.py`<br>`… --only hancast-douyin`<br>`… --no-relaunch`<br>`… --app /Applications/HanCast.app` | 仅 macOS（默认 `APP = /Applications/HanCast.app`，`:38`）；打包产物；能访问 `raw.githubusercontent.com`，走系统代理时带 `HTTPS_PROXY`；脚本会**整体退出并重启 GUI** |
| `cleanup-macos-aggregates.py` | 清掉探针残留在系统里的 HanCast CoreAudio 聚合设备（默认只打印计划） | `hancast-backend/.venv/bin/python tools/cleanup-macos-aggregates.py`<br>`… --apply`<br>`… --apply --product` | 仅 macOS；后端 venv（import `hancast_sidecar.screencast.macos_tap`） |
| `rust-check.sh` | 本地 `cargo check` 的前置：按 Tauri 配置建一次性空桩，让资源 glob 不落空；check 完只删自己建的文件 | `tools/rust-check.sh`<br>`tools/rust-check.sh --release`（参数透传给 `cargo check`） | 本地开发机；`rustc`/`cargo`；会**在 `src-tauri/` 下临时写文件**（`trap cleanup EXIT` 回收，已存在的路径绝不动） |
| `build-rule-packs.py` | 官方规则包的生产工具：`rule-packs/<name>/` → `rule-packs/dist/<name>-<version>.zip` + `index.json` | `python3 tools/build-rule-packs.py`（重建）<br>`… --check`（只校验，CI/验收用）<br>`… --clean`（删孤儿 zip） | 任意平台，纯 Python 标准库；详见 [rule-packs/README.md](../rule-packs/README.md) |

### 1.2 DLNA / SSDP 链路

| 脚本 | 一句话用途 | 运行方式 | 前置环境 |
|------|-----------|---------|---------|
| `ssdp_probe.py` | SSDP 现场探针：设备「发现不到」时先看网络上到底有什么。每块网卡各开一个组播成员，能看出「某块网卡收不到组播」这类单向发现问题 | `python3 tools/ssdp_probe.py`（全部网卡，听 20 秒并主动搜一轮）<br>`… --seconds 60`<br>`… --st MediaRenderer --search-only` | 真机 + 局域网；与 HanCast 同网段；会**向 239.255.255.250:1900 发组播** |
| `verify-ssdp-offline-eviction.py` | 静默下线的设备必须被探活摘掉：起一台假 MediaRenderer 替身，`SIGKILL` 掉它（发不出 byebye），等 sidecar 连探两次无应答后在日志里摘除 | `python3 tools/verify-ssdp-offline-eviction.py`（用打包版日志）<br>`… --log /path/to/hancast.log`<br>`… --serve --port 49217`（只当替身，内部用） | 默认读**打包版**日志（macOS 路径）；替身监听 `127.0.0.1`，LOCATION 故意写 127.0.0.1 以绕过 `_is_own_location` 自通告回环过滤；等待时长受后台参数 `discovery.device_stale_s`（默认 90s）影响 |
| `verify-ssdp-cast-gate.py` | 投屏目标准入（v2.3.13 的真机验收）：同时起两台替身——声明 AVTransport 的「电视」必须进列表，不提供该服务的「路由器」（MediaServer 描述 + XML 注释）必须被挡在门外并在日志留下「不列入投屏目标」理由 | `hancast-backend/.venv/bin/python tools/verify-ssdp-cast-gate.py --source --timeout 90`（就地跑**仓库源码**的发现链路，打 tag 前用；需要 1900 空闲）<br>`python3 tools/verify-ssdp-cast-gate.py`（读**已装产物**的 sidecar 日志，证明修复真进了打包版；v2.3.12 产物上这一步是 FAIL，即本次缺陷的复现）<br>`… --ports 49311 49312`<br>`… --serve tv\|router --port N`（只当替身，主流程内部调用） | 替身 LOCATION 同样必须写 `127.0.0.1` 以绕开 `_is_own_location` 自通告过滤；`--source` 要后端 venv（import `hancast_sidecar.ssdp`）；单测侧的等价契约在 `hancast-backend/tests/test_ssdp_cast_service_gate.py` |
| `verify-dlna-stop-chain.py` | 真机验证停止链路：用 CI 打进 `.app` 的 sidecar 把测试视频推给 Windows renderer，再从 renderer 自己那边确认 `TransportState` 真从 `PLAYING` 变回 `STOPPED`（只看本机状态会骗人） | `python3 tools/verify-dlna-stop-chain.py` | 仅 macOS 打包产物（`APP = /Applications/HanCast.app/Contents/MacOS`）；**必须先整体退出 HanCast**（sidecar 空载要占 8080）；`.tmp-win/hc_media/test.mp4` 存在（docstring 给了用 .app 自带 ffmpeg 生成的命令）；局域网里要有一台在跑的 Windows renderer（默认认 `display_name` 含 `AMD-YES` 或 IP `192.168.1.68`） |
| `verify-dlna-volume-chain.py` | 离线验证音量键链路（手机作控制端 → RenderingControl → MPV）：直接给 `DLNAProtocol.call()` 喂 SOAP 信封，用假 renderer / 截获的 IPC 命令做判据 | `hancast-backend/.venv/bin/python tools/verify-dlna-volume-chain.py` | **不需要真机**；必须用后端 venv 的解释器（系统 `python3` 没有 `requests`/`lxml`）；判据全部 PASS 才退出 0 |

### 1.3 屏幕投屏 — macOS

| 脚本 | 一句话用途 | 运行方式 | 前置环境 |
|------|-----------|---------|---------|
| `probe-macos-systap.py` | macOS 系统声音（CoreAudio Audio Tap）真机探针：`ensure_device()` → 设备出现在 avfoundation 列表 → 抓 3 秒里真带 440Hz → `destroy_device()` 后设备消失 | `hancast-backend/.venv/bin/python tools/probe-macos-systap.py [ffmpeg 路径]` | 仅 macOS；后端 venv；判据是 440Hz ±2% 带内能量显著高于静置基线（不是「有数据」就算过）——本机播放/采集链有约 −0.8% 固定音高偏差 |
| `run-macos-tap-probe.sh` | 把上面那个探针装进一个最小 `.app` 再跑，以复现产品路径的 TCC 身份 | `bash tools/run-macos-tap-probe.sh [ffmpeg 路径]` | 仅 macOS；`hancast-backend/.venv/bin/python` 存在；CoreAudio 采集是 TCC 权限，判给**负责进程**的 bundle 且要求 `NSAudioCaptureUsageDescription`——裸 python 没 bundle，`AudioHardwareCreateProcessTap` 照样返回 noErr 但环回永不产出数据。输出落仓库 `.tmp-win/hancast-tap-probe.log`；临时 app 建在 `/tmp/HanCast Tap Probe.app`（跑完即删），bundle id 可用 `HANCAST_TAP_BUNDLE_ID` 覆盖 |
| `probe-macos-audio-pitch.py` | 「音高是否跑掉」的对照实验：拿已在售的 BlackHole 走同一条测量链，判断 436.3Hz（−0.84%）是新环回路径的缺陷还是这台机器上任何进程内环回都有的表现 | `hancast-backend/.venv/bin/python tools/probe-macos-audio-pitch.py [ffmpeg 路径]` | 仅 macOS；**需装 BlackHole**；⚠️ 会**临时把系统默认输出改成 BlackHole 2ch**，结束（含异常）一定还原并复核；不写任何文件 |
| `probe-window-picker.py` | 验证「投屏指定窗口」的枚举与裁剪数学：CoreGraphics 显示器顺序 vs avfoundation `Capture screen N` 顺序、两套坐标系（CGDisplayBounds y 向上 / 窗口列表 y 向下）与 Retina 点→像素换算 | `hancast-backend/.venv/bin/python tools/probe-window-picker.py`（只枚举）<br>`… --capture`（抽样真投 2 秒） | 仅 macOS；后端 venv（import `screencast.ffmpeg_manager`）；多屏环境才有意义；坐标错了不会报错、画面照样出，所以必须真跑捕获；`--capture` 抽帧证据（整屏/裁剪/基准/PSNR 对比图）写仓库 `.tmp-win/win-picker/` |
| `dump-cgwindowlist.py` | 原样 dump `CGWindowListCopyWindowInfo`，用来核对 `darwin_windows()` 的过滤条件 | `python3 tools/dump-cgwindowlist.py` | 仅 macOS；**故意不用 pyobjc**——这台机器没装 Quartz 绑定，而打包 sidecar 走的就是 ctypes + CoreFoundation，必须复刻同一条读取路径，否则差异说明不了问题 |
| `verify-browser-segment-live.py` | 真机验证「浏览器通道分片时长可配置且不必重编译」：默认必须是 1 秒分片；`save_params` 把 `screencast.browser_segment_seconds` 改成 4 后同一份二进制停开一次必须变 4 秒；最后改回默认并清掉覆盖项 | `python3 tools/verify-browser-segment-live.py` | 仅 macOS 打包产物（`.app` 内的 sidecar）；**会整体退出 HanCast**（GUI 与 sidecar 抢 8080），结束把应用开回来；屏幕捕获依赖父进程的「录屏」权限，被拒时 start 直接报错、判据如实 FAIL |
| `verify-watch-page-live.mjs` | 浏览器通道观看页真机验收（1 秒分片）：驱动打包版 sidecar 起真实屏幕投屏，再用真实 Chromium（headless shell + CDP）打开观看页，看画面是否真在动、有没有卡死、端到端延迟多少 | `node tools/verify-watch-page-live.mjs [segmentSeconds]`（默认 1 = 出厂契约；传 4 会先 `save_params` 覆盖再测） | 仅 macOS 打包产物；`~/.cache/puppeteer/chrome-headless-shell` 下有 `mac_arm-*` 构建；**会整体退出 HanCast**，结束自动打开回来；截图落 `.tmp-win/hancast-watch-live.png`；退出码 0 = 全部断言通过 |
| `verify-watch-page-sound.mjs` | 观看页**出声**验收（v2.3.14）：真实 Chromium 双 pass 跑源码 `www/index.html`（替换占位符，hls.min.js 同目录现取）——pass A 最严自动播放策略下必须「静音兜底起播 → 「点击开启声音」按钮亮出 → CDP 合成点击解除静音（`muted=false`）→ 按钮收起 → 画面持续推进」，pass B 全放行策略下直接带声起播且不出按钮 | `node tools/verify-watch-page-sound.mjs` | 仅 macOS（本机实测）；ffmpeg 在 PATH（现生成 24s / 1s 分片 HLS 素材：testsrc2 1280x720@25 + 440Hz 音轨）；**必须「有头」**——headless 系连 muted 自动播放豁免都没有（连 muted 起播都 NotAllowedError），窗口会在屏幕上闪约 40 秒；Chrome 从 `~/.cache/puppeteer/chrome/mac_arm-*/` 现找（`HANCAST_VERIFY_CHROME` 可覆盖）；截图落 `.tmp-win/hancast-watch-sound-{A,B}.png`；退出码 0 = 两 pass 全过 |

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
| `probe-win-window-picker.py` | Windows「投屏指定窗口」真机探针（v2.3.14 起按 hwnd→区域合约重写），验四件事：`_list_windows()` 交出来的窗口任务栏里是否真有（裸 `EnumWindows` 会混进 `CHeadBarWindow`、`com.han.hancast-siw` 这类影子输入窗口，选到就是黑屏）；列表 id 是 `window:0x<hwnd>` 且起播按 hwnd 换算矩形抓 desktop 区域（生产路径 `_window_capture_rect`）——gdigrab 的 `-i title=` 窗口模式在 DWM 下对 GPU 合成窗口只出黑帧、标题一变就 I/O error，已弃用；抓到的画面尺寸 ≈ 窗口矩形（DWM 可见边框）且非全黑；真关掉一个探针窗口后拿它的 hwnd 起播必须**快速报错**（可读中文），而不是安静产出黑屏 | `run-winpick.cmd --survey`（只枚举并打印属性表，用来定过滤判据）<br>`run-winpick.cmd`（完整跑） | 仅 Windows（.68）**桌面会话**；必须用 `schtasks … /it` 投递（脚本头给了 `HANCAST_WINPICK` 的完整命令）；窗口由探针自己 `RegisterClass` + `CreateWindowExW` 开出来，标题直接进 Win32 API 不经 shell；探针会把自己置顶——区域抓取读的是屏幕上那块矩形，不是窗口自己的 buffer |
| `verify-win-capture-sources.ps1` | 屏幕捕获四变体对照取证（v2.3.14 窗口黑屏的根因判据）：同一台机器逐变体抓 PNG——`desktop`（DWM 合成）、每显示器区域、`-i hwnd=0x…` 窗口模式（GetDC(hwnd)，产品旧路径的行为）、窗口矩形区域（新修法）——外加 manifest（本进程 DPI awareness / 显示器矩形 / 窗口表 / 每个 PNG 的真实像素尺寸），让人自己判「哪条路径出黑帧」 | 经 `schtasks /create /f /tn hc_capchk /tr "powershell -NoProfile -ExecutionPolicy Bypass -File D:\Temp\verify-win-capture-sources.ps1" /sc once /sd 2999-01-01 /st 00:00 /ru Administrator /it` 投递后 `schtasks /run /tn hc_capchk`（收尾 `schtasks /delete /tn hc_capchk /f` + `rmdir /s /q D:\Temp\capture-check`） | 仅 Windows（.68）**交互桌面**（服务/ssh 会话没有桌面，gdigrab 抓黑）；输出默认 `D:\Temp\capture-check\`（`-OutDir` 可换）、`-Ffmpeg` 可指定用哪份；保持 ASCII-only；exit code = 失败抓取数 |
| `verify-screencast-audio-order.cmd` | 验证「屏幕 + 麦克风/系统声音」的 ffmpeg **参数顺序**：`-vf` 夹在两个 `-i` 之间会被判给下一个输入（麦克风）并以 `Option vf cannot be applied to input url audio=…` 死掉。三个 case：OLD 必须 FAIL（复现 v2.2.22 及以前）、A（仅麦克风）必须 PASS、B（麦克风 + 环回 s16le + `-filter_complex amix`）必须 PASS | 按上面的通用投递方式，任务名 `HANCAST_VFT`，脚本落 `D:/Temp/hcvf.cmd` | 仅 Windows（.68）交互桌面；ffmpeg 不再随包（v2.3.11），脚本自己按「依赖面板下载到 `%APPDATA%\HanCast\bin\ffmpeg.exe` → PATH 上的系统 ffmpeg」取那一份；结果看 `D:\Temp\hcvf-out.txt`：`OLD_EXIT` 非 0、`A_EXIT`/`B_EXIT` 为 0，且 `a.m3u8`/`b.m3u8` 各报一条视频 + 一条 aac |
| `verify-screencast-audio-order.ps1` | 上面那三个 case 的结果读取器：数每个 `m3u8` 的分片条数，并用 ffmpeg 打印 `Input #` / `Stream #` | `powershell -File verify-screencast-audio-order.ps1`（在 .68 上） | 仅 Windows（.68）；`D:\Temp\hcvf\{old,a,b,c}.m3u8` 已由 `.cmd` 那轮产出；缺文件打 `MISSING` 后跳过 |
| `verify-win-loopback-feed.cmd` | 分离「stdin 静默但打开」与「实时涓流」两种喂料方式：产品命令在 .68 上 8 秒不落播放列表（2.2.23），而同一套参数布局在 `verify-screencast-audio-order.cmd` 里能跑，唯一差别就是 `pipe:0` 的字节从哪来 | 按通用投递方式，任务名 `HANCAST_FEED`，脚本落 `D:/Temp/hcfeed.cmd` | 仅 Windows（.68）交互桌面；结果看 `D:\Temp\hcfeed-out.txt`：`D_*` 是空 stdin 下播放列表是否出现，`E_*` 是实时涓流基线；这里 loglevel 用 `info` 而非 `error`，好让 stall 原因可见 |
| `verify-win-screencast-audio-live.cmd` | Windows 屏幕投屏音频的**产品链路**验收：按真实 stdin/stdout JSON 协议驱动**已发布的** sidecar，让 ffmpeg 命令行由产品代码（`screencast/capture.py`）自己拼——手写 ffmpeg 命令只能证明某种参数布局，这条证明的是应用真正吐出来的那一份 | 按通用投递方式，任务名 `HANCAST_HCSL`，脚本落 `D:/Temp/hcsl.cmd` | 仅 Windows（.68）交互桌面；打包版 sidecar 在 `C:\Users\Administrator\AppData\Local\HanCast`；请求文件由 `gen-win-screencast-live-req.py` 生成（纯 ASCII）；结果看 `D:\Temp\hcsl\out.txt`：三轮（3/6/9 = 麦克风+系统声音 / 只系统声音 / 只麦克风）每个 start `"ok":true`、每个 status `"running":true`、`SIDECAR_EXIT=0`（停止路径无 `0xc0000005`）、无 `Option vf cannot be applied to input url`；`D:\Temp\hcsl\seg.txt` 是每轮运行**中途**抓的分片快照；收尾 `schtasks /delete /tn HANCAST_HCSL /f` + `rmdir /s /q D:\Temp\hcsl` |
| `gen-win-screencast-live-req.py` | 生成上面那条验收的请求行：把中文设备名转成纯 ASCII（`json.dumps(ensure_ascii)` 再把 `( ) & < > \| %` 也转成 `\uXXXX`），sidecar 的 `json.loads` 照样还原，批处理里一个特殊字符都不剩 | `python3 tools/gen-win-screencast-live-req.py phase1 -o req.txt`（只枚举设备，第一轮跑这个拿设备名）<br>`… devices <out.txt>`（列出 phase-1 响应里的音频输入设备）<br>`… req <out.txt> --mic I [--sys J] [--second mic\|system]… [-o req.txt]`（按序号取设备名拼请求文件，`--second` 可重复以把多路组合排进一次运行） | 任意平台（只是生成文本）；配套 `verify-win-screencast-audio-live.cmd`；默认**不发** `set_ffmpeg_path`，让 .68 上的产品按自己的检测链选 ffmpeg（v2.3.11 起不随包，旧版硬编码的 `C:\Users\Administrator\AppData\Local\HanCast\ffmpeg\ffmpeg.exe` 已不存在）；要强行指定某一份时给 `--ffmpeg <路径>` |
| `verify-win-screencast-dwell.sh` | 跑批的同时从本机拉产品自己的 HTTP 通道 `http://<host>:8765/hls/<session>/live.m3u8`，按秒记录最大分片号。协议里的 `screen_cast_status` 只说「进程还在」，说不出「画面还在往前走」；喂料线程/环流饿死时进程可以活着但分片不再增长 | `tools/verify-win-screencast-dwell.sh [host] [时长秒]`（默认 `192.168.1.68` / 60 秒） | 从 macOS/Linux 跑；能 `ssh administrator@<host>`；.68 上正有一路屏幕投屏在跑；`curl` |
| `verify-win-screencast-tone-content.sh` | 内容级判据：start 返回 ok、status 返回 running、退出码 0 全都只说明「FFmpeg 还活着」，声音到底有没有进分片只能把分片拿回来量。让 .68 循环播「440Hz 与 3500Hz 同时发声」的提示音，用产品协议起一路投屏，从产品 HTTP 通道拉回两段中途分片，用打包内同一份 ffmpeg 做 420–460Hz / 3300–3700Hz 带通 + `volumedetect` | `tools/verify-win-screencast-tone-content.sh [phase-1输出] [系统声音名字正则] [麦克风名字正则]`<br>环境变量 `HOST`（默认 `192.168.1.68`）<br>特殊值 `MIC=off`（只起「单独系统声音」那一路，判据最干净）、`TONE=off`（静音对照，量同一台机器同一副麦克风同一路投屏的地板） | 从 macOS/Linux 跑；能 ssh 到 .68；默认**自适应选设备**：系统声音 = 枚举里标着「当前出声」(default) 的那块环回，麦克风 = `麦克风 .*Realtek`；不给 phase-1 输出就自己跑一轮 `list_screen_sources` 枚举。判据：3.5kHz 带通有能量 → 提示音真被混进输出；3.5kHz = −91dB（数字静音）→ 输出里一点提示音都没有。为什么是 3.5kHz：单频 440Hz 判不准，风扇与麦克风底噪的能量就堆在低频（静音对照的 420–460Hz 带通已有 −35dB，跟放音时只差 3dB），3.5kHz 麦克风给不出来 |
| `verify-dshow-parse-offline.py` | 离线回放 Windows dshow 设备枚举，验 `_list_audio_devices` 的解析。判据是「认出的音频设备集合 == 原始文本里带 `(audio)` 后缀的行」，且 `Alternative name` 行一条都不能混进来（它同样带引号，早期只按分段头切段的实现会把它当设备） | `tools/verify-dshow-parse-offline.py <ffmpeg -list_devices 抓到的原始输出文件>` | **不需要 Windows**（设计上就是在 Mac 上跑）；后端源码可 import（`sys.path` 插入 `hancast-backend`）；输入文件需事先从 .68 抓来。为什么离线：.68 的麦克风名是中文、设备列表随插拔变化，UI 里点一遍既慢又不可复现 |
| `verify-ffmpeg-detect.ps1` | v2.3.11「FFmpeg 去捆绑」的真机总验收：驱动**装好的** `hancast-sidecar.exe` 走 stdin/stdout JSON，把检测链三层各造一次环境——② `source=system`（winget `Links` 那份）、③ `source=downloaded`（往 `managed_dir` 播种一份真 ffmpeg）、以及「本机确实没有 ffmpeg」时弹窗的数据源本体（`ok=False` + `missing` + `install_commands` + `download.available` + 错误原文）。`-Download` 再真按一次 gyan 下载，逐帧打印 `stage/received/total/percent/speed` | scp 进 `D:\Temp` 后 `powershell -NoProfile -ExecutionPolicy Bypass -File D:\Temp\verify-ffmpeg-detect.ps1 [-TempAppData] [-BarePath] [-SeedDownloaded <真 ffmpeg>] [-Download]` | 仅 Windows（.68），**不需要交互桌面**（不碰 gdigrab）；默认打 `%LOCALAPPDATA%\HanCast\hancast-sidecar.exe`。沙盒只重定向 APPDATA/LOCALAPPDATA/USERPROFILE 进一次性目录并在收尾删除，**不动用户的 winget 安装与真实设置**（seed 那份是从旧随包目录拷进沙盒的副本）。要造「没有 ffmpeg」必须 `-TempAppData` **加** `-BarePath`：这一台机器的进程 PATH 本来就带 winget `Links`（单独 `-TempAppData` 时 `_extra_dirs()` 已被沙盒中和却仍命中 system），只重定向目录照样检得到。脚本保持 ASCII-only；stderr 刻意**不**重定向（管道缓冲会在下载中途写满把 sidecar 顶死） |

### 1.5 解析与规则包

| 脚本 | 一句话用途 | 运行方式 | 前置环境 |
|------|-----------|---------|---------|
| `verify-rule-packs-live.py` | 用源码 sidecar 的真实解析管线跑规则包，打印每条链接是哪条规则命中的。不点 GUI、不起整个应用：只把 `ShareResolver` 拎出来配一个假媒体服务器（注册代理只记地址、不落流） | `hancast-backend/.venv/bin/python tools/verify-rule-packs-live.py <链接或口令> …`<br>`… --rules-dir <dir>`（用指定规则目录，内含 `plugins/` 与 `*.json`）<br>`… --no-packs`（不装官方包，只跑 `<dir>` 里已有的规则）<br>`… --keep`（保留临时目录，方便看插件缓存/落盘文件） | 后端 venv；默认把仓库根 `rule-packs/dist/` 的包当**用户导入的 zip** 装进临时目录，装前先比 `index.json` 的 sha256；需要外网访问站点。判据看输出的 `via`：`<包名>/<规则 id>` = 走了 JS 规则包，`<规则名>` 不带斜杠 = 声明式 JSON 规则。程序里没有编译进去的站点规则，所以「没装包 ⇒ 什么站点都解析不了」是**预期行为不是 bug**。证明的是「规则能不能取出可播地址」，不含电视端真实播放 |
| `share_resolve_probe.py` | 粘贴投屏解析现场复现器：把一段分享文案喂给真实解析管线，并验证播放器能不能解出画面 | `python tools/share_resolve_probe.py "<粘贴的链接或口令文案>"`<br>`… --play "<url>"`（解析完用 ffmpeg 实解一段）<br>`… --mpv "<url>"`（用打包版 mpv 实解）<br>`… --serve "<url>"`（起真实媒体代理）<br>`… --log "<url>"`（只看决策日志）<br>`… --rules-dir /tmp/r "<url>"`<br>`… --mpv --segment "<分片/流地址>" --referer "<页面地址>"`（跳过解析，只验代理+播放器）<br>`… --keep-ledger "<url>"`（失败条目写进**用户真实台账**，默认不写） | 后端源码可 import；**默认替身代理不占端口、不起服务**；`--serve`/`--play` 会**真的起 MediaServer**（占端口、写临时文件），于是「伪装分片走本机代理」这类结论可以直接看到播放器视角的结果。**默认还接管解析失败台账**：解不出来时 `resolve()` 会记一条，而那份台账是设置页给用户看的真实失败清单——探针调试用的假地址混进去会把真实失败冲淡，所以不带 `--keep-ledger` 时把 `failures.SETTING_DIR` 指到临时目录、退出即删 |
| `verify-proxy-live.py` | 端到端验收「粘贴投屏」的代理链路：真起一个 MediaServer，把远端播放列表挂成本机代理，再用 `ffprobe` 冒充播放器逐片检查字节能不能解出画面/声音 | `tools/verify-proxy-live.py <m3u8_url> [--referer U] [--picks 0,300,699]` | 后端源码可 import；`ffprobe` 在 PATH；**会真起 MediaServer**（本机端口）；需外网。退出码 0 = 每个抽样分片都被识别为 `mpegts` 且有视频流 |
| `verify-proxy-segment-decodable.py` | 产品代理链路验收：给一条需要防盗链的 m3u8，看本机代理转出来的分片能不能真解码。判据是播放器视角的三件事——首字节是 TS 同步字节 `0x47` 且按步长游走时绝大多数是 188；转发时 `Content-Type` 不再是 `image/*`、`text/html`；本机检测到的 ffmpeg 用**播放器的方式**（不指定 `-f`）探这段字节，解封装器不许是 `png_pipe`/`image2` 且要有真视频流 | `hancast-backend/.venv/bin/python3.12 tools/verify-proxy-segment-decodable.py <m3u8_url> --referer <页面或源站地址> [--ffmpeg <路径>]`（验工作树）<br>`… --root /Applications/HanCast.app/Contents/Resources`（验**已装上的那份产物**；`--ffmpeg` 不给就用本机检测到的那份）<br>`… --probe-only /path/to/skinned.ts`（反例自检：喂一段「皮没剥」的字节，工具必须判 FAIL） | 后端 venv；外网；**只读上游、只在本机 `127.0.0.1` 起一个临时端口，不碰任何产品配置**。退出码不算判据——带皮的字节 ffmpeg 自己会重同步，`-f mpegts -c copy` 照样 rc=0，拿它判等于没判 |
| `probe-hls-antileech.py` | 诊断 HLS 分片的防盗链/伪装行为：同一分片用不同请求头形状各拉一次首块，打印魔数、PNG 头长度、TS 同步偏移，用来决定代理该走哪套头、要不要剥皮 | `tools/probe-hls-antileech.py <m3u8_url> [--index N] [--cookie "k=v; k2=v2"]` | `requests`；外网；只读 |
| `probe-ts-skin-offset.py` | 定位「PNG 皮 + TS」的精确剥离偏移，并验证剥完能否被 ffmpeg 解封装 | `tools/probe-ts-skin-offset.py <m3u8_url> [--index N]` | `requests` + `ffmpeg`；外网；只读 |
| `probe-dyrs-headers.sh` | dyrs 采集站分片防盗链矩阵：同一分片换不同的头/Range/Cookie，看到底哪种给真 TS（要搞清它认的是页面 Referer、Set-Cookie 会话、`Sec-Fetch-*` 还是 Range） | `tools/probe-dyrs-headers.sh <m3u8_url> [页面 referer]` | `curl` + `/usr/bin/python3`；外网；产物写仓库 `.tmp-win/dyrs-headers/`（`probe-dyrs-skin.sh` 同款落 `.tmp-win/dyrs-skin/`）；全程 `--noproxy '*'` |
| `probe-dyrs-skin.sh` | 一次性复现：dyrs 采集站的分片到底是不是「披着 PNG 皮的 TS」，皮有多长（三种 Range 各拉一次，打头 32 字节 hex、是否 PNG、TS 同步偏移） | `tools/probe-dyrs-skin.sh <m3u8_url> [referer]` | 同上 |
| `verify-rule-pack-live.py` | **出货形态**的规则包联网验收：读 `rule-packs/dist/<name>-<version>.zip`（索引里那个文件）→ `import_zip_bytes` → `refresh()` → `ShareResolver.resolve()`，打印每条链接由哪条规则命中。存在的理由：`build-rule-packs.py --check` 只证明产物与源一致、`pytest` 全程离线，而「包里的 JS 在真站上还抓不抓得到地址」只有联网才能证明——采集站换皮就是静默失效 | `hancast-backend/.venv/bin/python tools/verify-rule-pack-live.py`（默认包与默认地址表）<br>`… --pack hancast-cms`<br>`… --url https://x.example/play/1` | 后端 venv；**需外网**，本机走代理时带 `HTTPS_PROXY=http://127.0.0.1:9565`；规则装进临时目录，不碰用户 `share_rules/`；本地供流服务换成替身。站点自己挂了/上验证码闸门时记进脚本里的 `EXPECTED_FAIL` 并注明取证日期，**别把环境故障算成包坏了**。每次发版前重跑 |
| `verify-github-mirror-live.py` | GitHub 镜像开关（v2.3.12）的联网验收，判据是四条契约而不是「镜像站打得开」：①开关打开时请求**真的先落到镜像前缀**（从日志认）；②镜像给的字节与直连**逐字节一致**（索引 JSON、包 zip，zip 还要对得上官方索引声明的 sha256）；③`api.github.com` 这类只有部分镜像代理支持的形态，开开关后检查更新照样拿得到最新版本号；④镜像死了（前缀指到 `127.0.0.1:1`）**自动回退直连** | `hancast-backend/.venv/bin/python tools/verify-github-mirror-live.py` | 后端 venv；**需外网**，走代理时带 `HTTPS_PROXY`。**只读**：不装包、不写台账、不碰用户配置——开关用 `params` 的内存覆盖位演，落盘的 `hancast_setting.json` 一个字节都不动 |

> 关于代理：macOS 上 `requests` 会继承系统代理（`127.0.0.1:9565`），代理端口一失效解析就全挂（见 `docs/分享解析规则.md:158`）。需要显式走代理时用
> `HTTPS_PROXY=http://127.0.0.1:9565 python3 tools/<script>.py …`；
> `probe-dyrs-*.sh` 反向操作，用 `curl --noproxy '*'` 明确绕开代理。

### 1.6 后台参数、界面与协议

| 脚本 | 一句话用途 | 运行方式 | 前置环境 |
|------|-----------|---------|---------|
| `verify-media-theme.cjs` | 真实 Vue/Pinia 验解析代次（清空、编辑、文件/URL 交错与失败）、直链兜底、安全解码及系统主题切换 | `node tools/verify-media-theme.cjs` | 已安装前端依赖；API/DOM/定时器使用内存桩，无网络及用户配置写入 |
| `verify-cast-ui.cjs` | 编译生产 store 与控制器 SFC，验证错误反馈、单飞轮询、操作代次、进度解锁/清理和封面回退 | `node tools/verify-cast-ui.cjs`；可附测试名子串只跑匹配项，例如 `node tools/verify-cast-ui.cjs "preview never"` | 同上；真实 Vue renderer/Pinia，假设备/API，无真实投屏；CSS 编译不等于跨平台渲染验收 |
| `verify-app-lifecycle.cjs` | 编译真实 App/Home，验证首页→设置→首页监听仍各一份、事件可达、App 卸载及迟到 listen 句柄清理 | `node tools/verify-app-lifecycle.cjs` | 同上；设置路由和叶子控件用桩，根组件及首页脚本/模板实际编译，不复制产品初始化代码冒充验证 |
| `verify-playlist-advance.cjs` | 播放队列自动续播与投屏历史验收（v2.4.2）：把真实 `playlist.ts` + `cast.ts` + `history.ts` 转译进隔离 vm、挂组件实例让 `onMounted` 真注册事件监听；10 项断言——顺序/随机/单曲循环推进、connect 瞬态不接管、**手动停止后 STOPPED 不再续投**、**投单文件清空旧队列**、**队列历史只记一条且按设备可查** | `node tools/verify-playlist-advance.cjs` | 同上；纯逻辑层转译，无浏览器无真机。可用「回退到旧写法」反证该项 FAIL，证明判据确实抓得住不续播/停不住/沿用旧队列三类 bug |
| `verify-params-ui.mjs` | 设置页「后台参数」区块的浏览器验收：用真实 Chromium（headless shell + CDP）渲染，不靠快照推断布局。走 vite dev 的 mock 通道，只验 UI 行为（展开 / 四类控件 / 待保存计数 / 保存回落 / 恢复默认）。分组标题是**逐串比对**的（v2.3.12 起期望值含「GitHub 加速」），所以新增 section 忘了补 i18n 时它会红 | `node tools/verify-params-ui.mjs [base-url]`（默认 `http://localhost:5199`） | 先起 vite：`node node_modules/vite/bin/vite.js --port 5199`；`~/.cache/puppeteer/chrome-headless-shell` 下有 `mac_arm-*` 构建；CDP 端口 9337；截图落 `.tmp-win/hancast-params-ui.png`；退出码 0 = 全部断言通过。**后端契约不由它负责**——那是 `hancast-backend/tests/test_params*.py` 与 `tools/accept-macos-artifact.sh` 的活 |
| `verify-error-wrap-ui.mjs` | 「解析失败原因」整行可见的浏览器验收（v2.3.12）：走 vite dev 的 mock 通道喂一条非媒体网页地址，mock 后端抛的就是真后端那条长错误，于是文案与样式都是真实形状。判据用**几何**不用文本——`textContent` 永远完整，截断只发生在渲染上，所以量「换了几行」「有没有一行的右边界超出元素盒子」，并在 **420px 窄窗**里测（宽窗会把老的 `nowrap` 样式也放过）<br>| `node tools/verify-error-wrap-ui.mjs [base-url]`（默认 `http://localhost:5199`） | 先起 vite：`node node_modules/vite/bin/vite.js --port 5199`；Chrome 同 §1.6 上一行；截图落 `.tmp-win/hancast-error-wrap.png`；退出码 0 = 全部断言通过 |
| `verify-sidecar-utf8-protocol.py` | 验证 sidecar 的 stdin/stdout 协议在「父进程没有 locale」时仍走得通。登记表/错误文案全是中文，而 stdout 就是与 Rust 的通信通道；父进程没给 `LANG`/`LC_*` 时 Python 的 stdout 会退回 ASCII，`emit_response(..., ensure_ascii=False)` 直接抛 `UnicodeEncodeError`，表现为「命令永远失败」而不是「编码不对」 | `python3 tools/verify-sidecar-utf8-protocol.py [python 二进制]`（缺省用 `sys.executable`；可指向打包版 sidecar） | 后端源码可 import；无需外网、无需真机。判据：hostile env（`LC_ALL=C` 等）下 `get_params` 必须成功、条数不少于 18、中文标签原样回来；`stop_screen_cast`（空载）这种带中文提示的响应也必须回来。v2.2.18 用打包版实测撞到过 `{"id":2,"success":false,"error":"'ascii' codec can't encode characters …"}` |

### 1.8 文档体检

| 脚本 | 一句话用途 | 运行方式 | 前置环境 |
|------|-----------|---------|---------|
| `check-doc-freshness.py` | **文档新鲜度闸门**（v2.3.23 立项）。补 `audit-doc-refs.py` 查不了的那一类：行号还在、但指的行已经不是那回事。三道检查：① **内容校验**——引用旁边反引号里的符号必须真的落在那个行号窗口里；归因规则是「贴着引用才算」（中间最多夹 4 个标点），放宽一点就会把表格第一列的命令名算给右边的行号、一次误报 489 条；② **校对戳**——头部「最后校对 vX.Y.Z」落后于 `package.json` 即报，已声明退役的历史稿（前 15 行含「已被/取代/历史稿/留档」）按 `audit-doc-refs.py` 同一口径放行；③ **硬数字**——壳层 `#[tauri::command]`、Sidecar 注册表、`PARAM_SPECS`、组件/store/locale 键数/官方包/测试文件数逐项与代码实测比对。`--defscan` 是返工用的加细档：报「窗口里只有调用、没有定义行」的引用，软提示、不影响退出码。立项当轮用它抓出并修掉 **108 处**静默漂移的 `文件:行号`（单是 `docs/屏幕投屏.md` 就有 70 处，且其中一半连闸门第一版都抓不到） | `python3 tools/check-doc-freshness.py [docs/x.md ...] [--citations|--stamps|--numbers|--defscan] [--max-lag N]` | 无依赖，纯 stdlib；只读仓库，不写盘、不动设备 |
| `audit-doc-refs.py` | 文档引用的**文件是否存在 + 行号是否越界**（内容对不对由上面那道管）；四类刻意引用进「放行」计数 | `python3 tools/audit-doc-refs.py [docs/x.md] [--show]` | 无依赖，纯 stdlib；需要 `git check-ignore` 判定运行时落盘文件 |

---

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
| `install-macos-artifact.sh` | **覆写 `/Applications/HanCast.app`** | 先校验 dmg 内版本 == 要求的版本，不符即 FAIL 退出；不提权（当前用户在 `admin` 组即可）；不 `pkill` 旧 sidecar（会留下没有子进程的空壳 GUI），只 `osascript quit` 后等它自己退；回滚备份（`/tmp/HanCast_pre_*.app`）只保留最近一份——装包前自动清掉历轮的（`/tmp` 的系统清扫不回收目录包，别指望它） |
| `pull-rule-packs-installed.py` | **写用户插件目录** `<share_rules>/plugins/`，并**退出/重启 GUI** | 先干净退出 GUI，否则会同时跑两个 sidecar，1900 端口与媒体服务互相干扰、验收日志被上一轮假象污染；`--no-relaunch` 可只拉不重启 |
| `verify-browser-segment-live.py` / `verify-watch-page-live.mjs` | **整体退出 HanCast**（抢 8080），并**写 `hancast_setting.json`** 覆盖后台参数 | 脚本结束把应用开回来；`verify-browser-segment-live.py` 最后把参数改回默认，且覆盖项应当被**清掉**，不是留一个 `== 默认值` 的脏键 |
| `verify-dlna-stop-chain.py` | 向局域网真实 renderer **推流并下发停止** | 要求先整体退出 HanCast；测试媒体固定在 `.tmp-win/hc_media/test.mp4`（不是用户路径） |
| `verify-watch-page-sound.mjs` | 用 ffmpeg 生成临时 HLS 素材、在 `127.0.0.1` 起随机端口 HTTP 服务、**弹出有头 Chromium 窗口**（屏幕上闪约 40 秒）与一次性浏览器 profile | 素材/浏览器 profile 全在 `mkdtemp` 临时目录、退出即删（`finally` + `rmSync`）；不碰用户配置、`/Applications` 产物与真实屏幕投屏 |
| `verify-ssdp-offline-eviction.py` | 在本机**起一台假 MediaRenderer**并监听端口 | LOCATION 故意写 `127.0.0.1`；替身端口可指定（`--port`） |
| `share_resolve_probe.py --serve/--play`、`verify-proxy-live.py` | **真起 MediaServer**，占本机端口、写临时文件 | 默认模式（不带 `--serve`/`--play`）用替身代理，不占端口不起服务 |
| `share_resolve_probe.py`（解析失败时） | 往**用户解析失败台账** `parse_failures.json` 追加一条 | 默认把 `failures.SETTING_DIR` 重定向到临时目录并在退出时删除，真实台账一个字节都不动；只有显式 `--keep-ledger` 才写用户数据。pytest 同理由 `tests/conftest.py` 的 session fixture 隔离，测试用的假地址不得出现在用户设置页 |
| `verify-proxy-segment-decodable.py` | 在 `127.0.0.1` 起一个临时端口 | 明确声明「只读上游、不碰任何产品配置」 |
| `probe-installed-ja3.py` | 起**打包版 sidecar** 进程并向用户的配置目录**写一条解析失败台账** | 只在 `127.0.0.1` 起取证端口；地址带一次性标记 `hancast-ja3-probe`，收尾用 `failure_delete{url}` 精确删自己那一条（绝不调 `failure_clear`，那是用户的数据）；`close()` 只收尾自己 spawn 的那个 pid——**绝不 `pkill` sidecar**，那会杀掉正在投屏的实例 |
| `browser-channel-livecheck/run.sh` | 往应用的**用户规则目录**装一个测试包，并起 HTTP 服务 | `trap cleanup EXIT` 只删本次自己建的那一个目录（`--keep` 才留）；服务是脚本自己起的子进程，退出即 `kill` |
| `verify-linux-window-opacity.sh` | **真的启动/杀掉 GUI**，并切换 Hyprland 工作区（背景需要裸桌面） | 结束把视图还回原 workspace（`--no-relaunch` 则不重开）；量之前先确认没有投屏在跑。**绝不用 `hl.dsp.window.move`**：它作用于「当前焦点窗口」，实测会把用户正在用的终端/编辑器整窗口挪到别的工作区，正确做法是先 `hl.dsp.focus({ workspace = N })` 切到空工作区、再启动应用让它落在那儿 |
| `verify-linux-wayland-cast.py` | 起**真实 FFmpeg + `hancast-wlgrab` 抓屏会话**；`--with-tone` 往**当前默认输出**播 3 秒测试音（不改音量、不动配置） | `HOME`/`XDG_*` 全部指向一次性临时沙箱（`:132-136`），**不碰用户真实的 `hancast_setting.json`**、不启停 GUI；`finally` 删掉沙箱（`:358-359`）。不想吵到人就不带 `--with-tone`，那时音频只做结构性判据 |
| `rust-check.sh` | 在 `src-tauri/` 下**创建桩文件与目录** | `stub()` 遇已存在的路径直接跳过（打印「已存在，跳过」）；`trap cleanup EXIT` 逆序回收，先删文件再 `rmdir -p` 空目录，绝不动仓库里原有的东西 |
| `build-rule-packs.py`（无 `--check`） | **写 `rule-packs/dist/*.zip` 与 `rule-packs/index.json`** | `--check` 是只读模式；`--clean` 才删文件，且只删「源目录已不存在」的孤儿 zip |
| Windows `.cmd` 全家 | 在 .68 上**创建计划任务**、写 `D:\Temp\*`、起真实屏幕捕获 | 每个脚本头部都写了收尾命令（`schtasks /delete /tn <TASK> /f` + `rmdir /s /q <dir>`）；`gen-win-screencast-live-req.py` 里的 ffmpeg 路径与输出目录都是 .68 专属硬编码 |

### 2.2 必须用打包产物、不能用源码冒充的脚本

「产物验收」的定义就是**对打包结果而非源码的验证**。下面这些脚本的判据只有在产物上才成立：

- `accept-macos-artifact.sh` — 读的是 `/Applications/HanCast.app/Contents/Resources/hancast_sidecar/`（`RES`，`:11`），并把里面的源文件与 `git show <tag>` 逐文件比 sha256（清单 `:37-43`，30 个文件，v2.3.12 起含 `utils/ghmirror.py`；v2.3.13 轮把仓库根改成从脚本自身位置推导，`:34-36`，不再写死本机绝对路径）。跑源码等于没跑。
- `install-macos-artifact.sh` — 输入就是 dmg。
- `pull-rule-packs-installed.py` — 存在的理由原话：「手改 `share_rules/plugins/` 里的文件则是拿补丁冒充验收，产物里根本没跑过 `plugin_pull`」（`:5-8`）。
- `verify-browser-segment-live.py`、`verify-watch-page-live.mjs`、`verify-dlna-stop-chain.py` — 都硬编码 `/Applications/HanCast.app/...` 下的 sidecar。
- `verify-win-screencast-audio-live.cmd` — 驱动 `C:\Users\Administrator\AppData\Local\HanCast` 下**已发布的** sidecar；docstring 明写「a hand-written ffmpeg line can only prove an argument layout; this proves the layout the app actually emits」。
- `verify-proxy-segment-decodable.py` — 提供 `--root` / `--ffmpeg` 两个开关专门指向已装产物，注释写着「禁止拿工作树冒充验收」。
- `verify-ssdp-offline-eviction.py` — 默认读打包版日志。
- `probe-installed-ja3.py` — 跑的是 `.app` 里的冻结 sidecar（`DEFAULT_BIN`，`:47`）。它存在的理由就是源码级探针测不到这一层：`probe-tls-ja3.py --client hancast` 走 `sys.path` 里的源码，只能证明接线对，v2.3.7 恰恰是接线对、白名单漏包。
- `verify-windows-artifact.sh` — 读的是 .68 安装目录里那份 `.py` 与二进制，与 `git show v<tag>` 逐文件比哈希；本地工作树跟它无关。
- `verify-linux-window-opacity.sh` — 默认打的是**装好的** `/usr/bin/HanCast`（`HC_BIN` 可指向解包产物），判据是合成器屏幕上的像素，跑 `npm run tauri dev` 等于没跑——透明窗口那套只在 bundle 里才成立。
- `verify-ffmpeg-detect.ps1` — 打的是 `%LOCALAPPDATA%\HanCast\hancast-sidecar.exe`（Nuitka 冻结产物）。源码级那三层判定由 `tests/test_ffmpeg_deps.py` / `test_ffmpeg_download_e2e.py` 管，这个脚本存在的理由正是「产物里 `bundle.resources` 到底有没有把 ffmpeg 带进去、装好的 sidecar 在真机上认不认得到系统那份」——跑源码等于没跑。

反过来，这些是**源码级**的，不需要产物：`verify-dlna-volume-chain.py`、`verify-dshow-parse-offline.py`、`verify-sidecar-utf8-protocol.py`、`verify-params-ui.mjs`、`verify-error-wrap-ui.mjs`、`verify-watch-page-sound.mjs`（真实 Chromium 但页面是源码 `www/index.html`——产物层由 `accept-macos-artifact.sh` 的逐文件 sha256 兜）、`ssdp_probe.py`、`dump-cgwindowlist.py`、`rust-check.sh`、`build-rule-packs.py`。另有一类**联网但仍跑工作树代码**的：`share_resolve_probe.py`、`probe-hls-antileech.py`、`probe-ts-skin-offset.py`、`probe-dyrs-*.sh`、`verify-proxy-live.py`、`verify-rule-pack-live.py`、`verify-github-mirror-live.py`——后两个读的是 `rule-packs/dist/` 那份 zip 与真实解析管线，但都不碰 `/Applications/HanCast.app`，所以不能当产物验收用。

### 2.3 必须在 .68 Windows 测试机上跑的脚本

`192.168.1.68`（脚本里简称 `.68`）：

- **必须在 .68 的交互桌面会话里跑**（gdigrab 抓不到 session 0，所以必须经 `schtasks … /it` 投递）：`verify-screencast-audio-order.cmd`、`verify-win-loopback-feed.cmd`、`verify-win-screencast-audio-live.cmd`、`probe-win-window-picker.py`、`verify-win-capture-sources.ps1`
- **必须在 .68 上跑但不需要桌面**：`probe-wasapi-loopback.py`、`probe-win-audio-pipe.py`（后者显式避开 gdigrab，视频输入用 `lavfi testsrc` 顶替）、`verify-screencast-audio-order.ps1`（读已产出的 m3u8）、`verify-ffmpeg-detect.ps1`（只发 stdin/stdout JSON 给 sidecar，可直接 ssh `powershell -File` 跑，不用 `schtasks /it`）
- **从 macOS/Linux 远程打 .68**：`verify-windows-artifact.sh`（产物验收）、`verify-win-screencast-dwell.sh`、`verify-win-screencast-tone-content.sh`（两者 `HOST` 默认 `192.168.1.68`，走 ssh + 产品的 `http://<host>:8765/hls/<session>/` 通道）
- **不需要 .68**：`verify-dshow-parse-offline.py`（离线回放 .68 抓来的文本，设计上就是在 Mac 上跑）、`gen-win-screencast-live-req.py`（只生成 ASCII 请求文本）
- `verify-dlna-stop-chain.py` 需要局域网里有一台在跑的 Windows renderer，默认认 `display_name` 含 `AMD-YES` 或 IP `192.168.1.68`

### 2.4 解释器选择

| 需求 | 用什么 |
|------|--------|
| 只 import `hancast_sidecar` 且用到 `requests`/`lxml` | `hancast-backend/.venv/bin/python`（系统 `python3` 没有这些依赖——`verify-dlna-volume-chain.py` 的 docstring 明写这条） |
| 纯标准库 / 只用 `requests` | `python3` |
| Node 脚本（CDP 驱动 Chromium） | `node`（`verify-params-ui.mjs`、`verify-error-wrap-ui.mjs`、`verify-watch-page-live.mjs`、`verify-watch-page-sound.mjs`） |
| `.mjs` 的 Chrome 定位 | 前三个从 `~/.cache/puppeteer/chrome-headless-shell/mac_arm-*/` 现找，`verify-watch-page-sound.mjs` 必须用**有头**那份 `~/.cache/puppeteer/chrome/mac_arm-*/`（headless 系没有 muted 自动播放豁免，见其头部注释）；都从 `~/.cache/puppeteer` 现找，不写死版本号 |

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
2. **别写死条数。** 提交 `9787f2b`（`fix(tools): 参数表验收改为与 tag 源码逐键比对，不再写死条数`）的原话：写死 `"18"` 的断言在加了 `rules.index_url`（第 19 条）之后必然 FAIL，而**这条 FAIL 只证明脚本过期，证明不了产物有问题**——每轮加参数都得同步改脚本。改法是从**被测 tag 的 git 树现读基准**（`git show <tag>:hancast-backend/hancast_sidecar/params.py`），逐 key 逐 default 比对，这样能同时抓住产物**多带 / 漏带 / 带旧值**三种情况（四种变异体验证过）。实现见 `accept-macos-artifact.sh:102-123`。
   - 顺带一个实现坑，同处 `:111` 注释：临时文件**必须叫 `params.py`**——importlib 按后缀选 loader，无后缀的 `mktemp` 文件会静默读不出表。
   - 下界断言（`len(specs) < 18` 这种「不少于」）不会因新增参数而假红，可以接受，但 docstring 里的数字要跟着更新——`verify-sidecar-utf8-protocol.py:11` 写的「18 项」已滞后于当前的 32 条（`params.py:41-265`），`:81` 的实际断言仍是 `< 18` 下界所以没坏。
3. **别写死路径/版本号当唯一入口。** 产物路径要做成可覆盖参数（`pull-rule-packs-installed.py --app`、`verify-proxy-segment-decodable.py --root/--ffmpeg`、`verify-ssdp-offline-eviction.py --log`）；Chrome 从 `~/.cache/puppeteer/` 现找而非钉版本；triple 用 `rustc -vV` 现取（`rust-check.sh:38`）。

### 3.3 其它约定

- **一次性脚本不进 `tools/`。** 真机取证的临时脚本放 `.tmp-win/`，该目录已被 `.gitignore:106` 忽略（注释：「本地临时工作区（探针脚本、验收截图与产物；不入库）」）。要沉淀成可重复判据时才搬进 `tools/`——`install-macos-artifact.sh` 就是「固化替代每轮 `/tmp` 一次性脚本」的产物（提交 `e1ee651`）。
- **临时落点二选一：`/tmp` 或仓库根 `.tmp-win/`——不散第三处（例如 per-user `$TMPDIR`）。** 要留给人看的证据（截图、探针 dump、测试媒体）写 `.tmp-win/`（gitignored、可见、每轮清）；只活在本次运行里的（chrome profile、备份目录、scp 中转、解包目录）走 `/tmp` 显式路径，**且必须由脚本自己清**（trap / rm / atexit）。别指望系统清扫：`/tmp` 的它只吃「>3 天的普通文件 + 空目录」——socket 永不回收、目录包层层滞后，实测等于不回收（2026-10-15：`install-macos-artifact.sh` 的 184M 回滚包躺满一天无人回收，此后装包前自清历轮、只留最近一份）。
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
