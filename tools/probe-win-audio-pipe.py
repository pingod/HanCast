"""Windows 系统声音链路真机探针（在 .68 上跑，用仓库里的真实代码）

覆盖 sidecar 里最容易在打包后才崩的那一段：
  loopback.open_stream → build_audio_input 的 `pipe:0` 输入与 amix 图
  → ffmpeg 的 stdin 管道 → HLS 里真的带上了一条可解码的音频流。

不碰 gdigrab（ssh 会话在 session 0，抓不到桌面，与音频无关），视频输入用
lavfi testsrc 顶替，其余命令行与 capture.start_hls 保持一致。

用法（.68）：
    set "PYTHONIOENCODING=utf-8"& set "HANCAST_SRC=D:\\Temp\\pkg"&
        D:\\Temp\\pa-probe\\Scripts\\python.exe D:\\Temp\\probe-win-audio-pipe.py
判据：播放 440Hz 时 HLS 解出来的该频点分量必须显著高于不播放时的基线。
"""
from __future__ import annotations

import array
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("HANCAST_SRC") or os.path.join(HERE, os.pardir, "hancast-backend")
sys.path.insert(0, SRC)

from hancast_sidecar.screencast.ffmpeg_manager import FfmpegManager, _run_utf8  # noqa: E402
from hancast_sidecar.screencast.loopback import loopback_devices, open_stream  # noqa: E402

RATE = 48000
TONE_HZ = 440.0
FAIL: list[str] = []


def default_ffmpeg() -> str:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg:
        return arg
    local = os.path.expandvars(r"%LOCALAPPDATA%\HanCast\ffmpeg\ffmpeg.exe")
    if os.path.isfile(local):
        return local
    raise SystemExit("找不到 ffmpeg，参数里给出路径")


def make_tone(path: str, seconds: int = 12):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        frames = array.array(
            "h", (int(12000 * math.sin(2 * math.pi * 440 * i / 44100))
                  for i in range(44100 * seconds)))
        w.writeframes(frames.tobytes())


def goertzel_level(pcm: bytes, freq: float) -> tuple[float, float]:
    """返回 (freq 分量幅值, 整体 RMS)，都是满刻度比例"""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0, 0.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32767.0
    k = int(0.5 + len(samples) * freq / RATE)
    w = 2 * math.pi * k / len(samples)
    coeff = 2 * math.cos(w)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s1, s2 = s0, s1
    return math.sqrt(s1 * s1 + s2 * s2 - coeff * s1 * s2) / len(samples) / 32767.0, rms


def decode_hls(ffmpeg: str, m3u8: str) -> bytes:
    """把 HLS 解成单声道 s16le 原始 PCM（走 stdout，不落中间文件）"""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", m3u8,
         "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-"],
        capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"解码 HLS 失败: {proc.stderr.decode('utf-8', 'replace')[:300]}")
    return proc.stdout


def run_case(ffmpeg: str, title: str, args: list, lb_name: str | None,
             tone: str | None, seconds: float = 5.0) -> tuple[float, float]:
    """起一条与生产一致的 FFmpeg 管线，返回 HLS 里读到的 (440Hz 幅值, RMS)"""
    out_dir = tempfile.mkdtemp(prefix="hancast-probe-")
    m3u8 = os.path.join(out_dir, "live.m3u8")
    if not args:
        FAIL.append(f"{title}: 音频参数为空")
        shutil.rmtree(out_dir, ignore_errors=True)
        return 0.0, 0.0

    lb = open_stream(lb_name) if lb_name else None
    if lb_name and lb is None:
        FAIL.append(f"{title}: 打不开 loopback 设备 {lb_name}")
        return 0.0, 0.0

    cmd = ([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15"]
           + args
           + ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
              "-g", "15", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
              "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
              "-hls_segment_filename", os.path.join(out_dir, "seg%03d.ts"),
              m3u8])
    print(f"  cmd: {' '.join(cmd)[:420]}")
    proc = subprocess.Popen(cmd,
                            stdin=subprocess.PIPE if lb else subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            bufsize=0)
    # stderr 必须边跑边读：只在结束时读一次的话，ffmpeg 写满管道缓冲区（几 KB）
    # 就会卡在 write 上，表现是「进程活着、一个分片都没产出」，而错误文本一条
    # 也拿不到 —— 排查时最容易把它误读成「管线建不起来」。
    errs: list = []

    def drain():
        try:
            errs.append(proc.stderr.read().decode("utf-8", "replace"))
        except Exception:
            pass

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()
    stop = threading.Event()
    fed = [0]

    def feed():
        while not stop.is_set() and proc.poll() is None:
            try:
                data = lb.read()
                proc.stdin.write(data)
                fed[0] += len(data)
            except Exception:
                break

    feeder = threading.Thread(target=feed, daemon=True) if lb else None
    if feeder:
        feeder.start()
    if tone:
        import winsound
        winsound.PlaySound(tone, winsound.SND_FILENAME | winsound.SND_ASYNC)
    time.sleep(seconds)
    stop.set()
    if tone:
        import winsound
        winsound.PlaySound(None, winsound.SND_PURGE)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    if feeder:
        feeder.join(timeout=2)
    if lb:
        lb.close()
    drainer.join(timeout=3)
    err = "".join(errs).strip()
    if not os.path.isfile(m3u8):
        FAIL.append(f"{title}: 没有生成播放列表（退出码 {proc.returncode}）{err[:200]}")
        shutil.rmtree(out_dir, ignore_errors=True)
        return 0.0, 0.0
    try:
        tone_level, rms = goertzel_level(decode_hls(ffmpeg, m3u8), TONE_HZ)
    except Exception as e:
        FAIL.append(f"{title}: {e}")
        tone_level, rms = 0.0, 0.0
    print(f"  {title}: 喂入 {fed[0]} 字节，HLS 解出 440Hz={tone_level:.4f} RMS={rms:.4f}"
          f"{'，stderr: ' + err[:200] if err else ''}")
    shutil.rmtree(out_dir, ignore_errors=True)
    return tone_level, rms


