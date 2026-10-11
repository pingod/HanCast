"""
FFmpeg 屏幕捕获管线

两种输出模式（由调用方选择）：
- raw 模式（供 WebRTC 房间）：
    视频 → rawvideo bgr24 → stdout（逐帧回调 numpy 数组）
    音频 → pcm_s16le 48kHz 双声道 → stdout（第二个独立进程，逐帧回调 bytes）
- hls 模式（供 DLNA 推流）：
    视频 + 音频 → h264/aac → HLS 分片写入指定目录（电视拉流）

两个模式均为独立 FFmpeg 子进程 + 独立读取线程，不阻塞 sidecar 主循环。
"""

import os
import sys
import time
import queue
import logging
import threading
import subprocess
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("hancast.screencast.capture")

# raw 音频帧：48kHz 双声道 s16 = 1920 samples * 2ch * 2bytes = 7680 bytes
RAW_AUDIO_SAMPLE_RATE = 48000
RAW_AUDIO_CHANNELS = 2
RAW_AUDIO_SAMPLES = 1920  # 20ms


def _read_exact(stream, size: int) -> Optional[bytes]:
    """从流中精确读取 size 字节；EOF/读取不完整返回 None"""
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None  # EOF：不完整帧无意义，直接失败
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class RawCaptureThread(threading.Thread):
    """读取 FFmpeg stdout 的线程（视频或音频）"""

    def __init__(self, proc: subprocess.Popen, frame_size: int,
                 on_frame: Callable, on_error: Callable, name: str):
        super().__init__(name=f"ffmpeg-{name}", daemon=True)
        self._proc = proc
        self._frame_size = frame_size
        self._on_frame = on_frame
        self._on_error = on_error
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            while not self._stop.is_set():
                data = _read_exact(self._proc.stdout, self._frame_size)
                if data is None:
                    break
                if len(data) < self._frame_size:
                    break
                try:
                    self._on_frame(data)
                except Exception:
                    logger.exception("Raw frame callback failed")
        except Exception as e:
            self._on_error(str(e))
        finally:
            # 进程可能还有 stderr 未消费，关闭管道
            try:
                if self._proc.stdout:
                    self._proc.stdout.close()
            except Exception:
                pass


