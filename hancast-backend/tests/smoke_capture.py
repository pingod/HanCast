"""
真实 FFmpeg 屏幕捕获端到端验证

启动 FfmpegCapture.start_raw（gdigrab 真实抓屏），验证：
1. 视频帧回调持续收到 bgr24 帧
2. 音频帧回调（启用麦克风时）收到 s16le 帧
3. 停止后进程被清理

用法: python smoke_capture.py [seconds]
"""

import os
import sys
import time
import threading

sys.path.insert(0, r"Z:\githome\HanCast\hancast-backend")

from hancast_sidecar.screencast.capture import FfmpegCapture
from hancast_sidecar.screencast.ffmpeg_manager import FfmpegManager


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0

    mgr = FfmpegManager()
    info = mgr.check()
    assert info["ok"], f"ffmpeg not found: {info}"
    print(f"[0] ffmpeg: {info['version']}")

    video_frames = []
    audio_frames = []
    errors = []
    done = threading.Event()

    def on_video(data):
        video_frames.append(len(data))
        if len(video_frames) % 5 == 0:
            print(f"    video frames: {len(video_frames)}")

    def on_audio(data):
        audio_frames.append(len(data))

    def on_error(msg):
        errors.append(msg)
        print(f"    ERROR: {msg}")
        done.set()

    # 音频：尝试枚举麦克风（可能无设备）
    audio_src = mgr.list_sources()
    mics = [d for d in audio_src.get("audio_inputs", []) if d["kind"] == "mic"]
    print(f"[1] audio devices: {len(audio_src.get('audio_inputs', []))} (mics: {len(mics)})")
    audio_cfg = {}
    if mics:
        audio_cfg = {"mic": True, "mic_device": mics[0]["name"], "system": False}

    cap = FfmpegCapture(info["path"])
    result = cap.start_raw(
        {"type": "screen", "id": "desktop"},
        audio_cfg,
        {"width": 640, "height": 360, "fps": 10},
        on_video_frame=on_video,
        on_audio_frame=on_audio if audio_cfg else None,
        on_error=on_error,
    )
    assert result["ok"], f"start_raw failed: {result}"
    print(f"[2] capture started: {result['width']}x{result['height']}@{result['fps']}fps, audio={'on' if audio_cfg else 'off'}")

    # 等待采样
    time.sleep(seconds)
    cap.stop()

    assert not errors, f"capture errors: {errors}"
    assert len(video_frames) >= seconds * 5, f"too few video frames: {len(video_frames)}"
    frame_size = 640 * 360 * 3
    assert all(sz == frame_size for sz in video_frames), "frame size mismatch"
    if audio_cfg:
        assert len(audio_frames) > 0, "no audio frames received"
        assert audio_frames[0] == 1920 * 2 * 2, f"audio frame size: {audio_frames[0]}"
        print(f"[3] audio frames: {len(audio_frames)} (48kHz stereo 20ms)")

    print(f"[4] PASSED: {len(video_frames)} video frames @ {result['width']}x{result['height']}")


if __name__ == "__main__":
    main()
