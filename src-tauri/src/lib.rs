mod browser;
#[cfg(target_os = "macos")]
mod macos_notify;
mod sidecar;

use serde::{Deserialize, Serialize};
use sidecar::SidecarManager;
use std::sync::{Arc, Mutex};
use std::fs;
use std::path::PathBuf;
use tauri::{
    menu::{CheckMenuItemBuilder, MenuBuilder, MenuItemBuilder},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Emitter, Listener, Manager, State,
};
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_clipboard_manager::ClipboardExt;
use chrono::Local;

#[cfg(target_os = "windows")]
use windows::{
    ApplicationModel::{StartupTask, StartupTaskState},
    core::HSTRING,
};

/// 窗口配置数据结构
#[derive(Debug, Serialize, Deserialize, Clone)]
struct WindowConfig {
    /// 窗口宽度
    width: f64,
    /// 窗口高度
    height: f64,
}

impl Default for WindowConfig {
    fn default() -> Self {
        Self {
            width: 434.0,
            height: 634.0,
        }
    }
}

/// 窗口配置状态（用于防抖）
#[derive(Clone)]
struct WindowConfigState {
    config: Arc<Mutex<WindowConfig>>,
    config_path: PathBuf,
    save_timer: Arc<Mutex<Option<tauri::async_runtime::JoinHandle<()>>>>,
}

impl WindowConfigState {
    /// 加载配置文件
    fn load(config_path: &PathBuf) -> Self {
        let config = if config_path.exists() {
            match std::fs::read_to_string(config_path) {
                Ok(content) => serde_json::from_str(&content).unwrap_or_default(),
                Err(_) => WindowConfig::default(),
            }
        } else {
            WindowConfig::default()
        };

        Self {
            config: Arc::new(Mutex::new(config)),
            config_path: config_path.clone(),
            save_timer: Arc::new(Mutex::new(None)),
        }
    }

    /// 更新配置并触发延迟保存（使用后台线程实现防抖）
    fn update_and_save(&self, new_config: WindowConfig) {
        // 更新内存中的配置
        {
            let mut config = self.config.lock().unwrap();
            *config = new_config;
        }

        // 取消之前的保存任务
        {
            let mut timer = self.save_timer.lock().unwrap();
            if let Some(handle) = timer.take() {
                handle.abort();
            }
        }

        // 启动新的延迟保存任务（500ms 防抖）
        let config_ref = self.config.clone();
        let path = self.config_path.clone();
        let timer_ref = self.save_timer.clone();

        let handle = tauri::async_runtime::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;

            let config = config_ref.lock().unwrap().clone();
            if let Ok(json) = serde_json::to_string_pretty(&config) {
                if let Some(parent) = path.parent() {
                    let _ = std::fs::create_dir_all(parent);
                }
                let _ = std::fs::write(&path, json);
            }

            // 清除定时器引用
            let mut timer = timer_ref.lock().unwrap();
            *timer = None;
        });

        // 保存新的定时器句柄
        let mut timer = self.save_timer.lock().unwrap();
        *timer = Some(handle);
    }

    /// 获取当前配置
    fn get_config(&self) -> WindowConfig {
        self.config.lock().unwrap().clone()
    }
}

/// 当前投屏 URL 缓存（供托盘菜单使用）
struct CastUrlState {
    url: Arc<Mutex<String>>,
}

/// 会话日志：每次启动写入新文件，同时输出到 stderr
struct SessionLogger {
    path: PathBuf,
}

impl SessionLogger {
    fn new(log_dir: &std::path::Path) -> Self {
        fs::create_dir_all(log_dir).ok();
        let path = log_dir.join("hancast.log");
        // 每次启动清空旧日志
        fs::write(&path, "").ok();
        Self { path }
    }

    fn log(&self, msg: &str) {
        let ts = Local::now().format("%Y-%m-%d %H:%M:%S");
        let line = format!("[{}] {}\n", ts, msg);
        eprint!("{}", line);
        fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .and_then(|mut f| {
                use std::io::Write;
                f.write_all(line.as_bytes())
            })
            .ok();
    }
}

#[tauri::command]
fn minimize_window(window: tauri::Window) {
    window.minimize().ok();
}

#[tauri::command]
fn close_window(window: tauri::Window) {
    // Hide to tray instead of closing
    window.hide().ok();
}

// ── Device management ──

#[tauri::command]
async fn get_devices(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar.send_command("get_devices", serde_json::json!({})).await
}

#[tauri::command]
async fn refresh_devices(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar.send_command("refresh_devices", serde_json::json!({})).await
}

#[tauri::command]
async fn set_default_device(sidecar: State<'_, SidecarManager>, id: String) -> Result<(), String> {
    sidecar
        .send_command("set_default_device", serde_json::json!({"id": id}))
        .await?;
    Ok(())
}

#[tauri::command]
async fn rename_device(
    sidecar: State<'_, SidecarManager>,
    id: String,
    name: String,
) -> Result<(), String> {
    sidecar
        .send_command("rename_device", serde_json::json!({"id": id, "name": name}))
        .await?;
    Ok(())
}

#[tauri::command]
async fn remove_device(sidecar: State<'_, SidecarManager>, id: String) -> Result<(), String> {
    sidecar
        .send_command("remove_device", serde_json::json!({"id": id}))
        .await?;
    Ok(())
}

#[tauri::command]
async fn hide_device(sidecar: State<'_, SidecarManager>, id: String) -> Result<(), String> {
    sidecar
        .send_command("hide_device", serde_json::json!({"id": id}))
        .await?;
    Ok(())
}

/// 取消隐藏设备。
///
/// Sidecar 侧早有 `unhide_device`，但壳层没暴露、前端没调用，于是"隐藏"成了单向门：
/// 手滑隐藏一台，界面上就再也找不回来，只能手动改 `hancast_setting.json`。
#[tauri::command]
async fn unhide_device(sidecar: State<'_, SidecarManager>, id: String) -> Result<(), String> {
    sidecar
        .send_command("unhide_device", serde_json::json!({"id": id}))
        .await?;
    Ok(())
}

/// 隐藏设备列表（带名称与在离线，供设置页管理与一键恢复）
#[tauri::command]
async fn get_hidden_devices(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("get_hidden_devices", serde_json::json!({}))
        .await
}

/// 删除一条手动添加的设备地址（不动已发现的设备）
///
/// 与 `remove_device` 的语义相反：那条是删设备并连带摘地址，这里只摘地址——
/// 填错的 IP 会每次开机自动重连一遍，之前没有任何入口能删掉它。
#[tauri::command]
async fn remove_manual_device(
    sidecar: State<'_, SidecarManager>,
    target: String,
) -> Result<(), String> {
    sidecar
        .send_command("remove_manual_device", serde_json::json!({"target": target}))
        .await?;
    Ok(())
}

/// 手动添加设备：组播被系统/安全软件拦截时的兜底通道
#[tauri::command]
async fn add_device(sidecar: State<'_, SidecarManager>, address: String) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("add_device", serde_json::json!({"address": address}))
        .await
}

/// 把「本机」加进设备列表：用于自己投自己（本机也是 DLNA 渲染端）
#[tauri::command]
async fn add_local_device(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("add_local_device", serde_json::json!({}))
        .await
}

#[tauri::command]
async fn get_manual_devices(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("get_manual_devices", serde_json::json!({}))
        .await
}

// ── Media parsing ──
// Frontend has two separate commands; Python has one `parse_media` with type param.

#[tauri::command]
async fn parse_media_file(
    sidecar: State<'_, SidecarManager>,
    file_path: String,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "parse_media",
            serde_json::json!({"type": "file", "path": file_path}),
        )
        .await
}

