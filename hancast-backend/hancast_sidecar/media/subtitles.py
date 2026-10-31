"""
外挂字幕 remux 内封

电视（DLNA 渲染端）基本都拿不到「外挂 srt」——它们只会拉一个视频地址，
不会再去拉第二个字幕文件。所以要给电视看字幕，只能把字幕**封进容器**里。

做法：ffmpeg -i 视频 -i 字幕 -c copy -c:s（mov_text / srt）→ 新文件，
然后让同一台电视重新拉这个新文件。视频流不重编码（-c copy），只花 remux 的时间。
"""

import os
import shutil
import hashlib
import logging
import subprocess
from typing import Dict, Optional, Tuple

from ..utils import config
from ..utils import ffmpeg_deps

logger = logging.getLogger("hancast.subtitles")

SUPPORTED_SUBS = (".srt", ".ass", ".ssa", ".vtt")

# 能被 MP4 直接吃下的视频容器；其余一律走 MKV（MKV 对 srt/ass 最宽容）
_MP4_CONTAINERS = (".mp4", ".m4v", ".mov")


def _cache_dir() -> str:
    d = os.path.join(config.SETTING_DIR, "subtitle_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _ffmpeg() -> str:
    path, _src = ffmpeg_deps.find_binary("ffmpeg")
    if not path:
        raise RuntimeError("找不到 FFmpeg，无法内封字幕（请先在「运行依赖」里安装）")
    return path


def pick_output_path(video_path: str, subtitle_path: str) -> str:
    """输出路径：按视频容器决定 mp4 / mkv；路径带内容哈希，同一对文件复用产物"""
    h = hashlib.sha1()
    for p in (video_path, subtitle_path):
        try:
            st = os.stat(p)
            h.update(f"{p}:{st.st_size}:{int(st.st_mtime)}".encode("utf-8", "replace"))
        except OSError:
            h.update(p.encode("utf-8", "replace"))
    ext = os.path.splitext(video_path)[1].lower()
    out_ext = ".mp4" if ext in _MP4_CONTAINERS else ".mkv"
    return os.path.join(_cache_dir(), f"sub-{h.hexdigest()[:16]}{out_ext}")


def build_command(ffmpeg: str, video_path: str, subtitle_path: str, out_path: str) -> list:
    ext = os.path.splitext(out_path)[1].lower()
    sub_codec = "mov_text" if ext == ".mp4" else "srt"
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-y",
        "-i", video_path,
        "-i", subtitle_path,
        "-map", "0:v:0?", "-map", "0:a?", "-map", "1:0",
        "-c", "copy", "-c:s", sub_codec,
        "-metadata:s:s:0", "language=chi",
        "-disposition:s:0", "default",
        out_path,
    ]


def remux(
    video_path: str,
    subtitle_path: str,
    duration_seconds: float = 0.0,
    on_progress=None,
) -> str:
    """把字幕后缀封进视频，返回产物路径。失败抛异常（已清理半成品）。"""
    if not os.path.isfile(video_path):
        raise RuntimeError(f"源视频不存在: {video_path}")
    if not os.path.isfile(subtitle_path):
        raise RuntimeError(f"字幕文件不存在: {subtitle_path}")
    sub_ext = os.path.splitext(subtitle_path)[1].lower()
    if sub_ext not in SUPPORTED_SUBS:
        raise RuntimeError(f"不支持的字幕格式 {sub_ext}（支持 srt/ass/ssa/vtt）")

    out_path = pick_output_path(video_path, subtitle_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        # 同一对「视频+字幕」已经封过，直接复用
        return out_path

    cmd = build_command(_ffmpeg(), video_path, subtitle_path, out_path) + [
        "-progress", "pipe:1", "-nostats",
    ]
    # 临时文件必须保留真实扩展名：ffmpeg 靠扩展名选封装器，`.part` 会直接被拒
    stem, ext = os.path.splitext(out_path)
    tmp_path = f"{stem}.part{ext}"
    cmd = [c if c != out_path else tmp_path for c in cmd]
    logger.info(f"[Subtitles] 开始内封: {os.path.basename(video_path)} + "
                f"{os.path.basename(subtitle_path)} -> {os.path.basename(out_path)}")

    err_tail = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key in ("out_time_us", "out_time_ms") and on_progress and duration_seconds > 0:
                try:
                    us = int(val)
                    secs = us / 1_000_000.0
                    pct = max(0.0, min(99.0, secs / duration_seconds * 100.0))
                    on_progress(pct)
                except ValueError:
                    pass
        stderr = proc.stderr.read() if proc.stderr else ""
        rc = proc.wait()
        if rc != 0:
            err_tail = [l for l in stderr.strip().splitlines() if l.strip()][-6:]
            raise RuntimeError("FFmpeg 内封失败: " + " | ".join(err_tail[-3:]))
        if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise RuntimeError("FFmpeg 已退出但没有产出文件")
        os.replace(tmp_path, out_path)
        if on_progress:
            on_progress(100.0)
        logger.info(f"[Subtitles] 内封完成: {out_path} ({os.path.getsize(out_path)} bytes)")
        return out_path
    except Exception:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def probe_has_subtitle_stream(path: str) -> Optional[int]:
    """用 ffprobe 数一下字幕流（可选校验，ffprobe 缺失就返回 None）"""
    ffprobe, _ = ffmpeg_deps.find_binary("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "s",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=20,
        )
        return len([l for l in out.stdout.splitlines() if l.strip()])
    except Exception:
        return None


def cleanup_cache(keep_paths: Optional[Dict[str, str]] = None) -> None:
    """删掉缓存目录里不再被引用的产物（会话结束时调用，避免占盘）"""
    keep = set((keep_paths or {}).values())
    d = os.path.join(config.SETTING_DIR, "subtitle_cache")
    if not os.path.isdir(d):
        return
    for name in os.listdir(d):
        full = os.path.join(d, name)
        if full in keep:
            continue
        try:
            if os.path.isfile(full):
                os.remove(full)
        except OSError:
            pass
