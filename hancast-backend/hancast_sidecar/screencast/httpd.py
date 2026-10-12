"""
屏幕投屏 HTTP 服务（独立 asyncio 线程）

路由：
- GET  /r/{session_id}            观看页（浏览器免安装，HLS 播放）
- GET  /static/{filename}         观看页静态资源（hls.js）
- GET  /hls/{session_id}/{file}   HLS 播放列表与分片（浏览器 + DLNA 电视共用）

播放器统一走 HLS：电视拉 /hls/<id>/live.m3u8，浏览器拉同一份分片。
延迟约 3-6 秒，换来的是零依赖（浏览器只要支持 MSE 或原生 HLS）。
"""

import os
import time
import asyncio
import logging
import threading
from collections import defaultdict
from typing import Dict, Optional

logger = logging.getLogger("hancast.screencast.httpd")

try:
    from aiohttp import web
except ImportError:
    web = None

DEFAULT_PORT = 8765

# 观看页目录（本文件同级的 www/）
WWW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")

# 观众判定窗口：N 秒内拉过播放列表的独立 IP 数
VIEWER_WINDOW = 15.0

_STATIC_TYPES = {
    ".js": "application/javascript",
    ".css": "text/css",
    ".html": "text/html",
}


class ScreenCastHttpd:
    """屏幕投屏 HTTP 服务"""

    def __init__(self, hls_root: str):
        self.hls_root = hls_root
        self.port: Optional[int] = None
        self.host: str = "0.0.0.0"
        # 当前活跃的投屏会话 id（观看页与 HLS 路径按此放行）
        self.sessions: set = set()
        # session_id -> 最近拉流过播放列表的客户端 IP → 时间戳
        self._viewers: Dict[str, Dict[str, float]] = defaultdict(dict)
        # session_id -> 累计拉流统计。电视是否真的在拉流只能靠这个判断：
        # 「点了投屏但电视黑屏」的两种根因（网络不通 / 解码不了）分界点就在这
        self._pull_stats: Dict[str, Dict] = {}
        self._reported: Dict[str, int] = {}
        self._viewer_callbacks: Dict[str, object] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._runner: Optional[object] = None
        self._site: Optional[object] = None
        self._lock = threading.Lock()
        os.makedirs(hls_root, exist_ok=True)

    # ── 生命周期 ──

    def start(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> bool:
        """启动服务（幂等）。返回是否成功。"""
        if self._thread and self._thread.is_alive():
            return True
        if web is None:
            logger.error("[ScreenCast] aiohttp 不可用，HTTP 服务无法启动")
            return False
        self.host = host
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
                # 端口冲突回退：随机端口
                for try_port in (port, 0):
                    try:
                        site = web.TCPSite(self._runner, self.host, try_port)
                        loop.run_until_complete(site.start())
                        self.port = site._server.sockets[0].getsockname()[1]
                        self._site = site
                        result["ok"] = True
                        break
                    except OSError as e:
                        logger.warning(f"[ScreenCast] Port {try_port} busy: {e}")
                started.set()
                if result["ok"]:
                    loop.run_forever()
            except Exception as e:
                logger.exception("[ScreenCast] Httpd thread crashed")
                result["error"] = str(e)
                started.set()
            finally:
                self._loop = None

        self._thread = threading.Thread(
            target=_run, name="screencast-httpd", daemon=True)
        self._thread.start()
        started.wait(timeout=10)
        return result.get("ok", False)

    def stop(self):
        """停止服务（幂等）"""
        loop = self._loop
        thread = self._thread
        if loop is not None:
            try:
                async def _shutdown():
                    if self._runner:
                        await self._runner.cleanup()
                fut = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
                fut.result(timeout=5)
            except Exception as e:
                logger.warning(f"[ScreenCast] Httpd shutdown: {e}")
            # 必须停掉 run_forever，否则线程存活、下次 start() 被
            # is_alive() 短路返回 True，但服务实际已死（会话注册静默失败）
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        self._runner = None
        self._site = None
        self._loop = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._thread = None

    # ── 会话注册 ──

    def register_session(self, session_id: str,
                         on_viewers=None) -> bool:
        with self._lock:
            if session_id in self.sessions:
                return False
            self.sessions.add(session_id)
        self._viewer_callbacks[session_id] = on_viewers
        logger.info(f"[ScreenCast] Session registered: {session_id}")
        return True

    def unregister_session(self, session_id: str):
        with self._lock:
            # sessions 是 set：pop() 不接受参数，写成 pop(id, None) 会在每次
            # 停止投屏时抛 TypeError 并冒到命令循环，表现为「点了停止没反应」
            self.sessions.discard(session_id)
            self._viewers.pop(session_id, None)
            self._pull_stats.pop(session_id, None)
            self._viewer_callbacks.pop(session_id, None)

    def pull_stats(self, session_id: str) -> Dict:
        """本会话的拉流累计：{pulls, misses, clients, last}"""
        with self._lock:
            stats = self._pull_stats.get(session_id)
            if not stats:
                return {"pulls": 0, "misses": 0, "clients": [], "last": None}
            return {"pulls": stats["pulls"], "misses": stats["misses"],
                    "clients": sorted(stats["clients"]), "last": stats["last"]}

    def viewers(self, session_id: str) -> int:
        """窗口内拉过播放列表的独立客户端数"""
        seen = self._viewers.get(session_id)
        if not seen:
            return 0
        cutoff = time.time() - VIEWER_WINDOW
        return sum(1 for ts in seen.values() if ts >= cutoff)

    def _note_pull(self, session_id: str, client: str):
        """记录一次拉流并按观众数变化通知管理侧"""
        with self._lock:
            bucket = self._viewers.setdefault(session_id, {})
            if client in bucket or len(bucket) < 64:
                bucket[client] = time.time()
            stats = self._pull_stats.setdefault(
                session_id, {"pulls": 0, "misses": 0,
                             "clients": set(), "last": None})
            stats["pulls"] += 1
            stats["last"] = time.time()
            if len(stats["clients"]) < 64:
                stats["clients"].add(client)
            before = self._reported.get(session_id)
            after = self.viewers(session_id)
            if before == after:
                return
            self._reported[session_id] = after
            cb = self._viewer_callbacks.get(session_id)
        if cb is None:
            return
        try:
            cb(session_id, after)
        except Exception:
            logger.exception("[ScreenCast] viewer callback failed")

    def _note_miss(self, session_id: str):
        """记录一次「要的文件不在」：电视拉到已被删除的旧分片说明它跟不上码率"""
        with self._lock:
            stats = self._pull_stats.setdefault(
                session_id, {"pulls": 0, "misses": 0,
                             "clients": set(), "last": None})
            stats["misses"] += 1

    # ── 应用 ──

    def _build_app(self):
        app = web.Application()
        app.router.add_get("/r/{session_id}", self._handle_watch_page)
        app.router.add_get("/static/{filename:.*}", self._handle_static)
        app.router.add_get("/hls/{session_id}/{filename:.*}", self._handle_hls)
        return app

    async def _handle_watch_page(self, request):
        session_id = request.match_info["session_id"]
        # aiohttp 禁止 content_type 里带参数（charset 要用 charset= 传），
        # 写在一起会让这两个分支在真正被触发时抛 ValueError 变成 500
        if session_id not in self.sessions:
            return web.Response(text="投屏已结束或地址无效", status=404,
                                content_type="text/plain", charset="utf-8")
        index_path = os.path.join(WWW_DIR, "index.html")
        if not os.path.isfile(index_path):
            return web.Response(text="观看页缺失", status=500,
                                content_type="text/plain", charset="utf-8")
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace("__SESSION_ID__", session_id)
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _handle_static(self, request):
        name = os.path.basename(request.match_info["filename"])
        path = os.path.join(WWW_DIR, name)
        ext = os.path.splitext(name)[1].lower()
        if ext not in _STATIC_TYPES or not os.path.isfile(path):
            return web.Response(status=404)
        return web.FileResponse(
            path, headers={"Content-Type": _STATIC_TYPES[ext] + "; charset=utf-8"})

    # ── HLS 静态服务 ──

    def _safe_hls_path(self, session_id: str, filename: str) -> Optional[str]:
        """防止目录穿越"""
        if ".." in filename or ":" in filename or "\\" in filename:
            return None
        base = os.path.join(self.hls_root, session_id)
        path = os.path.realpath(os.path.join(base, filename))
        base_real = os.path.realpath(base)
        if not path.startswith(base_real + os.sep) and path != base_real:
            return None
        return path

    async def _handle_hls(self, request):
        session_id = request.match_info["session_id"]
        filename = request.match_info["filename"]
        # 会话结束即断流：只看目录是否存在的话，异常退出留下的残留目录会让
        # 电视一直拉到一条死流，用户以为还在投屏
        if session_id not in self.sessions:
            self._note_miss(session_id)
            return web.Response(status=404)
        if filename.endswith(".m3u8"):
            self._note_pull(session_id, request.remote or "unknown")
        path = self._safe_hls_path(session_id, filename)
        if path is None or not os.path.isfile(path):
            self._note_miss(session_id)
            return web.Response(status=404)
        if filename.endswith(".m3u8"):
            ctype = "application/vnd.apple.mpegurl"
        elif filename.endswith(".ts"):
            ctype = "video/mp2t"
        else:
            ctype = "application/octet-stream"
        # 直播列表每 1 秒重生成，缓存会让观众看到过期分片
        return web.FileResponse(path, headers={
            "Content-Type": ctype, "Cache-Control": "no-cache, no-store",
            # DLNA 渲染器靠这两个头区分「边下边播」和「先下完再播」：缺
            # transferMode 的直播播放列表会被部分电视当普通文件缓冲，
            # 表现为控制器收到 Play 却迟迟等不到拉流请求
            "transferMode.dlna.org": "Streaming",
            "contentFeatures.dlna.org":
                "DLNA.ORG_OP=01;DLNA.ORG_CI=0;"
                "DLNA.ORG_FLAGS=01700000000000000000000000000000",
        })