/// 列出文件夹里的媒体文件（投文件夹 / 播放队列）：`{folder, files: [...]}`
/// JS 侧传 `folderPath`（camelCase），Tauri 默认按 snake_case 映射到 `folder_path`。
#[tauri::command]
async fn list_folder_media(
    sidecar: State<'_, SidecarManager>,
    folder_path: String,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "list_folder_media",
            serde_json::json!({"path": folder_path}),
        )
        .await
}

#[tauri::command]
async fn parse_media_url(
    sidecar: State<'_, SidecarManager>,
    url: String,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "parse_media",
            serde_json::json!({"type": "url", "url": url}),
        )
        .await
}

/// 从任意粘贴文本（分享口令/短链/网页/直链）解析可播放媒体信息
/// sidecar 后台线程执行，返回 {task_id}；结果经 get_resolve_result 轮询
#[tauri::command]
async fn resolve_share(
    sidecar: State<'_, SidecarManager>,
    text: String,
    quality: Option<String>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "resolve_share",
            serde_json::json!({"text": text, "quality": quality.unwrap_or_default()}),
        )
        .await
}

#[tauri::command]
async fn get_resolve_result(
    sidecar: State<'_, SidecarManager>,
    task_id: String,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("get_resolve_result", serde_json::json!({"task_id": task_id}))
        .await
}

// ── 解析插件（JS 规则包）管理 ──

#[tauri::command]
async fn plugin_list(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar.send_command("plugin_list", serde_json::json!({})).await
}

/// 规则总览：脚本包 + 声明式 JSON，一屏列全
#[tauri::command]
async fn rule_inventory(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar.send_command("rule_inventory", serde_json::json!({})).await
}

/// 解析失败台账（按域名分组）
#[tauri::command]
async fn failure_list(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar.send_command("failure_list", serde_json::json!({})).await
}

/// 删台账里一个域名或其中一条地址
#[tauri::command]
async fn failure_delete(
    sidecar: State<'_, SidecarManager>,
    host: Option<String>,
    url: Option<String>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "failure_delete",
            serde_json::json!({
                "host": host.unwrap_or_default(),
                "url": url.unwrap_or_default(),
            }),
        )
        .await
}

/// 清空解析失败台账
#[tauri::command]
async fn failure_clear(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar.send_command("failure_clear", serde_json::json!({})).await
}

/// 投屏（出站）失败台账
#[tauri::command]
async fn cast_failure_list(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar.send_command("cast_failure_list", serde_json::json!({})).await
}

/// 清空投屏失败台账
#[tauri::command]
async fn cast_failure_clear(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar.send_command("cast_failure_clear", serde_json::json!({})).await
}

/// 1 秒预算探一次地址是否还活着（历史条目重投前预检）
#[tauri::command]
async fn probe_url(
    sidecar: State<'_, SidecarManager>,
    url: String,
    timeout: Option<f64>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "probe_url",
            serde_json::json!({
                "url": url,
                "timeout": timeout.unwrap_or(1.0),
            }),
        )
        .await
}

/// 规则包的慢操作改走后台任务 + 轮询。
///
/// 之前 `plugin_check_updates` / `plugin_pull` / `plugin_import` 直接在 Sidecar
/// 的串行命令通道里跑网络下载：一次慢索引能把停止投屏、投屏确认一起卡住，
/// 而壳层 60s 超时只是「不再等这条响应」，后端照样会把包装完——用户看到失败，
/// 包却悄悄换了。轮询把响应窗口和执行时长解耦；总预算由后端任务持有（90s），
/// 这里 150s 只是兜底，超时先请求取消再报错，不自动重提同一个安装。
async fn run_plugin_task(
    sidecar: &SidecarManager,
    operation: &str,
    params: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let started = sidecar
        .send_command(
            "plugin_task_start",
            serde_json::json!({"operation": operation, "params": params}),
        )
        .await?;
    let task_id = started
        .get("task_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "规则包任务未返回 task_id".to_string())?;

    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(150);
    loop {
        let entry = sidecar
            .send_command(
                "plugin_task_result",
                serde_json::json!({"task_id": task_id}),
            )
            .await?;
        match entry.get("status").and_then(|v| v.as_str()).unwrap_or("") {
            "done" => {
                return Ok(entry
                    .get("result")
                    .cloned()
                    .unwrap_or(serde_json::Value::Null))
            }
            "error" => {
                return Err(entry
                    .get("error")
                    .and_then(|v| v.as_str())
                    .unwrap_or("规则包操作失败")
                    .to_string())
            }
            "cancelled" => return Err("规则包操作已取消".to_string()),
            "pending" => {}
            other => return Err(format!("规则包任务状态未知: {other}")),
        }
        if tokio::time::Instant::now() >= deadline {
            let _ = sidecar
                .send_command("plugin_task_cancel", serde_json::json!({"task_id": task_id}))
                .await;
            return Err("规则包操作超时，已请求取消".to_string());
        }
        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
    }
}

/// 对照官方索引看有哪些包可更新 / 可安装
#[tauri::command]
async fn plugin_check_updates(
    sidecar: State<'_, SidecarManager>,
    index_url: Option<String>,
) -> Result<serde_json::Value, String> {
    run_plugin_task(
        &sidecar,
        "plugin_check_updates",
        serde_json::json!({"index_url": index_url.unwrap_or_default()}),
    )
    .await
}

/// 按索引装/更新一个包（sha256 校验不过不落盘）
#[tauri::command]
async fn plugin_pull(
    sidecar: State<'_, SidecarManager>,
    name: String,
    index_url: Option<String>,
) -> Result<serde_json::Value, String> {
    run_plugin_task(
        &sidecar,
        "plugin_pull",
        serde_json::json!({"name": name, "url": index_url.unwrap_or_default()}),
    )
    .await
}

/// 导入插件 ZIP：file_path 本地路径 / data_base64 原始字节 / url 远程地址，三选一
#[tauri::command]
async fn plugin_import(
    sidecar: State<'_, SidecarManager>,
    file_path: Option<String>,
    data_base64: Option<String>,
    url: Option<String>,
) -> Result<serde_json::Value, String> {
    run_plugin_task(
        &sidecar,
        "plugin_import",
        serde_json::json!({
            "path": file_path,
            "data_base64": data_base64,
            "url": url,
        }),
    )
    .await
}

#[tauri::command]
async fn plugin_remove(
    sidecar: State<'_, SidecarManager>,
    name: String,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("plugin_remove", serde_json::json!({"name": name}))
        .await
}

#[tauri::command]
async fn plugin_set_rule_enabled(
    sidecar: State<'_, SidecarManager>,
    plugin: String,
    rule: String,
    enabled: bool,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "plugin_set_rule_enabled",
            serde_json::json!({"plugin": plugin, "rule": rule, "enabled": enabled}),
        )
        .await
}

/// 用一段分享文本试跑某插件；返回 {task_id}，结果经 get_resolve_result 轮询
#[tauri::command]
async fn plugin_test(
    sidecar: State<'_, SidecarManager>,
    plugin: String,
    text: String,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("plugin_test", serde_json::json!({"plugin": plugin, "text": text}))
        .await
}

/// 读取系统剪贴板文本。
///
/// WKWebView 默认不授予页面 clipboard-read 权限，前端
/// `navigator.clipboard.readText()` 在 macOS 上静默 reject，
/// 「粘贴」按钮因此失效——改由 Rust 侧（已装的 clipboard 插件）读取。
#[tauri::command]
fn read_clipboard(app: tauri::AppHandle) -> Result<String, String> {
    app.clipboard().read_text().map_err(|e| e.to_string())
}

/// 写入系统剪贴板。前端 `navigator.clipboard.writeText()` 的 reject
/// 不受同步 try/catch 保护，Web API 失败时需要有确定可用的通道。
#[tauri::command]
fn write_clipboard(app: tauri::AppHandle, text: String) -> Result<(), String> {
    app.clipboard()
        .write_text(text)
        .map_err(|e| e.to_string())
}