def main() -> int:
    ffmpeg = default_ffmpeg()
    print(f"ffmpeg: {ffmpeg}")
    devices = loopback_devices()
    print(f"loopback 设备 {len(devices)} 个（首个应为默认渲染设备）: "
          + ", ".join(f"{d['name']}[{d['rate']}Hz/{d['channels']}ch]"
                      for d in devices[:4]))
    if not devices:
        return 2
    target = devices[0]

    tone_wav = os.path.join(tempfile.gettempdir(), "hancast-probe-tone.wav")
    make_tone(tone_wav)

    # 用生产枚举（_list_audio_devices）挑一个真麦克风，排除立体声混音/环回：
    # 探针自己再抄一份解析的话，生产解析器坏掉时探针照样「PASS」——
    # .68 实测就是这样漏掉了「现在的 dshow 不打分段头，Windows 一个麦克风都列不出」。
    mics = [d for d in FfmpegManager()._list_audio_devices()
            if d.get("kind") == "mic" and not d.get("loopback")]
    mic = mics[0]["name"] if mics else None
    print("dshow 麦克风 %d 个: %s" % (
        len(mics), ", ".join(repr(d["name"]) for d in mics[:4])))
    if not mics:
        FAIL.append("选择器列不出任何 dshow 麦克风（应按设备行的 (audio) 后缀认）")

    lb_cfg = {"system": True, "system_device": target["name"],
              "_loopback": {"name": target["name"], "rate": target["rate"],
                            "channels": target["channels"]}}
    # 音频输入从 1 起（视频占 0），与 capture.start_hls 一致
    sys_args = FfmpegManager.build_audio_input(lb_cfg, input_offset=1)
    print(f"build_audio_input(系统声音): {' '.join(sys_args)}")

    # 1) 不放音时的基线（机器上也许有别的声音在放，用它做对照）
    base_tone, _ = run_case(ffmpeg, "系统声音·静置", sys_args, target["name"], None)
    # 2) 只开系统声音
    sys_tone, sys_rms = run_case(ffmpeg, "系统声音·播放 440Hz",
                                 sys_args, target["name"], tone_wav)
    if sys_rms <= 0.001:
        FAIL.append("系统声音：HLS 里没有音频（RMS≈0）")
    if sys_tone < max(0.02, base_tone * 3):
        FAIL.append(f"系统声音：没听到测试音（{sys_tone:.4f} vs 基线 {base_tone:.4f}）")

    # 3) 两路混音：amix 图最容易建不起来（输入序号引错→指向没有音频流的视频输入，
    #    FFmpeg 直接退出），必须真跑一次。
    if mic:
        mix_args = FfmpegManager.build_audio_input(
            dict(lb_cfg, mic=True, mic_device=mic), input_offset=1)
        mix_tone, mix_rms = run_case(ffmpeg, "麦克风+系统声音",
                                     mix_args, target["name"], tone_wav)
    else:
        # 这台机（.68）没有 dshow 麦克风，用一路 1000Hz 正弦顶替它占住输入 1，
        # 环回被挤到输入 2——正好检验标签是不是按真实序号生成的
        mix_args = (["-f", "lavfi", "-i", "aevalsrc=0.2*sin(2*PI*1000*t):s=44100"]
                    + FfmpegManager.build_audio_input(lb_cfg, input_offset=2)
                    + FfmpegManager._mix_filter(["[1:a]", "[2:a]"]))
        mix_tone, mix_rms = run_case(ffmpeg, "顶替麦克风+系统声音",
                                     mix_args, target["name"], tone_wav)
    if mix_rms <= 0.001:
        FAIL.append("混音：HLS 里没有音频，amix 图可能没建起来")
    if mix_tone < max(0.02, base_tone * 3):
        FAIL.append(f"混音：听不到测试音（{mix_tone:.4f}）")

    try:
        os.remove(tone_wav)
    except OSError:
        pass
    print("SUMMARY:", "FAIL: " + "; ".join(FAIL) if FAIL else "PASS")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
