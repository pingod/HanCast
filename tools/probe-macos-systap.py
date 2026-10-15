"""macOS 系统声音（CoreAudio Audio Tap）真机探针

验证的是 sidecar 里那段 ctypes/CoreAudio 桥 + ffmpeg avfoundation 的组合：
  ensure_device() → 设备出现在 avfoundation 列表 → 抓 3 秒里真的带着 440Hz →
  destroy_device() 之后设备消失。

判据：播放测试音时，440Hz ±2% 带内的能量必须显著高于静置基线（不是「有数据」就算过）。
带内而不是单根谱线，是因为这台机器的播放/采集链有约 −0.8% 的固定音高偏差，
BlackHole（已在售路径）实测同样是 436Hz —— 对照见 tools/probe-macos-audio-pitch.py。

用法：
    hancast-backend/.venv/bin/python tools/probe-macos-systap.py [ffmpeg 路径]
"""
from __future__ import annotations

import array
import ctypes
import logging
import math
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
FAIL: list[str] = []
_GLOBAL = T._kGlobalScope


logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

DIAGNOSE = "--diagnose" in sys.argv
VARIANTS = "--variants" in sys.argv


def set_num(obj: int, sel: str, value, restype) -> str:
    """给 CoreAudio 对象写一个标量属性（采样率/缓冲帧大小那类）"""
    import ctypes
    addr = T._Addr(T._fourcc(sel), _GLOBAL, 0)
    data = restype(value)
    rc = T._CA.AudioObjectSetPropertyData(
        ctypes.c_uint32(obj), ctypes.byref(addr), ctypes.c_uint32(0), None,
        ctypes.c_uint32(ctypes.sizeof(data)), ctypes.byref(data))
    return f"{sel}={value}→{rc:#x}"


def build_tap(oc, *, private=None, set_uuid=True):
    """建环回对象，返回 (对象 ID, 要写进聚合设备的 UID 字符串, 备注)

    Sunshine 是先给 CATapDescription 设一个自己生成的 NSUUID，再用它的字符串
    去描述聚合设备；我们是读环回对象的 'tuid'。两种写法都试一遍才知道哪种
    能被 HAL 认下来。
    """
    desc = oc.send(oc.alloc("CATapDescription"),
                   "initStereoGlobalTapButExcludeProcesses:", oc.array([]))
    oc.send(desc, "setName:", oc.string(T.TAP_DEVICE_NAME))
    uuid_str = ""
    if set_uuid:  # 显式 UUID（Sunshine 的做法）
        u = oc.alloc("NSUUID")
        u = oc.send(u, "init")
        oc._keep.append(u)
        oc.send(desc, "setUUID:", u)   # 这里给的是 NSUUID 对象，不是字符串
        uuid_str = oc.text(oc.send(u, "UUIDString"))
    if private is not None:
        # arm64 上 ObjC 的 BOOL 是 signed char
        oc.send(desc, "setPrivate:", int(private), argtypes=[ctypes.c_int8])
    out = ctypes.c_uint32(0)
    fn = T._CA.AudioHardwareCreateProcessTap
    fn.restype = ctypes.c_int32
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    rc = fn(ctypes.c_void_p(desc), ctypes.byref(out))
    if rc != 0:
        raise RuntimeError(f"CreateProcessTap → {rc:#x}")
    read_uid = T._prop_string(out.value, T._kTapUID)
    return out.value, (uuid_str or read_uid), read_uid


def build_agg(oc, tap_uid: str, *, uid: str, private, drift, autostart, stacked):
    import ctypes
    sub = {"uid": oc.string(tap_uid)}
    if drift:
        sub["drift"] = oc.number(1)
    d = {"name": oc.string(T.TAP_DEVICE_NAME), "uid": oc.string(uid),
         "taps": oc.array([oc.dictionary(sub)])}
    if private is not None:
        d["private"] = oc.number(private)
    if autostart is not None:
        d["tapautostart"] = oc.number(autostart)
    if stacked is not None:
        d["stacked"] = oc.number(stacked)
    out = ctypes.c_uint32(0)
    fn = T._CA.AudioHardwareCreateAggregateDevice
    fn.restype = ctypes.c_int32
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    rc = fn(ctypes.c_void_p(oc.dictionary(d)), ctypes.byref(out))
    return rc, out.value


