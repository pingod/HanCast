"""
屏幕投屏 HTTP 服务（独立 asyncio 线程）

路由：
- GET  /r/{room_id}            观看页（浏览器免安装）
- WS   /ws?room=&peer=         WebRTC 信令
- GET  /hls/{room_id}/{file}   HLS 分片（DLNA 电视拉流）
- GET  /api/rooms/{room_id}    房间状态（观众数等）
- GET  /api/rooms              （保留：房间列表）

使用 aiohttp + aiortc，必须在独立 asyncio 事件循环线程中运行，
避免阻塞 sidecar 主命令循环。
"""

import os
import re
import json
import asyncio
import logging
import threading
import secrets
from typing import Callable, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger("hancast.screencast.httpd")

try:
    from aiohttp import web, WSMsgType
except ImportError:
    web = None
    WSMsgType = None

DEFAULT_PORT = 8765

# 观看页目录（本文件同级的 www/）
WWW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")


class ScreenCastHttpd:
    """屏幕投屏 HTTP 服务"""

    def __init__(self, hls_root: str):
        self.hls_root = hls_root
        self.port: Optional[int] = None
        self.host: str = "0.0.0.0"
        self.rooms: Dict[str, object] = {}  # room_id -> WebRtcRoom
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._runner: Optional[object] = None
        self._site: Optional[object] = None
        os.makedirs(hls_root, exist_ok=True)

    # ── 生命周期 ──

    def start(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> bool:
        """启动服务（幂等）。返回是否成功。"""
        if self._thread and self._thread.is_alive():
            return True
        self.host = host
        started = threading.Event()
        result = {"ok": False}

        def _run():
            try:
                asyncio.set_event_loop(asyncio.new_event_loop())
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
        if loop is not None:
            try:
                async def _shutdown():
                    if self._runner:
                        await self._runner.cleanup()
                fut = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
                fut.result(timeout=5)
            except Exception as e:
                logger.warning(f"[ScreenCast] Httpd shutdown: {e}")
        self._runner = None
        self._site = None
        self._loop = None

    def run_coro(self, coro):
        """在 httpd 事件循环线程中执行协程（线程安全，返回 asyncio.Future）"""
        if self._loop is None:
            raise RuntimeError("httpd not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ── 房间注册 ──

    def register_room(self, room) -> bool:
        """注册 WebRTC 房间（在 httpd loop 中执行）"""
        try:
            fut = self.run_coro(self._register(room))
            return fut.result(timeout=5)
        except Exception as e:
            logger.error(f"[ScreenCast] register room failed: {e}")
            return False

    async def _register(self, room) -> bool:
        if room.room_id in self.rooms:
            return False
        self.rooms[room.room_id] = room
        logger.info(f"[ScreenCast] Room registered: {room.room_id}")
        return True

    def unregister_room(self, room_id: str):
        try:
            self.run_coro(self._unregister(room_id)).result(timeout=5)
        except Exception:
            pass

    async def _unregister(self, room_id: str):
        self.rooms.pop(room_id, None)

    # ── 应用 ──

    def _build_app(self):
        app = web.Application()
        app.router.add_get("/r/{room_id}", self._handle_watch_page)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/hls/{room_id}/{filename:.*}", self._handle_hls)
        app.router.add_get("/api/rooms/{room_id}", self._handle_room_status)
        return app

    async def _handle_watch_page(self, request):
        room_id = request.match_info["room_id"]
        if room_id not in self.rooms:
            return web.Response(text="房间不存在或已结束", status=404,
                                content_type="text/plain; charset=utf-8")
        index_path = os.path.join(WWW_DIR, "index.html")
        if not os.path.isfile(index_path):
            return web.Response(text="观看页缺失", status=500,
                                content_type="text/plain; charset=utf-8")
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()
        # 注入房间 id 与标题
        html = html.replace("__ROOM_ID__", room_id)
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _handle_room_status(self, request):
        room_id = request.match_info["room_id"]
        room = self.rooms.get(room_id)
        if room is None:
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        return web.json_response({
            "ok": True,
            "room_id": room_id,
            "viewers": room.viewer_count,
            "max_viewers": room.MAX_VIEWERS,
        })

    # ── WebSocket 信令 ──

    async def _handle_ws(self, request):
        room_id = request.query.get("room", "")
        peer_id = request.query.get("peer", "")
        room = self.rooms.get(room_id)
        if room is None:
            return web.Response(text="房间不存在", status=404)
        if not peer_id:
            peer_id = secrets.token_hex(4)

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        if not room.can_join():
            await ws.send_json({"type": "error", "message": "房间已满"})
            await ws.close()
            return ws

        async def _send_ice(candidate_dict: Dict):
            try:
                await ws.send_json({"type": "ice", "candidate": candidate_dict})
            except Exception:
                pass

        async def _on_close(_peer_id):
            try:
                await ws.close()
            except Exception:
                pass

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                mtype = payload.get("type")
                try:
                    if mtype == "offer":
                        sdp = await room.handle_offer(
                            peer_id, payload.get("sdp", ""),
                            ice_callback=_send_ice, close_callback=_on_close)
                        await ws.send_json({"type": "answer", "sdp": sdp})
                    elif mtype == "ice":
                        await room.add_ice(peer_id, payload.get("candidate", {}))
                except RuntimeError as e:
                    await ws.send_json({"type": "error", "message": str(e)})
                except Exception as e:
                    logger.warning(f"[ScreenCast] ws handler error: {e}")
        finally:
            try:
                await room.disconnect(peer_id)
            except Exception:
                pass
        return ws

    # ── HLS 静态服务 ──

    def _safe_hls_path(self, room_id: str, filename: str) -> Optional[str]:
        """防止目录穿越"""
        if ".." in filename or ":" in filename or "\\" in filename:
            return None
        base = os.path.join(self.hls_root, room_id)
        path = os.path.realpath(os.path.join(base, filename))
        base_real = os.path.realpath(base)
        if not path.startswith(base_real + os.sep) and path != base_real:
            return None
        return path

    async def _handle_hls(self, request):
        room_id = request.match_info["room_id"]
        filename = request.match_info["filename"]
        path = self._safe_hls_path(room_id, filename)
        if path is None or not os.path.isfile(path):
            return web.Response(status=404)
        if filename.endswith(".m3u8"):
            ctype = "application/vnd.apple.mpegurl"
        elif filename.endswith(".ts"):
            ctype = "video/mp2t"
        else:
            ctype = "application/octet-stream"
        return web.FileResponse(path, headers={"Content-Type": ctype})