// ── Cast control ──

/// 把外挂字幕封进当前视频容器，并让同一台电视重投（带字幕）
#[tauri::command]
async fn subtitle_apply(
    sidecar: State<'_, SidecarManager>,
    device_id: String,
    subtitle_path: String,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "subtitle_apply",
            serde_json::json!({ "device_id": device_id, "subtitle_path": subtitle_path }),
        )
        .await
}

/// 查询字幕内封任务进度
#[tauri::command]
async fn subtitle_status(
    sidecar: State<'_, SidecarManager>,
    device_id: String,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("subtitle_status", serde_json::json!({ "device_id": device_id }))
        .await
}

/// 启动手机遥控器页（局域网浏览器打开即用）
#[tauri::command]
async fn start_remote_control(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("start_remote_control", serde_json::json!({}))
        .await
}

/// 关闭手机遥控器页
#[tauri::command]
async fn stop_remote_control(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("stop_remote_control", serde_json::json!({}))
        .await
}

/// 查询手机遥控器页是否在跑、地址是什么
#[tauri::command]
async fn remote_status(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("remote_status", serde_json::json!({}))
        .await
}

/// 把前端权威状态（队列/历史/播放模式/播完即停）同步给遥控器页
#[tauri::command]
async fn remote_update_ui(
    sidecar: State<'_, SidecarManager>,
    payload: serde_json::Value,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("remote_update_ui", payload)
        .await
}


#[tauri::command]
async fn start_cast(
    sidecar: State<'_, SidecarManager>,
    device_id: String,
    media_uri: String,
    mime_type: Option<String>,
) -> Result<(), String> {
    sidecar
        .send_command(
            "start_cast",
            serde_json::json!({
                "device_id": device_id,
                "media_uri": media_uri,
                "mime_type": mime_type,
            }),
        )
        .await?;
    Ok(())
}

#[tauri::command]
async fn stop_cast(
    sidecar: State<'_, SidecarManager>,
    device_id: Option<String>,
) -> Result<serde_json::Value, String> {
    // 返回体里的 control_failed 不能丢：本地会话已释放、但电视没收到 Stop 时，
    // 前端要靠它解释「界面停了、电视还在播」。
    sidecar
        .send_command("stop_cast", serde_json::json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn pause_cast(
    sidecar: State<'_, SidecarManager>,
    device_id: Option<String>,
) -> Result<(), String> {
    sidecar
        .send_command("pause_cast", serde_json::json!({"device_id": device_id}))
        .await?;
    Ok(())
}

#[tauri::command]
async fn resume_cast(
    sidecar: State<'_, SidecarManager>,
    device_id: Option<String>,
) -> Result<(), String> {
    sidecar
        .send_command("resume_cast", serde_json::json!({"device_id": device_id}))
        .await?;
    Ok(())
}

#[tauri::command]
async fn seek_cast(
    sidecar: State<'_, SidecarManager>,
    position: String,
    device_id: Option<String>,
) -> Result<(), String> {
    sidecar
        .send_command(
            "seek_cast",
            serde_json::json!({"position": position, "device_id": device_id}),
        )
        .await?;
    Ok(())
}

#[tauri::command]
async fn get_cast_state(
    sidecar: State<'_, SidecarManager>,
    device_id: Option<String>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("get_cast_state", serde_json::json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn get_cast_url(
    sidecar: State<'_, SidecarManager>,
    device_id: Option<String>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("get_cast_url", serde_json::json!({"device_id": device_id}))
        .await
}

#[tauri::command]
async fn set_volume(
    sidecar: State<'_, SidecarManager>,
    volume: u32,
    device_id: Option<String>,
) -> Result<u32, String> {
    let result = sidecar
        .send_command(
            "set_volume",
            serde_json::json!({"volume": volume, "device_id": device_id}),
        )
        .await?;
    Ok(result.as_u64().unwrap_or(volume as u64) as u32)
}

#[tauri::command]
async fn set_mute(
    sidecar: State<'_, SidecarManager>,
    muted: bool,
    device_id: Option<String>,
) -> Result<(), String> {
    sidecar
        .send_command(
            "set_mute",
            serde_json::json!({"muted": muted, "device_id": device_id}),
        )
        .await?;
    Ok(())
}

// ── Settings ──

#[tauri::command]
async fn get_settings(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar.send_command("get_settings", serde_json::json!({})).await
}

#[tauri::command]
async fn save_settings(
    sidecar: State<'_, SidecarManager>,
    settings: serde_json::Value,
) -> Result<(), String> {
    sidecar
        .send_command("save_settings", serde_json::json!({"settings": settings}))
        .await?;
    Ok(())
}

#[tauri::command]
async fn list_network_interfaces(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("list_network_interfaces", serde_json::json!({}))
        .await
}

// ── 后台可调参数（改参数不用重新编译）──

#[tauri::command]
async fn get_params(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar.send_command("get_params", serde_json::json!({})).await
}

#[tauri::command]
async fn save_params(
    sidecar: State<'_, SidecarManager>,
    values: serde_json::Value,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("save_params", serde_json::json!({"values": values}))
        .await
}

// ── Update Check ──

#[tauri::command]
async fn check_update(
    sidecar: State<'_, SidecarManager>,
    current_version: String,
    force: Option<bool>,
) -> Result<serde_json::Value, String> {
    // v2.4.11 及之前这里硬编码 `"sources": ["github", "gitee"]`，gitee 那条源已在
    // Sidecar 里删除（`gitee.com/buxiangqumingzi/han-cast` 是**原作者**那条线，
    // 它的 releases/latest 停在 v2.1.0，GitHub 一不通就顶上来，用户看到的「新版本」
    // 指向原作者的下载页），入参不再下发。
    sidecar
        .send_command(
            "check_update",
            serde_json::json!({
                "current_version": current_version,
                "force": force.unwrap_or(false),
            }),
        )
        .await
}

#[tauri::command]
async fn ignore_update_version(
    sidecar: State<'_, SidecarManager>,
    version: String,
) -> Result<bool, String> {
    let result = sidecar
        .send_command("ignore_update_version", serde_json::json!({"version": version}))
        .await?;
    Ok(result.as_bool().unwrap_or(false))
}

// ── Device Guard (投屏确认) ──

#[tauri::command]
async fn respond_cast_confirm(
    sidecar: State<'_, SidecarManager>,
    request_id: String,
    approved: bool,
    policy: String,
) -> Result<bool, String> {
    let result = sidecar
        .send_command(
            "respond_cast_confirm",
            serde_json::json!({"request_id": request_id, "approved": approved, "policy": policy}),
        )
        .await?;
    Ok(result.as_bool().unwrap_or(false))
}

#[tauri::command]
async fn get_guard_devices(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("get_guard_devices", serde_json::json!({}))
        .await
}

#[tauri::command]
async fn remove_guard_device(
    sidecar: State<'_, SidecarManager>,
    device_key: String,
) -> Result<bool, String> {
    let result = sidecar
        .send_command(
            "remove_guard_device",
            serde_json::json!({"device_key": device_key}),
        )
        .await?;
    Ok(result.as_bool().unwrap_or(false))
}

#[tauri::command]
async fn set_guard_policy(
    sidecar: State<'_, SidecarManager>,
    device_key: String,
    policy: String,
) -> Result<bool, String> {
    let result = sidecar
        .send_command(
            "set_guard_policy",
            serde_json::json!({"device_key": device_key, "policy": policy}),
        )
        .await?;
    Ok(result.as_bool().unwrap_or(false))
}

#[tauri::command]
async fn get_guard_settings(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("get_guard_settings", serde_json::json!({}))
        .await
}

#[tauri::command]
async fn save_guard_settings(
    sidecar: State<'_, SidecarManager>,
    settings: serde_json::Value,
) -> Result<(), String> {
    sidecar
        .send_command("save_guard_settings", settings)
        .await?;
    Ok(())
}

// ── Microsoft Store Detection ──

#[tauri::command]
fn is_store_version() -> bool {
    let exe_path = std::env::current_exe().ok();
    if let Some(path) = exe_path {
        let path_str = path.to_string_lossy().to_lowercase();
        // 检测条件：
        // 1. 路径包含 "windowsapps"（微软商店标准路径）
        // 2. 路径包含 "553787e6.hancast"（MSIX Identity Name）
        // 3. 路径包含 "build-msix"（本地 MSIX 注册安装）
        return path_str.contains("windowsapps")
            || path_str.contains("553787e6.hancast")
            || path_str.contains("build-msix");
    }
    false
}

// ── MPV Management ──

#[tauri::command]
async fn check_mpv(sidecar: State<'_, SidecarManager>) -> Result<serde_json::Value, String> {
    sidecar.send_command("check_mpv", serde_json::json!({})).await
}

#[tauri::command]
async fn set_mpv_path(
    sidecar: State<'_, SidecarManager>,
    path: String,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("set_mpv_path", serde_json::json!({"path": path}))
        .await
}

// ── Screen Cast (电脑投屏：DLNA 电视 / 浏览器观看，两路共用 HLS) ──

#[tauri::command]
async fn list_screen_sources(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("list_screen_sources", serde_json::json!({}))
        .await
}

#[tauri::command]
async fn start_screen_cast(
    sidecar: State<'_, SidecarManager>,
    params: serde_json::Value,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("start_screen_cast", params)
        .await
}

#[tauri::command]
async fn stop_screen_cast(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("stop_screen_cast", serde_json::json!({}))
        .await
}

#[tauri::command]
async fn screen_cast_status(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("screen_cast_status", serde_json::json!({}))
        .await
}

#[tauri::command]
async fn set_ffmpeg_path(
    sidecar: State<'_, SidecarManager>,
    path: String,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("set_ffmpeg_path", serde_json::json!({"path": path}))
        .await
}

/// FFmpeg/FFprobe 检测报告（refresh=true 时重新扫描本机）
#[tauri::command]
async fn ffmpeg_status(
    sidecar: State<'_, SidecarManager>,
    refresh: Option<bool>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "ffmpeg_status",
            serde_json::json!({"refresh": refresh.unwrap_or(false)}),
        )
        .await
}

#[tauri::command]
async fn ffmpeg_download_start(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("ffmpeg_download_start", serde_json::json!({}))
        .await
}

#[tauri::command]
async fn ffmpeg_download_status(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("ffmpeg_download_status", serde_json::json!({}))
        .await
}

// ── 手机镜像接收（Android → 本机，adb + scrcpy）──

#[tauri::command]
async fn mirror_status(
    sidecar: State<'_, SidecarManager>,
    refresh: Option<bool>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "mirror_status",
            serde_json::json!({"refresh": refresh.unwrap_or(false)}),
        )
        .await
}

#[tauri::command]
async fn mirror_devices(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("mirror_devices", serde_json::json!({}))
        .await
}

#[tauri::command]
async fn mirror_connect(
    sidecar: State<'_, SidecarManager>,
    address: String,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("mirror_connect", serde_json::json!({"address": address}))
        .await
}

#[tauri::command]
async fn mirror_start(
    sidecar: State<'_, SidecarManager>,
    params: serde_json::Value,
) -> Result<serde_json::Value, String> {
    sidecar.send_command("mirror_start", params).await
}

#[tauri::command]
async fn mirror_stop(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("mirror_stop", serde_json::json!({}))
        .await
}

#[tauri::command]
async fn mirror_set_tool_path(
    sidecar: State<'_, SidecarManager>,
    tool: String,
    path: String,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "mirror_set_tool_path",
            serde_json::json!({"tool": tool, "path": path}),
        )
        .await
}

// ── 应用内更新（挑产物 → 下载 → 校验 → 装）──
//
// 分工：Sidecar 负责「取清单、下文件、算摘要、说该怎么装」，本文件只负责真的动手。
// 理由是权限与顺序：替换正在运行的 app 必须**在我们退出之后**发生，而孩子（Sidecar）
// 去覆盖父亲（主程序）的二进制、再把父亲叫醒，两头都不对。
// 路径一律由 Sidecar 给出（它自己下的文件），不接受前端传路径——同 `open_ffmpeg_dir`。

#[tauri::command]
async fn update_release_info(
    sidecar: State<'_, SidecarManager>,
    version: Option<String>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "update_release_info",
            serde_json::json!({"version": version.unwrap_or_default()}),
        )
        .await
}

