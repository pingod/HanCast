//! hancast-wlgrab —— HanCast 的 Linux(Wayland) 屏幕抓取 helper。
//!
//! 为什么需要它：FFmpeg 在 Linux 上只有 `x11grab`/`kmsgrab`/`fbdev` 三种屏幕输入
//! （`ffmpeg -hide_banner -demuxers` 实测 9.0.2 里没有 wayland、也没有 pipewire 屏幕源），
//! 而 Hyprland/sway 这类合成器的原生窗口**不在 XWayland 的窗口树里**：`x11grab :0` 连得上
//! （Xwayland 是 `-rootless` 起的），抓回来却是 `signalstats.YAVG=16.03`＝纯黑，同一时刻
//! `grim` 走 wlroots screencopy 量到 44.28。这就是「投屏出去全是黑屏」的真因。
//!
//! 协议选 `zwlr_screencopy_manager_v1`（grim/wf-recorder 同一套），走 SHM 路径把帧写成
//! **裸 BGRA 到 stdout**，由 sidecar 用 `-f rawvideo` 接进既有的 HLS 管线；帧率的补齐交给
//! ffmpeg 的 `-r`（实测 `-framerate 5` 输入 + `-r 25` 输出会 `dup=8` 自动补成 CFR），
//! 所以本程序只负责「合成器给一帧，我写一帧」，不带自己的定时器。
//!
//! 用法：
//! ```text
//! hancast-wlgrab list
//!     stdout: [{"name":"DP-2","description":…,"width":2560,"height":1440,"scale":1,
//!               "x":0,"y":0,"physical_width":608,"physical_height":342}, …]
//! hancast-wlgrab capture [--output DP-2] [--fps 15] [--frames 0] [--cursor] [--once]
//!                        [--x N --y N --width N --height N]
//!     stderr 第一行: HDR {"width":2560,"height":1440,"pix_fmt":"bgra","output":"DP-2","y_invert":false}
//!     stdout: 之后每帧 width*height*4 字节（已去掉 stride 末尾填充、已按 y_invert 摆正）
//!     stderr 结束行: DONE N 帧
//! ```
//!
//! 退出码：0 正常结束；2 参数错；3 连不上 Wayland 或合成器没有 screencopy/wl_shm；
//! 4 首帧失败或全程超时；5 像素格式不是 ARGB/XRGB（我们不敢猜，宁可报错也别投半屏花屏）。

use std::io::{self, Write};
use std::os::fd::{AsFd, AsRawFd, OwnedFd, RawFd};
use std::process::ExitCode;
use std::time::{Duration, Instant};

use wayland_client::protocol::wl_buffer::{self, WlBuffer};
use wayland_client::protocol::wl_output::{self, WlOutput};
use wayland_client::protocol::wl_registry::{self, WlRegistry};
use wayland_client::protocol::wl_shm::{self, WlShm};
use wayland_client::protocol::wl_shm_pool::{self, WlShmPool};
use wayland_client::{Connection, Dispatch, EventQueue, QueueHandle, WEnum};
use wayland_protocols_wlr::screencopy::v1::client::zwlr_screencopy_frame_v1::{
    self, ZwlrScreencopyFrameV1,
};
use wayland_protocols_wlr::screencopy::v1::client::zwlr_screencopy_manager_v1::{
    self, ZwlrScreencopyManagerV1,
};

/// screencopy 的 `flags` 位：1 = 内容纵向翻转（原点左下）
const FLAG_Y_INVERT: u32 = 1;
/// 一次缓冲区协商（`buffer` 事件）的等待上限
const BUF_TIMEOUT: Duration = Duration::from_secs(3);
/// 单次 poll 的最长等待；用来自我唤醒，避免合成器静止时永远挂住
const POLL_STEP: Duration = Duration::from_millis(200);

