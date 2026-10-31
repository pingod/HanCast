"""
手机遥控器页 HTTP 服务（局域网内浏览器打开即用）

路由（token 是每会话随机的路径段，防止隔壁同事随手点开你的投屏）：
- GET  /r/{token}/            遥控器页面（手机浏览器）
- GET  /r/{token}/api/state   当前投屏状态（JSON，页面每秒轮询）
- POST /r/{token}/api/cmd     控制指令（play/pause/stop/seek/volume/mute/next/prev）

只用 aiohttp，页面是内联 HTML + 原生 fetch，不用构建、不依赖外网。
"""

import os
import json
import time
import asyncio
import logging
import secrets
import threading
from typing import Callable, Dict, Optional

logger = logging.getLogger("hancast.remote")

try:
    from aiohttp import web
except ImportError:
    web = None

DEFAULT_PORT = 8899

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>HanCast 遥控器</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; min-height: 100vh; padding: 18px 16px 28px;
    font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: #16162a; color: #f2f2f7;
    display: flex; flex-direction: column; align-items: stretch; gap: 14px;
  }
  .hd { display: flex; flex-direction: column; gap: 4px; }
  .dev { font-size: 13px; color: #9a9ab0; }
  .title { font-size: 17px; font-weight: 600; line-height: 1.3; overflow-wrap: anywhere; }
  .empty { color: #9a9ab0; font-size: 14px; text-align: center; padding: 28px 0; }
  .bar { height: 6px; border-radius: 3px; background: #2a2a44; overflow: hidden; }
  .bar > i { display: block; height: 100%; width: 0; background: #6c7cff; }
  .times { display: flex; justify-content: space-between; font-size: 12px; color: #9a9ab0; font-variant-numeric: tabular-nums; }
  input[type=range] { width: 100%; accent-color: #6c7cff; }
  .row { display: flex; align-items: center; gap: 10px; }
  .btns { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  button {
    border: 1px solid #33334f; background: #22223a; color: #f2f2f7;
    border-radius: 14px; padding: 16px 0; font-size: 15px; cursor: pointer;
    transition: transform .08s ease, background .12s ease;
  }
  button:active { transform: scale(.96); background: #2e2e4d; }
  button.primary { background: #4b5bd6; border-color: #4b5bd6; }
  .vol { display: flex; align-items: center; gap: 12px; }
  .vol span { font-size: 12px; color: #9a9ab0; min-width: 34px; }
  .foot { margin-top: auto; font-size: 11px; color: #5c5c78; text-align: center; }
</style>
</head>
<body>
  <div class="hd">
    <div class="dev" id="dev">连接中…</div>
    <div class="title" id="title">—</div>
  </div>
  <div class="bar"><i id="fill"></i></div>
  <div class="times"><span id="pos">00:00</span><span id="dur">00:00</span></div>

  <div class="row">
    <button style="flex:1" onclick="seekRel(-10)">-10 秒</button>
    <button style="flex:1" onclick="seekRel(10)">+10 秒</button>
  </div>

  <div class="btns">
    <button class="primary" id="pp" onclick="togglePlay()">播放/暂停</button>
    <button onclick="cmd('stop')">停止</button>
    <button onclick="toggleMute()">静音</button>
  </div>

  <input type="range" id="seek" min="0" max="1000" value="0" step="1" onchange="onSeek(this.value)">
  <div class="vol">
    <span>音量</span>
    <input type="range" id="vol" min="0" max="100" value="50" step="1" onchange="onVol(this.value)">
    <span id="volv">50</span>
  </div>

  <div class="foot">HanCast 手机遥控器</div>

<script>
const TOKEN = __TOKEN__;
const BASE = '/r/' + TOKEN;
let state = null;
let seekDragging = false;

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
  await post('/api/cmd', { action, value });
  await poll();
}
function togglePlay() {
  if (!state || !state.session) return;
  cmd(state.session.status === 'playing' ? 'pause' : 'play');
}
function toggleMute() {
  if (!state || !state.session) return;
  cmd('mute', !state.session.is_muted);
}
function seekRel(delta) {
  if (!state || !state.session) return;
  const cur = toSec(state.session.position) + delta;
  cmd('seek', Math.max(0, cur));
}
function onSeek(v) {
  if (!state || !state.session) return;
  const dur = toSec(state.session.duration);
  cmd('seek', Math.round(dur * Number(v) / 1000));
}
function onVol(v) {
  document.getElementById('volv').textContent = v;
  cmd('volume', Number(v));
}
const seekEl = document.getElementById('seek');
seekEl.addEventListener('input', () => { seekDragging = true; });
seekEl.addEventListener('change', () => { seekDragging = false; });

function render() {
  const s = state && state.session;
  const dev = document.getElementById('dev');
  const title = document.getElementById('title');
  if (!s) {
    dev.textContent = '当前没有投屏';
    title.textContent = '在电脑上开始投屏后这里就会出现控制';
    document.getElementById('fill').style.width = '0%';
    document.getElementById('pos').textContent = '00:00';
    document.getElementById('dur').textContent = '00:00';
    return;
  }
  dev.textContent = s.device_name || s.device_id || '';
  title.textContent = s.title || '（未知标题）';
  const dur = toSec(s.duration), pos = toSec(s.position);
  document.getElementById('pos').textContent = hms(pos);
  document.getElementById('dur').textContent = hms(dur);
  document.getElementById('fill').style.width = (dur > 0 ? Math.min(100, pos / dur * 100) : 0) + '%';
  if (!seekDragging) {
    seekEl.value = dur > 0 ? String(Math.round(pos / dur * 1000)) : '0';
  }
  document.getElementById('pp').textContent = s.status === 'playing' ? '暂停' : '播放';
  const volEl = document.getElementById('vol');
  if (document.activeElement !== volEl && typeof s.volume === 'number') {
    volEl.value = String(s.volume);
    document.getElementById('volv').textContent = String(s.volume);
  }
}
async function poll() {
  try {
    const r = await fetch(BASE + '/api/state');
    state = await r.json();
  } catch (e) { /* 网络抖动就保持上一帧 */ }
  render();
}
poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""


class RemoteControlServer:
    """手机遥控器页服务（aiohttp，独立线程）"""

    def __init__(
        self,
        get_state: Callable[[], Dict],
        action: Callable[[str, object], Dict],
        local_ip: Callable[[], str],
    ):
        self._get_state = get_state
        self._action = action
        self._local_ip = local_ip
        self.host = "0.0.0.0"
        self.port: Optional[int] = None
        self.token: str = ""
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner = None
        self._site = None

    # ── 生命周期 ──
    def start(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        if web is None:
            logger.error("[Remote] aiohttp 不可用，遥控器页无法启动")
            return False
        self.host = host
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
        app = web.Application()
        r = "/r/{token}"
        app.router.add_get(r, self._handle_root)
        app.router.add_get(r + "/", self._handle_page)
        app.router.add_get(r + "/api/state", self._handle_state)
        app.router.add_post(r + "/api/cmd", self._handle_cmd)
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