#[tauri::command]
async fn update_download_start(
    sidecar: State<'_, SidecarManager>,
    asset: Option<String>,
    version: Option<String>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "update_download_start",
            serde_json::json!({
                "asset": asset.unwrap_or_default(),
                "version": version.unwrap_or_default(),
            }),
        )
        .await
}

#[tauri::command]
async fn update_download_status(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("update_download_status", serde_json::json!({}))
        .await
}

#[tauri::command]
async fn update_download_cancel(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("update_download_cancel", serde_json::json!({}))
        .await
}

/// 已下好的产物该怎么装（Sidecar 的 `update_install_plan` 原样转发）。
///
/// 这条必须有：前端在 `download_status` 落到 done 之后会单独取一次装法，好把
/// 「退出并安装」与「复制 sudo apt install …」两种主按钮分开渲染。少这一条转发，
/// 打包版里那次 invoke 直接 reject，界面拿不到 plan 就把「退出并安装」永久置灰
/// ——浏览器 mock 有 `case 'update_install_plan'`，所以只有装好的产物上才暴露。
#[tauri::command]
async fn update_install_plan(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("update_install_plan", serde_json::json!({}))
        .await
}

/// 把安装脚本写到临时目录，返回脚本路径。
///
/// 必须是**脚本**而不是直接 spawn 安装包：主程序一退出，安装程序就得在我们已经
/// 不在的时候继续干活，而 `Command::spawn` 的子进程会跟着父进程的退出被留在原地——
/// 谁也不知道它等到没有。脚本里那句 `sleep` 就是给退出流程留的余量。
/// macOS 尤其明显：`cp -R` 覆盖一个正在运行的 .app，轻则报「设备忙」，
/// 重则把半份新包留在 /Applications 里。
fn write_apply_helper(kind: &str, path: &str, log_path: &str) -> Result<String, String> {
    let script = if cfg!(windows) {
        let exe = std::env::current_exe()
            .map_err(|e| format!("取不到自身可执行路径: {e}"))?
            .to_string_lossy()
            .replace('/', "\\");
        WIN_APPLY
            .replace("{{INSTALLER}}", &path.replace('/', "\\"))
            .replace("{{EXE}}", &exe)
    } else if kind == "dmg" {
        // 替换目标 = 我们自己所在的 .app。倒推不出来就是开发布局（`cargo run` 跑的是
        // target/debug 里的裸二进制），那时**只把 dmg 打开**让用户自己拖——
        // 去动 target 里那份会立刻毁掉下一次调试，而且装完也起不来。
        match macos_bundle_path() {
            Some(bundle) => MAC_APPLY
                .replace("{{DMG}}", path)
                .replace("{{DST}}", &bundle)
                .replace("{{LOG}}", log_path),
            None => DEV_FALLBACK
                .replace("{{DMG}}", path)
                .replace("{{LOG}}", log_path),
        }
    } else {
        LINUX_APPLY
            .replace("{{IMG}}", path)
            .replace("{{LOG}}", log_path)
    };

    let mut file = std::env::temp_dir();
    file.push(if cfg!(windows) {
        "hancast-apply.cmd"
    } else {
        "hancast-apply.sh"
    });
    std::fs::write(&file, script.as_bytes()).map_err(|e| format!("写安装脚本失败: {e}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o755))
            .map_err(|e| format!("安装脚本不可执行: {e}"))?;
    }
    Ok(file.to_string_lossy().to_string())
}

// 三份模板都**先备份再替换、失败就回滚**：装坏了还能起来，比留下一份半包强。
// 路径一律加引号——macOS 的配置目录里就有空格（Application Support）。

const WIN_APPLY: &str = r#"@echo off
timeout /t 3 /nobreak >nul
start "" /wait "{{INSTALLER}}" /S
if %ERRORLEVEL%==0 start "" "{{EXE}}"
del "%~f0"
"#;

const MAC_APPLY: &str = r#"#!/bin/sh
LOG="{{LOG}}"
DMG="{{DMG}}"
DST="{{DST}}"
BAK="/tmp/HanCast_prev_$(date +%s).app"
echo "=== $(date) 自更新 $DMG -> $DST" >> "$LOG" 2>&1
# 等父进程退干净：正在运行的 .app 里做 mv/cp，轻则 Device busy，重则半份新包留在原地
sleep 2
MOUNT=$(hdiutil attach -nobrowse -noverify -noautoopen "$DMG" | awk -F'\t' '/\/Volumes\// {print $NF; exit}')
if [ -z "$MOUNT" ]; then
  echo "挂载失败，改为打开镜像由你手动拖拽" >> "$LOG" 2>&1
  open "$DMG"
  exit 1
fi
SRC="$MOUNT/HanCast.app"
if [ ! -d "$SRC" ]; then
  echo "镜像里找不到 HanCast.app（$MOUNT）" >> "$LOG" 2>&1
  hdiutil detach "$MOUNT" -force >> "$LOG" 2>&1
  open "$DMG"
  exit 1
fi
if ! mv "$DST" "$BAK"; then
  echo "旧版挪不动（多半是 /Applications 权限），改为打开镜像" >> "$LOG" 2>&1
  hdiutil detach "$MOUNT" -force >> "$LOG" 2>&1
  open "$DMG"
  exit 1
fi
if ! cp -R "$SRC" "$(dirname "$DST")"; then
  echo "复制失败，回滚旧版" >> "$LOG" 2>&1
  mv "$BAK" "$DST"
  hdiutil detach "$MOUNT" -force >> "$LOG" 2>&1
  open "$DMG"
  exit 1
fi
hdiutil detach "$MOUNT" -force >> "$LOG" 2>&1
rm -rf "$BAK"
# linker-signed 的包换过一次磁盘位置，Launch Services 未必认新 inode；touch 让它重新登记
touch "$DST"
echo "替换完成：$DST" >> "$LOG" 2>&1
open "$DST"
"#;

/// 开发布局的兜底：不替换任何东西，只把 dmg 打开。
const DEV_FALLBACK: &str = r#"#!/bin/sh
echo "=== $(date) 当前不是 .app 布局，跳过替换，改为打开镜像" >> "{{LOG}}" 2>&1
sleep 2
open "{{DMG}}"
"#;

const LINUX_APPLY: &str = r#"#!/bin/sh
LOG="{{LOG}}"
IMG="{{IMG}}"
echo "=== $(date) 改起新 AppImage $IMG" >> "$LOG" 2>&1
sleep 2
chmod +x "$IMG"
nohup "$IMG" >/dev/null 2>&1 &
"#;

/// macOS：从自身可执行路径倒推 .app 包路径（`/Applications/HanCast.app`）。
/// 不在 .app 里（开发构建）就回 None —— 那条路上没有可替换的包。
fn macos_bundle_path() -> Option<String> {
    let exe = std::env::current_exe().ok()?;
    let parents = exe.parent()?.parent()?.parent()?;
    let name = parents.file_name()?.to_string_lossy().to_string();
    if name.ends_with(".app") {
        Some(parents.to_string_lossy().to_string())
    } else {
        None
    }
}

/// spawn 一个**活过本进程**的脚本：新进程组 + Windows 侧 DETACHED_PROCESS。
/// 用 `app.shell().command()` 那条路（sidecar.rs 用的同一个插件）在这里不合适：
/// 壳层退出时插件会收拾它 spawn 的东西，而我们要的恰恰是「我们走了它还得继续」。
fn spawn_survivor(script: &str) -> Result<(), String> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const DETACHED_PROCESS: u32 = 0x0000_0008;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        std::process::Command::new("cmd")
            .args(["/C", script])
            .creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
            .spawn()
            .map_err(|e| format!("启动安装程序失败: {e}"))?;
        Ok(())
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let mut cmd = std::process::Command::new("/bin/sh");
        cmd.arg(script);
        // 新进程组：本程序退出时发出的信号不会顺带把安装脚本打死
        cmd.process_group(0);
        cmd.spawn()
            .map_err(|e| format!("启动安装脚本失败: {e}"))?;
        Ok(())
    }
}