/// 一块 wl_output 的信息（`list` 模式直接用，`capture` 用来选屏）
#[derive(Default)]
struct OutputInfo {
    name: String,
    description: String,
    /// 逻辑像素（合成器坐标，未乘 scale）
    logical_w: i32,
    logical_h: i32,
    scale: i32,
    x: i32,
    y: i32,
    phys_mm_w: i32,
    phys_mm_h: i32,
    seen_done: bool,
}

/// 一帧的缓冲区描述，来自 screencopy 的 `buffer` 事件——**这才是抓图尺寸的权威来源**，
/// wl_output.mode 给的是逻辑尺寸，scale≠1 时两者不同。
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
struct BufInfo {
    format: u32,
    width: u32,
    height: u32,
    stride: u32,
}

struct App {
    outputs: Vec<OutputInfo>,
    /// 与 outputs 一一对应的 wl_output 句柄（多实例全局只能自己 bind 自己存）
    bound_outputs: Vec<WlOutput>,
    shm: Option<WlShm>,
    shm_formats: Vec<u32>,
    manager: Option<ZwlrScreencopyManagerV1>,
    /// 本次 frame 会话的临时状态，每次 capture 前清空
    pending_buf: Option<BufInfo>,
    flags: u32,
    ready: bool,
    failed: bool,
}

impl App {
    fn new() -> Self {
        Self {
            outputs: Vec::new(),
            bound_outputs: Vec::new(),
            shm: None,
            shm_formats: Vec::new(),
            manager: None,
            pending_buf: None,
            flags: 0,
            ready: false,
            failed: false,
        }
    }

    fn reset_frame(&mut self) {
        self.pending_buf = None;
        self.flags = 0;
        self.ready = false;
        self.failed = false;
    }
}

// ────────────────────────────── registry ──────────────────────────────

impl Dispatch<WlRegistry, ()> for App {
    fn event(
        state: &mut App,
        registry: &WlRegistry,
        event: wl_registry::Event,
        _: &(),
        _: &Connection,
        qh: &QueueHandle<App>,
    ) {
        // wl_output 是多实例全局，必须逐个 bind 并把「它是第几块」作为 UserData 带着走
        //
        // ⚠️ `WlRegistry::bind` 的实参顺序是 **(name, version, qh, data)**，不是
        // (version, name)。写反了不会编译报错（两个都是 u32），但合成器会当成
        // 「把全局 1（wl_seat）绑成 wl_shm」直接协议错误踢掉连接：
        // `Protocol error 0 on object wl_registry@2: invalid interface for global 1:
        //  expected wl_seat, got wl_shm`（2026-10-17 CachyOS/Hyprland 实测）。
        if let wl_registry::Event::Global { name, interface, version } = event {
            match interface.as_str() {
                "wl_shm" if state.shm.is_none() => {
                    state.shm = Some(registry.bind::<WlShm, _, _>(name, 1, qh, ()))
                }
                "zwlr_screencopy_manager_v1" if state.manager.is_none() => {
                    // v3 才有 cursor planes，我们用不到；v1 就够 list+capture
                    let v = version.min(3).max(1);
                    state.manager =
                        Some(registry.bind::<ZwlrScreencopyManagerV1, _, _>(name, v, qh, ()));
                }
                "wl_output" => {
                    let idx = state.outputs.len() as u32;
                    state.outputs.push(OutputInfo {
                        name: format!("output-{idx}"),
                        ..Default::default()
                    });
                    // Name/Description 是 v4 的事件，Scale 是 v2 的
                    let v = version.min(4).max(1);
                    let bound = registry.bind::<WlOutput, _, _>(name, v, qh, idx);
                    if state.bound_outputs.len() > idx as usize {
                        state.bound_outputs[idx as usize] = bound;
                    } else {
                        state.bound_outputs.push(bound);
                    }
                }
                _ => {}
            }
        }
    }
}

// ────────────────────────────── wl_shm ──────────────────────────────

impl Dispatch<WlShm, ()> for App {
    fn event(
        state: &mut App,
        _: &WlShm,
        event: wl_shm::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<App>,
    ) {
        // format 事件给的是 WEnum，未知格式落在 Value 之外，直接忽略
        if let wl_shm::Event::Format { format } = event {
            if let WEnum::Value(f @ (wl_shm::Format::Argb8888 | wl_shm::Format::Xrgb8888)) = format
            {
                state.shm_formats.push(f as u32);
            }
        }
    }
}

