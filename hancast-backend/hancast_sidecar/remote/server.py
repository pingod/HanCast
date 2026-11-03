"""
手机遥控器页 HTTP 服务（局域网内浏览器打开即用）

设计要点：
- **随应用常驻**：sidecar 初始化时就启动（不再等第一次投屏），打开就能用；
  端口优先 8899，占用则回退随机。token 也是启动时生成、整个会话不变
  （重启/换端口才换），这样手机可以把地址加书签。
- 页面是完全自包含的单个 HTML（内联 CSS/JS，不构建、不依赖外网/CDN），
  手机上打开即用；媒体选择/粘贴解析/投屏都复用后端既有命令，不另起一套。

路由（token 是启动时随机生成、整个会话不变的路径段，防止隔壁同事随手点开）：
- GET  /r/{token}/                 遥控器页面（手机浏览器）
- GET  /r/{token}/api/state        当前状态（设备/会话/队列/历史，页面每秒轮询）
- POST /r/{token}/api/cmd          控制指令（play/pause/stop/seek/volume/mute/
                                   next/prev/mode/stop_after/focus_device/load_folder）
- POST /r/{token}/api/paste        粘贴链接/分享口令 → 解析（后台任务，返回 task_id）
- GET  /r/{token}/api/resolve      轮询解析任务结果（?task_id=）
- POST /r/{token}/api/upload       手机相册上传（multipart）→ 落地到本机临时目录 → 即投

只用 aiohttp，页面是内联 HTML + 原生 fetch，不用构建。
"""

import os
import io
import json
import time
import asyncio
import logging
import secrets
import tempfile
import threading
from typing import Callable, Dict, Optional

logger = logging.getLogger("hancast.remote")

try:
    from aiohttp import web
except ImportError:
    web = None

DEFAULT_PORT = 8899

# 上传上限：手机相册里一段视频可能很大，给到 4GB（超过就拒绝，避免把磁盘写满）
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024

