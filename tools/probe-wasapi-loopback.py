"""WASAPI loopback 真机探针（Windows）。

验证三件事，任何一件不成立都会推翻「用 pyaudiowpatch 采系统声音」的设计：
  1. 默认渲染设备的 loopback 能不能开到 int16/48k/立体声；
  2. 系统里真的在放声音时，loopback 缓冲里能不能读到非零采样；
  3. **静音期** PortAudio 是否照样按实时节奏交付（交付的是零块）还是会阻塞。
     第三点决定 ffmpeg 侧要不要在 Python 里按单调时钟补零——loopback 里
     WASAPI 在无数据时 NextPacketSize 为 0，若 PortAudio 直接阻塞，
     喂给 ffmpeg 的 PCM 就会比墙上时间慢，投出去的音画必然越漂越远。

用法（在 .68 上，需要 pyaudiowpatch）：
    python tools/probe-wasapi-loopback.py
"""
from __future__ import annotations

import array
import math
import os
import sys
import tempfile
import time
import wave

import pyaudiowpatch as pyaudio

RATE = 48000
CHANNELS = 2
CHUNK = 1024
FAIL: list[str] = []


def stats(chunks: list[bytes]) -> tuple[int, int, float]:
    """返回 (总字节, 最大绝对采样值, RMS 占满刻度比例)

    只看峰值会被削顶的垃圾骗过去：静音阶段读到 |s|=32768 就说明 int16 转换
    或数据本身有问题，而 RMS 才能区分「安静」和「在放音」。
    """
    data = b"".join(chunks)
    if not data:
        return 0, 0, 0.0
    samples = array.array("h")
    samples.frombytes(data[: len(data) - len(data) % 2])
    n = len(samples)
    if not n:
        return len(data), 0, 0.0
    peak = max(abs(s) for s in samples)
    rms = math.sqrt(sum(s * s for s in samples) / n) / 32767.0
    return len(data), peak, rms


def goertzel(samples, freq: float, rate: int) -> float:
    """单频点幅值（相对满刻度），用来确认 440Hz 的测试音真的被采到了"""
    k = int(0.5 + len(samples) * freq / rate)
    w = 2 * math.pi * k / len(samples)
    coeff = 2 * math.cos(w)
    s0 = s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    return math.sqrt(s1 * s1 + s2 * s2 - coeff * s1 * s2) / len(samples) / 32767.0


def capture(pa, device, seconds: float, play_wav: str | None = None):
    import winsound

    stream = pa.open(
        format=pyaudio.paInt16,
        channels=CHANNELS,
        rate=RATE,
        input=True,
        input_device_index=device["index"],
        frames_per_buffer=CHUNK,
    )
    if play_wav:
        # SND_ASYNC|SND_NOSTOP：后台放，且不抢占别的播放
        winsound.PlaySound(play_wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
    deadline = time.monotonic() + seconds
    chunks: list[bytes] = []
    t_start = time.monotonic()
    gaps = 0.0  # 单次 read 之间最长的空档
    last = t_start
    while time.monotonic() < deadline:
        raw = stream.read(CHUNK, exception_on_overflow=False)
        now = time.monotonic()
        gaps = max(gaps, now - last)
        last = now
        chunks.append(raw)
    elapsed = time.monotonic() - t_start
    if play_wav:
        winsound.PlaySound(None, winsound.SND_PURGE)
    stream.stop_stream()
    stream.close()
    n_bytes, peak, rms = stats(chunks)
    data = b"".join(chunks)
    samples = array.array("h")
    samples.frombytes(data[: len(data) - len(data) % 2])
    tone = goertzel(samples[::CHANNELS], 440.0, RATE)
    return n_bytes, peak, rms, elapsed, gaps, tone


def main() -> int:
    if sys.platform != "win32":
        print("本探针只在 Windows 上有效")
        return 2
    wav_path = os.path.join(tempfile.gettempdir(), "hancast-probe-tone.wav")
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        frames = array.array(
            "h", (int(12000 * math.sin(2 * math.pi * 440 * i / 44100))
                  for i in range(44100 * 6))
        )
        w.writeframes(frames.tobytes())

    pa = pyaudio.PyAudio()
    try:
        loopback = pa.get_default_wasapi_loopback()
        print(f"loopback device: index={loopback['index']} name={loopback['name']!r} "
              f"rate={loopback['defaultSampleRate']} ch={loopback['maxInputChannels']}")
        if loopback["maxInputChannels"] < 2:
            FAIL.append("loopback 设备声道数 < 2")

        nb, peak, rms, el, gap, tone = capture(pa, loopback, 2.0)
        print(f"静音 2s：读到 {nb} 字节 / 墙钟 {el:.2f}s，RMS={rms:.4f} 峰值={peak} "
              f"最长空档={gap*1000:.0f}ms 440Hz={tone:.4f}")
        expected = int(2.0 * RATE * CHANNELS * 2)
        if nb < expected * 0.9:
            FAIL.append(
                f"静音期 stream.read 阻塞：2s 只读到 {nb} 字节（实时应有 ~{expected}），"
                "必须自己按单调时钟补零，否则 ffmpeg 输入会持续落后")

        nb2, peak2, rms2, el2, gap2, tone2 = capture(pa, loopback, 4.0, play_wav=wav_path)
        print(f"播放 440Hz 4s：读到 {nb2} 字节 / 墙钟 {el2:.2f}s，RMS={rms2:.4f} "
              f"峰值={peak2} 最长空档={gap2*1000:.0f}ms 440Hz={tone2:.4f}")
        # 测试音幅 12000/满刻度 → Goertzel 约一半；机器上也许还有别的声音在放，
        # 所以判据用「440Hz 分量比静音阶段高一个量级」，不用绝对 RMS
        if tone2 < 0.02 or tone2 < tone * 3:
            FAIL.append(
                f"没在 loopback 里听到 440Hz 测试音：静音阶段 {tone:.4f} → 播放阶段 {tone2:.4f}")
        if peak2 >= 32760 and rms2 < 0.05:
            FAIL.append(f"RMS={rms2:.4f} 却出现满刻度峰值 {peak2}：int16 转换有垃圾")
    finally:
        pa.terminate()
        try:
            os.remove(wav_path)
        except OSError:
            pass

    print("SUMMARY:", "FAIL: " + "; ".join(FAIL) if FAIL else "PASS")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