class FfmpegCapture:
    """FFmpeg 屏幕/音频捕获管线"""

    def __init__(self, ffmpeg_path: str):
        self.ffmpeg = ffmpeg_path
        self._procs: List[subprocess.Popen] = []
        self._threads: List[RawCaptureThread] = []
        self._lock = threading.Lock()
        self._running = False
        self._error: Optional[str] = None

    # ────────────────────────────────────────
    # raw 模式（WebRTC）
    # ────────────────────────────────────────

    def start_raw(self, source: Dict, audio_cfg: Dict, video_cfg: Dict,
                  on_video_frame: Callable, on_audio_frame: Optional[Callable],
                  on_error: Optional[Callable] = None) -> Dict:
        """启动原始帧捕获（WebRTC 模式）

        返回 {"ok": bool, "width": int, "height": int, "fps": int, "error": str?}
        """
        self.stop()

        width = int(video_cfg.get("width") or 0) or 1280
        height = int(video_cfg.get("height") or 0) or 720
        fps = int(video_cfg.get("fps") or 15)

        def _err(msg):
            self._error = msg
            if on_error:
                on_error(msg)
            logger.error(f"[ScreenCast] {msg}")

        # ── 视频进程 ──
        video_input = self._video_input_args(source, fps, f"{width}:{height}")
        video_cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error"] + video_input + [
            "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
        ]
        try:
            vproc = self._spawn(video_cmd)
        except Exception as e:
            _err(f"启动视频捕获失败: {e}")
            return {"ok": False, "error": f"启动视频捕获失败: {e}"}

        frame_size = width * height * 3
        vt = RawCaptureThread(vproc, frame_size, on_video_frame, _err, "video")
        self._procs.append(vproc)
        self._threads.append(vt)

        # ── 音频进程（可选）──
        audio_cfg = audio_cfg or {}
        if (audio_cfg.get("mic") or audio_cfg.get("system")) and on_audio_frame:
            audio_input = self._audio_input_args(audio_cfg)
            if audio_input:
                audio_cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error"] + audio_input + [
                    "-f", "s16le", "-ar", str(RAW_AUDIO_SAMPLE_RATE),
                    "-ac", str(RAW_AUDIO_CHANNELS), "pipe:1",
                ]
                try:
                    aproc = self._spawn(audio_cmd)
                except Exception as e:
                    _err(f"启动音频捕获失败: {e}")
                    self.stop()
                    return {"ok": False, "error": f"启动音频捕获失败: {e}"}
                audio_frame_size = RAW_AUDIO_SAMPLES * RAW_AUDIO_CHANNELS * 2
                at = RawCaptureThread(aproc, audio_frame_size, on_audio_frame, _err, "audio")
                self._procs.append(aproc)
                self._threads.append(at)
            else:
                logger.warning("[ScreenCast] Audio requested but no capture device available")

        self._running = True
        for t in self._threads:
            t.start()

        # 预读一帧校验管线（1s 内无输出说明捕获失败）
        try:
            first = _read_exact(vproc.stdout, frame_size)
            if not first:
                _err("屏幕捕获无输出（可能是权限/窗口不存在）")
                self.stop()
                return {"ok": False, "error": "屏幕捕获无输出（可能是权限/窗口不存在）"}
            on_video_frame(first)
        except Exception as e:
            _err(f"屏幕捕获校验失败: {e}")
            self.stop()
            return {"ok": False, "error": f"屏幕捕获校验失败: {e}"}

        logger.info(f"[ScreenCast] Raw capture started: {width}x{height}@{fps}fps")
        return {"ok": True, "width": width, "height": height, "fps": fps}

    # ────────────────────────────────────────
    # hls 模式（DLNA）
    # ────────────────────────────────────────

    def start_hls(self, source: Dict, audio_cfg: Dict, video_cfg: Dict,
                  out_dir: str, segment_prefix: str = "seg") -> Dict:
        """启动 HLS 编码输出（DLNA 模式）

        输出 out_dir/live.m3u8 + out_dir/seg*.ts
        """
        self.stop()

        width = int(video_cfg.get("width") or 0)
        height = int(video_cfg.get("height") or 0)
        fps = int(video_cfg.get("fps") or 15)
        bitrate = video_cfg.get("bitrate") or "2M"

        scale = f"{width}:{height}" if width > 0 and height > 0 else ""

        os.makedirs(out_dir, exist_ok=True)
        # 清理旧分片
        for name in os.listdir(out_dir):
            if name.startswith(segment_prefix) and name.endswith(".ts"):
                try:
                    os.remove(os.path.join(out_dir, name))
                except OSError:
                    pass

        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error",
               "-nostdin"]
        cmd += self._video_input_args(source, fps, scale)
        audio_input = self._audio_input_args(audio_cfg)
        if audio_input:
            cmd += audio_input
        # 输出段
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-b:v", bitrate, "-pix_fmt", "yuv420p", "-g", str(fps * 2),
        ]
        if audio_input:
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
        else:
            cmd += ["-an"]
        cmd += [
            "-f", "hls",
            "-hls_time", "1",
            "-hls_list_size", "6",
            "-hls_flags", "delete_segments+independent_segments",
            "-hls_segment_filename", os.path.join(out_dir, f"{segment_prefix}%03d.ts"),
            os.path.join(out_dir, "live.m3u8"),
        ]

        logger.info(f"[ScreenCast] HLS cmd: {' '.join(cmd)}")
        try:
            proc = self._spawn(cmd)
        except Exception as e:
            self._error = f"启动 HLS 编码失败: {e}"
            logger.error(self._error)
            return {"ok": False, "error": self._error}

        self._procs.append(proc)
        self._running = True

        # 等待 m3u8 生成（最多 5s）
        m3u8_path = os.path.join(out_dir, "live.m3u8")
        deadline = time.time() + 5
        while time.time() < deadline:
            if proc.poll() is not None:
                self._error = "FFmpeg 提前退出，屏幕捕获失败"
                logger.error(self._error)
                self.stop()
                return {"ok": False, "error": self._error}
            if os.path.isfile(m3u8_path) and os.path.getsize(m3u8_path) > 0:
                break
            time.sleep(0.1)
        if not (os.path.isfile(m3u8_path) and os.path.getsize(m3u8_path) > 0):
            self._error = "HLS 输出超时未生成（屏幕捕获失败）"
            logger.error(self._error)
            self.stop()
            return {"ok": False, "error": self._error}

        logger.info(f"[ScreenCast] HLS capture started: {m3u8_path}")
        return {"ok": True, "m3u8": m3u8_path}

    # ────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────

    def _spawn(self, cmd: List[str]) -> subprocess.Popen:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=creationflags, bufsize=0,
        )

    def _video_input_args(self, source: Dict, fps: int, scale: str) -> List[str]:
        from .ffmpeg_manager import FfmpegManager
        return FfmpegManager.build_video_input(source, fps, scale)

    def _audio_input_args(self, audio_cfg: Dict) -> List[str]:
        from .ffmpeg_manager import FfmpegManager
        return FfmpegManager.build_audio_input(audio_cfg)

    def stop(self):
        """停止所有捕获进程（幂等）"""
        with self._lock:
            for t in self._threads:
                t.stop()
            for proc in self._procs:
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            for proc in self._procs:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            self._procs.clear()
            self._threads.clear()
            self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def error(self) -> Optional[str]:
        return self._error