def trial(label: str, ffmpeg: str, *, rate=None, tap_private=None,
          no_uuid=False, **agg_kw):
    """一个组合跑一遍：建环回→建聚合→（可选设采样率）→看输入流/在 avfoundation 里是否可见"""
    oc = T._objc()
    tap = agg = 0
    note = []
    try:
        tap, uid, read_uid = build_tap(oc, private=tap_private,
                                       set_uuid=not no_uuid)
        if uid != read_uid:
            note.append("uid≠tuid")
        agg_kw["uid"] = "com.hancast.trial." + str(abs(hash(label)) % 100000)
        rc, agg = build_agg(oc, uid, **agg_kw)
        if rc not in (0, 0x45787441):  # 'ExtA' = 同 UID 已存在
            print(f"{label:38s} 建聚合失败 {rc:#x}")
            return
        if rate:
            note.append(set_num(agg, "nsrt", float(rate), ctypes.c_double))
            note.append(set_num(agg, "fsby", 512, ctypes.c_uint32))
        time.sleep(1.5)
        n_in = T._input_streams(agg)
        active = ids_prop(agg, "atap")
        listed = [nm for _, nm in avfoundation_audio(ffmpeg)
                  if "HanCast" in nm]
        print(f"{label:38s} streams={n_in} atap={active} "
              f"avf={listed or '—'} {' '.join(str(x) for x in note)}")
    except Exception as e:  # noqa: BLE001
        print(f"{label:38s} 异常: {e}")
    finally:
        T._destroy_object("AudioHardwareDestroyAggregateDevice", agg)
        T._destroy_object("AudioHardwareDestroyProcessTap", tap)
        time.sleep(0.3)


def run_variants(ffmpeg: str):
    """对照实验：把「子环回到底怎样才能实例化」的几个变量各翻一遍"""
    base = dict(private=0, drift=False, autostart=None, stacked=None)
    trials = [
        ("A 现状（public, 无 drift）", {}),
        ("B +drift", dict(drift=True)),
        ("C +tapautostart=0", dict(autostart=0)),
        ("D agg private=1", dict(private=1)),
        ("E private+drift+autostart0", dict(private=1, drift=True, autostart=0)),
        ("F public+设采样率", dict(rate=48000)),
        ("G private+设采样率", dict(private=1, rate=48000)),
        ("H 环回也 private=1", dict(private=1, tap_private=1)),
        ("I public 环回+agg public", dict(tap_private=0)),
        ("J stacked=1", dict(stacked=1)),
        ("K 不显式设 UUID（用 tuid）", dict(no_uuid=True)),
    ]
    for label, over in trials:
        kw = dict(base)
        kw.update(over)
        trial(label, ffmpeg, **kw)



def ffmpeg_path() -> str:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    arg = args[0] if args else ""
    if arg:
        return arg
    found = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True)
    p = found.stdout.strip()
    if p:
        return p
    raise SystemExit("找不到 ffmpeg")


def make_tone(path: str, seconds: int = 12):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"sine=frequency={TONE_HZ}:duration={seconds}",
                    "-ar", "44100", path], check=True)


def avfoundation_audio(ffmpeg: str) -> list:
    """avfoundation 的音频设备列表（[(索引, 名称)]），与 sidecar 同一份输出格式"""
    r = subprocess.run([ffmpeg, "-hide_banner", "-f", "avfoundation",
                        "-list_devices", "true", "-i", ""],
                       capture_output=True, text=True, errors="replace")
    out, section, found = (r.stderr or "") + (r.stdout or ""), None, []
    for line in out.splitlines():
        stripped = line.strip()
        if "AVFoundation video devices" in stripped:
            section = "video"
        elif "AVFoundation audio devices" in stripped:
            section = "audio"
        elif section == "audio":
            m = re.search(r"\[(\d+)\]\s*(.*)", stripped)
            if m and m.group(2).strip():
                found.append((m.group(1), m.group(2).strip()))
    return found