impl Dispatch<WlShmPool, ()> for App {
    fn event(
        _: &mut App,
        _: &WlShmPool,
        _: wl_shm_pool::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<App>,
    ) {
    }
}

impl Dispatch<WlBuffer, ()> for App {
    fn event(
        _: &mut App,
        _: &WlBuffer,
        _: wl_buffer::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<App>,
    ) {
    }
}

// ────────────────────────────── wl_output ──────────────────────────────

impl Dispatch<WlOutput, u32> for App {
    fn event(
        state: &mut App,
        _: &WlOutput,
        event: wl_output::Event,
        idx: &u32,
        _: &Connection,
        _: &QueueHandle<App>,
    ) {
        let Some(out) = state.outputs.get_mut(*idx as usize) else {
            return;
        };
        match event {
            wl_output::Event::Geometry {
                x,
                y,
                physical_width,
                physical_height,
                ..
            } => {
                out.x = x;
                out.y = y;
                out.phys_mm_w = physical_width;
                out.phys_mm_h = physical_height;
                // Geometry 里的 width/height 是**毫米**，逻辑像素在 Mode 里；
                // scale 从 v2 起从 Geometry 拆出去了，见下面的 Scale 事件
            }
            wl_output::Event::Scale { factor } => {
                out.scale = if factor > 0 { factor } else { 1 };
            }
            wl_output::Event::Mode {
                flags,
                width,
                height,
                ..
            } => {
                // Mode 的 flags 是 wl_output.Mode 枚举，1 = current：只认当前模式
                let current = matches!(flags, WEnum::Value(wl_output::Mode::Current));
                if current && width > 0 && height > 0 {
                    out.logical_w = width;
                    out.logical_h = height;
                }
            }
            wl_output::Event::Name { name } => out.name = name,
            wl_output::Event::Description { description } => out.description = description,
            wl_output::Event::Done => out.seen_done = true,
            _ => {}
        }
    }
}

// ────────────────────────────── screencopy ──────────────────────────────

impl Dispatch<ZwlrScreencopyManagerV1, ()> for App {
    fn event(
        _: &mut App,
        _: &ZwlrScreencopyManagerV1,
        _: zwlr_screencopy_manager_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<App>,
    ) {
    }
}

impl Dispatch<ZwlrScreencopyFrameV1, ()> for App {
    fn event(
        state: &mut App,
        _: &ZwlrScreencopyFrameV1,
        event: zwlr_screencopy_frame_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<App>,
    ) {
        match event {
            zwlr_screencopy_frame_v1::Event::Buffer {
                format,
                width,
                height,
                stride,
            } => {
                // format 是 WEnum<wl_shm::Format>；未知值原样保留，后面按格式名报错
                let fmt = match format {
                    WEnum::Value(f) => f as u32,
                    WEnum::Unknown(v) => v,
                };
                state.pending_buf = Some(BufInfo {
                    format: fmt,
                    width,
                    height,
                    stride,
                });
            }
            zwlr_screencopy_frame_v1::Event::Flags { flags } => {
                state.flags = match flags {
                    WEnum::Value(f) => f.bits(),
                    WEnum::Unknown(v) => v,
                };
            }
            zwlr_screencopy_frame_v1::Event::Ready { .. } => state.ready = true,
            zwlr_screencopy_frame_v1::Event::Failed => state.failed = true,
            // damage / linux_dmabuf / buffer_done：SHM 全帧抓取用不到。
            // buffer_done 不追：ready 之后合成器已经把画面拷进我们的 shm，
            // 我们读完才销毁 buffer，不存在读到半帧的窗口。
            _ => {}
        }
    }
}

// ────────────────────────────── SHM 分配 ──────────────────────────────

