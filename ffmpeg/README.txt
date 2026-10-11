# FFmpeg（屏幕投屏功能）

构建/运行屏幕投屏需要 `ffmpeg.exe`（gdigrab/dshow 屏幕与音频捕获 + H.264/AAC 编码）。

## 放置位置

将 `ffmpeg.exe` 放入本目录（`<项目根>/ffmpeg/ffmpeg.exe`），`windows-build.ps1` 会在构建时
同步到 `src-tauri/ffmpeg/` 并随应用打包。开发环境未放置时，sidecar 会回退到系统 PATH 中的
ffmpeg（可在应用设置中手动指定路径）。

## 获取

Windows 推荐使用 gyan.dev 的 full build（含 gdigrab / dshow / libx264 / nvenc）：

- https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-full.7z

macOS 可用 `brew install ffmpeg`（屏幕捕获用 avfoundation）。
