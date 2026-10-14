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
from typing import Callable, Dict, List, Optional, Tuple

from ..params import get as param

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
        # Windows 系统声音：sidecar 自己采 WASAPI 环回，PCM 写进 ffmpeg 的 stdin
        self._loopback = None
        self._feeder: Optional[threading.Thread] = None

    # ────────────────────────────────────────
    # HLS 输出
    # ────────────────────────────────────────

    def start_hls(self, source: Dict, audio_cfg: Dict, video_cfg: Dict,
                  out_dir: str, segment_prefix: str = "seg",
                  on_error: Optional[Callable] = None,
                  target: str = "dlna") -> Dict:
        """启动 HLS 编码输出

        out_dir 必须是本次会话独占目录（旧内容整体删除）：复用同一目录会让
        上一轮的 live.m3u8 残留，电视拉到的是已死的分片。

        target 决定分片时长（浏览器通道可以比电视更激进，见 params 登记表）。
        """
        self.stop()

        width = int(video_cfg.get("width") or 0)
        height = int(video_cfg.get("height") or 0)
        fps = int(video_cfg.get("fps") or 15)
        bitrate = video_cfg.get("bitrate") or "2M"

        segment_seconds = int(param(f"screencast.{target}_segment_seconds"))
        playlist_size = int(param("screencast.playlist_size"))
        muxing_queue = int(param("screencast.muxing_queue_size"))
        preset = param("screencast.x264_preset")

        from .ffmpeg_manager import FfmpegManager
        vf = FfmpegManager.build_scale_filter(width, height)

        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir, exist_ok=True)

        started_at = time.time()
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        video_args, vf = self._video_input_args(source, fps, vf)
        cmd += video_args
        # 视频占输入 0，音频输入从 1 起（amix 标签需此偏移）
        audio_input = self._audio_input_args(audio_cfg, input_offset=1)
        if audio_input:
            cmd += audio_input
        # 视频滤镜必须排在所有 -i 之后：ffmpeg 把 -i 之前的选项判给「下一个输入」，
        # 夹在中间会落到麦克风上（EINVAL，选了音频就投不起来）
        if vf:
            cmd += ["-vf", vf]
        cmd += [
            "-c:v", "libx264", "-preset", str(preset), "-tune", "zerolatency",
            # 电视解码器普遍只认 Main profile（High 的 8x8 变换/CABAC 变长码
            # 在老款渲染器上直接拒绝播放，表现为"换个分辨率就黑屏"）
            "-profile:v", "main", "-level", "42",
            "-b:v", str(bitrate), "-pix_fmt", "yuv420p",
            # GOP 必须等于「分片时长 × 帧率」：HLS 只能在关键帧处切片，
            # 分片改成 1 秒而 GOP 还是 2 秒的话，切片点照样两秒一个，
            # 降延迟完全落空
            "-g", str(fps * segment_seconds),
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
            "-max_muxing_queue_size", str(muxing_queue),
            "-f", "hls",
            "-hls_time", str(segment_seconds),
            "-hls_list_size", str(playlist_size),
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

        # WASAPI 环回必须在 FFmpeg 之前打开：设备被别的程序独占、或者用户在
        # 选完之后把输出切走了，都要在这里就说清楚，而不是留下一个没声音的会话
        lb_cfg = audio_cfg.get("_loopback") if audio_input else None
        if lb_cfg:
            from .loopback import open_stream
            self._loopback = open_stream(lb_cfg.get("name"))
            if self._loopback is None:
                return {"ok": False,
                        "error": "系统声音采不起来：输出设备可能已被切换或独占，"
                                 "请重新选择系统声音设备"}

        try:
            proc = self._spawn(cmd, use_stdin=self._loopback is not None)
        except Exception as e:
            self._close_loopback()
            return {"ok": False, "error": f"启动 HLS 编码失败: {e}"}

        self._proc = proc
        self._running = True
        self._stop_requested = False
        self._threads.append(threading.Thread(
            target=self._drain_stderr, args=(proc,), daemon=True,
            name="ffmpeg-hls-stderr"))
        self._threads[-1].start()
        if self._loopback is not None:
            self._feeder = threading.Thread(
                target=self._feed_loopback, args=(proc,), daemon=True,
                name="ffmpeg-hls-loopback")
            self._feeder.start()

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
                # ffmpeg 还活着却没落播放列表，几乎都是某一路输入把管线饿住
                # （amix 要凑齐每路帧），stderr 里那句 buffer/queue 警告就是线索
                _err("HLS 输出超时未生成（屏幕捕获失败，可能是窗口不存在/"
                     "权限未授予）" + self._stderr_hint())
            self.stop()
            return {"ok": False, "error": self._error}

        self._threads.append(threading.Thread(
            target=self._watch, args=(proc, m3u8_path, _err), daemon=True,
            name="ffmpeg-hls-watch"))
        self._threads[-1].start()

        logger.info(f"[ScreenCast] HLS capture started: {m3u8_path}")
        return {"ok": True, "m3u8": m3u8_path,
                "width": width or None, "height": height or None, "fps": fps,
                "segment_seconds": segment_seconds}

    # ────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────

    def _spawn(self, cmd: List[str], use_stdin: bool = False) -> subprocess.Popen:
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # stdin 默认给 DEVNULL：不显式指定的话 FFmpeg 继承 sidecar 的 stdin，
        # 而 sidecar 的 stdin 正是与 Tauri 之间的 JSON 行协议管道 —— 一旦被
        # 子进程读走，主命令循环就会凭空丢指令。
        return subprocess.Popen(
            cmd, stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=creationflags, bufsize=0,
        )

    def _feed_loopback(self, proc: subprocess.Popen):
        """把 WASAPI 环回 PCM 持续写进 FFmpeg 的 stdin。

        LoopbackStream 自己按单调时钟补静音（见 loopback.py），所以这里拿到手
        的一直是实时节奏的字节流；返回 b"" 只是「这会儿不欠数据」，None 才是
        流收尾。写失败只意味着 FFmpeg 已经退了。

        流的关闭仍归采集线程：本线程只把「该收尾了」告诉它。
        """
        lb = self._loopback
        if lb is None:
            return
        try:
            while not self._stop_requested:
                data = lb.read()
                if data is None:
                    break          # 采集线程已收尾，正常结束
                if data:
                    proc.stdin.write(data)
        except (BrokenPipeError, ValueError, OSError):
            logger.info("[ScreenCast] loopback feeder: FFmpeg stdin 已关闭")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[ScreenCast] loopback feeder: {e}")
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            lb.close()
            if self._loopback is lb:
                self._loopback = None

    def _close_loopback(self):
        lb, self._loopback = self._loopback, None
        if lb is not None:
            lb.close()

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

    def _video_input_args(self, source: Dict, fps: int,
                          vf: str) -> Tuple[List[str], str]:
        """返回 (视频输入参数, 视频滤镜串)，滤镜串由调用方放到所有 -i 之后"""
        from .ffmpeg_manager import FfmpegManager
        return FfmpegManager.build_video_input(source, fps, vf, self.ffmpeg)

    def _audio_input_args(self, audio_cfg: Dict, input_offset: int = 0) -> List[str]:
        from .ffmpeg_manager import FfmpegManager
        return FfmpegManager.build_audio_input(audio_cfg, input_offset)

    def stop(self):
        """停止捕获进程（幂等）"""
        with self._lock:
            self._stop_requested = True
            feeder = self._feeder
            self._feeder = None
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
            # 先等 feeder 退出：它一停才没人再往 stdin 写。关流本身不在这里发生——
            # LoopbackStream.close() 只发通知，PortAudio 的 C 调用仍由采集线程自己收尾，
            # 别的线程去 close 就是踩已释放的 stream（.68 实测 0xc0000005）
            if feeder is not None:
                feeder.join(timeout=2)
                if feeder.is_alive():
                    logger.info("[ScreenCast] 环回喂料线程仍在写 stdin")
                else:
                    self._close_loopback()
            else:
                self._close_loopback()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def error(self) -> Optional[str]:
        return self._error