/// `memfd_create` 失败（老内核、被 seccomp 挡）就退回匿名 tmpfile，语义等价
fn make_shm_fd(len: usize) -> io::Result<OwnedFd> {
    let name = b"hancast-wlgrab\0";
    let raw = unsafe { libc::memfd_create(name.as_ptr() as *const libc::c_char, libc::MFD_CLOEXEC) };
    let fd: RawFd = if raw >= 0 {
        raw
    } else {
        let tmpl = b"/tmp/.hancast-wlgrab.XXXXXX\0";
        let mut buf = [0u8; 32];
        buf[..tmpl.len()].copy_from_slice(tmpl);
        let tmp = unsafe { libc::mkstemp(buf.as_mut_ptr() as *mut libc::c_char) };
        if tmp < 0 {
            return Err(io::Error::last_os_error());
        }
        let link = unsafe { std::ffi::CStr::from_ptr(buf.as_ptr() as *const libc::c_char) };
        unsafe { libc::unlink(link.as_ptr()) };
        tmp
    };
    let owned = unsafe { <OwnedFd as std::os::fd::FromRawFd>::from_raw_fd(fd) };
    let rc = unsafe { libc::ftruncate(owned.as_raw_fd(), len as i64) };
    if rc != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(owned)
}

/// 一帧的共享内存：pool + mmap 全程复用，**每帧只新建 wl_buffer**。
///
/// 为什么不复用 buffer：合成器对同一个 wl_buffer 的写入时机不受我们控制
/// （`buffer_done` 才是释放信号），复用等于拿着一块可能被并发覆写的内存去读。
/// 每帧 14 MB 的 memfd 重建倒是可以省掉——尺寸没变就不动 pool。
struct ShmSurface {
    _fd: OwnedFd,
    pool: WlShmPool,
    mmap: *mut libc::c_void,
    len: usize,
}

impl ShmSurface {
    fn new(shm: &WlShm, qh: &QueueHandle<App>, info: &BufInfo) -> io::Result<Self> {
        let len = (info.stride * info.height) as usize;
        let fd = make_shm_fd(len)?;
        let pool = shm.create_pool(fd.as_fd(), len as i32, qh, ());
        let mmap = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                len,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED,
                fd.as_raw_fd(),
                0,
            )
        };
        if mmap == libc::MAP_FAILED {
            pool.destroy();
            return Err(io::Error::last_os_error());
        }
        Ok(Self {
            _fd: fd,
            pool,
            mmap,
            len,
        })
    }

    /// 从已有的 pool 上切一块新 buffer 给这一帧用
    fn buffer(&self, qh: &QueueHandle<App>, info: &BufInfo) -> Result<WlBuffer, String> {
        let format = argb_format(info.format)
            .ok_or_else(|| format!("合成器给的像素格式 {} 不是 ARGB/XRGB", info.format))?;
        Ok(self
            .pool
            .create_buffer(0, info.width as i32, info.height as i32, info.stride as i32, format, qh, ()))
    }

    /// 把 mmap 里的一帧搬到 `dst`（紧凑行，去掉 stride 填充），并按 y_invert 摆正
    fn copy_into(&self, info: &BufInfo, y_inverted: bool, dst: &mut [u8]) {
        let row = (info.width * 4) as usize;
        let stride = info.stride as usize;
        let src = unsafe { std::slice::from_raw_parts(self.mmap as *const u8, self.len) };
        for y in 0..info.height as usize {
            let sy = if y_inverted { info.height as usize - 1 - y } else { y };
            let s = sy * stride;
            if s + row > src.len() || y * row + row > dst.len() {
                break;
            }
            let d = y * row;
            dst[d..d + row].copy_from_slice(&src[s..s + row]);
        }
    }

    fn destroy(self) {
        self.pool.destroy();
        unsafe {
            libc::munmap(self.mmap, self.len);
            // fd 由 OwnedFd 负责关闭
        }
    }
}

fn argb_format(raw: u32) -> Option<wl_shm::Format> {
    if raw == wl_shm::Format::Argb8888 as u32 {
        Some(wl_shm::Format::Argb8888)
    } else if raw == wl_shm::Format::Xrgb8888 as u32 {
        Some(wl_shm::Format::Xrgb8888)
    } else {
        None
    }
}

