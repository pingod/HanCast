//! macOS 系统通知：直接用 `UNUserNotificationCenter`，并把授权与投递结果写进程序日志。
//!
//! 为什么绕开 `tauri-plugin-notification`：它落到 notify-rust → mac-notification-sys 的
//! `NSUserNotificationCenter`（macOS 11 起废弃）。v2.3.21 真机实测五条失败记录 `presented` 全为 0
//! ——只进通知中心、不弹横幅。
//!
//! 换成 `UNUserNotificationCenter` 之后（v2.3.22）**仍然不弹**：记录里 `req.iden` 是我们给的
//! `hancast-cast`、没有 `scat.id="LEGACY"`，说明走的确实是新 API，但 `presented` 依旧为 0；
//! 同一时刻 `osascript display notification` 的对照横幅正常弹出，所以不是勿扰、也不是通道坏了。
//! 这一版把「在主线程上调 UN」「授权状态」「投递错误」三件事一起落到日志，
//! 下一轮看日志就知道原因，不用再靠猜。

use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};

use block2::RcBlock;
use objc2::runtime::Bool;
use objc2_foundation::{NSError, NSString};
use core::ptr::NonNull;
use objc2_user_notifications::{
    UNAuthorizationOptions, UNAuthorizationStatus, UNMutableNotificationContent,
    UNNotificationRequest, UNNotificationSetting, UNNotificationSettings, UNUserNotificationCenter,
};

/// 授权只问一次：第一次真要发通知时才问；被拒不重试也不报错（界面里的红条始终在）。
static AUTH_REQUESTED: AtomicBool = AtomicBool::new(false);

fn log_path() -> Option<PathBuf> {
    let home = std::env::var("HOME").ok()?;
    Some(
        PathBuf::from(home)
            .join("Library")
            .join("Application Support")
            .join("HanCast")
            .join("logs")
            .join("hancast.log"),
    )
}

/// 写进程序自己的日志：与 Python 侧同一份文件、同一行格式，所以后台日志面板读得到。
pub fn log(msg: &str) {
    let line = format!(
        "[{}] hancast.notify INFO: {}\n",
        chrono::Local::now().format("%Y-%m-%d %H:%M:%S%.3f"),
        msg
    );
    let Some(path) = log_path() else { return };
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(mut f) = OpenOptions::new().create(true).append(true).open(&path) {
        let _ = f.write_all(line.as_bytes());
    }
}

fn ns_err(err: *mut NSError) -> String {
    match unsafe { err.as_ref() } {
        Some(e) => e.localizedDescription().to_string(),
        None => "无错误对象".to_string(),
    }
}

fn status_name(s: UNAuthorizationStatus) -> &'static str {
    if s == UNAuthorizationStatus::NotDetermined {
        "notDetermined"
    } else if s == UNAuthorizationStatus::Denied {
        "denied"
    } else if s == UNAuthorizationStatus::Authorized {
        "authorized"
    } else if s == UNAuthorizationStatus::Provisional {
        "provisional"
    } else if s == UNAuthorizationStatus::Ephemeral {
        "ephemeral"
    } else {
        "其他"
    }
}

fn setting_name(s: UNNotificationSetting) -> &'static str {
    if s == UNNotificationSetting::NotSupported {
        "不支持"
    } else if s == UNNotificationSetting::Disabled {
        "关闭"
    } else {
        "开启"
    }
}

fn ask_authorization(center: &UNUserNotificationCenter) {
    let options = UNAuthorizationOptions::Alert | UNAuthorizationOptions::Sound;
    center.requestAuthorizationWithOptions_completionHandler(
        options,
        &RcBlock::new(|granted: Bool, err: *mut NSError| {
            log(&format!(
                "requestAuthorization granted={} err={}",
                bool::from(granted),
                ns_err(err)
            ));
        }),
    );
    center.getNotificationSettingsWithCompletionHandler(&RcBlock::new(
        |settings: NonNull<UNNotificationSettings>| {
            let settings = unsafe { settings.as_ref() };
            log(&format!(
                "通知设置 authorizationStatus={} alert={} 通知中心={}",
                status_name(settings.authorizationStatus()),
                setting_name(settings.alertSetting()),
                setting_name(settings.notificationCenterSetting())
            ));
        },
    ));
}

pub fn send(title: &str, body: &str) -> Result<(), String> {
    let center = UNUserNotificationCenter::currentNotificationCenter();

    if !AUTH_REQUESTED.swap(true, Ordering::Relaxed) {
        ask_authorization(&center);
    }

    let content = UNMutableNotificationContent::new();
    content.setTitle(&NSString::from_str(title));
    content.setBody(&NSString::from_str(body));

    // identifier 每条不同：固定值会让后一条顶掉前一条，通知中心里只剩最新一条。
    let id = format!("hancast-cast-{}", chrono::Local::now().timestamp_millis());
    let request = UNNotificationRequest::requestWithIdentifier_content_trigger(
        &NSString::from_str(&id),
        &content,
        None,
    );
    center.addNotificationRequest_withCompletionHandler(
        &request,
        Some(&RcBlock::new(move |err: *mut NSError| {
            let text = ns_err(err);
            if text == "无错误对象" {
                log(&format!("addNotificationRequest 已接受（id={id}）"));
            } else {
                log(&format!("addNotificationRequest 被拒（id={id}）：{text}"));
            }
        })),
    );
    Ok(())
}
