<!-- 最后校对：2026-10-25，对应版本 v2.3.24（package.json:4）。数字与 文件:行号 引用由 tools/check-doc-freshness.py 机械核对 -->

<p align="center">
  <a href="README.md"><img src="https://img.shields.io/badge/README-CN-blue?style=flat-square&logo=github" alt="中文"></a>
  <a href="README_en.md"><img src="https://img.shields.io/badge/README-EN-blue?style=flat-square&logo=github" alt="English"></a>
</p>

<p align="center">
  <img src="src-tauri/icons/icon.png" width="120" alt="HanCast Logo">
</p>

<h1 align="center">HanCast</h1>

<p align="center">
  <strong>Cross-Platform Wireless Casting & Receiver App</strong>
</p>

<p align="center">
  Open-source cross-platform casting tool — drag files to your TV, cast from phone to PC, all in one step.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Windows-10%2F11-blue?logo=windows" alt="Windows">
  <img src="https://img.shields.io/badge/macOS-14%2B-blue?logo=apple" alt="macOS">
  <img src="https://img.shields.io/badge/Linux-deb%2Frpm%2FAppImage%2Fpkg.tar.zst-blue?logo=linux" alt="Linux">
  <img src="https://img.shields.io/badge/License-GPL--3.0-green" alt="License">
</p>

<p align="center">
  <a href="https://apps.microsoft.com/detail/9nk1xwpg6hd5?launch=true&mode=mini">
    <img src="https://get.microsoft.com/images/en-us%20dark.svg" alt="Microsoft Store" height="48">
  </a>
</p>

<p align="center">
  <img src="docs/images/HanCast-English.png" alt="HanCast - One App, Every Screen Connected" width="100%">
</p>

<p align="center">
  <img src="docs/images/ui-native-light.png" alt="HanCast interface (light)" width="31%">
  <img src="docs/images/ui-native-dark.png" alt="HanCast interface (dark)" width="31%">
  <img src="docs/images/screenshot-windows.png" alt="HanCast Windows Interface Preview" width="31%">
</p>

<p align="center"><sub>Left / centre: the restrained native-tool UI introduced in v2.3.20 (light / dark); right: on real Windows hardware.</sub></p>

---

## Features

### Send Cast

Drag a file or paste a link, select a device, and cast in one click.

| Media Source | Action | Example |
|--------------|--------|---------|
| **Local Files** | Drag video / audio / image to the window | `.mp4` `.mp3` `.jpg` |
| **Online Links** | Paste HTTP / HTTPS direct links | `https://example.com/video.mp4` |
| **Platform links / share codes** | Just paste; a rule pack resolves the stream | `bilibili.com/video/BV...`, Douyin share codes, `b23.tv` short links |
| **Clipboard** | Copy a link and paste directly | — |

Resolution for platform links comes from **pluggable rule packs** (see "Rule Packs" below) —
the app itself ships no site rules. Bilibili casts can also **switch quality mid-playback**
and resume where you left off.

### Screen Cast

Mirror your whole screen or a **specific window** to the big screen, with an install-free
browser watch page handed out at the same time (HLS, two independent channels).

- Windows captures **system audio** (WASAPI loopback) and the mic; macOS uses a CoreAudio Audio Tap
- On Linux Wayland sessions (Hyprland / sway) frames come from the bundled native helper
  `hancast-wlgrab` — ffmpeg can only grab the empty root window there, which casts **at full frame
  rate, entirely black, with a clean log**, so since v2.3.19 we fail loudly instead of falling back to x11grab
- One click to copy the watch-page link, phone scans the QR and watches
- Live viewer count, segment status and failure reasons while casting

### Cast failures actually reach you

When the window is hidden in the tray nobody sees the red inline error — all you get is "I clicked and
nothing happened". All five failure exits therefore **raise a system notification** and **bring the
hidden window back to the front**, with the device's own words in the body:

| Situation | Notification |
|-----------|--------------|
| Device refused the cast | `Cast failed` + `device name: the reason it returned` (e.g. `HTTP 500 … open Multi-Screen / Screen Mirroring / DLNA on the TV`) |
| Stop never delivered | `Stopped locally, but the TV may still be playing` + which devices and why |
| Screen-cast session died mid-way | the capture process' exit reason (identical text is de-duplicated) |

macOS posts through `UNUserNotificationCenter` from the shell (notification permission has to be
re-granted after every re-pack, since each build is a new linker-signed identity); Linux uses DBus
desktop notifications; Windows uses Toast. Without permission everything degrades silently and the
inline error is still there.

### Rule Packs

