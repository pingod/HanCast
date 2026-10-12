"""
FFmpeg 屏幕捕获管线

输出 HLS（浏览器与 DLNA 电视共用同一路径）：
    视频 + 音频 → h264/aac → 分片写入 <out_dir>/live.m3u8 + seg*.ts

独立 FFmpeg 子进程 + 独立看门狗线程，不阻塞 sidecar 主命令循环。
"""

import os
import time
import shutil
import logging
import threading
import subprocess
from collections import deque
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("hancast.screencast.capture")

# stderr 尾部保留行数：FFmpeg 提前退出时用于把真实原因带给用户
_STDERR_TAIL = 40


class FfmpegCapture:
    """FFmpeg 屏幕/音频捕获管线（HLS 输出）"""

    def __init__(self, ffmpeg_path: str):
        self.ffmpeg = ffmpeg_path
        self._proc: Optional[subprocess.Popen] = None
        self._threads: List[threading.Thread] = []
        self._lock = threading.Lock()
        self._running = False
        self._error: Optional[str] = None
        self._stderr_tail: deque = deque(maxlen=_STDERR_TAIL)
        self._stop_requested = False

    # ────────────────────────────────────────
    # HLS 输出
    # ────────────────────────────────────────

    def start_hls(self, source: Dict, audio_cfg: Dict, video_cfg: Dict,
                  out_dir: str, segment_prefix: str = "seg",
                  on_error: Optional[Callable] = None) -> Dict:
        """启动 HLS 编码输出

        out_dir 必须是本次会话独占目录（旧内容整体删除）：复用同一目录会让
        上一轮的 live.m3u8 残留，电视拉到的是已死的分片。
        """
        self.stop()

        width = int(video_cfg.get("width") or 0)
        height = int(video_cfg.get("height") or 0)
        fps = int(video_cfg.get("fps") or 15)
        bitrate = video_cfg.get("bitrate") or "2M"

        from .ffmpeg_manager import FfmpegManager
        vf = FfmpegManager.build_scale_filter(width, height)

        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir, exist_ok=True)

        started_at = time.time()
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        cmd += self._video_input_args(source, fps, vf)
        # 视频占输入 0，音频输入从 1 起（amix 标签需此偏移）
        audio_input = self._audio_input_args(audio_cfg, input_offset=1)
        if audio_input:
            cmd += audio_input
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            # 电视解码器普遍只认 Main profile（High 的 8x8 变换/CABAC 变长码
            # 在老款渲染器上直接拒绝播放，表现为"换个分辨率就黑屏"）
            "-profile:v", "main", "-level", "42",
            "-b:v", str(bitrate), "-pix_fmt", "yuv420p", "-g", str(fps * 2),
            # 输入设备帧率失控时（macOS avfoundation 实测）按目标帧率 CFR 封顶
            "-r", str(fps),
            # scenecut=0：屏幕内容几乎每帧都是"场景切换"，默认策略会在每次
            # 切窗口时插 IDR，HLS 只能按关键帧切片，结果是几百毫秒的碎分片，
            # 电视解析器直接放弃。固定 GOP + 闭合 GOP + 重复 SPS/PPS，
            # 让每个分片都能独立起解。
            "-x264-params", "scenecut=0:open_gop=0:repeat_headers=1",
        ]
        if audio_input:
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
        else:
            cmd += ["-an"]
        m3u8_path = os.path.join(out_dir, "live.m3u8")
        cmd += [
            # 屏幕捕获帧到达是突发的（切窗口/滚动），默认 10 帧复用队列会溢出，
            # FFmpeg 报 "Error queuing more packets" 后直接退出——表现为投屏中途断流
            "-max_muxing_queue_size", "1024",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "6",
            # temp_file：先写 .tmp 再改名。否则电视/浏览器可能在半截播放列表
            # 上拿到没有 #EXTM3U 结尾的内容，判定流损坏
            "-hls_flags", "delete_segments+independent_segments+temp_file",
            "-hls_segment_filename", os.path.join(out_dir, f"{segment_prefix}%03d.ts"),
            m3u8_path,
        ]

        def _err(msg):
            self._error = msg
            logger.error(f"[ScreenCast] {msg}")
            if on_error:
                try:
                    on_error(msg)
                except Exception:
                    logger.exception("[ScreenCast] HLS error callback failed")

        logger.info(f"[ScreenCast] HLS cmd: {' '.join(cmd)}")
        try:
            proc = self._spawn(cmd)
        except Exception as e:
            return {"ok": False, "error": f"启动 HLS 编码失败: {e}"}

        self._proc = proc
        self._running = True
        self._stop_requested = False
        self._threads.append(threading.Thread(
            target=self._drain_stderr, args=(proc,), daemon=True,
            name="ffmpeg-hls-stderr"))
        self._threads[-1].start()

        # 等待首个播放列表生成（最多 8s）。必须在 m3u8 由本次进程新建之后
        # 才算成功，否则会把上一轮残留的播放列表当成就绪信号。
        deadline = time.time() + 8
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if os.path.isfile(m3u8_path) and os.path.getmtime(m3u8_path) >= started_at:
                ready = True
                break
            time.sleep(0.1)

        if not ready:
            if proc.poll() is not None:
                _err(f"FFmpeg 提前退出（退出码 {proc.returncode}）"
                     + self._stderr_hint())
            else:
                _err("HLS 输出超时未生成（屏幕捕获失败，可能是窗口不存在/权限未授予）")
            self.stop()
            return {"ok": False, "error": self._error}

        self._threads.append(threading.Thread(
            target=self._watch, args=(proc, m3u8_path, _err), daemon=True,
            name="ffmpeg-hls-watch"))
        self._threads[-1].start()

        logger.info(f"[ScreenCast] HLS capture started: {m3u8_path}")
        return {"ok": True, "m3u8": m3u8_path,
                "width": width or None, "height": height or None, "fps": fps}

    # ────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────

    def _spawn(self, cmd: List[str]) -> subprocess.Popen:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=creationflags, bufsize=0,
        )

    def _drain_stderr(self, proc: subprocess.Popen):
        """读取 FFmpeg stderr。不读满 64KB 管道会把 FFmpeg 卡死在写日志上"""
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._stderr_tail.append(line)
                    logger.debug(f"[ScreenCast] ffmpeg: {line}")
        except Exception:
            pass

    def _stderr_hint(self) -> str:
        tail = [l for l in self._stderr_tail if l.strip()]
        if not tail:
            return ""
        # 「Error while opening encoder」是汇总行，真正的原因排在它前面
        # （实测 "width not divisible by 2 (1921x1081)"）。只取最后三行的话，
        # 用户和日志里就只剩一句看不出所以然的编码失败。
        markers = ("not divisible", "Failed to", "Invalid argument",
                   "Permission denied", "No such file", "Cannot open",
                   "Device or resource busy")
        reason = next((l for l in tail if any(m in l for m in markers)), None)
        parts = tail[-3:]
        if reason and reason not in parts:
            parts = [reason] + parts
        return "：" + " / ".join(parts)

    def _watch(self, proc: subprocess.Popen, m3u8_path: str, _err: Callable):
        """看门狗：FFmpeg 中途退出（窗口被关闭、设备被拔出）必须上报，
        否则状态永远停在 running，用户既看不到画面也起不了下一次投屏"""
        last_mtime = 0.0
        stalled_since: Optional[float] = None
        while not self._stop_requested:
            time.sleep(1.0)
            if self._stop_requested:
                return
            rc = proc.poll()
            if rc is not None:
                _err(f"屏幕捕获进程已退出（退出码 {rc}）" + self._stderr_hint())
                return
            try:
                mtime = os.path.getmtime(m3u8_path)
            except OSError:
                continue
            if mtime != last_mtime:
                last_mtime = mtime
                stalled_since = None
            elif stalled_since is None:
                stalled_since = time.time()
            elif time.time() - stalled_since > 15:
                _err("屏幕捕获已停止输出（画面卡住）")
                self.stop()
                return

    def _video_input_args(self, source: Dict, fps: int, vf: str) -> List[str]:
        from .ffmpeg_manager import FfmpegManager
        return FfmpegManager.build_video_input(source, fps, vf, self.ffmpeg)

    def _audio_input_args(self, audio_cfg: Dict, input_offset: int = 0) -> List[str]:
        from .ffmpeg_manager import FfmpegManager
        return FfmpegManager.build_audio_input(audio_cfg, input_offset)

    def stop(self):
        """停止捕获进程（幂等）"""
        with self._lock:
            self._stop_requested = True
            proc = self._proc
            self._proc = None
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            if proc is not None:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def error(self) -> Optional[str]:
        return self._error
