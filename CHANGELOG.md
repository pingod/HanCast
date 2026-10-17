# Changelog

> 最后校对：2026-10-18，对应版本 v2.3.19（`package.json:4`）

本文件记录 HanCast 的版本历史。口径说明：

- **一个版本一笔发布提交**，提交信息自带该轮的完整契约说明；版本号与 tag 同名（`v2.3.6` ↔ 提交里的 `（v2.3.6）`）。
- 版本号唯一来源是 **`package.json` 的 `version`**（`scripts/sync-version.cjs:4,17`），`npm run version:set <x.y.z>` 把它刷进 7 处契约点：`src-tauri/tauri.conf.json`、`src-tauri/Cargo.toml`、`src-tauri/Cargo.lock`（本地包那条，v2.3.8 起收进来——此前每版都靠人肉补）、`hancast-backend/pyproject.toml`、`hancast_sidecar/utils/config.py:39`、`src/api/commands.ts`（mock）、`src/stores/settings.ts`。`scripts/check-version.cjs` 自称以 `tauri.conf.json` 为源（口径相反），但全仓无人调用，是未接线的孤儿脚本——判定以 `sync-version.cjs` 为准。详见 [docs/构建与发布.md](docs/构建与发布.md) §1。
- 下表「提交」列即 `git tag <版本>` 指向的提交；细节以 `git show <提交>` 为准，本文只留用户可感知的变化。
- 新加版本时**同一次提交**里补这里，并按 [docs/README.md](docs/README.md) §4 的约定同步对应文档。

---

## v2.3.19 — 2026-10-18（本轮发布提交）

Linux 装端第三轮，治用户报的两件事：**「linux 上的电脑投屏全部都是黑屏」** 与 **「麦克风、系统声音页都不可用」**（2026-10-17，CachyOS + Hyprland 0.56.2）。用户可感知面：Wayland 会话里屏幕投屏第一次投出**真实画面**、能只投某一块显示器，两个音频下拉里有得选了，选中的设备没声/没权限会**当场说清楚原因**。X11 会话仍走 `x11grab`，Windows / macOS 路径未动。

- **黑屏的根因是采集方式，而且它不报错**：Linux 的视频输入一律 `-f x11grab -i $DISPLAY`，而 Hyprland/sway 的 XWayland 以 `-rootless` 启动——**原生 Wayland 窗口不在 X 的窗口树里**，于是那颗空 root window 被抓得又稳又顺：会话 `ok`、`running` 为真、电视和浏览器都能拉流，日志一个字都不提。量化（同一台机器、同一时刻，`signalstats` 的 YAVG）：`x11grab` **16.03**（纯黑）vs `grim`（wlroots `zwlr_screencopy_manager_v1`）**44.28**（真实桌面）。FFmpeg 侧无解：n9.0.2 的 `-demuxers` 只有 `x11grab` / `kmsgrab` / `fbdev`，没有 wayland，pipewire 抓取要 portal 且官方构建没编；`kmsgrab` 要 DRM master，跟合成器抢独占，桌面会话里起不来。**所以只能自带原生 helper。**
- **修法一 · 随包 Rust helper**：新增 `linux-capture/`（`hancast-wlgrab`，`src/main.rs` 886 行，依赖只有 `wayland-client` + `wayland-protocols-wlr`，**不碰 GTK**）。screencopy 走 SHM 路径把帧按紧凑 BGRA 写 stdout，FFmpeg 以 `-f rawvideo -pixel_format bgra -video_size WxH -framerate N -i pipe:0` 接进**既有 HLS 管线**（`pixel_format` 而非 `pix_fmt`，rawvideo 只认前者）。两个坑都量过：① 帧尺寸必须取 helper 首行 `HDR`，不能取 `list` 的 mode——缩放屏上两者差 scale 倍，`-video_size` 差一点整条流读歪成斜纹，**而且也不报错**；② Linux 的 screencopy 单位是 **output**，所以给用户的可选项是「哪块屏」（`wl:DP-2` / `wl:DP-3`，`screencast/wlgrab.py:218`）而不是「哪个窗口」。
- **修法二 · 拿不到 helper 就失败，绝不退回 x11grab**（`screencast/capture.py:81-107`）：退回等于把这条原始故障再发一遍。前端同步把「开始投屏」置灰，并把 `capture.reason` 写在用户正看着的位置（`src/components/ScreenCastPanel.vue`），三种原因（不在 Wayland 会话 / helper 缺失 / `pactl` 缺失）分开说。helper 定位顺序 sidecar 同级 → `resource_dir` → 包上一级，**且必须真 exec 一次 `list`** 才算可用（`wlgrab.py:124` 逐候选聚合死因，不再只回一句「找不到」）——这是 v2.3.17「随包 mpv 谎报就绪」的同一条教训。
- **修法三 · Linux 音频补齐**：「页面不可用」的真因是 `_list_audio_devices()` 在 Linux **直接返回空表**，界面上两个下拉是空的，不是控件坏了。现在走三层拼装（新增 `screencast/pulse.py`，148 行）：`pactl --format=json list sources` 作权威清单、可读名从 **`pw-dump` 的 `node.description`** 补（PipeWire 在 pactl 的 JSON 里把 `description` 写成字符串 `"(null)"`，只信 pactl 就得到一排 `(null)`）、`pactl info` 的 Default Source/Sink 决定「（当前出声）」标在谁身上。系统声音 = `<sink>.monitor`。
- **打包接线三层，每一层都真的执行一次**：① CI 新步骤 `Build Wayland capture helper (Linux)`（`build.yml:259-280`）在 `linux` 矩阵里 `cargo build --release --locked` → 拷进 `src-tauri/hancast-wlgrab` → `ldd` → **`env -u WAYLAND_DISPLAY` 真 exec 一次 `list`**，要求退出码 **3** 且输出含「连不上 Wayland」（能干净地拒绝就证明二进制可执行、动态库齐全）；② `scripts/check-resources.cjs:223` 新增 `checkLinuxWlgrab()`：helper 必须是**带执行位的文件**且 `tauri.linux.conf.json` 的 `resources` 列出它（`:31-43`）；③ deb 装端冒烟追加 `test -x /usr/lib/HanCast/hancast-wlgrab`（`build.yml:314-315`）并**第二条协议命令 `list_screen_sources`**，要求返回含 `"capture"`（`build.yml:335-341`）——`wlgrab.py` / `pulse.py` 是函数内延迟 import，Nuitka 漏收时第一条 `get_settings` 照样绿。Arch 容器（`scripts/build-arch-pkg.cjs`）在 `pacman -U` 实装后跑同两条命令。
- **测试**：新增 `hancast-backend/tests/test_screencast_wayland.py`（507 行，**30 条**）覆盖 Wayland 门（不在会话 / helper 探不到 / 起不来 / 帧尺寸非法四种失败文案各一条）、`HDR` 优先于 mode、rawvideo 命令形状、双路 `pulse` + `amix`、monitor 与麦克风的静音与缺失判定。生产侧 `_spawn(cmd, use_stdin=…, stdin_stream=…)` 比测试桩宽，`tests/test_screencast.py:398,523` 与 `tests/test_params_commands.py:97` 的 `fake_spawn` 签名同步加宽；两处早期新用例先失败，查明是**生产代码把逐候选死因吞了**，改的是 `wlgrab.py` 而不是放松断言。本轮裁剪跑：`uv run python -m pytest tests/test_screencast_wayland.py tests/test_screencast.py tests/test_params_commands.py` → **108 passed**（未跑全量，按既定口径）。
- **验收判据（真机线，打 tag 之前做完）**：新增仓库脚本 `tools/verify-linux-wayland-cast.py`（368 行），按**产品代码路径** `FfmpegCapture.start_hls` 真起一次会话 → `PASS=14 FAIL=0 SKIP=0`（2026-10-18 00:07，CachyOS 真机）：helper 可用、`wl:DP-2`/`wl:DP-3` 在列、`desktop` 标题如实写主显示器、麦克风 3 条 + 系统声音 3 条 monitor、rawvideo 命令形状、双路 pulse + amix、**分片亮度 95.1 / 105.5（多次落在 59.6–105.5，对照修复前 16.03）**、**10/10 分片带 aac 流**、播测试音时 monitor RMS **−16.3 dB**（空闲 HDMI monitor **−91 dB**）、停止后无残留 `hancast-wlgrab` 进程、`running` 归位。两条判据坑写进脚本与文档：量亮度**不能拿 `seg000.ts`**（`delete_segments` + `hls_list_size=6` 早裁掉了，得在会话存活期间抄当下完整分片）；`astats` 的 `RMS level dB` 行带 `[Parsed_astats_0 @ 0x…] ` 前缀，`startswith` 永远匹配不到。探针 `HOME`/`XDG_*` 全指一次性沙箱，**不写用户真实的 `hancast_setting.json`**、不启停 GUI。
- **文档同轮修订**：AGENTS.md（技术栈屏幕抓取行、结构树 `linux-capture/` + `wlgrab.py` + `pulse.py`、Python 服务表新增 hancast-wlgrab 行、构建顺序 4b 步与手工准备第 3 条、Linux `bundle.resources` 清单、CI「Linux 装端冒烟」扩写 + 「CI 证明不了画面」警示、最后校对 v2.3.19）；docs/故障排查.md（§2 症状索引两行 + 新增 **§6.7**，含对照表与两条探针坑）；docs/屏幕投屏.md（§3.4 / §4.6 / §7.12–§7.14 机制与失败文案）；docs/构建与发布.md（§2.2 构建顺序加 helper 一步、§3.2 资源表加 helper 行、§3.3 Preflight 加第 7/8 条、§4.2 步骤表加 **13b** 并重锚 14–27、§4.3–§4.5 与 §7.1 逐条重锚、新增 **§6.8**）；tools/README.md（计数 49→50 / 55→56、§1.1 与 §2.1 各一行）。本轮另一件必须记账的事：`.github/workflows/build.yml` 因为插入 helper 步骤整体长了 **47 行**，`tools/audit-doc-refs.py` **只验「文件存在 + 行号在范围内」**，四十来条 `build.yml:NNN` 引用因此「行号还在、指的行已经不是那回事」而静默通过——已用 `difflib` 对 `git show HEAD:` 与工作区做逐块对齐后全部重锚。
- **版本与契约点**：`npm run version:set -- 2.3.19` 刷 7 处（`tauri.conf.json` / `Cargo.toml` / `Cargo.lock` / `pyproject.toml` / `config.py` / `commands.ts` / `settings.ts`），`hancast-backend/uv.lock` 的 editable 版本随 `uv sync` 跟到 2.3.19（非 SSOT 契约点，如实记录）。
- **没闭合的口子（别当成已验）**：① **Linux 的窗口级抓取仍未实现**——Wayland 协议不给窗口级 screencopy，screencopy 的单位是 output；X11 会话下的单显示器选择同样未做。② CI 三条新判据（helper 自检退出码 3 / Preflight / 装端 `capture` 字段）在 tag run 之前**只有本机 cargo 那一步真跑过**，构建机没有 Wayland 会话，画面判据只能由真机线提供，两边都要有。③ 往客厅电视推流属打扰面，本轮验到「浏览器观看页与 HLS 有画面有声」为止。④ Rust helper 与 `main.rs` 之外的 Rust 侧改动由 CI 三平台构建代验（本机无完整 Tauri 工具链）。⑤ 纯 ALSA（无 `pactl`）的机器仍拿不到音频设备列表——此时如实报「没有 pactl」而不是给空下拉。

