// Prevents additional console window on Windows in release
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

/// Linux + Wayland 的运行期兜底，必须在任何 GTK/WebKit 初始化之前设置。
///
/// WebKitGTK 2.4x 起给 webview 表面走 `wp_linux_drm_syncobj_*` 显式同步，
/// 而 Hyprland 的实现要求 commit 之前必须先绑定 acquire timeline；没绑就判客户端违约：
///
/// ```text
/// wl_display#1.error(wp_linux_drm_syncobj_surface_v1#52, 4, "Missing acquire timeline")
/// Gdk-Message: Error 71 (协议错误) dispatching to Wayland display.
/// ```
///
/// 用户表现为「图标点一下，窗口闪都没闪就没」。2026-10-17 在 CachyOS
/// （gtk4 4.24.1 / webkit2gtk-4.1 2.54.1 / hyprland 0.56.2）实测：
/// `GSK_RENDERER=cairo`、`GSK_RENDERER=gl`、`LIBGL_ALWAYS_SOFTWARE=1` 全都照崩，
/// 单独一个 `WEBKIT_DISABLE_DMABUF_RENDERER=1`（退回共享图像路径）即稳定存活，
/// 且仍是原生 Wayland 表面，不需要把用户推去 XWayland。
///
/// 只在 Wayland 下生效；用户自己设过就不覆盖（留出手动调试的口子）。
///
/// ⚠️ 这条兜底的**连带后果**：关掉 dmabuf 后 WebKitGTK 走共享图像（SHM）路径，
/// 透明（ARGB）窗口在鼠标划过触发 damage 时会漏提交矩形，那块 surface 保持 alpha=0，
/// Hyprland 就把后面的桌面（还带模糊）透出来——用户看到的是「晃一晃鼠标就出现透明洞」。
/// 上游 tauri#14924（仍未关闭）同一现象，并明确「不透明窗口没问题」，
/// `WEBKIT_DISABLE_COMPOSITING_MODE=1` 实测无效。所以 Linux 侧的窗口必须不透明：
/// 见 `src-tauri/tauri.linux.conf.json` 的 `app.windows[0]`（`transparent:false` +
/// 不透明 `backgroundColor`；JSON 不能写注释，完整因果链在 docs/故障排查.md）。
#[cfg(target_os = "linux")]
fn apply_display_workarounds() {
    if std::env::var_os("WAYLAND_DISPLAY").is_some()
        && std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none()
    {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }
}

#[cfg(not(target_os = "linux"))]
fn apply_display_workarounds() {}

fn main() {
    apply_display_workarounds();
    hancast_lib::run()
}