def capture(ffmpeg: str, idx: str, seconds: float, playing: bool, tone_wav: str) -> bytes:
    """从指定 avfoundation 音频设备录一段 s16le 单声道 PCM"""
    player = None
    if playing:
        player = subprocess.Popen(["afplay", tone_wav])
    time.sleep(0.3)
    out = f"/tmp/hancast-systap-{int(time.time()*1000)}.wav"   # 录完读进内存即删，显式 /tmp
    r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "avfoundation", "-i", f":{idx}",
                        "-t", f"{seconds}", "-ar", str(RATE), "-ac", "1",
                        "-c:a", "pcm_s16le", out], capture_output=True, text=True)
    if player:
        player.terminate()
    if r.returncode != 0 or not os.path.isfile(out):
        FAIL.append(f"录制失败（设备 {idx}）: {(r.stderr or '').strip()[-250:]}")
        return b""
    with wave.open(out, "rb") as w:
        data = w.readframes(w.getnframes())
    os.remove(out)
    return data


def describe_input(ffmpeg: str, idx: str) -> str:
    """avfoundation 报给 ffmpeg 的这条输入流是什么格式（采样率/声道数）"""
    r = subprocess.run([ffmpeg, "-hide_banner", "-f", "avfoundation", "-i", f":{idx}",
                        "-t", "0.1", "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    lines = [l.strip() for l in (r.stderr or "").splitlines() if "Audio:" in l]
    return " | ".join(lines)[-200:] or "（没读到音频流）"


def peak_tone(pcm: bytes, rate: int) -> tuple:
    """最强的正弦分量在哪：用来区分「没声音」和「声道错位导致音高跑掉」"""
    import numpy as np
    s = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(float)
    if s.size < 2048:
        return 0.0, 0.0
    spec = np.abs(np.fft.rfft(s * np.hanning(s.size))) / s.size
    freqs = np.fft.rfftfreq(s.size, 1.0 / rate)
    band = (freqs >= 60) & (freqs <= rate / 2)
    i = int(np.argmax(spec[band]))
    return float(freqs[band][i]), float(spec[band][i] * 2)


def band_energy(pcm: bytes, freq: float, tol: float = 0.05) -> tuple:
    """目标频率 ±tol 这段频带里的能量，和整段的 RMS

    不用单根 FFT 谱线（Goertzel 取最近 bin）：这台机器上播放链 + avfoundation
    采集链有约 −0.8% 的固定音高偏差（440Hz 实测 436Hz，BlackHole 同路径实测
    436.5Hz，见 tools/probe-macos-audio-pitch.py），单 bin 判据会把「真的录到了
    声音」误判成「听不到」。±5% 的带内能量既能证明声音在里面，又容得下这个偏差。
    """
    import numpy as np
    s = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(float)
    if s.size < 2048:
        return 0.0, 0.0
    rms = float(np.sqrt(np.mean(s ** 2)) / 32767.0)
    spec = np.abs(np.fft.rfft(s * np.hanning(s.size))) / s.size
    freqs = np.fft.rfftfreq(s.size, 1.0 / RATE)
    band = (freqs >= freq * (1 - tol)) & (freqs <= freq * (1 + tol))
    return float(np.sqrt(np.sum(spec[band] ** 2)) * 2), rms


def goertzel(pcm: bytes, freq: float) -> tuple:
    s = array.array("h")
    s.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if len(s) < 1000:
        return 0.0, 0.0
    rms = math.sqrt(sum(x * x for x in s) / len(s)) / 32767.0
    k = int(0.5 + len(s) * freq / RATE)
    w = 2 * math.pi * k / len(s)
    coeff = 2 * math.cos(w)
    s1 = s2 = 0.0
    for x in s:
        s0 = x + coeff * s1 - s2
        s1, s2 = s0, s1
    return math.sqrt(s1 * s1 + s2 * s2 - coeff * s1 * s2) / len(s) / 32767.0, rms


def find_tap(devices: list):
    return next(((i, n) for i, n in devices if T.is_tap_device(n)), None)


def wait_for_tap(ffmpeg: str, seconds: float = 6.0):
    """等 avfoundation 认出这台新设备。

    CoreAudio 的聚合设备是异步注册的（AudioHardwareCreateAggregateDevice 返回
    成功时对象还没进 kAudioHardwarePropertyDevices 列表），立刻枚举必然扑空。
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        tap = find_tap(avfoundation_audio(ffmpeg))
        if tap:
            return tap
        time.sleep(0.4)
    return None


def prop(obj: int, sel: str, scope: str = "") -> tuple:
    """读一个 CoreAudio 属性到 1KB 缓冲区：返回 (OSStatus, buffer, 实际字节数)"""
    import ctypes
    addr = T._Addr(T._fourcc(sel), T._fourcc(scope) if scope else _GLOBAL, 0)
    size = ctypes.c_uint32(1024)
    buf = (ctypes.c_uint8 * 1024)()
    rc = T._CA.AudioObjectGetPropertyData(ctypes.c_uint32(obj), ctypes.byref(addr),
                                          ctypes.c_uint32(0), None,
                                          ctypes.byref(size), buf)
    return rc, buf, size.value


def buffers(obj: int, scope_name: str) -> str:
    """'slay'/给定 scope 下的 AudioBufferList 摘要"""
    import ctypes
    rc, buf, _ = prop(obj, "slay", scope_name)
    if rc != 0:
        return f"{rc:#x}"
    # AudioBufferList：mNumberBuffers@0，mBuffers@8（每个 AudioBuffer 16 字节，
    # mNumberDataBytes@0、mNumberChannels@4）
    n = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32)).contents.value
    chans = [ctypes.cast(ctypes.byref(buf, 12 + 16 * i),
                         ctypes.POINTER(ctypes.c_uint32)).contents.value
             for i in range(min(n, 8))]
    return f"buffers={n} channels={chans}"


def cfobj(obj: int, sel: str) -> str:
    """读一个 CF 对象属性并用 description 打印（这台 HAL 不接受 NULL outData）"""
    import ctypes
    rc, buf, size = prop(obj, sel)
    if rc != 0 or size < ctypes.sizeof(ctypes.c_void_p):
        return f"{rc:#x}"
    ref = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
    if not ref:
        return "nil"
    oc = T._objc()
    return oc.text(oc.send(ref, "description"))[:700]


def ids_prop(obj: int, sel: str):
    import ctypes
    rc, buf, size = prop(obj, sel)
    if rc != 0 or size < 4:
        return f"{rc:#x} / {size}B"
    return list(ctypes.cast(buf, ctypes.POINTER(
        ctypes.c_uint32 * (size // 4))).contents)


def tap_report() -> str:
    """环回对象本身的状态：聚合设备没有流时，先确认环回自己有没有流"""
    import ctypes
    oc = T._objc()
    tap = T._state["tap_id"]
    if not tap:
        return "  （没有环回对象）"
    desc = cfobj(tap, "tdsc")
    rc, buf, size = prop(tap, "tfmt")
    asbd = "n/a"
    if rc == 0 and size >= 24:
        sr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_double)).contents.value
        fid = bytes(buf[8:12])[::-1].decode("ascii", "replace")
        frames = ctypes.cast(ctypes.byref(buf, 20),
                             ctypes.POINTER(ctypes.c_uint32)).contents.value
        asbd = f"{sr:.0f}Hz {fid} frames/pkt={frames} ch={ctypes.cast(ctypes.byref(buf, 16), ctypes.POINTER(ctypes.c_uint32)).contents.value}"
    out = [f"tap={tap} uid={T._state['uid']}",
           f"  inpt {buffers(tap, 'inpt')} | outp {buffers(tap, 'outp')}",
           f"  tfmt {asbd}（OSStatus {rc:#x}）",
           f"  tdsc {desc}"]
    # 系统里所有环回对象（能看到别的 App 建的环回，佐证权限是否生效）
    out.append(f"  全局 tap 列表 'tps#' → {ids_prop(1, 'tps#')}")
    out.append(f"  在跑的音频进程 'prs#' → {len(ids_prop(1, 'prs#'))} 个")
    return "\n  " + "\n  ".join(out)


def stream_report() -> str:
    """把新建设备的流配置、环回列表、组合字典打出来（诊断用）"""
    lines = []
    for dev in T._devices():
        if T._device_uid(dev) != T.TAP_DEVICE_UID:
            continue
        lines.append(f"dev={dev}")
        lines.append(f"  inpt  {buffers(dev, 'inpt')}")
        lines.append(f"  outp  {buffers(dev, 'outp')}")
        # 'atap' = 在用的子环回对象 ID 列表，'tap#' = 组合里登记的环回 UID
        lines.append(f"  atap → {ids_prop(dev, 'atap')}")
        lines.append(f"  tap# → {cfobj(dev, 'tap#')}")
        lines.append(f"  grup → {cfobj(dev, 'grup')}")
        lines.append(f"  acom → {cfobj(dev, 'acom')}")
    return "\n  " + "\n  ".join(lines) if lines else "  （没有找到我们的聚合设备）"


def wait_for_gone(ffmpeg: str, seconds: float = 6.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not find_tap(avfoundation_audio(ffmpeg)):
            return True
        time.sleep(0.4)
    return False


def main() -> int:
    ffmpeg = ffmpeg_path()
    print(f"ffmpeg: {ffmpeg}")
    if not T.supported():
        print("SUMMARY: SKIP（这台机器没有 Audio Tap，14.2 以下走 BlackHole）")
        return 0

    print("建之前:", ", ".join(f"[{i}]{n}" for i, n in avfoundation_audio(ffmpeg)))
    if VARIANTS:
        run_variants(ffmpeg)
        print("SUMMARY: DIAG")
        return 0
    if DIAGNOSE:
        if T.ensure_device():
            time.sleep(1.5)
            print(tap_report())
            print(stream_report())
            print("建之后:", ", ".join(f"[{i}]{n}"
                                  for i, n in avfoundation_audio(ffmpeg)))
            T.destroy_device()
        print("SUMMARY: DIAG")
        return 0
    if T.ensure_device():
        try:
            tap = wait_for_tap(ffmpeg)
            devices = avfoundation_audio(ffmpeg)
            print("建之后:", ", ".join(f"[{i}]{n}" for i, n in devices))
            if not tap:
                FAIL.append("设备建好了但 avfoundation 看不到它")
                print("诊断:" + tap_report() + stream_report())
            else:
                idx, name = tap
                print(f"环回设备: [{idx}]{name}")
                tone_wav = "/tmp/hancast-systap-tone.wav"   # 用完即删（下面 os.remove）
                make_tone(tone_wav)
                base_pcm = capture(ffmpeg, idx, 2.0, False, tone_wav)
                base_level, base_rms = band_energy(base_pcm, TONE_HZ, tol=0.02)
                tone_pcm = capture(ffmpeg, idx, 2.0, True, tone_wav)
                tone_level, tone_rms = band_energy(tone_pcm, TONE_HZ, tol=0.02)
                os.remove(tone_wav)
                pk_freq, pk_level = peak_tone(tone_pcm, RATE)
                print(f"ffmpeg 看到的输入流: {describe_input(ffmpeg, idx)}")
                print(f"静置: {TONE_HZ:.0f}Hz±2%带内={base_level:.4f} "
                      f"RMS={base_rms:.4f} | "
                      f"播放: {TONE_HZ:.0f}Hz±2%带内={tone_level:.4f} "
                      f"RMS={tone_rms:.4f} | "
                      f"最强分量 {pk_freq:.0f}Hz@{pk_level:.4f}")
                if tone_rms <= 0.001:
                    FAIL.append("环回里没有音频数据")
                if tone_level < max(0.01, base_level * 3):
                    FAIL.append(f"{TONE_HZ:.0f}Hz 听不到（{tone_level:.4f} vs 基线 "
                                f"{base_level:.4f}，最强分量 {pk_freq:.0f}Hz）")
        finally:
            T.destroy_device()
            gone = wait_for_gone(ffmpeg)
            print(f"销毁后设备已消失: {gone}")
            if not gone:
                FAIL.append("destroy_device() 之后「HanCast 系统声音」还在")
    else:
        FAIL.append("ensure_device() 建不出「HanCast 系统声音」")

    print("SUMMARY:", "FAIL: " + "; ".join(FAIL) if FAIL else "PASS")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