/// 应用刚下好的产物。`kind == "command"`（deb/rpm/Arch）不代跑：
/// 那要提权，壳层不该替用户 sudo，于是把计划原样退回，让界面给复制按钮。
#[tauri::command]
async fn update_apply(
    app: tauri::AppHandle,
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    let plan = sidecar
        .send_command("update_install_plan", serde_json::json!({}))
        .await?;
    let kind = plan
        .get("kind")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let path = plan
        .get("path")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if path.is_empty() {
        return Err("更新产物路径为空".to_string());
    }
    if kind == "command" {
        return Ok(serde_json::json!({"ok": false, "manual": true, "plan": plan}));
    }

    // 日志落在产物旁边：应用已经退了，程序日志没人再看，而这条链最需要事后取证
    let mut log_path = std::path::PathBuf::from(&path);
    log_path.set_file_name("update-apply.log");
    let helper = write_apply_helper(&kind, &path, &log_path.to_string_lossy())?;
    spawn_survivor(&helper)?;

    // 先回话再退出：退出是异步的（600ms），前端要能收到「已开始安装」这条回复，
    // 否则界面停在按钮上转圈，用户以为程序挂了。
    let handle = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(std::time::Duration::from_millis(600));
        handle.exit(0);
    });
    Ok(serde_json::json!({
        "ok": true,
        "kind": kind,
        "path": path,
        "log": log_path.to_string_lossy(),
        "helper": helper,
    }))
}

/// 打开存放更新产物的目录（路径由 Sidecar 的任务表给出，同 `open_ffmpeg_dir` 的理由）
#[tauri::command]
async fn open_update_dir(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    let task = sidecar
        .send_command("update_download_status", serde_json::json!({}))
        .await?;
    // 优先用任务表里的 `dir`（Sidecar 每次都会报，包括还没下载的时候）；
    // 只剩 `dest` 的旧 sidecar 就取它的父目录，两条都拿不到才失败。
    let dir = task
        .get("dir")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string())
        .or_else(|| {
            task.get("dest")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .and_then(|p| std::path::Path::new(p).parent())
                .map(|d| d.to_string_lossy().to_string())
        })
        .ok_or_else(|| "Sidecar 未返回更新产物目录".to_string())?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("目录创建失败: {e}"))?;
    if cfg!(target_os = "macos") {
        std::process::Command::new("open").arg(&dir).spawn()
    } else if cfg!(windows) {
        std::process::Command::new("explorer").arg(&dir).spawn()
    } else {
        std::process::Command::new("xdg-open").arg(&dir).spawn()
    }
    .map_err(|e| format!("无法打开目录: {e}"))?;
    Ok(serde_json::json!({"ok": true, "path": dir}))
}

