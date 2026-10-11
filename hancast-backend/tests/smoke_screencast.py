"""
屏幕投屏端到端冒烟测试（真实启动 httpd + aiortc 房间 + 模拟观众）

验证：
1. httpd 启动成功并占用端口
2. 观看页 /r/{room} 返回 HTML
3. HLS 静态服务返回正确 Content-Type
4. WebSocket 信令：模拟观众发 offer → 服务端返回 answer
5. 房间 viewer_count 统计正确
6. 共享 track 帧供给（捕获线程模拟 set_frame → recv 出帧）

运行：python smoke_screencast.py
"""

import os
import sys
import json
import asyncio
import threading
import tempfile

sys.path.insert(0, r"Z:\githome\HanCast\hancast-backend")
sys.path.insert(0, os.environ.get("SC_DEPS", r"%TEMP%\hancast_screencast_deps"))

from hancast_sidecar.screencast.httpd import ScreenCastHttpd
from hancast_sidecar.screencast.webrtc_room import WebRtcRoom


async def main():
    hls_root = os.path.join(tempfile.gettempdir(), "hancast_smoke_hls")
    os.makedirs(hls_root, exist_ok=True)

    httpd = ScreenCastHttpd(hls_root=hls_root)
    ok = httpd.start(port=0)  # 随机端口避免冲突
    assert ok, "httpd start failed"
    print(f"[1] httpd started on port {httpd.port}")

    # 创建并注册房间
    room = WebRtcRoom(loop=httpd._loop, room_id="abc123", fps=15)
    assert httpd.register_room(room), "register room failed"
    print("[2] room registered")

    port = httpd.port

    # 观看页
    import aiohttp
    async with aiohttp.ClientSession() as sess:
        async with sess.get(f"http://127.0.0.1:{port}/r/abc123") as resp:
            assert resp.status == 200
            html = await resp.text()
            assert "HanCast" in html and "abc123" in html
            print("[3] watch page OK")

        # HLS 静态
        hls_dir = os.path.join(hls_root, "dlna")
        os.makedirs(hls_dir, exist_ok=True)
        with open(os.path.join(hls_dir, "live.m3u8"), "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:1.0,\nseg001.ts\n#EXT-X-ENDLIST\n")
        with open(os.path.join(hls_dir, "seg001.ts"), "wb") as f:
            f.write(b"\x47" * 100)  # TS 包头
        async with sess.get(f"http://127.0.0.1:{port}/hls/dlna/live.m3u8") as resp:
            assert resp.status == 200
            assert "mpegurl" in resp.headers.get("Content-Type", "")
            print("[4] HLS m3u8 OK")
        async with sess.get(f"http://127.0.0.1:{port}/hls/dlna/seg001.ts") as resp:
            assert resp.status == 200
            assert "mp2t" in resp.headers.get("Content-Type", "")
            print("[5] HLS ts OK")
        # 目录穿越
        async with sess.get(f"http://127.0.0.1:{port}/hls/dlna/../secret") as resp:
            assert resp.status in (404, 400)
            print("[6] path traversal blocked")

        # WebSocket 信令：真实 aiortc 客户端 offer → answer → ICE 协商
        from aiortc import RTCPeerConnection, RTCSessionDescription
        pc = RTCPeerConnection()
        pc.addTransceiver("video", direction="recvonly")
        pc.addTransceiver("audio", direction="recvonly")
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        connected = asyncio.Event()
        failed = asyncio.Event()

        @pc.on("icecandidate")
        def _on_ice(candidate):
            if candidate is not None and ws.ready_state == 1:
                asyncio.ensure_future(ws.send_json({
                    "type": "ice",
                    "candidate": {
                        "candidate": candidate.candidate,
                        "sdpMid": candidate.sdpMid,
                        "sdpMLineIndex": candidate.sdpMLineIndex,
                    },
                }))

        @pc.on("connectionstatechange")
        def _on_state():
            state = pc.connectionState
            print(f"    client connectionState -> {state}")
            if state == "connected":
                connected.set()
            if state in ("failed", "closed"):
                failed.set()

        import aiohttp as aio
        async with aio.ClientSession() as ws_sess:
            async with ws_sess.ws_connect(
                    f"http://127.0.0.1:{port}/ws?room=abc123&peer=viewer1") as ws:
                await ws.send_json({"type": "offer", "sdp": pc.localDescription.sdp})
                while not connected.is_set() and not failed.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.receive_json(), timeout=5)
                    except asyncio.TimeoutError:
                        continue  # 无新消息，轮询连接状态
                    if msg["type"] == "answer":
                        desc = RTCSessionDescription(sdp=msg["sdp"], type="answer")
                        await pc.setRemoteDescription(desc)
                        print("[7] WS signaling offer->answer OK")
                    elif msg["type"] == "ice":
                        from aiortc import RTCIceCandidate
                        cand = msg.get("candidate", {})
                        await pc.addIceCandidate(RTCIceCandidate(
                            candidate=cand.get("candidate", ""),
                            sdpMid=cand.get("sdpMid"),
                            sdpMLineIndex=cand.get("sdpMLineIndex"),
                        ))
                    elif msg["type"] == "error":
                        failed.set()
                        print(f"    server error: {msg.get('message')}")
                if failed.is_set():
                    raise AssertionError("signaling failed")
                assert connected.is_set(), "ICE connection did not establish"
                print("[8] ICE connection established (P2P)")
                # 客户端保持连接期间，服务端应计入 1 个观众
                assert room.viewer_count == 1, f"viewer_count={room.viewer_count}"
                print("[9] viewer count = 1 during connection")

        await pc.close()
        # 断开后服务端应移除观众
        await asyncio.sleep(1)
        assert room.viewer_count == 0, f"viewer_count after close={room.viewer_count}"
        print("[10] viewer removed after disconnect")

    # 共享 track 帧供给（无需真实 FFmpeg，直接喂数组）
    import numpy as np
    arr = np.zeros((360, 640, 3), dtype=np.uint8)
    room.set_video_frame(arr)
    frame = await room.video_track.recv()
    assert frame.width == 640 and frame.height == 360
    print("[11] shared video track frame OK")

    # 关闭房间
    await room.close()
    assert room.viewer_count == 0
    print("[12] room closed cleanly")

    httpd.stop()
    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
