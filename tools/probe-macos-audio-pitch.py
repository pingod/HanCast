"""macOS 系统声音抓取的「音高是否跑掉」对照实验

背景：CoreAudio Audio Tap 抓到的 440Hz 参考音，长窗 FFT 峰值稳定落在 436.3Hz
（−0.84%，全程不漂移，所以不是时钟抖动）。要判断这是新环回路径的缺陷，还是
这套机器上「任何进程内环回」都有的表现，唯一的办法是拿已在售的 BlackHole 走
同一条测量链（同一个 afplay、同一个 ffmpeg avfoundation、同一个分析函数）。

用法：
    hancast-backend/.venv/bin/python tools/probe-macos-audio-pitch.py [ffmpeg 路径]

BlackHole 那一路会临时把系统默认输出改成 BlackHole 2ch，结束（含异常）一定还原
并复核，改动只在这台机器的音频路由上，不写任何文件。
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "hancast-backend"))

from hancast_sidecar.screencast import macos_tap as T  # noqa: E402

RATE = 48000
TONE_HZ = 440.0
BLACKHOLE_UID = "BlackHole2ch_UID"
SYSTEM = 1  # kAudioObjectSystemObject（不是 0，0 是 kAudioObjectUnknown）


def ffmpeg_path() -> str:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        return args[0]
    p = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True).stdout.strip()
    if p:
        return p
    raise SystemExit("找不到 ffmpeg")


def audio_devices(ffmpeg: str) -> list:
    r = subprocess.run([ffmpeg, "-hide_banner", "-f", "avfoundation",
                        "-list_devices", "true", "-i", ""],
                       capture_output=True, text=True, errors="replace")
    out, section, found = (r.stderr or "") + (r.stdout or ""), None, []
    for line in out.splitlines():
        s = line.strip()
        if "AVFoundation video devices" in s:
            section = "video"
        elif "AVFoundation audio devices" in s:
            section = "audio"
        elif section == "audio":
            m = re.search(r"\[(\d+)\]\s*(.*)", s)
            if m and m.group(2).strip():
                found.append((m.group(1), m.group(2).strip()))
    return found


def scalar(obj: int, sel: str, value=None, scope: str = "glob", elem: int = 0):
    """读（value=None）或写 CoreAudio 标量属性，返回 rc"""
    addr = T._Addr(T._fourcc(sel), T._fourcc(scope), elem)
    v = ctypes.c_uint32(value or 0)
    size = ctypes.c_uint32(4)
    if value is None:
        rc = T._CA.AudioObjectGetPropertyData(ctypes.c_uint32(obj), ctypes.byref(addr),
                                              0, None, ctypes.byref(size), ctypes.byref(v))
        return rc, v.value
    rc = T._CA.AudioObjectSetPropertyData(ctypes.c_uint32(obj), ctypes.byref(addr),
                                          0, None, size, ctypes.byref(v))
    return rc, None


def device_by_uid(uid: str) -> int:
    for d in T._devices():
        if T._device_uid(d) == uid:
            return d
    return 0


def make_tone(path: str, ffmpeg: str, seconds: int = 8):
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"sine=frequency={TONE_HZ}:sample_rate={RATE}"
                    f":duration={seconds}", "-c:a", "pcm_s16le", path], check=True)


def measure(ffmpeg: str, idx: str, tone: str, seconds: float = 5.0) -> str:
    """录一段正在播放 440Hz 的音频，报音高与电平（分析口径与探针一致）"""
    import numpy as np
    player = subprocess.Popen(["afplay", tone])
    time.sleep(0.4)
    out = f"/tmp/pitch-{idx}-{int(time.time() * 1000)}.wav"   # 录完读进内存即删，显式 /tmp
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "avfoundation", "-i", f":{idx}", "-t", f"{seconds}",
                        "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le", out],
                       capture_output=True, text=True)
    player.terminate()
    if r.returncode != 0 or not os.path.isfile(out):
        return f"录制失败: {(r.stderr or '').strip()[-120:]}"
    with wave.open(out, "rb") as w:
        s = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float) / 32768
    os.remove(out)
    if s.size < 4096:
        return f"样本太少（{s.size / RATE:.2f}s）"
    spec = np.abs(np.fft.rfft(s * np.hanning(s.size)))
    freqs = np.fft.rfftfreq(s.size, 1.0 / RATE)
    grid = RATE / s.size

    def refine(k: int) -> float:
        y0, y1, y2 = spec[k - 1], spec[k], spec[k + 1]
        d = 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2) if y1 > 0 else 0.0
        return (k + d) * grid

    lo, hi = int(100 / grid), int(1200 / grid)
    order = np.argsort(spec[lo:hi])[::-1]
    peaks, used = [], []
    for j in order:
        k = lo + int(j)
        if any(abs(k - u) * grid < 12 for u in used):
            continue
        used.append(k)
        peaks.append((refine(k), spec[k] / spec.max()))
        if len(peaks) == 4:
            break
    near = min(peaks, key=lambda p: abs(p[0] - TONE_HZ))
    text = " ".join(f"{f:.1f}Hz({a:.2f})" for f, a in peaks)
    return (f"{s.size / RATE:.2f}s rms {float(np.sqrt(np.mean(s ** 2))):.4f} "
            f"峰值 {text}；最接近 440 的分量 {near[0]:.2f}Hz "
            f"(偏差 {100 * (near[0] / TONE_HZ - 1):+.2f}%)")


def run_blackhole(ffmpeg: str, tone: str):
    rc, default_out = scalar(SYSTEM, "dOut")
    bh = device_by_uid(BLACKHOLE_UID)
    if not bh:
        print(f"BlackHole 对照: 没找到 {BLACKHOLE_UID}，跳过")
        return
    print(f"BlackHole 对照: 默认输出 {default_out} → {bh}（结束必还原）")
    try:
        rc, _ = scalar(SYSTEM, "dOut", bh)
        if rc != 0:
            print(f"  改默认输出失败 {rc:#x}")
            return
        time.sleep(0.5)
        idx = next((i for i, n in audio_devices(ffmpeg) if "BlackHole" in n), None)
        if not idx:
            print("  avfoundation 里没有 BlackHole")
            return
        print(f"  BlackHole[{idx}]: {measure(ffmpeg, idx, tone)}")
    finally:
        scalar(SYSTEM, "dOut", default_out)
        time.sleep(0.4)
        _, back = scalar(SYSTEM, "dOut")
        print(f"  还原默认输出: {back}（期望 {default_out}）"
              f"{'OK' if back == default_out else '!!! 未还原，手工检查 !!!'}")


def run_tap(ffmpeg: str, tone: str):
    if not T.ensure_device():
        print("环回对照: ensure_device() 失败")
        return
    try:
        idx = next((i for i, n in audio_devices(ffmpeg)
                    if T.is_tap_device(n)), None)
        if not idx:
            print("环回对照: avfoundation 里没有 HanCast 系统声音")
            return
        print(f"  环回[{idx}]: {measure(ffmpeg, idx, tone)}")
    finally:
        T.destroy_device()


def main() -> int:
    ffmpeg = ffmpeg_path()
    T._objc()  # 装载 CoreAudio / objc 桥
    tone = "/tmp/hancast-pitch-440.wav"   # 用完 os.remove，即抛件
    make_tone(tone, ffmpeg)
    print(f"源文件 440Hz@{RATE} 的实测：", end=" ")
    import numpy as np
    with wave.open(tone, "rb") as w:
        s = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)
    spec = np.abs(np.fft.rfft(s * np.hanning(s.size)))
    freqs = np.fft.rfftfreq(s.size, 1.0 / RATE)
    print(f"{freqs[np.argmax(spec)] if s.size else 0:.2f}Hz")
    run_blackhole(ffmpeg, tone)
    run_tap(ffmpeg, tone)
    os.remove(tone)
    return 0


if __name__ == "__main__":
    sys.exit(main())
