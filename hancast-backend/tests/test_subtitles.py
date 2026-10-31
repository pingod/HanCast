"""外挂字幕内封（v2.4.6）——命令拼装、容器选择、产物复用、真实 remux 冒烟

跑真 ffmpeg 的两条在这个环境没有 ffmpeg 时自动跳过（判据是 ffmpeg_deps.find_binary）。
"""
import os
import shutil
import subprocess

import pytest

from hancast_sidecar.media import subtitles
from hancast_sidecar.utils import ffmpeg_deps


def _ffmpeg():
    path, _ = ffmpeg_deps.find_binary("ffmpeg")
    return path


def test_mp4_用_mov_text_mkv_用_srt():
    cmd_mp4 = subtitles.build_command("ffmpeg", "a.mp4", "a.srt", "out.mp4")
    assert "mov_text" in cmd_mp4
    assert cmd_mp4[cmd_mp4.index("-c:s") + 1] == "mov_text"
    cmd_mkv = subtitles.build_command("ffmpeg", "a.mkv", "a.srt", "out.mkv")
    assert cmd_mkv[cmd_mkv.index("-c:s") + 1] == "srt"
    # 视频/音频一律 -c copy，绝不重编码
    assert cmd_mkv[cmd_mkv.index("-c") + 1] == "copy"


def test_输出容器跟随源视频(tmp_path, monkeypatch):
    monkeypatch.setattr(subtitles, "_cache_dir", lambda: str(tmp_path))
    mp4 = tmp_path / "movie.mp4"
    mp4.write_bytes(b"x")
    sub = tmp_path / "a.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    assert subtitles.pick_output_path(str(mp4), str(sub)).endswith(".mp4")
    mkv = tmp_path / "movie.mkv"
    mkv.write_bytes(b"x")
    assert subtitles.pick_output_path(str(mkv), str(sub)).endswith(".mkv")


def test_不支持的字幕格式直接报错(tmp_path, monkeypatch):
    monkeypatch.setattr(subtitles, "_cache_dir", lambda: str(tmp_path))
    vid = tmp_path / "v.mp4"; vid.write_bytes(b"x")
    sub = tmp_path / "a.txt"; sub.write_text("nope")
    with pytest.raises(RuntimeError, match="不支持的字幕格式"):
        subtitles.remux(str(vid), str(sub))


def test_源文件不存在报错(tmp_path, monkeypatch):
    monkeypatch.setattr(subtitles, "_cache_dir", lambda: str(tmp_path))
    sub = tmp_path / "a.srt"; sub.write_text("1\n")
    with pytest.raises(RuntimeError, match="源视频不存在"):
        subtitles.remux(str(tmp_path / "missing.mp4"), str(sub))


@pytest.mark.skipif(_ffmpeg() is None, reason="本机没有 ffmpeg")
def test_真实remux出字幕流并可复用(tmp_path, monkeypatch):
    monkeypatch.setattr(subtitles, "_cache_dir", lambda: str(tmp_path / "cache"))
    ff = _ffmpeg()
    vid = tmp_path / "v.mp4"
    subprocess.run([
        ff, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(vid),
    ], check=True)
    srt = tmp_path / "a.srt"
    srt.write_text("1\n00:00:00,300 --> 00:00:01,500\nhello\n")
    prog = []
    out = subtitles.remux(str(vid), str(srt), 2.0, lambda p: prog.append(p))
    assert os.path.getsize(out) > 0
    assert prog and prog[-1] == 100.0
    n = subtitles.probe_has_subtitle_stream(out)
    assert n is None or n >= 1
    # 同一对「视频+字幕」第二次直接复用，不再跑 ffmpeg
    assert subtitles.remux(str(vid), str(srt), 2.0) == out
