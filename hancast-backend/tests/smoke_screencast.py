"""
屏幕投屏端到端冒烟测试（真实 httpd + 真实 FFmpeg HLS，如有）

验证：
1. httpd 启动成功并占用端口
2. 观看页 /r/{session} 返回 HTML，且 hls.js 可加载
3. HLS 播放列表/分片的 Content-Type 正确、分片可解（TS 同步字节）
4. 目录穿越被拒
5. 拉流证据统计（pulls/viewers/clients）——DLNA 排错唯一依赖的信号
6. 停止会话后观看页与播放列表都 404（不给电视留死流）

运行：python tests/smoke_screencast.py [--hold 60]
      --hold N：跑完后保留服务 N 秒并打印观看地址，便于真机/真浏览器验证
"""

import os
import sys
import time
import shutil
import asyncio
import argparse
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.screencast.ffmpeg_manager import FfmpegManager
from hancast_sidecar.screencast.httpd import ScreenCastHttpd

SEG_BYTES = 188 * 200  # 一个 ~37KB 的 TS 分片，够 hls.js 建缓冲


def write_fake_hls(hls_dir: str):
    """没有 FFmpeg 时用的最小合法 HLS（TS 同步字节 0x47）"""
    os.makedirs(hls_dir, exist_ok=True)
    seg = bytes([0x47] + [0] * 187) * 200
    with open(os.path.join(hls_dir, "seg000.ts"), "wb") as f:
        f.write(seg)
    with open(os.path.join(hls_dir, "live.m3u8"), "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n"
                "#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nseg000.ts\n")


def ffmpeg_live_hls(ffmpeg: str, hls_dir: str) -> bool:
    """真实 FFmpeg 出直播流（testsrc 合成画面），验证编码参数可用"""
    import subprocess
    os.makedirs(hls_dir, exist_ok=True)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15",
           "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
           "-profile:v", "main", "-level", "42", "-pix_fmt", "yuv420p",
           "-g", "30", "-r", "15",
           "-x264-params", "scenecut=0:open_gop=0:repeat_headers=1",
           "-max_muxing_queue_size", "1024",
           "-t", "25", "-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
           "-hls_flags", "delete_segments+independent_segments+temp_file",
           "-hls_segment_filename", os.path.join(hls_dir, "seg%03d.ts"),
           os.path.join(hls_dir, "live.m3u8")]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    deadline = time.time() + 12
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if os.path.isfile(os.path.join(hls_dir, "live.m3u8")):
            return True
        time.sleep(0.2)
    proc.terminate()
    return False


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold", type=int, default=0,
                        help="保留服务并打印观看地址的秒数")
    args = parser.parse_args()

    hls_root = os.path.join(tempfile.gettempdir(), "hancast_smoke_hls")
    shutil.rmtree(hls_root, ignore_errors=True)
    httpd = ScreenCastHttpd(hls_root=hls_root)
    assert httpd.start(port=0), "httpd start failed"
    print(f"[1] httpd started on port {httpd.port}")

    sid = "smoke1"
    assert httpd.register_session(sid, on_viewers=lambda s, n: None)
    hls_dir = os.path.join(hls_root, sid)
    ffmpeg = FfmpegManager().get_path()
    real = False
    if ffmpeg:
        real = ffmpeg_live_hls(ffmpeg, hls_dir)
        print(f"    ffmpeg={'yes'} live_hls={real}")
    if not real:
        write_fake_hls(hls_dir)
        print("[2] using static fixture HLS")
    else:
        print("[2] real FFmpeg HLS pipeline")

    port = httpd.port
    import aiohttp
    async with aiohttp.ClientSession() as sess:
        async with sess.get(f"http://127.0.0.1:{port}/r/{sid}") as resp:
            assert resp.status == 200, resp.status
            html = await resp.text()
            assert "HanCast" in html and sid in html, "watch page wrong"
            assert "/static/hls.min.js" in html
            print("[3] watch page OK")

        async with sess.get(f"http://127.0.0.1:{port}/static/hls.min.js") as resp:
            assert resp.status == 200, "hls.js missing from bundle"
            body = await resp.text()
            assert "Hls" in body or "hls" in body
            print(f"[4] hls.js served OK ({len(body)} bytes)")

        async with sess.get(f"http://127.0.0.1:{port}/hls/{sid}/live.m3u8") as resp:
            assert resp.status == 200
            assert "mpegurl" in resp.headers.get("Content-Type", "")
            assert "no-cache" in resp.headers.get("Cache-Control", "")
            text = await resp.text()
            assert text.startswith("#EXTM3U"), text[:40]
            print("[5] playlist OK: " + " / ".join(
                line for line in text.splitlines() if line and
                not line.startswith("#"))[:60])

        seg = next((line.strip() for line in text.splitlines()
                    if line.strip().endswith(".ts")), None)
        assert seg, "playlist has no segment"
        async with sess.get(f"http://127.0.0.1:{port}/hls/{sid}/{seg}") as resp:
            assert resp.status == 200
            assert "mp2t" in resp.headers.get("Content-Type", "")
            data = await resp.read()
            assert data[:1] == b"\x47", "TS sync byte missing"
            assert len(data) > 188
            print(f"[6] segment OK ({len(data)} bytes, ts synced)")

        async with sess.get(f"http://127.0.0.1:{port}/hls/{sid}/../live.m3u8") as resp:
            assert resp.status in (404, 400), resp.status
            print("[7] path traversal blocked")

        stats = httpd.pull_stats(sid)
        assert stats["pulls"] >= 1 and "127.0.0.1" in stats["clients"]
        assert httpd.viewers(sid) >= 1
        print(f"[8] pull evidence OK: {stats}")

        # 停止会话：观看页与流都必须立刻 404，电视才不会一直拉死流
        httpd.unregister_session(sid)
        async with sess.get(f"http://127.0.0.1:{port}/r/{sid}") as resp:
            assert resp.status == 404
        async with sess.get(f"http://127.0.0.1:{port}/hls/{sid}/live.m3u8") as resp:
            assert resp.status == 404
        assert httpd.pull_stats(sid)["pulls"] == 0
        print("[9] session teardown: watch page + playlist 404")

    if args.hold:
        # 真机/真浏览器验证：重新起一个会话并挂起
        sid2 = "hold1"
        httpd.register_session(sid2)
        d2 = os.path.join(hls_root, sid2)
        if not (real and ffmpeg_live_hls(ffmpeg, d2)):
            write_fake_hls(d2)
        url = f"http://127.0.0.1:{httpd.port}/r/{sid2}"
        print(f"\nWATCH_URL={url}\nplaylist={httpd.hls_root}/"
              f"{sid2}/live.m3u8  (holding {args.hold}s)")
        await asyncio.sleep(args.hold)

    httpd.stop()
    assert httpd._thread is None and httpd._loop is None
    print("[10] httpd stopped cleanly (restart-safe)")
    shutil.rmtree(hls_root, ignore_errors=True)
    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