/// 在系统文件管理器里打开「本程序下载 FFmpeg 用的目录」。
/// 路径由 Sidecar 给出（程序自己的配置目录），不接受前端传路径——
/// 否则等于开放「用资源管理器打开任意路径」。
#[tauri::command]
async fn open_ffmpeg_dir(
    sidecar: State<'_, SidecarManager>,
) -> Result<serde_json::Value, String> {
    let report = sidecar
        .send_command("ffmpeg_status", serde_json::json!({"refresh": false}))
        .await?;
    let dir = report
        .get("managed_dir")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .ok_or("Sidecar 未返回 FFmpeg 目录")?
        .to_string();
    std::fs::create_dir_all(&dir).map_err(|e| format!("目录创建失败: {e}"))?;

    // 只 spawn 不 wait：xdg-open 会活到文件管理器关掉为止，等它就等于把一个
    // 按钮按死在命令循环里；而 explorer 常返回非 0 却照样开窗，退出码本来就不是判据。
    if cfg!(target_os = "macos") {
        std::process::Command::new("open").arg(&dir).spawn()
    } else if cfg!(windows) {
        std::process::Command::new("explorer").arg(&dir).spawn()
    } else {
        std::process::Command::new("xdg-open").arg(&dir).spawn()
    }
    .map_err(|e| format!("无法打开目录: {e}"))?;
    // 成败由调用方按「有没有窗口」判断，这里只把路径回显出去。
    Ok(serde_json::json!({"ok": true, "path": dir}))
}

/// 系统通知。macOS 走 `UNUserNotificationCenter`（理由见 `macos_notify` 里的实测记录），
/// 其余平台返回错误，让前端退回 `tauri-plugin-notification`——那两条路径是好的。
#[tauri::command]
fn notify_os(app: tauri::AppHandle, title: String, body: String) -> Result<(), String> {
    // UN 的授权弹窗与投递都要在主的 run loop 上走，从命令的工作线程直接调会被系统判成「不允许」。
    #[cfg(target_os = "macos")]
    {
        app.run_on_main_thread(move || {
            if let Err(e) = macos_notify::send(&title, &body) {
                macos_notify::log(&format!("通知失败：{e}"));
            }
        })
        .map_err(|e| format!("无法排到主线程：{e}"))
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (app, title, body);
        Err("本平台由前端走通知插件".into())
    }
}


// ── Export Logs ──

/// 应用内查看日志（分模块/级别/关键字过滤），排错时不用再去翻文件
#[tauri::command]
async fn get_logs(
    sidecar: State<'_, SidecarManager>,
    params: serde_json::Value,
) -> Result<serde_json::Value, String> {
    sidecar.send_command("get_logs", params).await
}

#[tauri::command]
async fn set_log_level(
    sidecar: State<'_, SidecarManager>,
    level: String,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command("set_log_level", serde_json::json!({"level": level}))
        .await
}

/// 把 WebView 里的报错写进同一份 hancast.log（前端 app.config.errorHandler /
/// window.onerror / unhandledrejection 都走这里）。
/// 为什么需要：界面上一块区域整个不见这类问题只发生在 WebView 里，Python 侧
/// 毫无线索，用户在日志面板里查不到任何东西（2026-11-03 的「媒体输入区消失」）。
#[tauri::command]
async fn frontend_log(
    sidecar: State<'_, SidecarManager>,
    level: Option<String>,
    message: String,
    stack: Option<String>,
    source: Option<String>,
) -> Result<serde_json::Value, String> {
    sidecar
        .send_command(
            "frontend_log",
            serde_json::json!({
                "level": level.unwrap_or_else(|| "error".into()),
                "message": message,
                "stack": stack.unwrap_or_default(),
                "source": source.unwrap_or_else(|| "webview".into()),
            }),
        )
        .await
}

#[tauri::command]
async fn export_logs(app: tauri::AppHandle) -> Result<bool, String> {
    use tauri_plugin_dialog::DialogExt;

    // 获取日志文件路径（跨平台，与 Python 后端 logger.py 一致）
    let log_path = if cfg!(target_os = "windows") {
        // Windows: %APPDATA%/HanCast/logs/hancast.log
        let app_data = std::env::var("APPDATA")
            .map_err(|_| "无法获取 APPDATA 目录".to_string())?;
        PathBuf::from(app_data).join("HanCast").join("logs").join("hancast.log")
    } else if cfg!(target_os = "macos") {
        // macOS: ~/Library/Application Support/HanCast/logs/hancast.log
        let home = std::env::var("HOME")
            .map_err(|_| "无法获取 HOME 目录".to_string())?;
        PathBuf::from(home).join("Library").join("Application Support").join("HanCast").join("logs").join("hancast.log")
    } else {
        // Linux: ~/.config/hancast/logs/hancast.log
        let home = std::env::var("HOME")
            .map_err(|_| "无法获取 HOME 目录".to_string())?;
        PathBuf::from(home).join(".config").join("hancast").join("logs").join("hancast.log")
    };

    if !log_path.exists() {
        return Err("日志文件不存在".to_string());
    }

    // 打开文件保存对话框
    let file_path = app
        .dialog()
        .file()
        .set_title("导出日志")
        .set_file_name("hancast.log")
        .add_filter("日志文件", &["log"])
        .add_filter("所有文件", &["*"])
        .blocking_save_file();

    match file_path {
        Some(path) => {
            let path_buf = path.into_path().map_err(|e| format!("Invalid path: {e}"))?;
            std::fs::copy(&log_path, &path_buf)
                .map_err(|e| format!("复制日志文件失败: {e}"))?;
            Ok(true)
        }
        None => Ok(false), // 用户取消了对话框
    }
}

/// 托盘菜单项：复制当前投屏地址（固定文本，有 URL 时可点击）
const CAST_URL_LABEL: &str = "复制当前投屏地址";

/// MSIX StartupTask ID（与 AppxManifest.xml 中的 TaskId 一致）
#[cfg(target_os = "windows")]
const STARTUP_TASK_ID: &str = "HanCastStartup";

// ── Autostart ──

/// MSIX 环境下获取开机自启状态（使用 WinRT StartupTask API）
#[cfg(target_os = "windows")]
async fn get_autostart_msix() -> Result<bool, String> {
    let task_id = HSTRING::from(STARTUP_TASK_ID);
    let task = StartupTask::GetAsync(&task_id)
        .map_err(|e| format!("获取启动任务失败: {e}"))?
        .await
        .map_err(|e| format!("等待启动任务失败: {e}"))?;

    let state = task
        .State()
        .map_err(|e| format!("获取启动任务状态失败: {e}"))?;

    Ok(state == StartupTaskState::Enabled)
}

/// MSIX 环境下设置开机自启状态（使用 WinRT StartupTask API）
/// 参数通过 uap10:Parameters="--autostart" 在 AppxManifest.xml 中配置
#[cfg(target_os = "windows")]
async fn set_autostart_msix(enabled: bool) -> Result<(), String> {
    let task_id = HSTRING::from(STARTUP_TASK_ID);
    let task = StartupTask::GetAsync(&task_id)
        .map_err(|e| format!("获取启动任务失败: {e}"))?
        .await
        .map_err(|e| format!("等待启动任务失败: {e}"))?;

    if enabled {
        task.RequestEnableAsync()
            .map_err(|e| format!("请求启用启动任务失败: {e}"))?
            .await
            .map_err(|e| format!("等待启用结果失败: {e}"))?;
        eprintln!("[Autostart] MSIX 开机自启已启用");
    } else {
        task.Disable()
            .map_err(|e| format!("禁用启动任务失败: {e}"))?;
        eprintln!("[Autostart] MSIX 开机自启已禁用");
    }

    Ok(())
}
// 以下两个 stub 让非 Windows 平台也能通过编译。
// is_store_version() 是运行时判断，但上面两个函数是编译期门控的，
// 没有 stub 的话所有调用点（get_autostart_state / get_autostart /
// set_autostart / 托盘菜单回调）在 macOS/Linux 上都会报 E0425。
// 实际上非 Windows 上 is_store_version() 恒为 false，stub 不会被执行到。

