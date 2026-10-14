//! 浏览器通道的宿主半：离屏 WebView 渲染 + 流地址嗅探 + Cookie 同步
//!
//! sidecar 用 `browser_request` 事件发起一次渲染，本模块建一个离屏窗口真渲染，
//! 再用 `browser_result` 命令把 HTML/候选地址/Cookie 回传。
//! 链路、预算与熔断的完整契约写在
//! `hancast-backend/hancast_sidecar/utils/browser_channel.py` 的模块文档里。

use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tauri::utils::config::BackgroundThrottlingPolicy;
use tauri::webview::PageLoadEvent;
use tauri::{AppHandle, Manager, Url, WebviewUrl, WebviewWindowBuilder};
use tokio::sync::{mpsc, Semaphore};

use crate::sidecar::SidecarManager;

/// 一次只渲染一页：解析本来就是串行跑规则的，并发开窗口只会把 CPU 和内存打满
fn gate() -> &'static Arc<Semaphore> {
    static GATE: OnceLock<Arc<Semaphore>> = OnceLock::new();
    GATE.get_or_init(|| Arc::new(Semaphore::new(1)))
}

/// 文档构建前注入（所有 frame）：把站点自己发起的请求地址汇进 window.__HC
///
/// 为什么在 JS 里钩而不是在 Rust 里截网络：Tauri 的 `on_web_resource_request`
/// 只覆盖自家自定义协议（asset/tauri://）的请求，外站的 https 请求根本不经它。
const INIT_JS: &str = r#"(function () {
  try {
    var store = window.__HC = { urls: [] };
    var isTop = false;
    try { isTop = window.top === window.self; } catch (e) { isTop = true; }
    function add(u) {
      try {
        if (typeof u !== 'string' || !/^https?:/i.test(u)) return;
        u = u.slice(0, 2000);
        if (store.urls.length < 800 && store.urls.indexOf(u) < 0) store.urls.push(u);
        if (isTop) return;
        // 同源子帧汇到顶层；跨帧读顶层会抛，忽略即可
        var t = window.top.__HC;
        if (t && t.urls && t.urls.length < 800 && t.urls.indexOf(u) < 0) t.urls.push(u);
      } catch (e) {}
    }
    var of = window.fetch;
    if (of) window.fetch = function (input) {
      try { add(typeof input === 'string' ? input : (input && input.url)); } catch (e) {}
      return of.apply(this, arguments);
    };
    var ox = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (m, u) {
      try { add(u); } catch (e) {}
      return ox.apply(this, arguments);
    };
    // 播放器拿到地址后是往 media 元素上挂 src 的，这一步最可靠
    [['HTMLMediaElement', 'src'], ['HTMLSourceElement', 'src']].forEach(function (p) {
      try {
        var proto = window[p[0]] && window[p[0]].prototype;
        var d = proto && Object.getOwnPropertyDescriptor(proto, p[1]);
        if (!d || !d.set) return;
        Object.defineProperty(proto, p[1], {
          get: d.get,
          set: function (v) { try { add(String(v)); } catch (e) {} return d.set.call(this, v); }
        });
      } catch (e) {}
    });
    try {
      document.addEventListener('loadstart', function (ev) {
        var t = ev && ev.target;
        if (!t) return;
        try { if (t.currentSrc) add(String(t.currentSrc)); } catch (e) {}
        try { if (t.src) add(String(t.src)); } catch (e) {}
      }, true);
    } catch (e) {}
  } catch (err) {
    try { window.__HC_err = String(err); } catch (e) {}
  }
})();"#;

/// 加载完先替用户点一次播放键（静音）：流地址是播放器请求出来的，不播就嗅不到
const AUTOPLAY_JS: &str = r#"(function () {
  try {
    var vs = document.querySelectorAll('video');
    for (var i = 0; i < vs.length; i++) {
      try {
        vs[i].muted = true;
        var p = vs[i].play();
        if (p && p.catch) p.catch(function () {});
      } catch (e) {}
    }
  } catch (e) {}
  return 1;
})();"#;

fn harvest_js(want_html: bool) -> String {
    format!(
        r#"(function () {{
  var out = {{ url: location.href, title: document.title || '', urls: [], html: '' }};
  function push(u) {{
    try {{
      if (typeof u === 'string' && /^https?:/i.test(u)
          && out.urls.length < 400 && out.urls.indexOf(u) < 0) out.urls.push(u.slice(0, 2000));
    }} catch (e) {{}}
  }}
  try {{
    var seen = (window.__HC && window.__HC.urls) || [];
    for (var i = 0; i < seen.length; i++) push(seen[i]);
  }} catch (e) {{}}
  try {{
    var res = performance.getEntriesByType('resource');
    for (var j = 0; j < res.length; j++) push(res[j].name);
  }} catch (e) {{}}
  var html = '';
  try {{ html = document.documentElement ? document.documentElement.outerHTML : ''; }} catch (e) {{}}
  if ({with_html}) out.html = html.slice(0, 3000000);
  // 地址直接写在正文里的站点（内联 JSON 的 playurl）
  try {{
    var re = /(https?:\/\/[^"'\s<>\\]+?\.(?:m3u8|mpd|mp4|flv|m4v|webm|mov|mp3|m4a|ogg|aac|f4v))(?:\?[^"'\s<>\\]*)?/gi;
    var m, n = 0;
    while (n++ < 300 && (m = re.exec(html)) !== null) push(m[1]);
  }} catch (e) {{}}
  return JSON.stringify(out);
}})()"#,
        with_html = if want_html { "true" } else { "false" }
    )
}

