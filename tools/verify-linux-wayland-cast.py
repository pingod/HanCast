#!/usr/bin/env python3
"""
Linux（Wayland）屏幕投屏验收：画面非黑 + 音频设备可用

这一条链路上出过的两个故障，都必须由这个脚本「先复现、后证明」：

1. 黑屏：Linux 侧以前一律 `-f x11grab -i $DISPLAY`，而 Hyprland 的 XWayland 以
   `-rootless` 启动，原生 Wayland 窗口不在 X 窗口树里 → 投屏启动成功、帧率正常、
   每一帧纯黑（2026-10-17 实测 x11grab YAVG=16.03，同屏 grim=44.28）。
   现在改由随包 helper `hancast-wlgrab`（wlroots screencopy）把 BGRA 帧灌进
   FFmpeg 的 stdin，本脚本按**产品代码路径**（FfmpegCapture.start_hls）跑一遍，
   再对切出来的分片做亮度判据。
2. 麦克风 / 系统声音「不可用」：`_list_audio_devices()` 在 Linux 上直接返回空表，
   界面里两个下拉都是空的。现在走 pactl/pw-dump 枚举，monitor 源就是系统声音。

跑法（在装了 Wayland 桌面的 Linux 真机上，仓库根）：

    python3 tools/verify-linux-wayland-cast.py                  # 结构 + 画面
    python3 tools/verify-linux-wayland-cast.py --output DP-2    # 指定抓哪块屏
    python3 tools/verify-linux-wayland-cast.py --seconds 8
    python3 tools/verify-linux-wayland-cast.py --with-tone      # 会真的出声 3 秒

⚠️ `--with-tone` 会往**当前默认输出**播一段测试音（不改音量、不动配置），用来
   证明 `<sink>.monitor` 抓到的确实是系统在放的声音；不想吵到人的时候别带它，
   那时音频只做「命令拼装 + 分片里有没有 aac 流」的结构性判据。

前置：Wayland 会话（$WAYLAND_DISPLAY 存在）、本机装了 ffmpeg 与 pactl；
      helper 可从环境变量 HANCAST_WLGRAB 指定，否则按打包/开发布局自动找。
退出码：0 全 PASS / 1 有 FAIL / 2 环境不满足（不是产品问题）。

脚本只写一次性目录（HOME/XDG_* 全指向临时路径），不碰用户真实的
hancast_setting.json，也不启停 GUI。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS: List[str] = []
FAIL: List[str] = []
SKIP: List[str] = []


def ok(msg: str) -> None:
    PASS.append(msg)
    print(f"PASS  {msg}")


def bad(msg: str) -> None:
    FAIL.append(msg)
    print(f"FAIL  {msg}")


def skip(msg: str) -> None:
    SKIP.append(msg)
    print(f"SKIP  {msg}")


def run(argv: List[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, timeout=timeout,
                          text=True, encoding="utf-8", errors="replace")


def luma(path: str) -> Optional[float]:
    """一段媒体的平均亮度（signalstats YAVG）。黑帧 ≈16，有画面就明显更高"""
    r = run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
             "-i", path, "-vf",
             "signalstats,metadata=print:key=lavfi.signalstats.YAVG",
             "-f", "null", "-"], 40)
    vals = [float(line.split("=")[1])
            for line in (r.stderr or "").splitlines()
            if "lavfi.signalstats.YAVG=" in line]
    return sum(vals) / len(vals) if vals else None


def rms_db(path: str) -> Optional[float]:
    """音频流的 RMS 电平（dB）。-inf/None = 数字静音

    ⚠️ astats 的元数据行是 `[Parsed_astats_0 @ 0x55…] RMS level dB: -70.5`，
    前面带滤镜名和地址——按行首匹配 `RMS level dB` 永远取不到值（第一次跑就是这么
    把「有声音」误判成「取不到 RMS」的）。所以按子串匹配、取冒号后面的数。
    """
    r = run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
             "-i", path, "-af", "astats=metadata=1", "-f", "null", "-"], 40)
    vals: List[float] = []
    for line in (r.stderr or "").splitlines():
        if "RMS level dB" not in line:
            continue
        try:
            vals.append(float(line.rsplit(":", 1)[1].strip()))
        except (ValueError, IndexError):
            pass
    return max(vals) if vals else None


def audio_streams(path: str) -> int:
    r = run(["ffmpeg", "-nostdin", "-hide_banner", "-i", path, "-map", "0:a",
             "-c:a", "copy", "-t", "0.1", "-f", "null", "-"], 20)
    return 0 if r.returncode != 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Linux Wayland 投屏验收")
    ap.add_argument("--output", default=None, help="抓哪块 wl_output（默认取主屏）")
    ap.add_argument("--seconds", type=int, default=6, help="捕获时长（秒）")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--with-tone", action="store_true",
                    help="播 3 秒测试音并验证系统声音镜像真的收到了信号（会出声）")
    args = ap.parse_args()

    if sys.platform != "linux":
        print(f"本脚本只在 Linux 上跑（当前 {sys.platform}）", file=sys.stderr)
        return 2
    if not os.environ.get("WAYLAND_DISPLAY"):
        print("当前 shell 没有 WAYLAND_DISPLAY：不是 Wayland 会话，"
              "请在桌面终端里跑（X11 会话请验 x11grab 那条路径）", file=sys.stderr)
        return 2
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("本机没有 ffmpeg", file=sys.stderr)
        return 2

    # 一次性 HOME/XDG_*：探针绝不写用户真实的 hancast_setting.json
    sandbox = tempfile.mkdtemp(prefix="hancast-wlverify-")
    for var in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                "XDG_STATE_HOME"):
        os.environ[var] = os.path.join(sandbox, var.lower())
    sys.path.insert(0, os.path.join(REPO, "hancast-backend"))

    try:
        from hancast_sidecar.screencast import pulse, wlgrab
        from hancast_sidecar.screencast.capture import FfmpegCapture
        from hancast_sidecar.screencast.ffmpeg_manager import FfmpegManager

        # ── 1. helper 定位与自检 ──
        path, reason = wlgrab.find_helper()
        if path:
            ok(f"Wayland helper 可用：{path}")
        else:
            bad(f"Wayland helper 不可用：{reason}")
            print("没有 helper 就不可能摆脱 x11grab 的黑屏，后续判据无意义")
            return 1
        outputs = wlgrab.list_outputs()
        if outputs:
            ok(f"helper 枚举到 {len(outputs)} 块输出："
               + "、".join(f"{o.get('name')}({o.get('width')}x{o.get('height')})"
                           for o in outputs))
        else:
            bad("helper 没枚举到任何 wl_output")
            return 1
        target = args.output or wlgrab.resolve_output({"id": "desktop"})
        if not any(str(o.get("name")) == target for o in outputs):
            bad(f"选中的输出 {target} 不在列表里")
            return 1

        # ── 2. 产品枚举链路（list_sources） ──
        mgr = FfmpegManager()
        src = mgr.list_sources()
        screens = src.get("screens") or []
        ids = [s["id"] for s in screens]
        if any(i.startswith("wl:") for i in ids):
            ok(f"screens 已带 Wayland 单屏条目：{ids}")
        else:
            bad(f"screens 里没有 wl: 条目（前端就只有「整个屏幕」一项可选）：{ids}")
        desktop = next((s for s in screens if s["id"] == "desktop"), None)
        if desktop and "Wayland" in str(desktop.get("title")):
            ok(f"desktop 标题已说明 Wayland 语义：{desktop['title']}")
        else:
            bad(f"desktop 标题仍在宣称「所有显示器」：{desktop}")

        devices = src.get("audio_inputs") or []
        mics = [d for d in devices if d.get("kind") == "mic"]
        systems = [d for d in devices if d.get("kind") == "system"]
        if mics:
            ok(f"麦克风可选 {len(mics)} 条："
               + "、".join(f"{d.get('title') or d['name']}"
                           + ("（默认）" if d.get("default") else "")
                           for d in mics[:3]))
        else:
            bad("麦克风列表为空（Linux 上这就是界面上「不可用」的根因）")
        if systems:
            ok(f"系统声音可选 {len(systems)} 条（monitor 镜像）："
               + "、".join(f"{d.get('title') or d['name']}"
                           + ("（默认）" if d.get("default") else "")
                           for d in systems[:3]))
        else:
            bad("系统声音列表为空：没枚举到任何 <sink>.monitor")
        if not (src.get("capture") or {}).get("helper"):
            bad(f"list_sources 的 capture 字段没带 helper：{src.get('capture')}")

        # ── 3. 参数拼装（不实际起流也要断言形状） ──
        vargs, _ = FfmpegManager.build_video_input(
            {"type": "screen", "id": f"wl:{target}"}, 15, "", ffmpeg,
            wl_size=(2560, 1440))
        joined = " ".join(vargs)
        if "-pixel_format" in vargs and "pipe:0" in joined and "-pix_fmt" not in vargs:
            ok(f"Wayland 视频输入拼装正确：{joined}")
        else:
            bad(f"Wayland 视频输入拼装不对（rawvideo 要 -pixel_format，不是 -pix_fmt）："
                f"{joined}")
        mic_name = (mics[0]["name"] if mics else "")
        sys_name = (systems[0]["name"] if systems else "")
        aargs = FfmpegManager.build_audio_input(
            {"mic": bool(mic_name), "mic_device": mic_name,
             "system": bool(sys_name), "system_device": sys_name},
            input_offset=1)
        aj = " ".join(aargs)
        if mic_name and sys_name:
            if aj.count("-f pulse") == 2 and "amix" in aj:
                ok("麦克风+系统声音双路 pulse 且带 amix 混音")
            else:
                bad(f"双路音频拼装不对：{aj}")
        elif mic_name or sys_name:
            ok(f"单路音频拼装：{aj}")
        else:
            skip("现场没有任何 pulse 输入源，音频拼装判据跳过")

        # ── 4. 真起一路会话（走 FfmpegCapture，判据落在切出来的分片上） ──
        out_dir = os.path.join(sandbox, "hls")
        os.makedirs(out_dir, exist_ok=True)
        cap = FfmpegCapture(ffmpeg)
        audio_cfg = {}
        if sys_name:
            audio_cfg = {"system": True, "system_device": sys_name}
        started = time.time()
        res = cap.start_hls({"type": "screen", "id": f"wl:{target}"},
                            audio_cfg,
                            {"width": args.width, "height": args.height,
                             "fps": 15, "bitrate": "2M"},
                            out_dir, target="browser")
        if not res.get("ok"):
            bad(f"HLS 会话起不来：{res.get('error')}")
            cap.stop()
            return 1
        ok(f"HLS 会话已启动（输出 {res.get('width')}x{res.get('height')}，"
           f"{res.get('fps')}fps，分片 {res.get('segment_seconds')}s）")

        # 会话期间**边跑边抄分片**。产品参数是 `-hls_flags delete_segments` +
        # `hls_list_size=6`（params.py screencast.playlist_size），磁盘上只留最后
        # 6 片：等 stop() 再去 listdir，拿到的是会话尾部的画面，而测试音在前 3 秒
        # 就播完了——RMS 恒为 -inf，判据变成假阴性（真机上第一次跑就是这么误报的）。
        keep_dir = os.path.join(sandbox, "segments")
        os.makedirs(keep_dir, exist_ok=True)
        kept: List[str] = []
        seen = set()

        def sweep() -> None:
            """把当下存在的完整分片抄进沙箱（只抄已经写完的：跳过最新那一片）"""
            try:
                names = sorted(f for f in os.listdir(out_dir)
                               if f.endswith(".ts"))
            except OSError:
                return
            for name in names[:-1] if len(names) > 1 else names:
                if name in seen:
                    continue
                src = os.path.join(out_dir, name)
                try:
                    if os.path.getsize(src) == 0:
                        continue
                    shutil.copy2(src, os.path.join(keep_dir, name))
                except OSError:
                    continue
                seen.add(name)
                kept.append(name)

        tone_proc = None
        tone_rc = None
        tone_at = time.time() + 2.0
        deadline = time.time() + max(args.seconds, 5.0)
        while time.time() < deadline:
            sweep()
            if args.with_tone and tone_proc is None and time.time() >= tone_at:
                wav = os.path.join(sandbox, "tone.wav")
                r = run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                         "-y", "-f", "lavfi", "-i", "sine=frequency=660:duration=3",
                         "-ar", "48000", "-ac", "2", wav])
                player = shutil.which("pw-play") or shutil.which("paplay")
                if r.returncode == 0 and player:
                    tone_proc = subprocess.Popen([player, wav],
                                                 stdin=subprocess.DEVNULL,
                                                 stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.PIPE,
                                                 text=True)
                else:
                    skip("没有 pw-play/paplay 或生成测试音失败，跳过出声判据")
            time.sleep(0.3)
        if tone_proc is not None:
            tone_proc.wait(timeout=15)
            tone_rc = tone_proc.returncode
            if tone_rc != 0:
                tail = (tone_proc.stderr.read() or "").strip()[:120] if tone_proc.stderr else ""
                skip(f"测试音播放失败（rc={tone_rc}）：{tail}")
        sweep()
        cap.stop()
        sweep()
        if not kept:
            bad("会话期间没有切出任何可读的 .ts 分片")
        else:
            # 亮度取中间那片：起流第一片可能还含编码器空档
            pick = kept[len(kept) // 2]
            first = os.path.join(keep_dir, pick)
            avg = luma(first)
            if avg is None:
                bad(f"分片读不出亮度（{pick}，{os.path.getsize(first)} 字节）")
            elif avg <= 30:
                bad(f"分片平均亮度 {avg:.1f} —— 还是黑屏"
                    f"（纯黑 ≈16，实测正常桌面 ≥40）")
            else:
                ok(f"分片 {pick}（共抄到 {len(kept)} 片）平均亮度 {avg:.1f}，"
                   "画面不是黑的")
            if audio_cfg:
                # 音频流与 RMS 都跨全部副本取最好值：测试音只播 3 秒，
                # 落在哪一片取决于时序，只看一片会把它当成静音
                with_audio = [n for n in kept if audio_streams(
                    os.path.join(keep_dir, n))]
                if with_audio:
                    ok(f"{len(with_audio)}/{len(kept)} 片带得到音频流（aac）")
                    levels = [x for x in (rms_db(os.path.join(keep_dir, n))
                                          for n in with_audio) if x is not None]
                    rms = max(levels) if levels else None
                    if args.with_tone and rms is not None:
                        if rms < -60:
                            bad(f"带着音频流但 RMS={rms:.1f}dB，等于没收到声音"
                                f"（测试音播放 rc={tone_rc}）")
                        else:
                            ok(f"系统声音镜像收到信号：RMS={rms:.1f}dB")
                    elif rms is not None:
                        skip(f"没带 --with-tone：有音频流（RMS={rms:.1f}dB），"
                             "不判有没有信号")
                    else:
                        skip("有音频流但取不到 RMS")
                else:
                    bad("选了系统声音却没有一片带得上音频流")
            else:
                skip("现场没有可选的系统声音，未验音频流")

        # ── 5. 收尾：helper 不能留孤儿进程 ──
        time.sleep(0.5)
        r = run(["pgrep", "-a", "hancast-wlgrab"])
        if (r.stdout or "").strip():
            bad(f"停止会话后仍有抓取进程残留：\n{r.stdout.strip()}")
        else:
            ok("停止会话后没有残留的 hancast-wlgrab 进程")
        if cap.running:
            bad("cap.stop() 之后 FfmpegCapture.running 还是 True")
        else:
            ok("FfmpegCapture 状态已归位（running=False）")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    print(f"\nSUMMARY  PASS={len(PASS)}  FAIL={len(FAIL)}  SKIP={len(SKIP)}")
    for m in FAIL:
        print(f"  FAIL  {m}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
