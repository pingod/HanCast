//! macOS 系统通知：直接用 `UNUserNotificationCenter`。
//!
//! `tauri-plugin-notification` 在 macOS 上落到 notify-rust → mac-notification-sys 的
//! `NSUserNotificationCenter`（自 macOS 11 起废弃）。当前 macOS 仍会把消息存进通知中心，
//! 但**不再弹横幅**：2026-10-24 在装好的 2.3.21 上实测，五次投屏失败留下五条记录、
//! `presented` 全为 0，而同机走新 API 的 App 是 1；期间系统自己的横幅正常弹出。
//! 「窗口收进托盘时让用户看见投屏失败的原因」是这条链路存在的唯一理由，所以绕开插件。

use std::sync::atomic::{AtomicBool, Ordering};

use block2::RcBlock;
use objc2::runtime::Bool;
use objc2_foundation::{NSError, NSString};
use objc2_user_notifications::{
    UNAuthorizationOptions, UNMutableNotificationContent, UNNotificationRequest,
    UNUserNotificationCenter,
};

/// 授权只问一次：第一次真要发通知时才问；被拒不重试也不报错（界面里的红条始终在）。
static AUTH_REQUESTED: AtomicBool = AtomicBool::new(false);

pub fn send(title: &str, body: &str) -> Result<(), String> {
    let center = UNUserNotificationCenter::currentNotificationCenter();

    if !AUTH_REQUESTED.swap(true, Ordering::Relaxed) {
        let options = UNAuthorizationOptions::Alert | UNAuthorizationOptions::Sound;
        center.requestAuthorizationWithOptions_completionHandler(
            options,
            &RcBlock::new(|_granted: Bool, _error: *mut NSError| {}),
        );
    }

    let content = UNMutableNotificationContent::new();
    content.setTitle(&NSString::from_str(title));
    content.setBody(&NSString::from_str(body));

    let request = UNNotificationRequest::requestWithIdentifier_content_trigger(
        &NSString::from_str("hancast-cast"),
        &content,
        None,
    );
    center.addNotificationRequest_withCompletionHandler(&request, None);
    Ok(())
}