fn failed(error: impl Into<String>) -> Value {
    json!({
        "ok": false, "error": error.into(), "html": "", "finalUrl": "",
        "title": "", "ua": "", "candidates": [], "cookies": [],
    })
}

fn remaining(deadline: Instant) -> Duration {
    deadline.saturating_duration_since(Instant::now())
}

/// `browser_request` 事件的入口：算好本次预算，渲染完回传 sidecar
///
/// 必须在 `reader_loop` 之外另起任务：结果要经 stdin 命令送回，而它的应答正是
/// 由那条循环读取的——在循环里等就等于自己堵死自己。
pub async fn handle_request(app: AppHandle, req: Value) {
    let request_id = req
        .get("request_id")
        .and_then(|v| v.as_str())
        .unwrap_or_default()
        .to_string();
    if request_id.is_empty() {
        eprintln!("[browser] browser_request 缺 request_id，丢弃");
        return;
    }
    let opts = req.get("options").cloned().unwrap_or_else(|| json!({}));
    let url = req.get("url").and_then(|v| v.as_str()).unwrap_or("").to_string();
    let ua = opts
        .get("ua")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("")
        .to_string();
    let want_html = opts
        .get("want_html")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    let settle = Duration::from_millis(
        opts.get("settle_ms")
            .and_then(|v| v.as_u64())
            .unwrap_or(1_500)
            .min(15_000),
    );
    let budget = Duration::from_secs_f64(
        opts.get("timeout_s")
            .and_then(|v| v.as_f64())
            .unwrap_or(12.0)
            .clamp(1.0, 25.0),
    );

    let started = Instant::now();
    // 许可握到渲染结束：排队的那次不另算超时，Python 侧的预算自己会到点
    let permit = tokio::time::timeout(budget, gate().clone().acquire_owned()).await;
    let mut payload = match permit {
        Ok(Ok(p)) => {
            let out = render(&app, &request_id, &url, &ua, want_html, settle, started + budget).await;
            drop(p);
            out
        }
        Ok(Err(_)) => failed("宿主浏览器通道已关闭"),
        Err(_) => failed("宿主浏览器通道正忙（上一次渲染未结束）"),
    };
    payload["request_id"] = json!(request_id);
    payload["elapsedMs"] = json!(started.elapsed().as_millis() as u64);

    let sidecar = app.state::<SidecarManager>();
    if let Err(e) = sidecar.send_command("browser_result", payload).await {
        eprintln!("[browser] browser_result 回传失败: {e}");
    }
}