// ────────────────────────────── 事件泵 ──────────────────────────────

/// 先吃掉队列里已有的事件，再最多 poll `timeout` 等一批新的。
/// 返回 `true` 表示这轮真的从 socket 读到了东西（超时不是失败）。
///
/// 为什么不用 `blocking_dispatch`：合成器静止不动时**不会**给我们任何事件，
/// 阻塞式读取会永远挂住，sidecar 那头只能靠杀进程收场。带超时的 poll 让我们能
/// 把「这一帧没来」如实报上去，管线自己决定跳过还是收尾。
fn pump(queue: &mut EventQueue<App>, app: &mut App, timeout: Duration) -> Result<bool, String> {
    queue.flush().map_err(|e| format!("发送 Wayland 请求失败: {e}"))?;
    // 已排队的事件先派发掉，否则 socket 空闲时 poll 白等
    queue
        .dispatch_pending(app)
        .map_err(|e| format!("派发 Wayland 事件失败: {e}"))?;

    // prepare_read 必须在 poll 之前拿（多线程同步的前提，见 wayland-backend 的说明）
    let Some(guard) = queue.prepare_read() else {
        return Ok(false);
    };
    let mut pfd = libc::pollfd {
        fd: guard.connection_fd().as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    let ms = timeout.as_millis().min(i32::MAX as u128) as libc::c_int;
    let rc = unsafe { libc::poll(&mut pfd, 1, ms) };
    if rc < 0 {
        let e = io::Error::last_os_error();
        if e.raw_os_error() == Some(libc::EINTR) {
            drop(guard);
            return Ok(false);
        }
        return Err(format!("poll Wayland socket 失败: {e}"));
    }
    if rc == 0 {
        drop(guard); // 放弃这次预读，下一轮重新 prepare
        return Ok(false);
    }
    guard
        .read()
        .map_err(|e| format!("读取 Wayland 事件失败: {e}"))?;
    queue
        .dispatch_pending(app)
        .map_err(|e| format!("派发 Wayland 事件失败: {e}"))?;
    Ok(true)
}

/// 等到 `cond` 成立；超过 timeout 返回 false
fn wait_for(
    queue: &mut EventQueue<App>,
    app: &mut App,
    cond: impl Fn(&App) -> bool,
    timeout: Duration,
) -> Result<bool, String> {
    let start = Instant::now();
    while !cond(app) {
        let spent = start.elapsed();
        if spent >= timeout {
            return Ok(false);
        }
        pump(queue, app, (timeout - spent).min(POLL_STEP))?;
    }
    Ok(true)
}

// ────────────────────────────── 建连 ──────────────────────────────

fn connect() -> Result<(Connection, EventQueue<App>, App), String> {
    let conn = Connection::connect_to_env()
        .map_err(|e| format!("连不上 Wayland 合成器（WAYLAND_DISPLAY 是否设置？）: {e}"))?;
    let mut queue: EventQueue<App> = conn.new_event_queue();
    let mut app = App::new();
    let qh = queue.handle();
    let _ = conn.display().get_registry(&qh, ());
    queue
        .roundtrip(&mut app)
        .map_err(|e| format!("registry 往返失败: {e}"))?;
    // wl_output 的 Geometry/Mode/Name/Scale/Done 在 roundtrip 之后才陆续到齐，
    // 补一轮：每块输出都以 Done 收尾，全部 Done 才算枚举完
    let deadline = Instant::now() + Duration::from_millis(400);
    while Instant::now() < deadline {
        if !app.outputs.is_empty() && app.outputs.iter().all(|o| o.seen_done) {
            break;
        }
        if !pump(&mut queue, &mut app, Duration::from_millis(80))? {
            // 没有新事件也别 tight-loop
            std::thread::sleep(Duration::from_millis(5));
        }
    }
    Ok((conn, queue, app))
}

fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn cmd_list() -> Result<(), String> {
    let (_conn, _queue, app) = connect()?;
    let items: Vec<String> = app
        .outputs
        .iter()
        .map(|o| {
            let scale = if o.scale > 0 { o.scale } else { 1 };
            format!(
                "{{\"name\":\"{}\",\"description\":\"{}\",\"width\":{},\"height\":{},\
                 \"scale\":{},\"x\":{},\"y\":{},\"physical_width\":{},\"physical_height\":{}}}",
                json_escape(&o.name),
                json_escape(&o.description),
                o.logical_w,
                o.logical_h,
                scale,
                o.x,
                o.y,
                o.phys_mm_w,
                o.phys_mm_h
            )
        })
        .collect();
    println!("[{}]", items.join(","));
    if app.outputs.is_empty() {
        eprintln!("ERR 没有枚举到任何 wl_output");
    }
    Ok(())
}

struct Opts {
    output: Option<String>,
    x: i32,
    y: i32,
    width: i32,
    height: i32,
    fps: f64,
    frames: u64,
    cursor: bool,
    once: bool,
}

fn parse_args() -> Result<(String, Opts), String> {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let Some(head) = argv.first() else {
        return Err(
            "用法：hancast-wlgrab list | capture [--output 名称] [--fps 15] [--frames N] [--cursor] [--once] [--x --y --width --height]"
                .to_string(),
        );
    };
    let mut o = Opts {
        output: None,
        x: -1,
        y: -1,
        width: -1,
        height: -1,
        fps: 15.0,
        frames: 0,
        cursor: false,
        once: false,
    };
    let mut i = 1usize;
    // 每条分支自己推进 `i`：写成闭包会把 `i` 借走，循环体后面就没法再改它
    macro_rules! take_num {
        ($key:expr) => {{
            i += 1;
            argv.get(i)
                .ok_or_else(|| format!("{} 缺参数", $key))?
                .replace(',', "")
                .parse::<i64>()
                .map_err(|e| format!("{}: {}", $key, e))?
        }};
    }
    while i < argv.len() {
        match argv[i].as_str() {
            "--output" => {
                i += 1;
                o.output = argv.get(i).cloned().filter(|s| !s.is_empty());
            }
            "--fps" => o.fps = take_num!("--fps") as f64,
            "--frames" => o.frames = take_num!("--frames") as u64,
            "--x" => o.x = take_num!("--x") as i32,
            "--y" => o.y = take_num!("--y") as i32,
            "--width" => o.width = take_num!("--width") as i32,
            "--height" => o.height = take_num!("--height") as i32,
            "--cursor" => o.cursor = true,
            "--once" => {
                o.once = true;
                o.frames = 1;
            }
            other => return Err(format!("不认识的参数: {other}")),
        }
        i += 1;
    }
    if o.fps < 1.0 || o.fps > 120.0 {
        return Err(format!("--fps 要在 1..120，给的是 {}", o.fps));
    }
    Ok((head.clone(), o))
}

fn cmd_capture(opts: Opts) -> Result<(), String> {
    let (_conn, mut queue, mut app) = connect()?;
    let qh = queue.handle();

    let manager = app
        .manager
        .clone()
        .ok_or_else(|| "合成器没有暴露 zwlr_screencopy_manager_v1（grim 同样用不了）".to_string())?;
    let shm = app
        .shm
        .clone()
        .ok_or_else(|| "合成器没有 wl_shm，抓不了帧".to_string())?;
    if app.shm_formats.is_empty() {
        return Err("wl_shm 不支持 ARGB8888/XRGB8888，投不了这个色".to_string());
    }
    if app.outputs.is_empty() {
        return Err("没有枚举到任何 wl_output".to_string());
    }

    // 选屏：按 `Name` 全局事件给的显示器名（wl_output v4）；老协议没名字就退回第一块
    let mut idx = 0usize;
    if let Some(want) = &opts.output {
        if let Some(pos) = app.outputs.iter().position(|o| &o.name == want) {
            idx = pos;
        } else {
            return Err(format!(
                "找不到显示器 {want}，现有：{}",
                app.outputs
                    .iter()
                    .map(|o| o.name.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }
    }
    let target = app
        .bound_outputs
        .get(idx)
        .cloned()
        .ok_or_else(|| "内部错误：目标显示器句柄丢了".to_string())?;
    let out_name = app.outputs[idx].name.clone();

    let mut frame_no = 0u64;
    let mut surface: Option<ShmSurface> = None;
    let mut cur_size: Option<BufInfo> = None;
    let mut dst: Vec<u8> = Vec::new();
    let mut header_done = false;
    let tick = Duration::from_secs_f64(1.0 / opts.fps);
    let stdout = io::stdout();
    let mut out = stdout.lock();
    // 帧率节流：合成器给帧的节奏跟着屏幕刷新走（144Hz 屏上能到 144fps），
    // 不由我们决定。所以按 `tick` 排下一次请求的时间点，宁可少给也别灌爆管道
    // （ffmpeg 那头 `-r` 只会补帧、不会丢帧，但 220MB/s 的裸帧能把 CPU 吃干）。
    let mut next_at = Instant::now();

    /// 这一帧没来：重复上一帧，别让 ffmpeg 的输入断流。
    /// 屏幕静止时合成器**不提交**，screencopy 就是不回帧；rawvideo 的时间戳按到达
    /// 顺序推，输入停 15 秒会让 sidecar 的看门狗（capture.py 的 `_watch`）把
    /// 「画面卡住」误判成采集挂了，用户看到投屏自己断了。
    macro_rules! repeat_last {
        () => {
            if frame_no > 0 && header_done && !dst.is_empty() {
                out.write_all(&dst)
                    .map_err(|e| format!("写 stdout 失败（sidecar 已收流？管道断了）: {e}"))?;
                out.flush().map_err(|e| format!("flush 失败: {e}"))?;
                frame_no += 1;
                if opts.frames > 0 && frame_no >= opts.frames {
                    eprintln!("DONE {frame_no} 帧");
                    return Ok(());
                }
            }
        };
    }

    loop {
        if frame_no > 0 {
            // 上一帧之后没到 tick 的话补个觉；已经超了就直接进下一帧
            if let Some(sleep_for) = next_at.checked_duration_since(Instant::now()) {
                std::thread::sleep(sleep_for);
            }
            next_at = Instant::now() + tick;
        } else {
            next_at = Instant::now() + tick;
        }

        // frame 对象是 one-shot 的，每帧重新请求
        app.reset_frame();

        // 协议里 `frame` 是 new_id，扫描器把它挪到参数表最前面，
        // 所以 Rust 侧的实参顺序是 (overlay_cursor, output, [x, y, w, h], &qh, data)
        let region = opts.width > 0 && opts.height > 0;
        let frame: ZwlrScreencopyFrameV1 = if region {
            manager.capture_output_region(
                opts.cursor as i32,
                &target,
                opts.x.max(0),
                opts.y.max(0),
                opts.width,
                opts.height,
                &qh,
                (),
            )
        } else {
            manager.capture_output(opts.cursor as i32, &target, &qh, ())
        };

        // ① 合成器告诉我们缓冲区尺寸/格式
        let got_buf = wait_for(
            &mut queue,
            &mut app,
            |a| a.pending_buf.is_some() || a.failed,
            BUF_TIMEOUT,
        )?;
        if !got_buf {
            frame.destroy();
            if frame_no == 0 {
                return Err("等待缓冲区描述超时（合成器没响应 screencopy 请求）".to_string());
            }
            repeat_last!();
            continue;
        }
        let info = match app.pending_buf {
            Some(i) => i,
            None => {
                frame.destroy();
                if frame_no == 0 {
                    return Err("抓帧被合成器拒绝（缓冲区协商失败）".to_string());
                }
                repeat_last!();
                continue;
            }
        };
        if info.width == 0 || info.height == 0 {
            frame.destroy();
            return Err(format!("合成器给的帧尺寸非法: {}x{}", info.width, info.height));
        }
        if argb_format(info.format).is_none() {
            frame.destroy();
            return Err(format!(
                "合成器给的像素格式 {} 不是 ARGB/XRGB",
                info.format
            ));
        }
        // ② 尺寸变了才重建共享内存（改分辨率/换屏时走这里）
        if cur_size != Some(info) || surface.is_none() {
            if let Some(old) = surface.take() {
                old.destroy();
            }
            surface = Some(
                ShmSurface::new(&shm, &qh, &info)
                    .map_err(|e| format!("共享内存分配失败: {e}"))?,
            );
            cur_size = Some(info);
            dst = vec![0u8; (info.width * info.height * 4) as usize];
        }
        let surface = surface
            .as_ref()
            .ok_or_else(|| "内部错误：共享内存还没建好".to_string())?;
        let buffer = surface.buffer(&qh, &info)?;
        frame.copy(&buffer);

        // ③ 等 ready：合成器在下一次输出提交时把画面拷进我们的 shm
        let got = wait_for(
            &mut queue,
            &mut app,
            |a| a.ready || a.failed,
            tick.mul_f64(4.0) + Duration::from_millis(500),
        )?;
        if !got || app.failed {
            frame.destroy();
            buffer.destroy();
            if frame_no == 0 {
                return Err("首帧抓取失败（屏幕静止，或合成器把这次捕获判为不可用）".to_string());
            }
            repeat_last!();
            continue;
        }

        // ④ 写 stdout（紧凑 BGRA；ffmpeg 用 -video_size/-pixel_format 直接吃）
        let y_inverted = (app.flags & FLAG_Y_INVERT) != 0;
        if !header_done {
            // 头一行给 sidecar：尺寸、格式、来源显示器。放在第一帧真正要写之前——
            // ready 一定排在 flags 之后，此时 y_invert 才是确定的
            eprintln!(
                "HDR {{\"width\":{},\"height\":{},\"stride\":{},\"pix_fmt\":\"bgra\",\
                 \"output\":\"{}\",\"scale\":{},\"y_invert\":{}}}",
                info.width,
                info.height,
                info.stride,
                json_escape(&out_name),
                if app.outputs[idx].scale > 0 { app.outputs[idx].scale } else { 1 },
                y_inverted
            );
            let _ = io::stderr().flush();
            header_done = true;
        }
        surface.copy_into(&info, y_inverted, &mut dst);
        out.write_all(&dst)
            .map_err(|e| format!("写 stdout 失败（sidecar 已收流？管道断了）: {e}"))?;
        out.flush()
            .map_err(|e| format!("flush 失败: {e}"))?;
        frame.destroy();
        buffer.destroy();

        frame_no += 1;
        if opts.frames > 0 && frame_no >= opts.frames {
            eprintln!("DONE {frame_no} 帧");
            // 带值 break：这个 loop 是函数尾表达式，返回值类型是 Result
            break Ok(());
        }
    }
}

fn main() -> ExitCode {
    let (head, opts) = match parse_args() {
        Ok(v) => v,
        Err(e) => {
            eprintln!("ERR {e}");
            return ExitCode::from(2);
        }
    };
    let r = match head.as_str() {
        "list" => cmd_list(),
        "capture" => cmd_capture(opts),
        other => Err(format!("不认识的子命令: {other}（只有 list / capture）")),
    };
    match r {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("ERR {e}");
            // 连不上/没协议 → 3；格式不支持 → 5；其余（首帧失败、超时、管道断）→ 4。
            // sidecar 只需要看「非 0 + ERR 行」，这三档用来决定是回退 x11grab 还是直接报错。
            if e.contains("连不上 Wayland")
                || e.contains("zwlr_screencopy")
                || e.contains("wl_shm")
                || e.contains("registry 往返失败")
            {
                return ExitCode::from(3);
            }
            if e.contains("不是 ARGB/XRGB") {
                return ExitCode::from(5);
            }
            ExitCode::from(4)
        }
    }
}