Site resolution runs on JS rule packs inside a QuickJS sandbox, or on declarative JSON rules:

- Import a ZIP or URL from the settings page, or **check the official index for updates** (sha256 verified)
- The sandbox exposes `host.fetch` / DOM selection / cookies / signing primitives (md5, sha, HMAC, AES, RSA); scripts never touch Python
- Addresses you pasted that failed to resolve are logged per domain in a **failure ledger**,
  viewable and clearable in settings — it is the to-do list for the next batch of rule packs

### Receive Cast

![Receive Cast Preview](docs/images/cast-preview.png)

Turn your PC into a DLNA receiver. Tap the cast button in apps like iQIYI or Bilibili on your phone, and it plays instantly on your PC's big screen.

- Powered by MPV player, supporting virtually all media formats
- Auto-starts playback when a cast request arrives; remembers window position and size

### Cast Control

![Cast Control Panel](docs/images/cast-control.png)

After casting starts, click the device card to open the control panel:

- Real-time media cover and title preview
- Volume and progress control
- One-click stop cast

#### Multi-Device Cast Control

![Cast Control](docs/images/back.png)

- Cast to multiple devices simultaneously while maintaining independent controller states

### Cast Security

![Cast Confirmation Dialog](docs/images/cast-confirm.png)

- **Cast Confirmation** — Popup confirmation for unknown device cast requests to prevent accidental casting
- **Timeout Auto-Reject** — Automatically rejects after 15 seconds of inactivity (configurable)
- **Trust Management** — Supports "Allow Once" or "Always Allow"; trusted devices won't prompt again

### Background Parameters & Log Panel

![Settings page](docs/images/settings.png)

- 32 background parameters (discovery heartbeat, index timeout, user agents, segment length, GitHub mirror, …)
  are tunable from the settings page, take effect on the next call, and stay on your machine
- The in-app log panel shows Sidecar logs with per-module filtering, temporary level escalation and
  one-click export — no need to guess where the log file lives

## Download

| Channel | Link |
|---------|------|
| GitHub Releases (**current builds**) | https://github.com/pingod/HanCast/releases/latest |
| Gitee Releases (faster in mainland China) | https://gitee.com/buxiangqumingzi/han-cast/releases/latest |
| Microsoft Store | https://apps.microsoft.com/detail/9nk1xwpg6hd5 |