async fn render(
    app: &AppHandle,
    request_id: &str,
    url: &str,
    ua: &str,
    want_html: bool,
    settle: Duration,
    deadline: Instant,
) -> Value {
    let parsed = match Url::parse(url) {
        Ok(u) => u,
        Err(e) => return failed(format!("地址无法解析: {e}")),
    };
    let host = parsed.host_str().unwrap_or("").to_lowercase();
    let label = format!("hc-render-{request_id}");

    let (tx, mut rx) = mpsc::unbounded_channel::<()>();
    let builder = WebviewWindowBuilder::new(app, &label, WebviewUrl::External(parsed.clone()))
        .title("HanCast")
        .visible(true)
        .decorations(false)
        .skip_taskbar(true)
        .always_on_bottom(true)
        .focused(false)
        .resizable(false)
        .inner_size(1366.0, 864.0)
        // 可见但挪出屏幕：真隐藏的窗口里 rAF 可能不跑，靠 rAF 的站点会停在骨架屏
        .position(-8000.0, -8000.0)
        .initialization_script_for_all_frames(INIT_JS)
        .background_throttling(BackgroundThrottlingPolicy::Disabled)
        .on_page_load(move |_w, payload| {
            if matches!(payload.event(), PageLoadEvent::Finished)
                && payload.url().scheme().starts_with("http")
            {
                let _ = tx.send(());
            }
        });
    let builder = if ua.is_empty() {
        builder
    } else {
        builder.user_agent(ua)
    };
    let win = match builder.build() {
        Ok(w) => w,
        Err(e) => return failed(format!("离屏窗口创建失败: {e}")),
    };

    // 回收脚本与 Cookie 各要一次原生 IPC 往返，先给它们留出额度
    let allowance = Duration::from_millis(1_500);
    let load_budget = remaining(deadline)
        .saturating_sub(settle + allowance)
        .max(Duration::from_millis(500));
    let loaded = tokio::time::timeout(load_budget, rx.recv()).await;
    let load_ok = matches!(loaded, Ok(Some(())));
    let load_secs = load_budget.as_secs();

    if load_ok || rx.try_recv().is_ok() {
        let _ = win.eval(AUTOPLAY_JS);
    }

    // 加载完再等 settle；期间若又跳转（分享页常见两跳）就重新计时，
    // 但绝不越过 deadline - allowance
    let settle_end = (deadline - allowance).max(Instant::now());
    let mut timer = Box::pin(tokio::time::sleep(settle));
    loop {
        let left = settle_end.saturating_duration_since(Instant::now());
        if left.is_zero() {
            break;
        }
        tokio::select! {
            _ = &mut timer => break,
            more = rx.recv() => {
                if more.is_none() {
                    break;
                }
                let _ = win.eval(AUTOPLAY_JS);
                timer = Box::pin(tokio::time::sleep(settle.min(left)));
            }
        }
    }

    // 回收回调是 `Fn`（可能被调多次），所以用 unbounded 通道而不是 oneshot
    let (htx, mut hrx) = mpsc::unbounded_channel::<String>();
    if let Err(e) = win.eval_with_callback(harvest_js(want_html), move |s| {
        let _ = htx.send(s);
    }) {
        let _ = win.close();
        return failed(format!("宿主回收脚本注入失败: {e}"));
    }
    let harvested = match tokio::time::timeout(allowance, hrx.recv()).await {
        Ok(Some(raw)) => decode_eval(&raw).and_then(|v| v.as_object().cloned()),
        _ => None,
    };

    let cookies = collect_cookies(app, &label, parsed, &host).await;
    let _ = win.close();

    let Some(fields) = harvested else {
        return failed(if load_ok {
            "页面已加载但宿主未取回数据（渲染被中断或脚本被拦）".to_string()
        } else {
            format!("页面加载超时（{load_secs}s）")
        });
    };

    let candidates = fields
        .get("urls")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let text = |k: &str| -> String {
        fields
            .get(k)
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    };

    // 加载没跑完但已经嗅到地址就算成功：嗅出流地址正是建这个通道的首要目的
    let ok = load_ok || !candidates.is_empty();
    let final_url = {
        let jumped = text("url");
        if jumped.is_empty() {
            url.to_string()
        } else {
            jumped
        }
    };
    json!({
        "ok": ok,
        "error": if ok { String::new() } else { format!("页面加载超时（{load_secs}s）") },
        "html": text("html"),
        "finalUrl": final_url,
        "title": text("title"),
        "ua": ua,
        "candidates": candidates,
        "cookies": cookies,
    })
}

/// `eval_with_callback` 两端都给「JS 结果的 JSON 编码」，而脚本自己已经
/// `JSON.stringify` 过一次，所以这里要解两层
fn decode_eval(raw: &str) -> Option<Value> {
    let outer: Value = serde_json::from_str(raw).ok()?;
    match outer {
        Value::String(inner) => serde_json::from_str(&inner).ok(),
        other => Some(other),
    }
}

/// WebView 罐子里属于这个站的 cookie（含 HttpOnly——`document.cookie` 拿不到的那批）
async fn collect_cookies(app: &AppHandle, label: &str, url: Url, host: &str) -> Vec<Value> {
    let app = app.clone();
    let label = label.to_string();
    let host = host.to_string();
    // cookies() 内部要阻塞等原生回调（Windows 上还会和 UI 线程对咬），挪到专用线程
    let task = tauri::async_runtime::spawn_blocking(move || {
        let win = match app.get_webview_window(&label) {
            Some(w) => w,
            None => return Vec::new(),
        };
        let wants_https = url.scheme() == "https";
        win.cookies_for_url(url)
            .unwrap_or_default()
            .into_iter()
            .filter(|c| {
                let domain = c
                    .domain()
                    .unwrap_or("")
                    .trim_start_matches('.')
                    .to_lowercase();
                let usable = !c.secure().unwrap_or(false) || wants_https;
                !domain.is_empty() && usable && (host == domain || host.ends_with(&format!(".{domain}")))
            })
            .map(|c| {
                json!({
                    "name": c.name(),
                    "value": c.value(),
                    "domain": c.domain().unwrap_or(""),
                    "path": c.path().unwrap_or("/"),
                })
            })
            .collect::<Vec<Value>>()
    });
    match tokio::time::timeout(Duration::from_secs(3), task).await {
        Ok(Ok(list)) => list,
        Ok(Err(e)) => {
            eprintln!("[browser] cookie 读取任务失败: {e}");
            Vec::new()
        }
        Err(_) => {
            eprintln!("[browser] cookie 读取超时，按空处理");
            Vec::new()
        }
    }
}