# 形如 http 的地址才在这里接受粘贴（其余走 share_resolver 的分享口令/网页解析）
_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#16162a">
<title>HanCast 遥控器</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; min-height: 100vh; padding: 16px 14px 26px;
    font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: #16162a; color: #f2f2f7;
    display: flex; flex-direction: column; align-items: stretch; gap: 12px;
  }
  .hd { display: flex; align-items: center; gap: 10px; }
  .devsel { flex: 1; min-width: 0; }
  select {
    width: 100%; background: #22223a; color: #f2f2f7; border: 1px solid #33334f;
    border-radius: 12px; padding: 10px 12px; font-size: 14px;
  }
  .card {
    background: #1e1e33; border: 1px solid #2c2c46; border-radius: 16px; padding: 14px;
    display: flex; flex-direction: column; gap: 12px;
  }
  .nowrow { display: flex; gap: 14px; align-items: center; }
  .cover {
    width: 84px; height: 84px; border-radius: 12px; flex-shrink: 0;
    background: #2a2a44 center/cover no-repeat; display: flex; align-items: center;
    justify-content: center; color: #6c7cff; font-size: 30px; overflow: hidden;
  }
  .cover img { width: 100%; height: 100%; object-fit: cover; }
  .meta { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
  .title { font-size: 16px; font-weight: 600; line-height: 1.3; overflow-wrap: anywhere; }
  .sub { font-size: 12px; color: #9a9ab0; }
  .empty { color: #9a9ab0; font-size: 14px; text-align: center; padding: 22px 0; }
  .bar { height: 6px; border-radius: 3px; background: #2a2a44; overflow: hidden; }
  .bar > i { display: block; height: 100%; width: 0; background: #6c7cff; }
  .times { display: flex; justify-content: space-between; font-size: 12px; color: #9a9ab0; font-variant-numeric: tabular-nums; }
  input[type=range] { width: 100%; accent-color: #6c7cff; height: 30px; }
  .row { display: flex; align-items: center; gap: 10px; }
  .btns { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .btns4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  button {
    border: 1px solid #33334f; background: #22223a; color: #f2f2f7;
    border-radius: 14px; padding: 14px 0; font-size: 15px; cursor: pointer;
    transition: transform .08s ease, background .12s ease;
    font-family: inherit;
  }
  button:active { transform: scale(.95); background: #2e2e4d; }
  button:disabled { opacity: .4; }
  button.primary { background: #4b5bd6; border-color: #4b5bd6; }
  button.sm { padding: 10px 0; font-size: 13px; }
  button.on { background: #4b5bd6; border-color: #4b5bd6; }
  .vol { display: flex; align-items: center; gap: 12px; }
  .vol span { font-size: 12px; color: #9a9ab0; min-width: 34px; }
  .hint { font-size: 12px; color: #9a9ab0; }
  .err { font-size: 13px; color: #ff8b8b; }
  .seg { display: flex; gap: 8px; }
  .seg button { flex: 1; padding: 9px 0; font-size: 13px; }
  .panel { display: none; flex-direction: column; gap: 10px; }
  .panel.show { display: flex; }
  input[type=text] {
    width: 100%; background: #16162a; color: #f2f2f7; border: 1px solid #33334f;
    border-radius: 12px; padding: 12px; font-size: 14px;
  }
  .hist { display: flex; flex-direction: column; gap: 8px; max-height: 42vh; overflow-y: auto; }
  .hist-item { display: flex; gap: 10px; align-items: center; background: #22223a; border-radius: 12px; padding: 9px 10px; }
  .hist-item .h-cover { width: 40px; height: 40px; border-radius: 8px; flex-shrink: 0; background: #2a2a44 center/cover no-repeat; display:flex; align-items:center; justify-content:center; font-size: 16px; overflow: hidden; }
  .hist-item .h-cover img { width:100%; height:100%; object-fit: cover; }
  .hist-item .h-meta { flex: 1; min-width: 0; }
  .hist-item .h-title { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hist-item .h-sub { font-size: 11px; color: #9a9ab0; }
  .queue-list { display: flex; flex-direction: column; gap: 6px; max-height: 40vh; overflow-y: auto; }
  .q-item { display: flex; gap: 8px; align-items: center; padding: 8px 10px; border-radius: 10px; background: #22223a; font-size: 13px; }
  .q-item.playing { border: 1px solid #4b5bd6; }
  .q-item .q-idx { color: #9a9ab0; min-width: 24px; font-variant-numeric: tabular-nums; }
  .q-item .q-t { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .foot { margin-top: auto; font-size: 11px; color: #5c5c78; text-align: center; }
  .toast { position: fixed; left: 50%; bottom: 30px; transform: translateX(-50%); background: #4b5bd6; color: #fff; padding: 10px 18px; border-radius: 20px; font-size: 13px; opacity: 0; transition: opacity .2s; pointer-events: none; z-index: 50; }
  .toast.show { opacity: 1; }
  @media (orientation: landscape) and (max-height: 520px) {
    body { gap: 8px; padding: 10px 22px 16px; }
    .cover { width: 56px; height: 56px; }
    button { padding: 10px 0; }
  }
</style>
</head>
<body>
  <div class="hd">
    <div class="devsel"><select id="devsel"></select></div>
    <button class="sm" style="width:44px;padding:10px 0" onclick="refresh()" title="刷新">⟳</button>
  </div>

  <div class="card" id="nowcard">
    <div class="nowrow">
      <div class="cover" id="cover"><img id="coverimg" style="display:none"><span id="coverph">🎬</span></div>
      <div class="meta">
        <div class="title" id="title">—</div>
        <div class="sub" id="devname">连接中…</div>
      </div>
    </div>
    <div class="bar"><i id="fill"></i></div>
    <div class="times"><span id="pos">00:00</span><span id="dur">00:00</span></div>
    <input type="range" id="seek" min="0" max="1000" value="0" step="1">
    <div class="btns4">
      <button class="sm" onclick="seekRel(-10)">⏪10</button>
      <button class="sm" onclick="seekRel(-5)">⏪5</button>
      <button class="sm" onclick="seekRel(5)">5⏩</button>
      <button class="sm" onclick="seekRel(10)">10⏩</button>
    </div>
    <div class="btns">
      <button class="sm" onclick="cmd('prev')">⏮ 上一集</button>
      <button class="sm primary" id="pp" onclick="togglePlay()">播放/暂停</button>
      <button class="sm" onclick="cmd('next')">下一集 ⏭</button>
    </div>
    <div class="btns">
      <button class="sm" onclick="cmd('stop')">⏹ 停止</button>
      <button class="sm" id="mute" onclick="toggleMute()">🔇 静音</button>
      <button class="sm" id="sac" onclick="toggleStopAfter()">播完即停</button>
    </div>
    <div class="vol">
      <span>音量</span>
      <input type="range" id="vol" min="0" max="100" value="50" step="1">
      <span id="volv">50</span>
    </div>
  </div>

  <div class="seg">
    <button class="sm" onclick="showPanel('pick')">📂 选媒体</button>
    <button class="sm" onclick="showPanel('queue')">📋 队列</button>
    <button class="sm" onclick="showPanel('hist')">🕘 历史</button>
  </div>

  <!-- 选媒体 -->
  <div class="card panel" id="pick">
    <div class="row"><input type="text" id="pasteInput" placeholder="粘贴视频链接或分享口令…"></div>
    <div class="row">
      <button class="sm primary" style="flex:1" onclick="doPaste()">解析并投屏</button>
    </div>
    <div class="row">
      <label class="sm" style="flex:1;text-align:center;border:1px solid #33334f;background:#22223a;border-radius:14px;padding:11px 0;font-size:13px;cursor:pointer">
        📱 上传手机视频
        <input type="file" id="uploadInput" accept="video/*,audio/*" style="display:none">
      </label>
    </div>
    <div class="hint" id="pickMsg"></div>
    <div v-if="false"></div>
  </div>

  <!-- 队列 -->
  <div class="card panel" id="queue">
    <div class="row"><span class="sub" id="qcount">队列为空</span></div>
    <div class="seg">
      <button class="sm" id="mSeq" onclick="setMode('sequential')">顺序</button>
      <button class="sm" id="mShuf" onclick="setMode('shuffle')">随机</button>
      <button class="sm" id="mRep" onclick="setMode('repeat-one')">单曲</button>
    </div>
    <div class="queue-list" id="qlist"></div>
  </div>

  <!-- 历史 -->
  <div class="card panel" id="hist">
    <div class="hint">点条目一键重投到当前设备</div>
    <div class="hist" id="histlist"></div>
  </div>

  <div class="foot">HanCast 手机遥控器 · 常驻可用</div>
  <div class="toast" id="toast"></div>

<script>
const TOKEN = __TOKEN__;
const BASE = '/r/' + TOKEN;
let state = null;
let seekDragging = false;
let activePanel = '';

function hms(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(2, '0'), ss = String(s).padStart(2, '0');
  return h > 0 ? (h + ':' + mm + ':' + ss) : (mm + ':' + ss);
}
function toSec(t) {
  if (!t) return 0;
  const p = String(t).split(':').map(Number);
  if (p.length === 3) return p[0]*3600 + p[1]*60 + p[2];
  if (p.length === 2) return p[0]*60 + p[1];
  return p[0] || 0;
}
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 1600);
}
async function post(path, body) {
  try {
    const r = await fetch(BASE + path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    return await r.json();
  } catch (e) { return { ok: false, error: String(e) }; }
}
async function cmd(action, value) {
  const r = await post('/api/cmd', { action, value });
  if (r && r.ok === false) toast(r.error || '操作失败');
  await poll();
  return r;
}
function togglePlay() {
  if (!state || !state.session) return;
  cmd(state.session.status === 'playing' ? 'pause' : 'play');
}
function toggleMute() {
  if (!state || !state.session) return;
  cmd('mute', !state.session.is_muted);
}
function toggleStopAfter() {
  if (!state) return;
  cmd('stop_after', !state.stop_after_current);
}
function seekRel(delta) {
  if (!state || !state.session) return;
  const cur = toSec(state.session.position) + delta;
  cmd('seek', Math.max(0, cur));
}
const seekEl = document.getElementById('seek');
seekEl.addEventListener('input', () => { seekDragging = true; });
seekEl.addEventListener('change', () => {
  seekDragging = false;
  const dur = state && state.session ? toSec(state.session.duration) : 0;
  cmd('seek', Math.round(dur * Number(seekEl.value) / 1000));
});
const volEl = document.getElementById('vol');
volEl.addEventListener('input', () => { document.getElementById('volv').textContent = volEl.value; });
volEl.addEventListener('change', () => { cmd('volume', Number(volEl.value)); });

function showPanel(name) {
  activePanel = (activePanel === name) ? '' : name;
  ['pick','queue','hist'].forEach(p => {
    document.getElementById(p).classList.toggle('show', p === activePanel);
  });
  if (activePanel === 'queue') renderQueue();
  if (activePanel === 'hist') renderHistory();
}

// 选设备
function onDevChange() {
  const v = document.getElementById('devsel').value;
  if (v) cmd('focus_device', v);
}
function renderDevSel() {
  const sel = document.getElementById('devsel');
  const sessions = (state && state.sessions) || [];
  const cur = state && state.focus;
  if (!sessions.length) { sel.innerHTML = '<option>暂无设备</option>'; return; }
  sel.innerHTML = sessions.map(s =>
    '<option value="' + s.device_id + '"' + (s.device_id === cur ? ' selected' : '') + '>' +
    (s.device_name || s.device_id) + (s.status === 'playing' ? ' ▶' : '') + '</option>'
  ).join('');
}

function render() {
  const s = state && state.session;
  renderDevSel();
  const title = document.getElementById('title');
  const dev = document.getElementById('devname');
  const coverimg = document.getElementById('coverimg');
  const coverph = document.getElementById('coverph');
  if (!s) {
    title.textContent = '当前没有投屏';
    dev.textContent = '用下方「选媒体」开始播放';
    document.getElementById('fill').style.width = '0%';
    document.getElementById('pos').textContent = '00:00';
    document.getElementById('dur').textContent = '00:00';
    coverimg.style.display = 'none'; coverph.style.display = '';
    return;
  }
  title.textContent = s.title || '（未知标题）';
  dev.textContent = s.device_name || s.device_id || '';
  if (s.thumbnail) { coverimg.src = s.thumbnail; coverimg.style.display = ''; coverph.style.display = 'none'; }
  else { coverimg.style.display = 'none'; coverph.style.display = ''; }
  const dur = toSec(s.duration), pos = toSec(s.position);
  document.getElementById('pos').textContent = hms(pos);
  document.getElementById('dur').textContent = hms(dur);
  document.getElementById('fill').style.width = (dur > 0 ? Math.min(100, pos / dur * 100) : 0) + '%';
  if (!seekDragging) seekEl.value = dur > 0 ? String(Math.round(pos / dur * 1000)) : '0';
  document.getElementById('pp').textContent = s.status === 'playing' ? '暂停' : '播放';
  if (document.activeElement !== volEl && typeof s.volume === 'number') {
    volEl.value = String(s.volume);
    document.getElementById('volv').textContent = String(s.volume);
  }
  document.getElementById('mute').classList.toggle('on', !!s.is_muted);
  document.getElementById('sac').classList.toggle('on', !!(state && state.stop_after_current));
  if (activePanel === 'queue') renderQueue();
}

function renderQueue() {
  const q = (state && state.queue) || [];
  document.getElementById('qcount').textContent = q.length ? ('队列 ' + q.length + ' 项') : '队列为空';
  const mode = (state && state.play_mode) || 'sequential';
  document.getElementById('mSeq').classList.toggle('on', mode === 'sequential');
  document.getElementById('mShuf').classList.toggle('on', mode === 'shuffle');
  document.getElementById('mRep').classList.toggle('on', mode === 'repeat-one');
  const list = document.getElementById('qlist');
  list.innerHTML = q.map((it, i) =>
    '<div class="q-item' + (it.playing ? ' playing' : '') + '"><span class="q-idx">' + (i+1) + '</span>' +
    '<span class="q-t">' + esc(it.title) + '</span></div>'
  ).join('') || '<div class="hint">在电脑端选文件夹，或手机上传多个视频，队列会出现在这里</div>';
}
function renderHistory() {
  const h = (state && state.history) || [];
  const list = document.getElementById('histlist');
  if (!h.length) { list.innerHTML = '<div class="hint">还没有投屏历史</div>'; return; }
  list.innerHTML = h.map((e, i) =>
    '<div class="hist-item" onclick="castHistory(' + i + ')">' +
      '<div class="h-cover">' + (e.thumbnail ? '<img src="' + esc(e.thumbnail) + '">' : '🎬') + '</div>' +
      '<div class="h-meta"><div class="h-title">' + esc(e.title) + '</div>' +
      '<div class="h-sub">' + esc(e.device_name || '') + ' · ' + (e.kind === 'queue' ? '文件夹' : e.kind === 'url' ? '链接' : '文件') + '</div></div>' +
    '</div>'
  ).join('');
}
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

async function castHistory(i) {
  const e = (state.history || [])[i];
  if (!e) return;
  toast('重新投屏中…');
  const r = await post('/api/cmd', { action: 'cast_history', value: i });
  if (r && r.ok === false) toast(r.error || '重投失败');
  await poll();
}

function setMode(mode) { cmd('mode', mode); }

async function doPaste() {
  const v = document.getElementById('pasteInput').value.trim();
  const msg = document.getElementById('pickMsg');
  if (!v) { msg.textContent = '请先粘贴链接或分享口令'; return; }
  msg.textContent = '解析中…（最长约 25 秒）';
  const r = await post('/api/paste', { text: v });
  if (!r || !r.task_id) { msg.textContent = '解析启动失败：' + ((r && r.error) || '未知'); return; }
  // 轮询
  const t0 = Date.now();
  const timer = setInterval(async () => {
    if (Date.now() - t0 > 30000) { clearInterval(timer); msg.textContent = '解析超时'; return; }
    const rr = await fetch(BASE + '/api/resolve?task_id=' + encodeURIComponent(r.task_id)).then(x => x.json().catch(() => ({})));
    if (rr.status === 'done') {
      clearInterval(timer);
      msg.textContent = '已投屏：' + (rr.result && rr.result.title ? rr.result.title : '');
      document.getElementById('pasteInput').value = '';
      await poll();
    } else if (rr.status === 'error') {
      clearInterval(timer); msg.textContent = '解析失败：' + (rr.error || '');
    }
  }, 1200);
}

document.getElementById('uploadInput').addEventListener('change', async (ev) => {
  const f = ev.target.files && ev.target.files[0];
  ev.target.value = '';
  if (!f) return;
  const msg = document.getElementById('pickMsg');
  msg.textContent = '上传中… ' + (f.size > 1048576 ? (Math.round(f.size/1048576) + ' MB') : (Math.round(f.size/1024) + ' KB'));
  const fd = new FormData();
  fd.append('file', f, f.name || 'upload.mp4');
  try {
    const r = await fetch(BASE + '/api/upload', { method: 'POST', body: fd }).then(x => x.json());
    if (r && r.ok) { msg.textContent = '上传完成，开始播放：' + (r.title || ''); await poll(); }
    else { msg.textContent = '上传失败：' + ((r && r.error) || '未知'); }
  } catch (e) { msg.textContent = '上传失败：' + e; }
});

async function poll() {
  try {
    const r = await fetch(BASE + '/api/state');
    state = await r.json();
  } catch (e) { /* 网络抖动保持上一帧 */ }
  render();
}
// 刷新按钮
function refresh() { poll(); toast('已刷新'); }

// 防息屏（Wake Lock）
async function keepAwake() {
  try {
    if ('wakeLock' in navigator) { await navigator.wakeLock.request('screen'); }
  } catch (e) { /* 忽略：部分浏览器不支持 */ }
}
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') keepAwake(); });
keepAwake();

poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""


class RemoteControlServer:
    """手机遥控器页服务（aiohttp，独立线程，随应用常驻）"""

    def __init__(
        self,
        get_state: Callable[[], Dict],
        action: Callable[[str, object], Dict],
        local_ip: Callable[[], str],
    ):
        self._get_state = get_state
        self._action = action
        self._local_ip = local_ip
        # 上传/解析相关的钩子，由 CommandHandler 注入（见 set_content_hooks）
        self._on_upload: Optional[Callable[[str, str], Dict]] = None
        self._on_paste: Optional[Callable[[str], Dict]] = None
        self._on_resolve_poll: Optional[Callable[[str], Dict]] = None
        self.host = "0.0.0.0"
        self.port: Optional[int] = None
        self.token: str = ""
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner = None
        self._site = None
        # 上传落地目录（懒建）
        self._upload_dir: Optional[str] = None

    def set_content_hooks(
        self,
        on_upload: Callable[[str, str], Dict],
        on_paste: Callable[[str], Dict],
        on_resolve_poll: Callable[[str], Dict],
    ) -> None:
        """注入「上传/粘贴解析」三条后端动作（避免遥控器页自己复制一套解析/投屏逻辑）。

        - on_upload(tmp_path, filename) -> {ok, title, uri}：把已落地的临时文件解析并投给当前设备
        - on_paste(text) -> {task_id}：起一个后台解析任务（复用 resolve_share）
        - on_resolve_poll(task_id) -> {status, result?, error?}
        """
        self._on_upload = on_upload
        self._on_paste = on_paste
        self._on_resolve_poll = on_resolve_poll

    # ── 生命周期 ──
    def start(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        if web is None:
            logger.error("[Remote] aiohttp 不可用，遥控器页无法启动")
            return False
        self.host = host
        if not self.token:
            self.token = secrets.token_urlsafe(9)
        started = threading.Event()
        result = {"ok": False}

        def _run():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                app = self._build_app()
                self._runner = web.AppRunner(app)
                loop.run_until_complete(self._runner.setup())
                for try_port in (port, 0):
                    try:
                        site = web.TCPSite(self._runner, self.host, try_port)
                        loop.run_until_complete(site.start())
                        self.port = site._server.sockets[0].getsockname()[1]
                        self._site = site
                        result["ok"] = True
                        break
                    except OSError as e:
                        logger.warning(f"[Remote] Port {try_port} busy: {e}")
                started.set()
                if result["ok"]:
                    loop.run_forever()
            except Exception as e:
                logger.exception("[Remote] thread crashed")
                result["error"] = str(e)
                started.set()

        self._thread = threading.Thread(target=_run, name="hancast-remote", daemon=True)
        self._thread.start()
        started.wait(timeout=5)
        if result.get("ok"):
            logger.info(f"[Remote] 遥控器页已启动: {self.url}")
        return bool(result.get("ok"))

    def stop(self) -> None:
        loop = self._loop
        if loop and self._runner:
            try:
                asyncio.run_coroutine_threadsafe(self._cleanup(), loop).result(timeout=3)
            except Exception:
                pass
        self._thread = None
        self._loop = None
        self._runner = None
        self._site = None
        self.port = None

    async def _cleanup(self):
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        loop = asyncio.get_event_loop()
        loop.stop()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self.port)

    @property
    def url(self) -> str:
        if not self.port:
            return ""
        return f"http://{self._local_ip()}:{self.port}/r/{self.token}/"

    # ── 路由 ──
    def _build_app(self):
        app = web.Application(client_max_size=MAX_UPLOAD_BYTES)
        r = "/r/{token}"
        app.router.add_get(r, self._handle_root)
        app.router.add_get(r + "/", self._handle_page)
        app.router.add_get(r + "/api/state", self._handle_state)
        app.router.add_post(r + "/api/cmd", self._handle_cmd)
        app.router.add_post(r + "/api/paste", self._handle_paste)
        app.router.add_get(r + "/api/resolve", self._handle_resolve)
        app.router.add_post(r + "/api/upload", self._handle_upload)
        return app

    def _bad_token(self, request) -> bool:
        return request.match_info.get("token", "") != self.token

    async def _handle_root(self, request):
        if self._bad_token(request):
            raise web.HTTPNotFound()
        raise web.HTTPFound(f"/r/{self.token}/")

    async def _handle_page(self, request):
        if self._bad_token(request):
            raise web.HTTPNotFound()
        html = _PAGE.replace("__TOKEN__", json.dumps(self.token))
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _handle_state(self, request):
        if self._bad_token(request):
            raise web.HTTPNotFound()
        try:
            data = self._get_state()
        except Exception as e:
            logger.warning(f"[Remote] 取状态失败: {e}")
            data = {"session": None}
        return web.json_response(data, dumps=lambda o: json.dumps(o, ensure_ascii=False))

    async def _handle_cmd(self, request):
        if self._bad_token(request):
            raise web.HTTPNotFound()
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str(body.get("action", ""))
        value = body.get("value")
        try:
            result = self._action(action, value)
            ok = result is not False
        except Exception as e:
            logger.warning(f"[Remote] 指令 {action} 失败: {e}")
            return web.json_response({"ok": False, "error": str(e)},
                                     dumps=lambda o: json.dumps(o, ensure_ascii=False))
        return web.json_response({"ok": ok, "result": result},
                                 dumps=lambda o: json.dumps(o, ensure_ascii=False))

    async def _handle_paste(self, request):
        """粘贴链接/分享口令 → 起后台解析任务，返回 task_id（页面轮询 /api/resolve）"""
        if self._bad_token(request):
            raise web.HTTPNotFound()
        if self._on_paste is None:
            return web.json_response({"ok": False, "error": "粘贴解析不可用"},
                                     dumps=lambda o: json.dumps(o, ensure_ascii=False))
        try:
            body = await request.json()
        except Exception:
            body = {}
        text = str(body.get("text", "")).strip()
        if not text:
            return web.json_response({"ok": False, "error": "内容为空"},
                                     dumps=lambda o: json.dumps(o, ensure_ascii=False))
        try:
            r = self._on_paste(text)
        except Exception as e:
            logger.warning(f"[Remote] 启动解析失败: {e}")
            return web.json_response({"ok": False, "error": str(e)},
                                     dumps=lambda o: json.dumps(o, ensure_ascii=False))
        return web.json_response(r, dumps=lambda o: json.dumps(o, ensure_ascii=False))

    async def _handle_resolve(self, request):
        """轮询解析任务结果"""
        if self._bad_token(request):
            raise web.HTTPNotFound()
        task_id = request.query.get("task_id", "")
        if self._on_resolve_poll is None:
            return web.json_response({"status": "error", "error": "不可用"},
                                     dumps=lambda o: json.dumps(o, ensure_ascii=False))
        try:
            r = self._on_resolve_poll(task_id)
        except Exception as e:
            return web.json_response({"status": "error", "error": str(e)},
                                     dumps=lambda o: json.dumps(o, ensure_ascii=False))
        return web.json_response(r, dumps=lambda o: json.dumps(o, ensure_ascii=False))

    async def _handle_upload(self, request):
        """手机相册上传：multipart 表单里一个 file 字段，落地到临时文件后交给后端解析并投屏"""
        if self._bad_token(request):
            raise web.HTTPNotFound()
        if self._on_upload is None:
            return web.json_response({"ok": False, "error": "上传不可用"},
                                     dumps=lambda o: json.dumps(o, ensure_ascii=False))
        try:
            reader = await request.multipart()
        except Exception as e:
            return web.json_response({"ok": False, "error": f"表单解析失败: {e}"},
                                     dumps=lambda o: json.dumps(o, ensure_ascii=False))
        saved_path = None
        filename = "upload.mp4"
        try:
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name != "file":
                    continue
                filename = part.filename or "upload.mp4"
                if self._upload_dir is None:
                    self._upload_dir = os.path.join(tempfile.gettempdir(), "hancast-remote-uploads")
                    os.makedirs(self._upload_dir, exist_ok=True)
                # 保留原扩展名（后端按扩展名判断能否被播放器拉流）
                ext = os.path.splitext(filename)[1] or ".mp4"
                fd, saved_path = tempfile.mkstemp(prefix="up_", suffix=ext, dir=self._upload_dir)
                size = 0
                with os.fdopen(fd, "wb") as f:
                    while True:
                        chunk = await part.read_chunk(256 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_UPLOAD_BYTES:
                            raise ValueError("文件超过 4GB 上限")
                        f.write(chunk)
                logger.info(f"[Remote] 收到上传 {filename} ({size} 字节) → {saved_path}")
                r = self._on_upload(saved_path, filename)
                return web.json_response(r, dumps=lambda o: json.dumps(o, ensure_ascii=False))
            return web.json_response({"ok": False, "error": "没有收到文件"},
                                     dumps=lambda o: json.dumps(o, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"[Remote] 上传失败: {e}")
            if saved_path and os.path.exists(saved_path):
                try:
                    os.remove(saved_path)
                except Exception:
                    pass
            return web.json_response({"ok": False, "error": str(e)},
                                     dumps=lambda o: json.dumps(o, ensure_ascii=False))