/// 非 Windows 平台没有 MSIX 概念，视为未启用
#[cfg(not(target_os = "windows"))]
async fn get_autostart_msix() -> Result<bool, String> {
    Ok(false)
}

/// 非 Windows 平台不支持 MSIX 开机自启
#[cfg(not(target_os = "windows"))]
async fn set_autostart_msix(_enabled: bool) -> Result<(), String> {
    Err("MSIX 开机自启仅在 Windows 上可用".to_string())
}

/// 统一获取开机自启状态（根据环境自动选择实现）
async fn get_autostart_state(app: &tauri::AppHandle) -> bool {
    if is_store_version() {
        get_autostart_msix().await.unwrap_or(false)
    } else {
        app.autolaunch().is_enabled().unwrap_or(false)
    }
}

#[tauri::command]
async fn get_autostart(app: tauri::AppHandle) -> Result<bool, String> {
    if is_store_version() {
        // MSIX 环境：使用 WinRT StartupTask API
        get_autostart_msix().await
    } else {
        // 非 MSIX 环境：使用 tauri-plugin-autostart
        app.autolaunch()
            .is_enabled()
            .map_err(|e| e.to_string())
    }
}

#[tauri::command]
async fn set_autostart(app: tauri::AppHandle, enabled: bool) -> Result<(), String> {
    if is_store_version() {
        // MSIX 环境：使用 WinRT StartupTask API（参数通过 uap10:Parameters 传递）
        set_autostart_msix(enabled).await?;
    } else {
        // 非 MSIX 环境：使用 tauri-plugin-autostart
        if enabled {
            app.autolaunch()
                .enable()
                .map_err(|e| e.to_string())?;
        } else {
            app.autolaunch()
                .disable()
                .map_err(|e| e.to_string())?;
        }
    }
    // 通知托盘菜单同步 CheckMenuItem 状态
    let _ = app.emit("autostart-changed", enabled);
    Ok(())
}