---

## v2.3.18 — 2026-10-17（本轮发布提交）

Linux 装端第二轮，只治一件事：**v2.3.17 让窗口活下来了，但界面会「漏桌面」**。用户可感知面：Wayland（Hyprland）下鼠标在界面上划过之后不再出现透明洞，整窗内容始终不透明；Windows / macOS 的毛玻璃观感与行为不变。

- **症状与量化判据**：窗口正常映射、能点能拖，但光标经过的条带变成透明，露出后面的桌面（Hyprland 还带模糊）。判据不靠眼睛——`grim` 抓窗口矩形 → 杀进程 → 同矩形再抓一张裸桌面 → `ffmpeg -pix_fmt rgba` 转 raw → 逐行求平均绝对差。v2.3.17 装机版实测 800×694 的窗口扫一遍光标后 **353/694 行与裸桌面一致**（含 198–287、615–680 两条整带），四次复跑落在 **25%–51%**。
- **根因是 v2.3.17 那条兜底的连带后果**：`WEBKIT_DISABLE_DMABUF_RENDERER=1` 让 WebKitGTK 从 dmabuf 退回**共享图像（SHM）路径**，透明（ARGB）窗口在这条路径上会漏提交 damage 矩形——没提交的那块 surface 保持 alpha=0，合成器直接把桌面透出来。上游 [tauri#14924](https://github.com/tauri-apps/tauri/issues/14924)（记录时仍 OPEN）是同一现象（"persistent ghost pixels from stale SHM buffer regions"）并写明「**The opaque settings window has no issues**」；同一台真机叠加 `WEBKIT_DISABLE_COMPOSITING_MODE=1` 无效，`GSK_RENDERER=cairo`/`gl`、`LIBGL_ALWAYS_SOFTWARE=1` 只治「启动即没」、不治漏提交。**结论：这条环境变量不能撤（撤了窗口起不来），那就只能让窗口不透明。**
- **修法只动窗口属性**：`src-tauri/tauri.linux.conf.json` 补一份**完整**的 `app.windows[0]`（Tauri 平台合并是 RFC 7386，数组整体替换，只写 `{transparent:false}` 等于把 `minWidth`、`dragDropEnabled` 全丢掉），`transparent:false` + 不透明 `backgroundColor` `{22,20,50,255}`。取色理由：暗色主题 `--bg-primary` 是 `rgba(22,20,50,.95)`，取它的等色，所以暗色主题像素级不变；亮色主题下 95% 白叠它是 `(243,242,247)`，与纯白差 12/13/8 个 RGB 单位。`wry` 侧走 `webkitgtk/mod.rs` 的非透明分支 `webview.set_background_color(...)`，`tauri-runtime-wry` 把 config 的 `background_color` 透下去。**Rust、Vue、Sidecar 一行没改。**
- **判据脚本入库**：这轮的一次性手搓探针封成 **`tools/verify-linux-window-opacity.sh`**（自动现读 `XDG_RUNTIME_DIR`/`WAYLAND_DISPLAY`/`HYPRLAND_INSTANCE_SIGNATURE`，按 `class` 从 `hyprctl clients -j` 取矩形，Lua 蛇形扫光标，grim 成对截图，逐行差分，最差一轮占比超 `--max-pct` 即 FAIL）。真机自跑 `--sweeps 2 --workspace auto` → 切到空 workspace 17、窗口 800×900、**两轮各 0/900 = 0%**、`PASS` 退出 0，结束把视图还回原工作区。三条现场纪律写进脚本与文档：装机前 `db.lck` 被他的 `yay -S nyaterm-bin` 占着（21:39 LOCKED → 21:46 FREE），等它自然释放、不删锁不杀事务；**启动前先杀掉已在跑的实例**（Tauri single-instance 让第二个进程只去聚焦旧窗口，不杀就会量到旧窗口所在的工作区）；**背景必须裸桌面**（同一 workspace 上有别的窗口压住矩形就 `exit 2`，不拿它当背景量出假 PASS）；**绝不用 `hl.dsp.window.move`**——它作用于「当前焦点窗口」，本轮取证时误把他正在用的 DSH 窗口挪到别的工作区，已切回原工作区把它搬回（平铺布局随窗口增删自行重排，无数据损失），正确做法是先 `hl.dsp.focus({workspace=N})` 切视图再启动。索引见 tools/README.md §1.1/§2.1/§2.2。
- **顺带排除两个假因**（都留了证据，别照着改）：① 「界面完全显示不全」不是布局裁切——346×472 与 716×472 两种尺寸截图核对过首页，`.device-list__items` 自带 `overflow-y:auto`，设备列表可滚、`解析 / 准备` 可达；② 那条「毛玻璃」不是 CSS 的 `backdrop-filter`——`.media-input__overlay` 只在真拖拽时 `v-if` 存在，`:hover` 没有任何规则把卡片调透。
- **真机装机验收（同一台 CachyOS + Hyprland 0.56.2 / webkit2gtk-4.1 2.54.1）**：`pacman -U` 从 `2.3.17-1` 升到 `2.3.18-1`，`-Qkk` → **115 全部文件 / 0 变化的文件**；同一条光标扫描探针**三次复跑都是 0/694 = 0%**（基线 353/694）；`hyprctl clients` → `class HanCast`、`mapped: 1`；日志里 `Error 71` / `断开的管道` / `Some(127)` 各 **0**，`Found sidecar: "/usr/lib/HanCast/hancast-sidecar"`、`SSDP sock_list count: 1`、抓到 `TCL 85T8G Max-21C9(192.168.1.13)` 与本机 Mac 实例、`Found system MPV: /usr/bin/mpv` → `MPV IPC connected OK`。截图核对整窗无一处露桌面。完整数字与取证方法见 [docs/构建与发布.md](docs/构建与发布.md) §6.7 的「v2.3.18 透明洞复测」。
- **CI 与产物两层各跑一遍**：分支构建 run `39030775070`（手动 dispatch）四条 job 全 `success`、`Upload to draft Release` 按设计 `skipped`，Arch 产物 51,427,440 字节（md5 `f012ac031f88bfcba9f9070dd3c5ddc3`）；**tag `v2.3.18` 构建 run `39035731337`** 五条 job（含 `Upload to draft Release`）全 `success`，draft Release `415822424` 资产 10 项，Arch 资产 `hancast-2.3.18-1-x86_64.pkg.tar.zst` **51,427,471 字节**、sha256 `40766e7fbbfd2884c8ffcfd46c64924bed3d45598101b042b82ae46125f759a0`（与 release `digest` 逐字一致）、md5 `d908727b88990cc462c7e1bb8bcfa19d`，scp 到真机后 md5 一致 → `pacman -U` → `-Qkk` **115 / 0**、`/usr/bin` 无 sidecar、`hancast-sidecar` 与 `libpython3.12.so.1.0` 同在 `/usr/lib/HanCast/`。装端复测用的是**tag 那一份**：`tools/verify-linux-window-opacity.sh --sweeps 3 --workspace auto` → **三轮各 0/900 = 0%**、`PASS`；老判据同轮 `Error 71` / `断开的管道` / `Some(127)` / `error while loading shared libraries` 各 **0**，`SSDP sock_list count: 1`、`Found system MPV: /usr/bin/mpv` → `MPV IPC connected OK`。`Tauri bundle` 步骤能过本身就是证据——`build.rs` 会解析并合并 `tauri.linux.conf.json`，字段名写错这份配置就白改。
- **文档同轮修订**：AGENTS.md（「窗口配置」表加 Linux 覆盖行 + RFC 7386 数组整体替换警告、Wayland 段加透明洞连带后果、最后校对 v2.3.18）；docs/故障排查.md（症状索引加一行 + 新增 **§8.4** + §10.6 加指向 §8.4 的警告 + `main.rs:21-28`→`main.rs:29-36` 重锚）；docs/构建与发布.md（§6.7 加 v2.3.18 复测表、`main.rs` 行号重锚）；docs/HanCast-Frontend-Spec.md（`transparent` 行标注 Linux 例外）；tools/README.md（新脚本登记进 §1.1 分类表 + §2.1 破坏性清单 + §2.2 产物层清单，计数 48→49 条目、`git ls-files tools/` 54→55、顶层脚本 49→50）；AGENTS.md（`tools/` 两处计数 49→50）。全仓 `python3 tools/audit-doc-refs.py`：引用写入后 **3175 处，可疑 0**（本轮落账前 3147）。
- **本轮没做**：① 亮色主题只在真机做了**合成计算**，没做点击核对——`wtype` 这个版本没有鼠标能力（`-m` 是 modifier），`ydotool` 未装，不为一次配色去装输入注入工具；② 随包 mpv 的打包策略（Linux 去随包 / 换静态构建）仍等决策，v2.3.17 的运行时让位照旧生效；③ 往客厅电视真推一路流属打扰面，仍未做。

---

## v2.3.17 — 2026-10-17（本轮发布提交）

Linux 真机（CachyOS + Hyprland）三连崩的修复轮：**装完点图标就没了**这件事从三个独立成因上各修一处，并把「CI 全绿但真机崩」这个盲区补死。用户可感知面：Linux/deb/rpm/AppImage/Arch 五种装端从「双击即没」变成能开窗口，且接收端播放不再对着一个跑不起来的随包 mpv 自报就绪；macOS 与 Windows 行为不变。

- **缺陷一：sidecar 与依赖被装到两个目录 → `exit 127`**。tauri 的 Linux bundler 把 `externalBin` 落 `/usr/bin`、`bundle.resources` 落 `/usr/lib/HanCast`，而 Nuitka standalone 只从**可执行文件所在目录**解析依赖（`libpython3.12.so.1.0` 走 `DT_NEEDED` + RPATH `$ORIGIN`，`lxml/etree.so` 走 Python 层按 exe 同级拼出的绝对路径）。真机取证：`/usr/bin/hancast-sidecar: error while loading shared libraries: libpython3.12.so.1.0`，主程序日志 `[SidecarManager] Process terminated with code Some(127)`。修法只动打包层：`src-tauri/tauri.linux.conf.json` 把 `externalBin` 置空、`hancast-sidecar` 作为 `resources` 首项，`flatten_dependencies(..., copy_executable=True)`（`build_sidecar.py:53,122`）在 Linux 上连 exe 一起提升到 `src-tauri/` 根，安装后 exe 与依赖同在 `/usr/lib/HanCast/`。**Rust 一行没改**：`sidecar.rs:186` 的 `search_dirs` 首选 `resource_dir`，命中即与 `$ORIGIN` 同目录；`current_dir` 那层（`:205,210`）本来就对，只是 `cwd` 救不了 `$ORIGIN`。本机把 CI 出的 2.3.17 产物 `bsdtar -x` 解出来喂 `{"id":1,"cmd":"get_settings","params":{}}` 拿到 `{"id": 1, "success": true, ... "version": "2.3.17" ...}`。
- **缺陷二：Wayland 下窗口起不来 → 协议错误秒杀客户端**。WebKitGTK 2.4x 用 `wp_linux_drm_syncobj_*` 做显式同步，Hyprland 要求 commit 前绑定 acquire timeline，没绑就判违约断连；用户侧表现为「图标点一下，闪都没闪就没」，stderr 只留 `Gdk-Message: Error 71 (协议错误) dispatching to Wayland display.`。2026-10-17 在 gtk4 4.24.1 / webkit2gtk-4.1 2.54.1 / hyprland 0.56.2 上实测 `GSK_RENDERER=cairo`、`GSK_RENDERER=gl`、`LIBGL_ALWAYS_SOFTWARE=1` **全都照崩**，只有 `WEBKIT_DISABLE_DMABUF_RENDERER=1` 稳定存活且仍是原生 Wayland 表面（不必把用户推去 XWayland）。修法：`src-tauri/src/main.rs:21-28` 在 GTK/WebKit 初始化前补该变量，仅当 `WAYLAND_DISPLAY` 存在**且用户自己没设过**时生效（保留手动调试的口子）。判据同机实测：`timeout 20 /usr/bin/HanCast` 修复前 exit **1**、带变量 exit **124**（活到超时）。
- **缺陷三：随包 mpv 在 Arch 上根本跑不起来，却被当成就绪**。`ldd /usr/lib/HanCast/mpv/mpv` 报 **13 条 not found**（`libavcodec.so.60`、`libavdevice.so.60`、`libavfilter.so.9`、`libavformat.so.60`、`libavutil.so.58`、`libbluray.so.2`、`libcdio.so.19`、`liblua5.2.so.0`、`libmujs.so.3`、`libplacebo.so.338`、`librubberband.so.2`、`libswresample.so.4`、`libswscale.so.7`；第一次只 `grep | head` 数出 10 条，`wc -l` 复核更正为 13）——那是 CI 的 **Ubuntu runner** 构建，滚动发行版早已是 ffmpeg 7.x/8.x 的 soname。而 `utils/mpv_manager.py` 第 2 层（随包）**从不验能跑**，直接把路径写成 `READY` 并 `_save_to_config`，于是第 3 层「系统 PATH」永远走不到，装了可用 mpv 的机器反而没播放器。修法取运行时兜底（与 v2.3.11 的 FFmpeg 同一思路）：`:56-63` 对随包候选先真跑一次 `--version`，失败就带 warning 让位系统 mpv。测试 `tests/test_mpv_bundled_path.py` 加三条（能跑时 bundled 仍优先 / 跑不起来落 system / 两者皆无才 NOT_FOUND），`20 passed`（含 `test_params.py`）。打包侧**没动**：既不随包静态 mpv，也不在 Linux 去掉 mpv——同一台机器 `which mpv` 是 **v0.41.0**，与 `env.MPV_TAG` 同版本，说明「让位」这条路是真能落地的。
- **CI 从此查运行时，而不查「文件在不在包里」**：v2.3.16 的四条装端判据全绿却漏掉 127，根因是它们全是存在性断言，而缺陷是**位置关系**。本轮三层补齐——① `scripts/check-resources.cjs:179` `checkLinuxSidecarLayout()`：Linux 上 sidecar exe 必须在 `resources` 里、且不允许留在 `externalBin`；② `build.yml:263-296` 新步骤「Smoke test: sidecar inside deb (Linux)」：`dpkg-deb -x` 解包 → 硬失败若 `/usr/bin/hancast-sidecar` 复现 → `test -x /usr/lib/HanCast/hancast-sidecar` + `libpython3.12.so.1.0` 在场 → **cd 进去真跑一条 `get_settings`**，要求 stdout 含 `"success": true`、stderr 不含 `error while loading shared libraries` / `ImportError` / `ModuleNotFoundError`；③ `scripts/build-arch-pkg.cjs` 的容器验收在 `pacman -U` 实装之后追加同一条协议断言。rpm/AppImage 与 deb 共用同一份 `resources`，布局相同，故解 deb 验一次。
- **`.gitignore` 补齐提升产物**（`git check-ignore` 实测证明此前**没被忽略**）：`src-tauri/*.so`、`*.so.*`、`*.dylib`、`src-tauri/curl_cffi/`（`:57-61, 66`）。`src-tauri/hancast-sidecar` 那行**去掉尾斜杠**（`:70`）——Windows/macOS 上它是 Nuitka dist 目录，Linux 上是提升出来的单体可执行文件，带斜杠只匹配目录，Linux 根产物会露在 `git status` 里。
- **上一轮的两个口子本轮闭合**：v2.3.16 的「① `-Qkk` 不含 GUI 真启动，本轮没有 Arch 实机」——现在有实机、而且是**装机的**实机（`pacman -U` 升级后窗口 `mapped: 1`，判据见下方装机验收）；「② 随包 mpv 的 soname 遗留」降级为「不再谎报就绪」（横幅同步改进 docs/构建与发布.md §6.6）。
- **文档同轮修订**：AGENTS.md（技术栈 MPV 行、MPV Renderer 行加定位顺序与验跑、`bundle.resources` 逐平台清单更正、依赖提升流程改写、运行目录树与「certifi/lxml/charset_normalizer 不在运行目录」那条**已证伪**的旧结论更正为目录形态、Wayland 兼容行与专门段落、构建顺序第 5/7 步、CI 加「Linux 装端冒烟」、最后校对 v2.3.17）；docs/构建与发布.md（§2.2 构建顺序按平台分叉、§2.3 Nuitka 参数与产物表 + 「Linux 为什么改产出目录」、§4.2 步骤表新增 15b 并重锚后续行号、§6.6 加协议判据行、新增 **§6.7 Linux 装端真机取证**九行实测、**装机验收**一节八行 + 真机取证操作纪律、§7 遗留横幅更新、`.gitignore` 表行号）；docs/故障排查.md（症状索引三行 + 新增 **§10.5 / §10.6 / §10.7** + 关键字表三行）；docs/架构总览.md（`mpv_manager.py` 行：205→214、定位顺序与让位）；docs/屏幕投屏.md、rule-packs/README.md（`build.yml` 行号因新步骤整体后移 43 行，逐条重锚）。全仓 `python3 tools/audit-doc-refs.py`：本轮 docs 落账（CI/产物层 + 装机验收两笔）引用写入后 **3142 处，可疑 0**（本轮落账前 3100）。
- **版本与契约点**：`npm run version:set -- 2.3.17` 刷 7 处（`tauri.conf.json` / `Cargo.toml` / `Cargo.lock` / `pyproject.toml` / `config.py` / `commands.ts` / `settings.ts`），`hancast-backend/uv.lock` 里的 editable 版本随 `uv sync` 跟到 2.3.17（非 SSOT 契约点，如实记录）。闸门 `npm run version:sync` 复跑七处全「已是最新」。
- **落账：CI 与真机两侧实测（打 tag 之前做完）**。run **39022840537**（手动 dispatch 到 `feat/js-rule-plugins`，2026-10-17）`Resolve version` / `windows-x64` / `macos-arm64` / `linux-x64` 四条全 `success`，`Upload to draft Release` 按设计 `skipped`。三层新防回归各自第一次执行就绿：Preflight `ok hancast-sidecar -> 1 file(s)`、`*.so -> 4`、`*.so.* -> 1`、`hancast_sidecar/**/* -> 56`、合计 **75 个文件**，布局校验 `ok hancast-sidecar -> exe (36.0 MiB, mode 755)`；deb 冒烟解包后 `/usr/bin/hancast-sidecar` 不存在、`get_settings` 回 `{"id": 1, "success": true, ... "version": "2.3.17"}` → `deb sidecar OK`；Arch 容器出包 `hancast-2.3.17-1-x86_64.pkg.tar.zst` **51,427,251 字节**（49.0 MiB），四条依赖命中、`pacman -U` 真装 **124 包 / 164.37 MiB / 861.84 MiB**、`-Qkk` **115 total files, 0 altered files**、`-Qo` 归属 `hancast 2.3.17-1`、容器内 sidecar `ready 事件已推送` + `应答成功`、三个关键文件 `root:root`、`pacman -R clean`。真机（CachyOS）侧把该产物 `bsdtar -x` 解到 `/tmp` 后**不装机直接跑**：布局三条 ok（该层 12 项），`get_settings` 应答带 `"version": "2.3.17"`、stderr 61 行里 shared-lib / ImportError / Traceback **0 次**；`WAYLAND_DISPLAY=wayland-1 timeout 30 .../usr/bin/HanCast` → **exit 124**、`Error 71` **0 次**、`断开的管道` **0 次**、`resource_dir` 与 `Found sidecar` 同层、`MPV IPC connected OK`；`check_mpv` → `{"status":"ready","path":"/usr/bin/mpv","source":"system"}`（随包那份被 `--version` 拒掉并让位）。对照：CI 的 deb 冒烟里是 `"source": "bundled"`——构建机上随包 mpv 本来就跑得起来，不构成反例。数字更正一处：随包 mpv 的缺失 soname 首轮记成 **10 条**（`grep | head` 截断），`wc -l` 复核为 **13 条**，AGENTS.md / 故障排查 §10.7 / 构建与发布 §6.7 同轮改掉。
- **装机验收（tag 之后，同一台 CachyOS 真 `pacman -U`）**：包从 tag run 39025264710 的 artifact 拉取（**51,427,252 字节**，md5 `38a093f00b970bd0ae4618b03bd59f9b`；与分支 run 的 51,427,251 差 1 字节，仍是 `zstd -T0` 非确定性），scp 后 md5 一致。`sudo pacman -U --noconfirm` 从 `hancast 2.3.16-1` 升级成功（`全部安装大小 147.52 MiB / 净更新 0.01 MiB`，依赖早在场；snapper 钩子快照 173→174）；`pacman -Q hancast` → `2.3.17-1`，`-Qkk` → **115 全部文件，0 变化的文件**；**旧的半份产物 `/usr/bin/hancast-sidecar`（38M）随升级消失**，`/usr/lib/HanCast/` 12 项与 exe 同级。装端真跑：`timeout 30/40/45 /usr/bin/HanCast` 三次全 **exit 124**，`Error 71` / `断开的管道` / `Process terminated with code Some(127)` **各 0 次**；`hyprctl clients` 里 `Window … -> HanCast: mapped: 1 / hidden: 0`（日志 `窗口尺寸: 630x472 px`，读的是他的 `window_config.json`）——「点图标什么都没发生」在装机路径上确实是没了。mpv 与发现同机成立：`Bundled MPV exists but cannot run, fall back to system` → `Found system MPV: /usr/bin/mpv` → `MPV IPC connected OK`；`SSDP started … sock_list count: 1` 并 `Found device via M-SEARCH: TCL 85T8G Max-21C9(192.168.1.13:16418)`。**tag 那一次 CI 逐条复现**（四条 success + `Upload to draft Release` 执行），draft `v2.3.17` 资产实测 **10 项**、第一行就是这个 Arch 包，`build.yml:644` 的 tag-gated glob 连续两版命中。过程插曲：第一次想装时被 `/var/lib/pacman/db.lck` 挡住（他自己的 `yay -Syyu` 跑了 55 分钟正在升级系统），**没删锁也没杀他的事务**，改用解包直跑先拿产物层证据；他升级完机器 18:28 重启（`/tmp` 是 tmpfs，解包目录随之消失，两段测量互相独立），锁自然释放后才装机。**装机后重启复查（19:19）**：在同一个 Hyprland 会话里重跑 `/usr/bin/HanCast` → `mapped: 1` / `hidden: 0`、`SSDP bound to port 1900` + `sock_list count: 1`（先前那次 `Errno 98` 的 AppImage 已在场外）、`Found system MPV: /usr/bin/mpv` → `MPV IPC connected OK`（`ps -C mpv` 证实跑的就是系统那份）、三条崩溃特征各 **0**。同一次复查顺手清掉一处**我自己留下的污染**：重启前那次裸 sidecar 探针把 `/tmp/hancast-v2317-layout-probe/mpv/mpv`（`source="bundled"`）写进了他的 `~/.config/hancast/hancast_setting.json`，而 Sidecar 命令表里没有清除该键的入口，只能备份后手改 JSON——操作纪律写进 docs/构建与发布.md 的装机验收节。
- **没闭合的口子（别当成已验）**：① **真实投屏播放没在 Linux 装端跑**——往客厅电视推流属打扰面，本轮只验到「装端能抓到那台电视」（§6.3 的完整链路留给下一次或他自己点）。② 那次解包运行里 `SSDP bind error: [Errno 98]`（1900 被本机另一个 HanCast AppImage 占用）是环境冲突，装机运行已 `sock_list count: 1`。③ Rust 侧只改 `main.rs`，本机无完整 Tauri 工具链，编译由 CI 三平台构建代验（两轮全绿）。④ Arch 用户若系统里也没有 mpv，接收端仍不可用——只是从「谎报就绪」变成如实 `NOT_FOUND`。⑤ 随包 mpv 的打包层未动（静态 mpv / Linux 去捆绑两条更重的路留给决策）。⑥ 产品未变的两台 Windows 测试机按惯例豁免。

---

## v2.3.16 — 2026-10-16（本轮发布提交）

GitHub CI 现在能打出 Arch 装得上的包：Linux 矩阵项在 tauri bundle 之后补一步，产出 `hancast-<版本>-1-x86_64.pkg.tar.zst` 并在同一次容器调用里真装真验。产品行为零变化（非文档 diff 只有 `scripts/build-arch-pkg.cjs`、`.github/workflows/build.yml` 与 8 处契约点的版本串）。

- **为什么必须后处理**：tauri-bundler 根本没有 Arch 后端——上游 crate 的 `src/bundle/linux/` 下只有 `appimage/`、`debian.rs`、`rpm.rs`（不在本仓，`build.yml:420-422` 注释记录了这个事实），所以 `--bundles deb,rpm,appimage` 里**加不出** pacman 认的格式。补的步骤排在 `Tauri bundle` 之后（`build.yml:426-428`，`if: matrix.runner-os == 'Linux'`），**故意不加 `continue-on-error`**：这一步静默失败等于 Linux 少一个格式，用户只能去翻 AUR，宁可让 CI 红。
- **输入直接复用 deb**：不重拼安装树，解 `bundle/deb/HanCast_<ver>_amd64.deb` 里的 `data.tar.gz`（那就是 tauri `debian::generate_data` 的产物）进 `$pkgdir`（`build-arch-pkg.cjs:162-163`）。自己按 `bundle.resources` 的 glob 重排等于复刻那份布局逻辑，以后改了 glob 不会同步报错，只会打出一个缺文件的包——**deb 对了包就对了**。License 单独 `install -Dm644` 到 `/usr/share/licenses/$pkgname/COPYING`（`arch-pkg` 硬要求，`:168`）。
- **元数据交给 makepkg，不手写**：容器内 `makepkg -f --nodeps --skipchecksums --noextract`（`:217`），`.PKGINFO`/`.MTREE`/`.BUILDINFO` 由 Arch 自己的工具链生成。手写最坏的后果不是装不上（装不上看得见），而是 `pacman -Qkk` 永远报「missing MTREE data」这类**静默劣化**。两个 flag 坑：`--nodeps` 是给构建期依赖检查用的（本包 `package()` 只搬运，gtk/webkit 不在场也正常），装端验不验依赖另有判据；`--noextract` **不会把 source 拷进 `$srcdir`**，所以 `package()` 里显式在 `$srcdir`/`$startdir`/`$SRCDEST` 三处定位 deb（`:151-157`），否则 `bsdtar: Failed to open '/work/src/HanCast.deb'`。
- **依赖名逐条映射，名字靠查不靠猜**：`libgtk-3-0→gtk3`、`libwebkit2gtk-4.1-0→webkit2gtk-4.1`、`libayatana-appindicator3-1→libayatana-appindicator`，再加 Arch 侧要显式写的 `hicolor-icon-theme`（deb 靠 dpkg trigger 隐式满足）（`:69-75`）。照抄 deb 包名是最常见的翻车方式——`libwebkit2gtk-4.1-0` 在 Arch 仓库里不存在，装的那一刻才炸；verify 阶段用 `pacman -Si` 逐条真查（`:232-237`）。
- **非特权构建**：makepkg 拒绝以 root 跑，容器里 `id build || useradd -m -u 1000 build`（`:197`）再 `su - build`；`.PKGINFO` 里的 `packager` 靠 `~/.makepkg.conf` 的 `PACKAGER`，不设就是 `Unknown Packager`；压缩档 `COMPRESSZSTD=(zstd -T0 -19)`（`:210`，与官方包同级，`ARCH_ZSTD_LEVEL=12` 可降做本地冒烟）。
- **验收内建在同一条命令**（`--no-verify` 才跳；本地与 CI 同一入口，判据表见 [docs/构建与发布.md](docs/构建与发布.md) §6.6）。**CI run 38923996592 / linux-x64 全绿实测**：出包 `hancast-2.3.16-1-x86_64.pkg.tar.zst` **51,418,890 字节（49.0 MiB）**，`.PKGINFO` 记 `pkgver = 2.3.16-1`、`size = 154,682,352`、`license = GPL-3.0-or-later`、`depend` 四条、`packager = HanCast CI <lanzeweie@foxmail.com>`、makepkg 7.1.0 + fakeroot 1.37.2；`pacman -Si` 四条依赖全命中（gtk3 `1:3.24.52-2`、webkit2gtk-4.1 `2.54.1-1`、libayatana-appindicator `0.6.1-1`、hicolor-icon-theme `0.18-1`）；`pacman -U` **真装**：**124 个包、下载 164.36 MiB、装完 861.84 MiB、约 11 秒**；`pacman -Qkk hancast` → **115 total files, 0 altered files**（`.MTREE` 与落盘字节一致）；`/usr/bin/HanCast`、`/usr/bin/hancast-sidecar`、`/usr/lib/HanCast/mpv/mpv` 属主都是 `root:root`（`:259` 断言）；四条存在性 ok（sidecar 可执行、捆绑 mpv、`.desktop`、license）；`pacman -Rn hancast` → `ok pacman -R clean`。产物落 `bundle/arch/`，随 `dist/` 进合一 artifact `bundle-HanCast-2.3.16-linux-x64`（354,334,288 字节）与拆分 artifact `HanCast-2.3.16-linux-x64.pkg.tar.zst`（zip 51,369,933 字节），release 上传 glob `artifacts/**/*.pkg.tar.zst`（`build.yml:601`）。
- **`pacman -U --nodeps` 是个假开关（实测更正）**：本以为能跳过依赖解析，pacman 7.1 的 `-U` 照旧 resolve 并安装 124 个包。既然跳不过就去掉 flag，把这件事**当证据用**——Depends 不但名字存在，还真的能被解析并装进系统（`:27-30,240-241`）。此前所有「不会拉依赖」的注释与文档措辞同轮改掉。
- **第一次 CI 失败是清理 bug，不是包**：run 38919966019 出包与装端四条全绿，红的在最后一行——宿主侧 `EACCES: scandir '/tmp/hancast-arch-2.3.16-XXXXXX'`。容器里为了跑 makepkg 把 bind mount 的暂存目录 chown 给了 uid 1000，而 runner 上的宿主 uid 不是 1000，**脚本删不掉自己造的目录**。修法：`docker run` 传 `-e HOST_UID/-e HOST_GID`（`:325`），容器内 `trap restore EXIT` 把属主还原并放开读权限（`:198-202`）；挂 EXIT trap 而不是只放在成功路径，是因为构建失败时同样得把目录还给宿主，否则连失败诊断都读不到。本地按同一路径复现：失败退出后暂存目录是 `drwxr-xr-x pavia wheel`，可读可删；复跑即绿。
- **本地能验到哪一步（Rosetta 天花板，别把本机绿色当成装过）**：Apple Silicon 上这是 x86_64 容器走模拟，pacman 7 的下载沙箱固定报 `error restricting syscalls via seccomp: 22` → `switching to sandbox user 'alpm' failed`，`pacman -Sy` 第一步就失败；`--security-opt seccomp=unconfined`（`:321-322`）解不开——模拟层不允许在已有过滤器上再收窄，不是 Docker profile 的限制，而 `archlinux/archlinux` 也没有 arm64 变体可绕。所以本机只验到出包与归档层：v2.3.15 的 deb 出包 51,420,759 字节、`.PKGINFO`/`.MTREE`/`.BUILDINFO` 齐、归档内 118 个条目记录的属主全是 `root:root`、`.MTREE` 83 个带摘要条目抽检 5 个（HanCast / hancast-sidecar / mpv / `.desktop` / COPYING）SHA-256 全中，包内清单减 MTREE 清单为空。**同一版本本机与 CI 的字节数差在 KB 级**（本机 51,420,790 vs CI 51,418,890）：`zstd -T0` 的多线程分块随核数变，不是内容差异。
- **镜像选型两处实测**：默认 `ghcr.io/archlinux/archlinux:base-devel`（`:40-48`）。① ghcr 而非 docker.io：同名 tag 内容逐字节相同（config digest `sha256:689ecdd6…`，层 `db96cd089ee2` + `db0a66162ecf`，2026-10-16 核对 manifest），但 docker.io 对匿名拉取有配额，共享 runner 出口 IP 撞上限会**连带整条 Linux 构建失败**；② base-devel 而非 `:latest`：makepkg 要 fakeroot，fakeroot 在 base-devel 里，纯 latest 进来先得 `pacman -Sy base-devel`（多一次联网、多一个失败面），脚本仍保留缺 fakeroot 时现补的兜底（`:180-183`），所以 `--image …:latest` 也能跑。换源走 `ARCH_PKG_IMAGE`（环境变量优先，CI 一处改全局）或 `--image`。
- **临时落点守 v2.3.15 政策**：即抛的暂存目录走**显式** `/tmp` 路径（Windows 才落 `os.tmpdir()`）并由脚本自清（`:288-293,347-348`），不留 `--keep-stage` 之外的第三处落点。本轮另清掉上一轮尺寸调查留下的临时物：`/tmp/hc-arch/` 的三个镜像 tarball（721 MB → 151 MB）与 brew 装的 `squashfs` 4.7.5（`unsquashfs` 只为读那个 AppImage 而装，无其他 formula 依赖）。
- **文档同轮修订**：AGENTS.md（`scripts/` 清单加 `build-arch-pkg`、完整构建顺序多出 Linux 专属第 7 步、CI 矩阵 Linux 产物加 `.pkg.tar.zst`、最后校对 v2.3.16）；README.md / README_en.md（badge 加 `pkg.tar.zst`，下载段点名 Arch 产物名与 `sudo pacman -U <包文件>` 装法）；docs/构建与发布.md（§4.1「无 Arch 后端」事实 + 镜像选型、§4.2 步骤 19 行、§4.3 产物名与 artifact 表、§6.6 四条判据表 + 本机/CI 两段实测）；docs/README.md（索引行加「含 Arch 后处理」，全表行数按实测重刷）。
- **tag 那一次跑复现了全部验收，release glob 也闭合了**：run 38927743540（push tag `v2.3.16`，三平台 + `Upload to draft Release` 全绿）步骤 20 与分支上那次逐条一致——出包 51,418,873 字节（与 run 38923996592 的 51,418,890 差 17 字节，正是 `zstd -T0` 非确定性的量级）、四条 `pacman -Si` 命中、124 包 / 164.36 MiB / 861.84 MiB、`-Qkk` **115 total files, 0 altered files**、`-Qo` 归属本包、三个关键文件 `root:root`、四条存在性 ok、`-Rn` 干净。**draft Release v2.3.16 的资产清单实测 10 项**，第一行就是 `hancast-2.3.16-1-x86_64.pkg.tar.zst` 49.0 MiB（其余：dmg 80.9 / exe 65.3 / deb 51.9 / rpm 51.9 / AppImage 186.8 MiB + 4 个规则包 zip）——tag-gated 的 `artifacts/**/*.pkg.tar.zst` glob（`build.yml:601`）不再是未执行过的假设。
- **没闭合的口子（别当成已验）**：① `-Qkk` 验的是文件字节与属主，**不含 GUI 真启动**：装完在 Arch 真机上开起来、托盘与 WebKit 渲染是否正常，本轮没有 Arch 实机。② 捆绑的 `usr/lib/HanCast/mpv/mpv` 是把构建机 apt 那份**二进制单拷**进包的（`build.yml:191-207` 不拷它动态依赖的 `libav*`），soname 不同的发行版上可能起不来——deb/rpm 同病，不是 Arch 引入的回归，§6.6 有横幅。③ 产品二进制本轮未变，`.68` Windows 装机复测按惯例豁免。

---

## v2.3.15 — 2026-10-16（本轮发布提交）

纯工具与文档轮：产品行为零变化（非 `tools/`／文档面的 diff 只有 8 处契约点的版本串，`npm run version:set 2.3.15` 刷入）。主题是把临时文件落点全仓收成二选一（`/tmp` 或仓库根 `.tmp-win/`），另修一处文档体检的存量误报。

- **落点政策（用户定死，不许第三处）**：要留给人复看的证据（截图、探针 dump、失败样本、测试媒体）写 `.tmp-win/`（`.gitignore:106` 忽略；可见、每轮清）；只活在本次运行里的即抛件（chrome profile、测试 HLS 素材、备份目录、scp 中转文件）走 `/tmp` **显式路径**，且必须由脚本自己清（bash `trap`／python `finally`+`atexit`／node 退出前删）。此前一批脚本用裸 `mktemp`（默认落 per-user `$TMPDIR`）或 `tempfile.gettempdir()`，落点随环境变量漂移——`$TMPDIR` 是 `/var/folders/…` 随机路径，用户找不到、系统也不回收，本轮清零。
- **「系统会清」是错误假设（有实测）**：macOS 的 `/tmp` 清扫只吃「>3 天的普通文件 + 空目录」——`.app` 这类目录包要分多轮才啃得动、socket 永不回收，等于不回收（2026-10-15：`install-macos-artifact.sh` 的 184M 回滚包在「系统会自行清扫」的错注释下躺满一天原样；同轮清掉的存量还有 81 个孤儿 mpv（ppid=1、全 idle、约 1.1GB RSS）与 426 个 socket 死文件，`/private/tmp` 306M → 24K）。`install-macos-artifact.sh:44-51` 因此加**装包前自清历轮回滚包、只留本次一份**（`[ "$old" = "$ASIDE" ] && continue` 跳过本次），`:63` 的错注释同轮改掉；三场景实测（无匹配不误杀／多份全清／只剩本次原样）。
- **`tools/` 25 个脚本逐个改**（7 `.sh` + 14 `.py` + 4 `.mjs`；语法校验全过）：
  - **bash**：`mktemp` 显式化——`accept-macos-artifact.sh:116,446-447`（tag 目录与探针文件，用完即删）、`verify-win-screencast-tone-content.sh:38`（trap 自清）、`verify-windows-artifact.sh:51,55,90`（scp 中转列表与两份 ps1，用完即删）；证据件改落 `.tmp-win/`——`probe-dyrs-headers.sh:12-14`、`probe-dyrs-skin.sh:8-10`（每个先 `rm -rf` 再建）；`run-macos-tap-probe.sh:20-22,67` 日志留 `.tmp-win/hancast-tap-probe.log`、临时 `.app` 固定 `/tmp/HanCast Tap Probe.app` 且结尾自清。
  - **python**：`tempfile.gettempdir()` → 字面 `/tmp`（「写完读进内存即删」的即抛件）——`probe-macos-audio-pitch.py:95,179`、`probe-macos-systap.py:207,433`、`verify-proxy-live.py:67`；`mkdtemp()` 补 `dir="/tmp"` 与自清——`share_resolve_probe.py:188`、`verify-github-mirror-live.py:131-132`（配 `atexit`）、`verify-proxy-segment-decodable.py:218,256`、`verify-rule-pack-live.py:86`、`verify-rule-packs-live.py:121`、`verify-browser-segment-live.py:131,215`（setting 备份目录，还原后 rmtree）；证据件改落 `.tmp-win/`——`probe-tls-ja3.py:31,204-205`（TLS 失败样本）、`probe-ts-skin-offset.py:64-66`（剥皮对照 ts）、`probe-window-picker.py:98-101`（`--capture` 抽帧证据，先 rmtree 再 makedirs）、`verify-dlna-stop-chain.py:25`（测试媒体，docstring 同步）。
  - **node/mjs**：`mkdtempSync(path.join(tmpdir(), …))` → `mkdtempSync('/tmp/…-')`（profile 与测试 HLS 即抛件；`verify-error-wrap-ui.mjs:46`、`verify-params-ui.mjs:42`、`verify-watch-page-live.mjs:146`、`verify-watch-page-sound.mjs:174,249`）；截图 5 张改落 `.tmp-win/`（error-wrap `:164-166`、params-ui `:234`、watch-live `:239-241`、watch-sound `:235-237`——落点从脚本自身位置推仓库根，不依赖 cwd）。`tmpdir()` 保留给 puppeteer 缓存目录的兜底读（`process.env.HOME ?? tmpdir()`），不是写点。
- **`tools/audit-doc-refs.py` 修存量误报**：`D:/Temp/x.cmd` 这类盘符路径会被 REF_RE 误捕成仓库相对路径（`:` 不在字符类里，捕获从盘符后的 `/` 开始；`tools/README.md:75-76` 两处，该文件不在默认扫描范围、只在显式指定时才露头，`:73` 那位靠行内历史词侥幸放行）。新增 `WINPATH_RE`（`:62`）先按整段盘符路径划 span，落进 span 的命中放行并计入「运行时」类（`:153,156`；docstring `:8-9` 同步）。修后：显式扫 `tools/README.md` **235 处 0 可疑**（此前 2 处误报），全仓 **3025 处 0 可疑**（3026 → 3025：1 处盘符路径命中转入放行类）。
- **文档同轮修订**：`tools/README.md` 按新落点逐行改 10 处登记行（§1.2 一行、§1.3 四行、§1.5 一行、§1.6 两行、§2 安全表两行），§3.3 新增政策两条（「临时落点二选一」+「别指望系统清扫」）；`.gitignore:106` 注释改为「本地临时工作区（探针脚本、验收截图与产物；不入库）」；`docs/构建与发布.md:650` 表格行同步。
- **验收（本轮真实输出，打 tag 之前做完）**：25 个脚本语法校验全过（7 `bash -n`／14 `py_compile`／4 `node --check`）；`python3 tools/audit-doc-refs.py` → 合计引用 **3025 处、可疑 0 处**（本节落账前的口径；写入本节 33 处 `file:line` 引用后复跑为 **3058 处、可疑 0 处**）；`npm run version:sync` → **2.3.15，更新了 0 个文件**；`node node_modules/vue-tsc/bin/vue-tsc.js --noEmit` **rc=0**（附带发现：本机 `node_modules/.bin/vue-tsc` 无执行权限，`npx vue-tsc` 会「Permission denied」静默没跑成，改用 node 直调）。产品代码零改动，无产物行为变化。
- **没闭合的口子（别当成已验）**：① 25 个脚本没有逐个真跑一遍（多数要真机/网络/特定环境），判据来自逐文件 diff 复核 + 语法校验；运行时取证只覆盖 `install-macos-artifact.sh` 的三场景回滚清理。② `.68` 上跑的 Windows 批处理（`verify-screencast-audio-order.cmd` 等）落 `D:\Temp` 本轮不动——落点政策管的是**本机 macOS** 脚本。③ `$TMPDIR` 的否决理由按本机实测（`/var/folders/…` 随机路径）立论，不追求跨平台严格性。

---

## v2.3.14 — 2026-10-15（本轮发布提交）

两条投诉各修一处：浏览器观看页不再钉死静音（带声起播失败才要用户点一下），Windows 窗口投屏不再走黑帧路径（改按 hwnd 换算矩形抓屏）。

- **观看页声音**（`www/index.html`，214 行）：旧 `<video>` 标签硬编码 `autoplay playsinline muted`——`muted` 是给「自动播放策略只放过免手势静音起播」当保险的，代价是**任何浏览器里都永远没声音**，且脚本里两处 `video.play().catch(function(){})` 把失败吞得无迹可循。现在去掉硬编码，起播统一走 `tryStart()`（`:117-140`）：先试带声 `play()`，被自动播放策略拒绝才 `fallbackToMuted()`（`:110-115`）静音兜底，并亮出悬浮的「点击开启声音」按钮（`:59` 定义、`showVideo()` `:155` 亮出）；按钮点击（`:102-108`）或原生控件解除静音都算手势，`volumechange`（`:95-100`）收到 `!muted` 就收起按钮。两条边角：个别引擎的 `play()` 被拦下时既不 resolve 也不 reject，1.2 秒保险丝（`:132-139`）盯住「已定性/已出画面」两个标志，别让用户干等；已知本浏览器拦带声起播时（重连路径 `:118-121`）直接静音起播，不重复挨拒。`startNative`（`:164`）与 `MANIFEST_PARSED`（`:179-180`）两条起播路径都改走 `tryStart()`。
- **Windows 窗口投屏**（`ffmpeg_manager.py`，1156 行）：窗口源 id 从 `window:<标题>` 改为 `window:0x<hwnd>`（`_list_windows` `:770`），抓取从 gdigrab 的 `-i title=` 窗口模式改为**按 hwnd 换算矩形、抓 desktop 区域**（`build_video_input` 窗口分支 `:1010-1030`）。矩形由 `_window_capture_rect(hwnd)`（`:87-157`）给出：`IsWindow` 验活 → `DWMWA_CLOAKED` 挡掉其它虚拟桌面/隐藏窗口 → 最小化先 `SW_RESTORE` 再等 0.15s → `DWMWA_EXTENDED_FRAME_BOUNDS`(9) 优先、`GetWindowRect` 兜底 → clamp 到虚拟桌面 → 宽高 <80 拒 → 偶数化。窗口没了（已关闭/最小化/异虚拟桌面）抛 `ValueError` 点名「选中的窗口已关闭或不可见（最小化/其它虚拟桌面），请刷新窗口列表重选」（`:1015-1017`）；旧客户端回传的 `window:<标题>` 仍保留 `title=` 兜底（`:1024-1030`，注释里写明正常不会走到）。转换函数 `_parse_window_hwnd`（`:72-84`）对 `desktop`、无 hwnd 的旧格式、`0x0`、带尾巴等 7 种坏格式一律判 `None`。
- **为什么必须换**（.68 真机取证，`tools/verify-win-capture-sources.ps1` 逐变体抓 PNG）：gdigrab 的窗口模式走 `GetDC(hwnd)` + BitBlt，在 DWM/DComp 下对 GPU 合成窗口取不到帧——抖音窗口 `hwnd` 抓 20,864 B（纯白+鼠标指针）vs `region` 抓 1,462,990 B（完整界面）；Vivaldi 13,640 vs 204,463 B；连 Program Manager 都是 46,213 vs 1,780,819 B。区域模式读的是 DWM 合成后的屏幕内容，天然拿到真实画面。抓取参数排在 `-i` 之前（`:1019-1023` 注释：`-framerate` 是 gdigrab 选项，不给就退 1fps 幻灯片）。
- **`-i title=` 还附带一个真 bug**：`FindWindowW` 精确匹配窗口标题，标题一变（浏览器换标签、播放器换曲）就 I/O error 断流——hwnd 形态没有这个问题，标题仅供展示（`:768` 注释）。
- **测试**（`tests/test_screencast.py`，1045 行，+4 条）：`test_parse_window_hwnd`（`:215-221`，含 7 个坏格式判 `None`）、`test_window_hwnd_reaches_region_grab`（`:223-243`，patch 矩形 `(100,50,996,1288)` 后断言 `-offset_x/-offset_y/-video_size/-framerate` 全部到达且排在 `-i` 前、不再出现 `title=`）、`test_window_hwnd_gone_raises`（`:245-256`，窗口没了抛 `ValueError`）、`test_build_video_input_window_legacy_title`（`:208-213`，旧格式在 nt 下保留 `title=记事本` 兜底）。
- **验收（本轮真实输出）**：`uv run python -m pytest tests/test_screencast.py -q` → **72 passed in 5.08s**；`node tools/verify-watch-page-sound.mjs`（真实 Chromium，双 pass）→ **RESULT: ALL PASS**——pass A 最严策略 `document-user-activation-required` 下 11 条断言全 PASS（静音兜底起播 → 按钮亮出 → CDP 合成点击后 `muted=false` → 按钮收起 → 画面持续推进），pass B `no-user-gesture-required` 下 7 条全 PASS（直接带声起播、按钮不出现）；`.68` 抓图四变体对照见上（desktop 1,634,198 B / 副屏区域 1,417,366 B / 三窗口 hwnd vs region 对照）。
- **产物级复测（.68 装机后；闭合原③「Windows 窗口投屏生产链路」）**：`.tmp-win/hc_recheck_214.py`（一次性探针，不入库）走 **stdin/stdout 协议驱动装好的 2.3.14**、不手写 ffmpeg 行，四阶段 **14 PASS / 1 FAIL**，数字全部来自存档输出（会话均为 `target=browser` 经观看页拉流取证；DLNA 与它共用同一条抓取/切片管线，`manager.py:5,86-97`，黑帧的病灶层就是这一层）：
  - 真实 cmd 窗口投屏有画面（产品分片 1920x1006 `YAVG=27.8`，判据 ≥20，窗口抓取在产物里走通）；单屏 desktop 投屏有画面（1680x1080 `YAVG=59.1`），①的「单屏黑屏」未复现；已关闭窗口起流即败、报错正文就是「选中的窗口已关闭或不可见（最小化/其它虚拟桌面），请刷新窗口列表重选」（`ffmpeg_manager.py:1015-1017` 的新文案在产物里跑通）。
  - 三个会话的 HLS 另由 Mac 侧拉流复检有画面（`YAVG` 27.6894 / 133.71 / 59.1，判据=非黑）。
  - 同时刻实锤换算链与多屏：白窗 `GetWindowRect`(40,40)-(940,660) → DWM 扩展边界 (47,40)-(933,653) → 产物 HLS 行 `-offset_x 47 -offset_y 40 -video_size 886x612 -i desktop` 完全一致；.68 主屏 2560x1440 + 右竖屏 1440x2560 → 虚拟桌面 4000x2571（与 [docs/屏幕投屏.md](docs/屏幕投屏.md) §3.3 吻合）。
- **唯一 FAIL 定性「窗口遮挡，非缺陷」**（`.tmp-win/hc_p2_repro.py` 系统级复现，带 z-order 与 `DWMWA_CLOAKED` 取证）：探针白窗（分片 1564x1080 `YAVG=133.7`，阈 ≥150）不是抓取缺陷——投屏前该区域抓屏是干净纯白（`YAVG=233.3`，6,185 B）；随后全屏游戏窗（街霸 6，2560x1440，`cloaked=0` 系真实合成窗）升到白窗之上（z07→z05），三轮抓屏 158.4 / 161.8 / 134.9 随帧变化，产品同时刻分片 `YAVG=120.0` 与同轮屏幕抓屏内容同源（同款游戏 HUD）。gdigrab 区域抓的永远是**屏幕上的那块**（§3.3 本就如此文档化，前端提示文案还写着「压着它」）；run #1 的 133.7 未拍 z-order，事后用产品 p1 会话日志自己算出的 cmd 区域 (267,260,978x512) 估遮挡 ≈48%、合成 130–134 与实测自洽（旁证）。投诉的黑屏（全黑）与此本质不同。另记探针自身边界：`SetWindowPos(HWND_TOPMOST)` 两轮恒失败（ret=0、`WS_EX_TOPMOST` 不置位），「置顶」从未生效——环境行为，与产品无关（探针与取证归档在 `.tmp-win/`，不入库）。
- **没闭合的口子（别当成已验）**：①「单屏（区域）投屏浏览器黑屏」的坐标错位假说**被排除**——三方坐标系实测一致（PowerShell awareness=unaware / ffmpeg desktop=4000x2571 / sidecar 屏幕参数 dpi 96 无缩放）、副屏区域抓取 1.42 MB 有画面；用户那次会话黑屏的最终解释没锁定，本轮对单屏路径无修复动作，复现时按 `tools/verify-win-capture-sources.ps1` 抓图定位（.68 产物级复测单屏投屏全程有画面，仍未复现）。②观看页声音验的是**源码 `www/index.html`**（替换占位符）跑真实 Chromium；产物层判据是 `accept-macos-artifact.sh` 的逐文件 sha256 比对（`screencast/www/index.html` 在清单里），不是直接播放打包版 HLS。
- **文档同轮修订**：docs/屏幕投屏.md（§3.3 窗口抓取机制、§5.1 报错文案、§8.3 判据表新增两行、§8.4 测试类表与行数）、docs/故障排查.md（§2 症状索引两行、§6.1 Windows 窗口源行、§6.2 第 5 条观看页声音）、AGENTS.md、tools/README.md（两个新工具登记 + 计数）。
- **新入库工具**：`tools/verify-watch-page-sound.mjs`（268 行，真实 Chromium 双 pass 验起播出声，需有头运行——headless 没有自动播放豁免）；`tools/verify-win-capture-sources.ps1`（227 行，.68 交互会话逐变体抓 PNG：`desktop` / 显示器区域 / 旧 gdigrab 窗口模式 / 窗口矩形区域 + manifest，exit code = 失败抓取数）；`tools/probe-win-window-picker.py` 随之更新（hwnd 列表与区域换算合约）。

---

## v2.3.13 — 2026-10-15（本轮发布提交）

设备列表里不再躺着路由器：投屏目标改成**按服务识别**，不提供 AVTransport 的 UPnP 设备一律不登记。

- **准入门槛**（`ssdp.py:1074` 常量、`ssdp.py:1076-1090` 判定、`ssdp.py:1169-1175` 落在 `_parse_device_description` 里）：整份描述 XML（含内嵌设备）必须出现 `urn:schemas-upnp-org:service:AVTransport:`。判定是**命名空间无关**的（`tag.rpartition('}')[2]` 取 local-name）且**大小写不敏感**——URN 规范写法是 `AVTransport`，但山寨描述里 a/v 混乱，宁可放宽匹配也别把真能投屏的设备挡在门外。`deviceType` 降级为展示标签，不再参与准入：控制端能做的所有事（SetAVTransportURI / Play / Pause / Stop / Position）都走 AVTransport 这一个服务，标签写 `MediaServer` 的盒子只要带这个服务就照样收进来，写 `MediaRenderer` 但没有服务的设备照样挡掉。
- **手动添加走同一条门槛**（`ssdp.py:1231-1235`）：报错文案从「该地址不是有效的 DLNA 设备描述」改成点名准入条件——用户手输的往往正是路由器后台地址，那些机器**确实有**合法的 UPnP 描述，旧文案会让人反复检查自己填的格式对不对。`_MANUAL_PORTS`（`ssdp.py:1212`）仍是手输裸 IP 时的猜端口候选，**不是端口扫描**；发现链路自始至终只被动收 NOTIFY / M-SEARCH 响应。
- **顺带抓出一个真 bug**：lxml 的注释与处理指令节点 `.tag` 是**可调用对象**而非字符串，服务遍历在其上抛 `AttributeError`，被外层宽 `except` 吞成「描述 XML 解析失败」——**表现是「带 XML 注释的合格电视从列表消失」而不是报错**。守卫是 `isinstance(tag, str)`（`ssdp.py:1081-1084`），回归锁为 `test_ssdp_cast_service_gate.py::test_xml_comments_do_not_break_the_service_scan`。这条只有真机替身（描述里带注释）才会暴露，纯桩数据容易漏。
- **拒绝理由按 location 去重**（`ssdp.py:1092-1103`、计数表 `ssdp.py:182`）：同一地址首次打 INFO/WARNING、之后降 DEBUG。路由器这类设备每 30s 重播一轮 NOTIFY（抓取去重窗口 `fetch_dedup_window`，`ssdp.py:175`），不去重就是每天上万行噪音；无 device 元素 / 缺 UDN / XML 解析失败三种「数据本身有问题」的理由仍按 WARNING 露首行，正常「不是投屏目标」按 INFO。两个发现调用点（NOTIFY 与 M-SEARCH）不再各自打笼统 WARNING，理由由解析侧统一给。
- **数字口径**：`ssdp.py` 1264 → **1322** 行；测试新增 `tests/test_ssdp_cast_service_gate.py` **13 条**（电视/路由器/IGW 三份描述 fixture + 大小写 + 命名空间 + 内嵌设备 + 注释回归 + 日志去重 + 手动添加文案），`tests/test_ssdp_discovery_retry.py` 的 `RENDERER_DESC` 随之补上 `<serviceList>`（它原本没有服务，正是新门槛要挡的形状）。命令面、事件面、后台参数本轮**未动**（Rust 66 条 / Sidecar 59 条 / `commands.ts` 65 个 / `PARAM_SPECS` 32 项）。
- **新入库探针** `tools/verify-ssdp-cast-gate.py`（登记在 [tools/README.md](tools/README.md) §1.2）：一次起**两台**替身——带 AVTransport 的「电视」必须进列表，不提供的「路由器」（MediaServer 描述 + XML 注释）必须被挡下并留下理由。`--source` 就地跑仓库源码（打 tag 前用，需要 1900 空闲），默认模式读**已装产物**的 sidecar 日志（证明修复到了打包版）。两台替身的 LOCATION 都写 `127.0.0.1`，否则会撞上 `_is_own_location` 自通告回环过滤而「看起来脚本没生效」（§3.3 那条老坑）。
- **验收（本轮真实输出，打 tag 之前做完）**：`.venv/bin/python -m pytest tests/test_ssdp_cast_service_gate.py tests/test_ssdp_discovery_retry.py -q` → **30 passed in 1.25s**；`hancast-backend/.venv/bin/python tools/verify-ssdp-cast-gate.py --source --timeout 90` → **PASS（源码）：渲染端进列表、路由器挡在门外并留理由**，替身列表只认到 `HanCastCastGateTV-49311`，拒的那行是 `不列入投屏目标：HanCastCastGateRouter-49312 不提供 AVTransport 投屏服务（deviceType=urn:schemas-upnp-org:device:MediaServer:1） — http://127.0.0.1:49312/description.xml`；**同一台替身在 v2.3.12 产物上是 FAIL 的**（`python3 tools/verify-ssdp-cast-gate.py` → `[FAIL] 没有 AVTransport 的替身仍被登记为投屏目标：Found device via NOTIFY: HanCastCastGateRouter-49312`），即缺陷在打包版上复现、修复只可能在源码里自证的区分做完了；`python3 tools/audit-doc-refs.py` → 合计引用 **2963 处、可疑 0 处**（本轮 ssdp.py 插入代码使 179 行之后的行号整体位移，故障排查/架构总览/Frontend-API/AGENTS 里 19 处引用按**被引原文逐条重导**，不是统一加偏移）。装包后还要在 Mac 与 .68 各跑一次非 `--source` 模式作为产物级判据。
- **文档同轮修订**：AGENTS.md（SSDP 服务行加准入与判据、`ssdp.py` 行数、tools 计数口径、最后校对）、docs/故障排查.md（新增 §3.5 与本条症状索引行，含「不做端口扫描」的澄清）、docs/架构总览.md（§2 网络面新增发现准入段）、docs/HanCast-Frontend-API.md（`device_found` 触发时机 + `add_device` 错误清单与新行号）、hancast-backend/README.md（目录树 ssdp.py 一行）、tools/README.md（新脚本登记 + 计数 46/52 项）。

---

## v2.3.12 — 2026-10-15（本轮发布提交）

采集站那批失败页现在有包可装了；GitHub 连不通时有镜像开关；报错文案不再被界面砍掉后半句；老程序随包的 FFmpeg 残留从两台机器上清干净。

- **新官方规则包 `hancast-cms` 1.0.0**（通用兜底，不认域名），来源就是 `.68` 解析失败台账里的地址。JS 包 `rule-packs/hancast-cms/main.js`（128 行）两条规则，都是 `hosts: ['*']`、`stage: 'page'`、`priority: 8`：苹果CMS/maccms 的 `player_aaaa` 编码地址（`:50-53`）、myui 模板 share 播放器页的相对地址（`:103-106`）。索引 `rule-packs/index.json` 加在第 2 条，产物 `rule-packs/dist/hancast-cms-1.0.0.zip`（sha256 `7d3caa03…`）走 main 分支 raw 路径——程序里点「检查规则包更新」就能下载装上。
- **`hosts: ["*"]` 的语义是「兜底作用域」，靠排序让位而不是 priority**（`media/rules/base.py:35,129-133,160-170`、`declarative.py:80-91`）：`matching(url, stage)` 把命中的规则切成「站点专属在前、通用兜底在后」两段返回，两组内部各自保持 `(priority, name)`。装了 B 站/抖音精确规则的站永远先试精确的，只有全都拿不下才轮到通用规则，两边不会互相遮挡。声明式规则的通配只免掉域名判定，`path_re` 依然生效。**没有新增 manifest 字段、没有新增用户开关**，解不出来时的报错与台账记录路径一字未改——「通用规则还是不行再报错和记录」就是这个次序。
- **GitHub 国内镜像开关**（新文件 `utils/ghmirror.py`，83 行；`rewrite()` `:65`、`candidates()` `:73`）：
  - 两个新后台参数（`params.py:245-264`，section 键 `network`，设置页分组标题「GitHub 加速」/ "GitHub acceleration"）：`github.mirror_enabled`（bool，**默认关**）与 `github.mirror_prefix`（text，默认 `https://gh-proxy.com/`，`params.py:28`）。前缀做成参数而不是常量是有实测依据的：gh-proxy 对 raw 索引 / dist zip / releases/download / `api.github.com` 四种形态全通，而 ghfast.top 前三样通、`api` 回 403——我们不能替用户钉死一个镜像。
  - **改写只发生在一个入口**：`candidates(url)` 返回 `[("镜像", 前缀+原地址), ("直连", 原地址)]`，开关关掉就只剩直连；只有 GitHub 那 5 个主机会被改写，Gitee 等自建源原样通过。四个触点各自循环它：规则包索引 `jsplugins.py:352`、包 zip 下载 `jsplugins.py:562`、检查新版本 `commands.py:892`、FFmpeg 按需下载 `ffmpeg_deps.py:334`。
  - **失败自动回退，两条原因都留**：`索引拉取失败: 镜像 …；直连 …`，用户分得清「镜像挂了」和「根本没网」。走镜像时若 sha256 对不上也换下一条路（`import_url` 的 docstring `:548-556` 写明了为什么退回直连不构成放宽）。
  - **镜像是加速器，不是新的信任边界**：sha256 始终只认官方索引里那一份，镜像换包会被当场拦下——所以改前缀不需要重新签名任何东西；但把 `rules.index_url` 换成自建源就必须连 sha256 一起照抄（对照表在 [rule-packs/README.md](rule-packs/README.md) §2.3）。
  - 附带调整：`check_update` 直连的单次预算仍是 1 秒（GitHub 不通时不该拖住界面），**走镜像放宽到 3 秒**（`commands.py:895`）——国内往返本身 1~2 秒，照抄 1 秒等于开关白开。
- **报错信息不再被界面截断**：`.media-input__error` 从 `overflow:hidden + text-overflow:ellipsis + white-space:nowrap` 改成 `white-space:normal + overflow-wrap:anywhere + line-height:1.45`（`MediaInput.vue:697-707`）。失败原因是一整句话（站点 + 状态码 + 下一步动作），截断等于把最有用的后半句丢掉；而长 URL 里没有可断点，光 `normal` 折不下来，必须配 `overflow-wrap`。
- **旧程序内置 FFmpeg 的残留清理**：macOS 删掉 `src-tauri/target/debug/ffmpeg/`（0 字节桩，gitignore 的构建目录里的遗留，其它搜到的 ffmpeg 都属于第三方软件，没动）；Windows 测试机 `.68` 删掉 `C:\Users\Administrator\AppData\Local\HanCast\ffmpeg\`（≤v2.3.10 随包那份 `ffmpeg.exe`，82,797,568 字节），删后全目录复扫再无 HanCast 的 ffmpeg 文件。系统 ffmpeg 确认还在两处（`WinGet\Links` 与 yt-dlp.FFmpeg 包），删除后检测报 `ok=True source=system path=…\WinGet\Links\ffmpeg.exe`、版本 `8.1.2-full_build`，ffprobe 同目录一并认成 `source=system`。
  - 这正是 v2.3.11「升级遗留」预告的场景落地：那台机器 `hancast_setting.json` 里 `screencast.ffmpeg_path` 仍指向刚被删掉的路径，而 `set_ffmpeg_path` 只能设不能清（[docs/屏幕投屏.md](docs/屏幕投屏.md) §5.1），值会一直留着。留着的代价只有每次检测多一次失败的 `-version` 子进程和一条 `Configured FFmpeg path no longer valid` 告警——`ffmpeg_manager._configured()`（`:416-427`）先校验再把结果交给 `status()`，死路径按设计降级到「系统已装 / 本程序下载」两层，功能没有锁死。
- **数字口径**：`PARAM_SPECS` 30 → **32**（`params.py:41-265`；分组 screencast 9 / discovery 4 / player 3 / http 3 / browser 3 / failures 3 / media 2 / rules 2 / **network 2** / logs 1）；官方规则包 3 → **4** 个。命令面本轮未动（Rust `#[tauri::command]` 66 条 `lib.rs:1373-1440` / Sidecar 注册 59 条 `commands.py:110-197` / `commands.ts` 导出 65 个）。
- **新入库探针**（登记在 [tools/README.md](tools/README.md) §1.5/§1.6）：`tools/verify-rule-pack-live.py`（联网跑真实失败站点——采集站换皮就是静默失效，每次发版前重跑；已知失败站以 SKIP 记着，不许悄悄删判据）、`tools/verify-github-mirror-live.py`（四条契约：镜像可用 / 字节逐位一致 / 镜像死了能回退 / 关掉开关不走镜像。**只读**：开关用 `params` 的内存覆盖位演，落盘的 `hancast_setting.json` 一个字节都不动）、`tools/verify-error-wrap-ui.mjs`（按几何判据量折行与溢出，不比对文案）。`verify-params-ui.mjs` 随之扩到四类控件（新增 text）。
- **验收（本轮真实输出，打 tag 之前做完）**：定向 `pytest tests/test_params.py tests/test_jsplugins.py tests/test_share_resolver.py tests/test_ghmirror.py` → **139 passed**；`node tools/verify-error-wrap-ui.mjs` **ALL PASS**（报错行折成 4 行 / 宽 336px，面板内宽 388px，左右边界都不溢出，截图 `/tmp/hancast-error-wrap.png`）；`node tools/verify-params-ui.mjs` **ALL PASS**（分组标题按 section 出现「屏幕投屏 / 浏览器播放器 / 解析规则 / GitHub 加速」，text 控件带上镜像前缀）；`tools/verify-github-mirror-live.py` **ALL PASS** 共 17 条（镜像索引与直连逐字节一致 `1352 vs 1352`、镜像 zip 对得上官方 sha256 `70e223a6ef25` 且能正常解包、`import_url` 经镜像装包成功、镜像与直连给出同一个最新版本 `v2.2.20`、把镜像地址改死之后仍能取到索引与装包并留下回退日志、关掉开关不走镜像）；`tools/verify-rule-pack-live.py` 装入 `hancast-cms-1.0.0.zip` 认出 2 条规则，并把 `.68` 台账里两条失败页解出可播直链——`66dpw.vip` → `https://vv.jisuzyv.com/play/ZdPgAx4b/index.m3u8`、`lvsc168.com` → `https://cdn.ryplay11.com/20240930/1543_e77c4196/2000k/hls/index.m3u8`，第三条 `qiakon.com` 按 SKIP 记（XCDN PoW + 源站 502，不是规则缺项）。`vue-tsc --noEmit` rc=0；`python3 tools/audit-doc-refs.py` 合计引用 **2883 处、可疑 0 处**。
- **文档同轮修订**：AGENTS.md、README.md（32 项后台参数 + 解析规则包两条新说明）、docs/分享解析规则.md、docs/解析插件开发指南.md、rule-packs/README.md、docs/故障排查.md（§7.2 加「先开镜像开关」为第 0 步与新判据，§8.3 新增报错截断的修法）、docs/README.md、docs/HanCast-Frontend-API.md、docs/开发指南.md、docs/构建与发布.md、docs/架构总览.md、docs/屏幕投屏.md、docs/投屏确认.md、hancast-backend/README.md、tools/README.md。参数表插了 2 条导致行号整体后移，`discovery.guard_mx`、UA 默认值、`rules.index_timeout_s` 等引用按**被引原文逐条重导**后修，不是统一加偏移。
- **本轮没闭合的口子（别当成已验）**：① `verify-github-mirror-live.py` 与 `verify-rule-pack-live.py` 跑的是**当时线上的 3 包索引**，`hancast-cms` 要等本轮 push 到 main 才在线上——程序内拿到 4 包的判据留到 tag 之后复跑。② 镜像开关只验到源码层与联网层，**产物层还没跑**：本轮把新增的 `utils/ghmirror.py` 补进了 `accept-macos-artifact.sh:35-44` 的逐文件 sha256 清单（29 → **30 项**，同步改了 [docs/构建与发布.md](docs/构建与发布.md) §6.5 与 [tools/README.md](tools/README.md) §2.2 的计数口径），但这一判据要等 CI 出包后跑 `accept-macos-artifact.sh 2.3.12` 才闭合。③ 公共镜像自己会抽风：本轮第一次跑就撞上 `ProxyError: gh-proxy.com … RemoteDisconnected`（本机 9565 间歇），ALL PASS 是重试那一次——默认值不等于 SLA。④ 通配兜底只覆盖「一套程序换域名再开一家」的采集站，带 PoW/签名风控的站仍需专属规则（`qiakon.com` 是现例）。⑤ 前端截断的判据来自 Vite dev server + mock 的浏览器实测，产物内前端 `strings` 拿不到（同 v2.3.10 那条限制）。

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
- **验收（产物层 + 真机，打 tag 之前做完）**：三件留给产物的判据都已闭合，下面的话全部来自脚本输出，不是源码推断。①这一句曾经是「只证明到配置里再没有 ffmpeg glob、CI 不再有下载步骤、`check-resources.cjs` 不再报 ffmpeg 缺失」——本机那次剩下的 MISSING 全是还没跑 `npm run build:sidecar` 的 Nuitka 产物，现在由产物层证据接上。
  - **① bundle 里确实不含 ffmpeg**：CI 运行 `38683403875` 三平台全绿，两个产物跨机核对字节数与 SHA256 一致；`tools/accept-macos-artifact.sh 2.3.11` 判 **ALL PASS**，其中本轮新增的判据是整个 `.app` 里 `ffmpeg`/`ffprobe`/`ffmpeg.exe` **零命中**（v2.3.10 的产物里有两份）、三条 `ffmpeg_*` 命令注册在打包的 `commands.py` 里、`ffmpeg_manager.py` 里 `_get_base_path` / `_find_in_program_dir` 两个函数名已不存在。产物内 29 个 sidecar 源文件（口径：`accept-macos-artifact.sh:35-44` 的清单，含 `screencast/www/index.html`；整轮共 88 条 PASS）逐个与 `git show v2.3.11:…` 比 sha256，含新增的 `utils/ffmpeg_deps.py`（验收脚本一度把它错登在 `screencast/` 下，判据因此拿到空哈希——路径已修，重跑才全绿）。
  - **② Windows 能检出系统已装那份**：`tools/verify-ffmpeg-detect.ps1 -TempAppData`（只把 APPDATA/LOCALAPPDATA/USERPROFILE 指进一次性目录，PATH 原样）驱动**装好的** `hancast-sidecar.exe` → `ok=True source=system path=C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe`，版本 `8.1.2-full_build`，ffprobe 同目录一并认成 `source=system`。**这一跑的命中只能来自进程 PATH**：`-TempAppData` 已经把 `LOCALAPPDATA` 指进沙盒，而 `_extra_dirs()`（`ffmpeg_deps.py:60-91`）读的正是 `LOCALAPPDATA` 下的 `WinGet\Links|Packages`——那条通道被中和了，还能命中就说明是 PATH 给的。⚠️ 探针进程是 ssh 起来的 PowerShell 的子进程，**不等于 GUI 冷启动时那份 PATH**（`_extra_dirs()` 存在的理由「装了但没进 PATH」这次没有被复现，也没被推翻）。
  - **③ 没有 ffmpeg 的机器上弹窗与「让程序下载」可用**：同一脚本再加 `-BarePath`（PATH 削到只剩 `C:\Windows`）拿到弹窗的数据源本体——`ok=False`、`missing=FFmpeg`、`optional=FFprobe`、两条安装命令（`winget install --id Gyan.FFmpeg -e` / `choco install ffmpeg -y`）、`download.available=True kind=zip`，错误原文一起进载荷（弹窗正文就是它）。再把一份真 ffmpeg 放进 `managed_dir`，复检立刻 `ok=True source=downloaded`——第三层归属在产物里成立。按下下载确实在流：任务 `ffmpeg_429515`，600 秒内 `received` 从 0 走到 11,010,048 / 114,768,076（9.6%），`speed` 16–20 KB/s 逐帧更新。
  - **macOS 的下载分支跑到收口**：验收脚本用一次性 `HOME` + `PATH=/usr/bin:/bin` 起打包 sidecar，先 `source=system` 命中 `/opt/homebrew/bin/ffmpeg`（9.0.2）并真跑出 `-version`，再 `ffmpeg_download_start` → 阶段轨迹 `downloading → extracting → done` → 落盘那份能执行（`ffmpeg version 6.0`，来自 `b6.1.1` 的 `ffmpeg-darwin-arm64.gz`）→ 复检按「系统已装 > 自下载」归位 `source=system` → `set_ffmpeg_path` 后 `source=manual` 且 `ok=True` 不破。
- **本轮没闭合的口子（别当成已验）**：Windows 那条 gyan zip **只验到「开始流 + 进度真实」，没跑到 `done`**——`.68` 直连 gyan 只有 ~18 KB/s，114.8 MB 单包要两小时。zip 的解包与 `-version` 收口由 `tests/test_ffmpeg_download_e2e.py` 用本地 HTTP 源覆盖，那是源码层证据；「gyan → 解包 → 可用」的产物级端到端仍未实测。
- **升级遗留（本轮发现的边界，不改代码）**：从 ≤2.3.10 升上来的机器，`hancast_setting.json` 的 `screencast.ffmpeg_path` 仍指向**已经不再随包的那个目录**（`.68` 实测 `C:\Users\Administrator\AppData\Local\HanCast\ffmpeg\ffmpeg.exe`，6.1.1），而 NSIS 卸载不清理残留文件——于是①层优先命中，**这些机器实际还在用旧版随包那份 FFmpeg**，既不会自动改用系统新装的，也不会随之更新。它不算坏（`-version` 跑得通，功能可用），文件一旦消失会按设计降级到②③；要清只能用户在弹窗里重填路径（`set_ffmpeg_path` 只能设不能清，§屏幕投屏 5.1）。另外 Windows 单包 114.8 MB，慢链路上「让程序下载」近似不可用，弹窗里那两条安装命令才是主路。
- **产物层判据与真机探针**：见 [docs/屏幕投屏.md](docs/屏幕投屏.md) §5.8、[docs/故障排查.md](docs/故障排查.md) §6.6，以及新入库的 `tools/verify-ffmpeg-detect.ps1`（一次性沙盒，不碰用户的 winget 安装与真实设置；登记在 [tools/README.md](tools/README.md) §1.4，产物层判据的表格在 [docs/构建与发布.md](docs/构建与发布.md) §6.5）。
- **已知边界**：`set_ffmpeg_path` 只能设不能清（§屏幕投屏 5.1），靠失效降级兜住；流式下载经 curl_cffi 时会打 `CurlCffiWarning: chunk_size is ignored`（curl 没有分块大小的概念），只影响进度粒度不影响字节；`_SOURCES` 四条源里 **darwin 的 gz 已在产物级端到端验过、win32/amd64 的 gyan zip 只验到开始流**（见上一条），win32/arm64（BtbN）与 linux（johnvansickle `tar.xz`）两条从未真下载验证过；Linux 产物路径整体仍只在源码层。
- **两个词表并存（读日志时容易踩）**：`ffmpeg_download_status` 的 `status` 全程是 `pending`——它是任务表的早退闸门（`ffmpeg_deps.py:450`，`pending` 期间不重复起线程），面向用户的细粒度阶段只在 `stage` 里。前端只看 `stage`，所以功能没问题，但按 `status` 判断「下载卡住了」会一直读到 pending。

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