> CI builds a **draft** release for every tag and a human publishes it after on-device acceptance,
> so the link above points at the last accepted version (**v2.3.23** when this line was written;
> 10 assets: Windows installer, macOS `.dmg`, Linux deb / rpm / AppImage and the Arch
> `.pkg.tar.zst`, plus 4 official rule-pack zips). Code and artifacts live on that release line:
> [pingod/HanCast](https://github.com/pingod/HanCast). The upstream
> [lanzeweie/HanCast](https://github.com/lanzeweie/HanCast) `main` branch is still at v2.1.0 (the
> merge request bringing it up to date is open), so clone the fork for current source.

CI publishes Windows (x64 NSIS installer), macOS (arm64 `.app` / `.dmg`) and Linux
(deb / rpm / AppImage, plus a pacman-installable `.pkg.tar.zst`) artifacts for every tag —
see [docs/构建与发布.md](docs/构建与发布.md).

---

## Quick Start for Developers

### Prerequisites

| Dependency | Version | Notes |
|------------|---------|-------|
| [Node.js](https://nodejs.org/) | >= 18 | Frontend build |
| [Rust](https://rustup.rs/) | Latest stable | Tauri build |
| [Python](https://python.org/) | >= 3.11 | Backend runtime |
| [uv](https://docs.astral.sh/uv/) | Latest | Python package manager (recommended) |
| [MPV](https://mpv.io/) | Any | Required for receive casting; optional for send casting |
| [FFmpeg](https://ffmpeg.org/) | needs x264 + HLS | Required for screen casting; **not bundled** — detected at runtime |

Packaging reads mpv from `src-tauri/mpv/` (the `mpv/*` entry in `bundle.resources`).
Then run `node scripts/check-resources.cjs` to confirm every glob resolves.

**FFmpeg is no longer bundled (since v2.3.11)**: the Sidecar looks for it in
"path set in Settings → this machine's `PATH` plus the usual install directories per platform →
the copy this app downloaded into `<config dir>/bin/`". When nothing is found, the screen-casting panel
opens a "runtime dependency" dialog offering the platform's package-manager command, a
download-and-continue action, and a button to open the dependency directory.
See [docs/屏幕投屏.md](docs/屏幕投屏.md).

### Install & Run

```bash
# Clone the repo
git clone https://github.com/pingod/HanCast.git
cd HanCast

# Install dependencies
npm install
cd hancast-backend && uv sync && cd ..

# Start dev mode (frontend + Rust + Python all-in-one)
npm run tauri dev
```

**Frontend only** (browser mock mode, no Python/Rust needed):

```bash
npm run dev
# Open http://localhost:1420 in browser
```

**Python backend debugging only**:

```bash
cd hancast-backend
uv run python -m hancast_sidecar.main
```

### Production Build

```bash
npm run tauri build
```

Build output is located at `src-tauri/target/release/bundle/`: an NSIS `.exe` on Windows,
`.app` plus `dmg/HanCast_<version>_aarch64.dmg` on macOS (the dmg is assembled by CI, see
[docs/构建与发布.md](docs/构建与发布.md) §4), deb / rpm / AppImage on Linux, and
`hancast-<version>-1-x86_64.pkg.tar.zst` for Arch (Tauri's Linux bundler has no Arch backend,
so `scripts/build-arch-pkg.cjs` converts the deb right after bundling).
The Sidecar is built first with `npm run build:sidecar` (Nuitka) and merged into the shell
resources by `scripts/sync-sidecar.cjs`.

> ⚠️ Running the packaged `HanCast` binary without its Sidecar crashes — it depends on the bundled
> Python Sidecar. Use `npm run tauri dev` (frontend + Rust + Python at once) for development testing.

---

## Architecture

```
┌──────────────────┐    invoke()     ┌─────────────────┐
│  Vue 3 Frontend  │ ──────────────→ │  Rust Tauri      │
│  (WebView)       │ ←────────────── │  (SidecarManager)│
└──────────────────┘  Promise/events └────────┬────────┘
                                              │ line-delimited JSON over
                                              │ stdin/stdout (forced UTF-8)
                                              ▼
                                     ┌─────────────────┐
                                     │  Python Sidecar  │
                                     │ DLNA/SSDP/resolve│
                                     └───────┬─────────┘
                                    IPC / child │
                              ┌────────────────┴────────────────┐
                              │   mpv (receiver playback)       │
                              │   ffmpeg (screen capture / HLS) │
                              └─────────────────────────────────┘
```

| Layer | Tech | Responsibility |
|-------|------|----------------|
| Frontend | Tauri 2.0 + Vue 3 + TypeScript + Pinia | UI rendering, user interaction, state management |
| Bridge | Rust (Tauri Commands) | Process management, Sidecar lifecycle, IPC, clipboard/tray/autostart |
| Backend | Python 3.11+ (Sidecar, Nuitka-compiled) | DLNA protocol, SSDP discovery, media services, screen casting, resolve pipeline |
| Resolution | QuickJS sandbox + declarative JSON | Site rules come only from user-installed packs; the app ships none |
| Player | MPV (external dependency) | Receiver playback, IPC control |
| Capture | FFmpeg (external dependency) + WASAPI / Audio Tap | Screen & window capture, HLS segmentation |

The frontend calls **67** Rust commands via Tauri `invoke()` (`generate_handler!`,
`src-tauri/src/lib.rs:1457-1525`); Rust talks to the Python Sidecar over stdin/stdout JSON
(**62** commands registered Sidecar-side; **8** events reach the frontend = 6 from Python + 2 from the
shell, plus `browser_request` which stops at the shell). Full contract:
[docs/HanCast-Frontend-API.md](docs/HanCast-Frontend-API.md). `python3 tools/check-doc-freshness.py`
re-checks every one of these numbers against the code.

---

## Project Structure

```
HanCast/
├── src/                        # Vue 3 Frontend
│   ├── components/             # UI Components (20; inventory in docs/HanCast-Frontend-Spec.md)
│   │   ├── TitleBar.vue        # Custom title bar
│   │   ├── MediaInput.vue      # Media input (drag/link/paste)
│   │   ├── DeviceList.vue      # Device list
│   │   ├── DeviceCard.vue      # Device card (cast/rename/remove)
│   │   ├── CastController.vue  # Cast control bar (incl. quality switch)
│   │   ├── ScreenCastPanel.vue # Screen cast panel
│   │   └── PluginSettings.vue  # Rule pack management & "check for updates"
│   ├── stores/                 # Pinia state management (9 stores)
│   ├── locales/                # i18n resources (zh-CN / en-US, 324 keys each, must match)
│   ├── i18n.ts                 # vue-i18n instance + tt() for use outside components
│   └── api/commands.ts         # Tauri invoke wrapper
│
├── src-tauri/                  # Rust Tauri Shell
│   ├── src/
│   │   ├── lib.rs              # Tauri commands (67; see generate_handler! for the list)
│   │   ├── macos_notify.rs     # macOS notifications via UNUserNotificationCenter (main thread!)
│   │   └── sidecar.rs          # SidecarManager (Rust ↔ Python)
│   ├── mpv/                    # External player binary (placed before build; FFmpeg is not bundled)
│   └── Cargo.toml
│
├── hancast-backend/            # Python Backend (Sidecar)
│   └── hancast_sidecar/
│       ├── main.py             # Sidecar entry (stdin/stdout JSON loop)
│       ├── commands.py         # Command router (registry of 62 commands lives here)
│       ├── params.py           # 32 background parameters
│       ├── ssdp.py             # SSDP discovery (broadcast fallback when multicast fails)
│       ├── protocol/
│       │   ├── dlna.py         # DLNA protocol implementation
│       │   └── server.py       # DLNA HTTP server
│       ├── renderer/
│       │   └── mpv.py          # MPV renderer (IPC control)
│       ├── media/
│       │   ├── parser.py       # Media file/URL parsing
│       │   ├── rules/          # Rule registry (JS packs + declarative JSON; no compiled-in rules)
│       │   ├── share_resolver.py # Share text -> playable URL (rules come from installed packs)
│       │   ├── failures.py     # Resolve failure ledger (per domain)
│       │   └── server.py       # Local file HTTP server + in-path proxy
│       ├── screencast/         # Screen cast: capture (incl. the Wayland helper wlgrab), HLS, serving, watch page
│       ├── security/           # Cast confirmation (Device Guard)
│       └── xml/                # UPnP description files
│
├── rule-packs/                 # Official rule packs (4: bilibili / cms / douyin / template)
├── linux-capture/              # hancast-wlgrab source (Rust; Wayland screen capture)
├── tools/                      # Acceptance & probe scripts (index in tools/README.md)
├── scripts/                    # Version sync and artifact verification scripts
├── .github/workflows/build.yml # 3-platform CI + draft Release
├── CHANGELOG.md                # Version history
└── docs/                       # Documentation (start at docs/README.md)
```

---

## Documentation

**Start at [docs/README.md](docs/README.md)** — it maps questions to documents, lists reading
paths and states the maintenance rules. Frequently used:

| Document | Description |
|----------|-------------|
| [docs/架构总览.md](docs/架构总览.md) | Process layout, ports/threads/on-disk state, invariants |
| [docs/开发指南.md](docs/开发指南.md) | Setup, run modes, test discipline, debugging, release rules |
| [docs/HanCast-Frontend-API.md](docs/HanCast-Frontend-API.md) | Full contract of 67 invoke commands and 8 events |
| [docs/HanCast-Frontend-Spec.md](docs/HanCast-Frontend-Spec.md) | Components / stores / views, styling, i18n |
| [docs/故障排查.md](docs/故障排查.md) | Symptom-indexed: devices not found, cast won't start, won't stop, no audio… |
| [docs/屏幕投屏.md](docs/屏幕投屏.md) · [docs/投屏确认.md](docs/投屏确认.md) | Behaviour contracts of the two subsystems |
| [docs/分享解析规则.md](docs/分享解析规则.md) · [docs/解析插件开发指南.md](docs/解析插件开发指南.md) · [rule-packs/README.md](rule-packs/README.md) | Resolve pipeline, writing rule packs, publishing official packs |
| [docs/构建与发布.md](docs/构建与发布.md) | Version SSOT, 3-platform packaging, artifact acceptance, release order |
| [CHANGELOG.md](CHANGELOG.md) | What changed in each version |
| [AGENTS.md](AGENTS.md) | Quick reference and hard constraints for coding agents (formerly CLAUDE.md; depth lives in the docs above) |

> `docs/Macast-Frontend-*.md` and `docs/HanCast-Backend-Spec.md` are superseded historical drafts, kept only for reference.
>
> Every `file:line` citation is machine-checkable with `python3 tools/audit-doc-refs.py`; docs are updated in the same commit as the code they describe (conventions: [docs/README.md](docs/README.md) §4).

---

## License

[GPL-3.0](LICENSE) © 2024-2026 [lanzeweie](https://github.com/lanzeweie)

Based on [xfangfang/Macast](https://github.com/xfangfang/Macast), inheriting the original project's license.

## Acknowledgments

- [xfangfang/Macast](https://github.com/xfangfang/Macast) — Original project
- [Tauri](https://tauri.app/) — Cross-platform desktop app framework
- [akFace/mpv.config](https://github.com/akFace/mpv.config) — MPV theme skin (modernz)
