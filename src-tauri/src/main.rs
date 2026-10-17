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