/// 重建托盘菜单（copy URL / show / autostart / quit）
/// 只在 URL 或 autostart 状态真正变化时调用，避免无谓的菜单重建
fn update_tray_menu(
    app: &tauri::AppHandle,
    tray_id: &str,
    url: &str,
    autostart_enabled: bool,
) {
    if let Some(tray) = app.tray_by_id(tray_id) {
        let new_item = MenuItemBuilder::with_id("copy_cast_url", CAST_URL_LABEL)
            .enabled(!url.is_empty())
            .build(app)
            .unwrap();
        let show_item = MenuItemBuilder::with_id("show", "显示窗口")
            .build(app)
            .unwrap();
        let autostart_item = CheckMenuItemBuilder::with_id("autostart", "开机自启")
            .checked(autostart_enabled)
            .build(app)
            .unwrap();
        let quit_item = MenuItemBuilder::with_id("quit", "退出")
            .build(app)
            .unwrap();
        let new_menu = MenuBuilder::new(app)
            .item(&new_item)
            .item(&show_item)
            .item(&autostart_item)
            .item(&quit_item)
            .build()
            .unwrap();
        tray.set_menu(Some(new_menu)).ok();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(tauri_plugin_dialog::init())
        // 投屏失败要能落到系统通知：窗口收进托盘时，界面里那条红字用户根本看不到
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec!["--minimized"]),
        ))
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // 当第二个实例启动时，聚焦到已运行的窗口
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .on_menu_event(|app, event| {
            if event.id() == "show" {
                if let Some(window) = app.get_webview_window("main") {
                    window.show().ok();
                    window.set_focus().ok();
                }
            } else if event.id() == "copy_cast_url" {
                // 从状态中读取完整 URL 并复制到剪贴板
                if let Some(state) = app.try_state::<CastUrlState>() {
                    let url = state.url.lock().unwrap().clone();
                    if !url.is_empty() {
                        app.clipboard().write_text(url).ok();
                    }
                }
            } else if event.id() == "quit" {
                // 优雅关闭：先通知 sidecar 清理（杀 MPV 等），再退出应用
                let app_handle = app.clone();
                tauri::async_runtime::spawn(async move {
                    let sidecar = app_handle.state::<SidecarManager>();
                    sidecar.shutdown().await;
                    app_handle.exit(0);
                });
            } else if event.id() == "autostart" {
                // CheckMenuItem 已自动切换 checked 状态，执行实际操作
                let app_handle = app.clone();
                tauri::async_runtime::spawn(async move {
                    let current = get_autostart_state(&app_handle).await;
                    if is_store_version() {
                        set_autostart_msix(!current).await.ok();
                    } else {
                        if current {
                            app_handle.autolaunch().disable().ok();
                        } else {
                            app_handle.autolaunch().enable().ok();
                        }
                    }
                    // 通知前端同步状态
                    let enabled = get_autostart_state(&app_handle).await;
                    let _ = app_handle.emit("autostart-changed", enabled);
                });
            }
        })
        .setup(|app| {
            // 初始化会话日志（每次启动清空旧日志）
            let log_dir = app
                .path()
                .app_data_dir()
                .unwrap_or_else(|_| PathBuf::from("."))
                .join("logs");
            let logger = SessionLogger::new(&log_dir);
            logger.log("=== HanCast 启动 ===");
            app.manage(logger);

            // 加载窗口配置
            let config_path = app
                .path()
                .app_data_dir()
                .unwrap_or_else(|_| PathBuf::from("."))
                .join("window_config.json");

            let window_config_state = WindowConfigState::load(&config_path);

            // 开发模式下自动打开 DevTools
            #[cfg(debug_assertions)]
            if let Some(window) = app.get_webview_window("main") {
                window.open_devtools();
            }

            // 开机自启检测（必须在窗口显示之前）
            // 非 MSIX: 通过 --minimized 参数检测（tauri-plugin-autostart 通过注册表传递）
            // MSIX: 通过 --autostart 参数检测（uap10:Parameters 在 AppxManifest.xml 中配置）
            let should_start_minimized = std::env::args().any(|a| a == "--autostart" || a == "--minimized");

            if should_start_minimized {
                eprintln!("[Startup] 检测到启动参数，最小化到托盘");
            }

            // 恢复窗口尺寸（窗口默认隐藏，设置好后再显示，避免闪烁）
            if let Some(window) = app.get_webview_window("main") {
                let config = window_config_state.get_config();

                // 最小尺寸保护（防止配置丢失时窗口变成 1x1）
                const MIN_WIDTH: u32 = 346;
                const MIN_HEIGHT: u32 = 472;
                let width = (config.width as u32).max(MIN_WIDTH);
                let height = (config.height as u32).max(MIN_HEIGHT);

                // 设置窗口尺寸
                let _ = window.set_size(tauri::Size::Physical(tauri::PhysicalSize {
                    width,
                    height,
                }));

                // 打印窗口尺寸（启动时一次性记录）
                let logger = app.state::<SessionLogger>();
                logger.log(&format!("窗口尺寸: {}x{} px (config: {}x{}, min: {}x{})", width, height, config.width, config.height, MIN_WIDTH, MIN_HEIGHT));

                // 禁用最大化（阻止双击标题栏最大化）
                window.set_maximizable(false).ok();

                // 监听窗口事件
                let window_config_clone = window_config_state.clone();
                let app_handle_for_close = app.handle().clone();
                window.on_window_event(move |event| {
                    match event {
                        tauri::WindowEvent::Resized(size) => {
                            let mut config = window_config_clone.get_config();
                            // 保存时也强制最小尺寸保护
                            config.width = (size.width as f64).max(346.0);
                            config.height = (size.height as f64).max(472.0);
                            window_config_clone.update_and_save(config);
                        }
                        // 拦截关闭请求，隐藏到托盘而不是真正关闭
                        tauri::WindowEvent::CloseRequested { api, .. } => {
                            // 阻止默认关闭行为
                            api.prevent_close();
                            // 隐藏窗口到托盘
                            if let Some(window) = app_handle_for_close.get_webview_window("main") {
                                window.hide().ok();
                            }
                        }
                        _ => {}
                    }
                });

                // 根据启动参数决定是否显示窗口
                if !should_start_minimized {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }

            app.manage(window_config_state);

            // Initialize Python sidecar
            // 注意：这里失败会让 setup 返回 Err，Tauri 随即退出，表现为「窗口一闪就退」。
            // 所以失败时必须把原因打出来，否则用户只看到闪退、无从排查。
            let sidecar = SidecarManager::new(app.handle().clone()).map_err(|e| {
                eprintln!("[FATAL] Failed to start sidecar, aborting startup: {e}");
                e
            })?;
            app.manage(sidecar);

            // Initialize cast URL state
            let cast_url = Arc::new(Mutex::new(String::new()));
            app.manage(CastUrlState {
                url: cast_url.clone(),
            });

            // Build tray menu
            let cast_url_item = MenuItemBuilder::with_id("copy_cast_url", CAST_URL_LABEL)
                .enabled(false)
                .build(app)?;
            let show = MenuItemBuilder::with_id("show", "显示窗口")
                .build(app)?;
            let autostart_item = CheckMenuItemBuilder::with_id("autostart", "开机自启")
                .checked(tauri::async_runtime::block_on(get_autostart_state(app.handle())))
                .build(app)?;
            let quit = MenuItemBuilder::with_id("quit", "退出")
                .build(app)?;
            let menu = MenuBuilder::new(app)
                .item(&cast_url_item)
                .item(&show)
                .item(&autostart_item)
                .item(&quit)
                .build()?;

            // Build tray icon (禁用左键弹出菜单，左键只显示窗口)
            let tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip(format!("HanCast v{}", env!("CARGO_PKG_VERSION")))
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            // 始终先 show + unminimize，再 set_focus，确保各平台行为一致
                            let _ = window.show();
                            let _ = window.unminimize();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)?;

            // Background task: startup update check (non-blocking, 2s timeout)
            let app_handle_update = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                // 微软商店版本由 Store 自动管理更新，跳过检查
                if is_store_version() {
                    eprintln!("[Update] Microsoft Store version — skipping auto-update check");
                    return;
                }

                // 等待 sidecar 就绪
                tokio::time::sleep(std::time::Duration::from_secs(3)).await;

                let current_version = app_handle_update
                    .config()
                    .version
                    .clone()
                    .unwrap_or_else(|| "0.0.0".to_string());

                let sidecar = app_handle_update.state::<SidecarManager>();
                match sidecar
                    .send_command(
                        "check_update",
                        // 启动自检同样不带 sources：见 `check_update` 命令里的说明
                        serde_json::json!({"current_version": current_version}),
                    )
                    .await
                {
                    Ok(result) => {
                        if result.get("has_update").and_then(|v| v.as_bool()).unwrap_or(false) {
                            let _ = app_handle_update.emit("update-available", &result);
                            eprintln!(
                                "[Update] New version available: {} → {}",
                                current_version,
                                result.get("latest").and_then(|v| v.as_str()).unwrap_or("?")
                            );
                        }
                    }
                    Err(e) => {
                        eprintln!("[Update] Startup check failed: {e}");
                    }
                }
            });

            // Background task: poll get_cast_url every second and update tray menu
            //
            // autostart 状态是事件驱动的：set_autostart 命令 / 托盘菜单点击
            // 都会 emit "autostart-changed"。这里订阅该事件更新菜单勾选，
            // 而不是每秒调用 get_autostart_state（WinRT StartupTask::GetAsync
            // / 注册表读取是系统调用，每秒执行纯属浪费）。
            let app_handle = app.handle().clone();
            let tray_id = tray.id().clone();
            tauri::async_runtime::spawn(async move {
                let mut last_url = String::new();
                let mut last_autostart = get_autostart_state(&app_handle).await;

                let (autostart_tx, mut autostart_rx) =
                    tokio::sync::mpsc::channel::<bool>(8);
                let listen_handle = app_handle.clone();
                let _ = listen_handle.listen("autostart-changed", move |event| {
                    if let Some(enabled) = event.payload().parse::<bool>().ok() {
                        let _ = autostart_tx.try_send(enabled);
                    }
                });

                let mut ticker =
                    tokio::time::interval(std::time::Duration::from_secs(1));
                // 首次 tick 立即执行
                ticker.tick().await;

                loop {
                    tokio::select! {
                        _ = ticker.tick() => {
                            // Call sidecar to get current cast URL
                            let sidecar = app_handle.state::<SidecarManager>();
                            let new_url = match sidecar
                                .send_command("get_cast_url", serde_json::json!({}))
                                .await
                            {
                                Ok(val) => val
                                    .get("url")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("")
                                    .to_string(),
                                Err(_) => String::new(),
                            };

                            if new_url != last_url {
                                last_url = new_url.clone();
                                // Update state for clipboard copy
                                if let Some(state) = app_handle.try_state::<CastUrlState>() {
                                    *state.url.lock().unwrap() = new_url.clone();
                                }
                                update_tray_menu(&app_handle, &tray_id.0, &new_url, last_autostart);
                            }
                        }
                        Some(enabled) = autostart_rx.recv() => {
                            if enabled != last_autostart {
                                last_autostart = enabled;
                                update_tray_menu(&app_handle, &tray_id.0, &last_url, enabled);
                            }
                        }
                    }
                }
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            minimize_window,
            close_window,
            get_devices,
            refresh_devices,
            set_default_device,
            rename_device,
            remove_device,
            hide_device,
            unhide_device,
            get_hidden_devices,
            add_device,
            add_local_device,
            get_manual_devices,
            remove_manual_device,
            parse_media_file,
            parse_media_url,
            list_folder_media,
            resolve_share,
            get_resolve_result,
            plugin_list,
            rule_inventory,
            failure_list,
            failure_delete,
            failure_clear,
            cast_failure_list,
            cast_failure_clear,
            probe_url,
            plugin_check_updates,
            plugin_pull,
            plugin_import,
            plugin_remove,
            plugin_set_rule_enabled,
            plugin_test,
            read_clipboard,
            write_clipboard,
            start_cast,
            subtitle_apply,
            subtitle_status,
            start_remote_control,
            stop_remote_control,
            remote_status,
            remote_update_ui,
            stop_cast,
            pause_cast,
            resume_cast,
            seek_cast,
            get_cast_state,
            get_cast_url,
            set_volume,
            set_mute,
            get_settings,
            save_settings,
            list_network_interfaces,
            get_params,
            save_params,
            check_update,
            ignore_update_version,
            respond_cast_confirm,
            get_guard_devices,
            remove_guard_device,
            set_guard_policy,
            get_guard_settings,
            save_guard_settings,
            check_mpv,
            set_mpv_path,
            list_screen_sources,
            start_screen_cast,
            stop_screen_cast,
            screen_cast_status,
            set_ffmpeg_path,
            ffmpeg_status,
            ffmpeg_download_start,
            ffmpeg_download_status,
            mirror_status,
            mirror_devices,
            mirror_connect,
            mirror_start,
            mirror_stop,
            mirror_set_tool_path,
            update_release_info,
            update_download_start,
            update_download_status,
            update_download_cancel,
            update_install_plan,
            update_apply,
            open_update_dir,
            open_ffmpeg_dir,
            notify_os,
            export_logs,
            get_logs,
            set_log_level,
            frontend_log,
            is_store_version,
            get_autostart,
            set_autostart,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
