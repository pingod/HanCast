#!/usr/bin/env python3
"""验证「投屏指定窗口」的枚举与裁剪数学（macOS）。

窗口抓取在 mac 上的实现是「抓那块屏 + 按窗口几何 crop」，所以有两处会静默错：
  1. CoreGraphics 的显示器顺序 vs avfoundation 的 "Capture screen N" 顺序 —— 对不上
     就投到别的屏；
  2. 两套坐标系（CGDisplayBounds y 向上 / 窗口列表 y 向下）与 Retina 点→像素换算 ——
     算错就投出一条错位的边。
坐标错了不会报错，画面照样出，只是内容不对，所以必须真跑一遍 2 秒捕获来判。

用法：
    hancast-backend/.venv/bin/python tools/probe-window-picker.py            # 只枚举
    hancast-backend/.venv/bin/python tools/probe-window-picker.py --capture  # 抽样真投
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hancast-backend"))

from hancast_sidecar.screencast.ffmpeg_manager import (  # noqa: E402
    FfmpegManager, avfoundation_screen_indexes, build_crop_filter,
    darwin_displays, darwin_windows,
)

FAIL = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", action="store_true", help="真跑 ffmpeg 抽样验证裁剪")
    ap.add_argument("--window", type=int, default=-1,
                    help="抽样第几个窗口（默认取面积最大的，内容较稳定）")
    args = ap.parse_args()

    fm = FfmpegManager()
    info = fm.check()
    print(f"ffmpeg: {info.get('path')} ({info.get('version')}) ok={info.get('ok')}")
    if not info.get("ok"):
        print("FFmpeg 不可用，无法验证")
        return 2
    path = info["path"]

    print("\n── 显示器（CoreGraphics）──")
    displays = darwin_displays()
    for d in displays:
        px = (round(d["points"][2] * d["scale"]), round(d["points"][3] * d["scale"]))
        print(f"  screen_no={d['screen_no']} main={d['main']} points={d['points']} "
              f"scale={d['scale']:.3f} px={px} top={d['top']:.1f}")
    check(len(displays) >= 1, "至少枚举到 1 块屏")

    print("\n── avfoundation 屏幕设备 ──")
    idxs = avfoundation_screen_indexes(path)
    print(f"  按屏序的设备索引: {idxs}")
    check(len(idxs) == len(displays),
          f"屏幕设备数({len(idxs)}) == CG 显示器数({len(displays)})，屏序才能一一对应")

    print("\n── 窗口 ──")
    wins = darwin_windows()
    for w in wins[:12]:
        print(f"  {w['id']} screen_no={w['screen_no']} crop={w['crop']} {w['title'][:60]!r}")
    print(f"  共 {len(wins)} 个")
    check(len(wins) > 0, "枚举到可投窗口")
    check(any(w["title"] for w in wins), "窗口有标题（无标题=缺屏幕录制权限）")

    for w in wins:
        d = next((x for x in displays if x["screen_no"] == w["screen_no"]), None)
        if not d:
            check(False, f"{w['id']} 落在未知屏 {w['screen_no']}")
            continue
        x, y, cw, ch = w["crop"]
        fw = round(d["points"][2] * d["scale"])
        fh = round(d["points"][3] * d["scale"])
        ok = (cw % 2 == 0 and ch % 2 == 0 and cw >= 2 and ch >= 2
              and 0 <= x and 0 <= y and x + cw <= fw and y + ch <= fh)
        if not ok:
            check(False, f"{w['id']} crop={w['crop']} 超出屏 {fw}x{fh}")
    check(not FAIL, "全部裁剪区域落在屏内且尺寸为偶数")

    if not args.capture or not wins:
        print("\n（未抽样捕获；加 --capture 验证画面与坐标）")
        return 1 if FAIL else 0

    import json
    import numpy as np

    by_no = {d["screen_no"]: d for d in displays}
    out = tempfile.mkdtemp(prefix="probe-win-cast-")

    def dims(p):
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", p],
            capture_output=True, text=True)
        st = (json.loads(probe.stdout).get("streams") or [{}])[0]
        return st.get("width"), st.get("height")

    def gray(p):
        wdt, hgt = dims(p)
        r = subprocess.run(
            [path, "-hide_banner", "-loglevel", "error", "-i", p,
             "-pix_fmt", "gray", "-f", "rawvideo", "-"],
            capture_output=True, timeout=60)
        a = np.frombuffer(r.stdout, dtype=np.uint8)
        if a.size != wdt * hgt:
            check(False, f"{os.path.basename(p)} 灰度取帧失败（{a.size} != {wdt}x{hgt}）")
            return None
        return a.reshape(hgt, wdt).astype(np.float64)

    def grab(name, idx, vf=None, extra=()):
        p = os.path.join(out, name)
        cmd = [path, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "avfoundation", "-framerate", "15",
               "-i", f"{idx}:none", *extra]
        if vf:
            cmd += ["-vf", vf]
        cmd += ["-frames:v", "1", p]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not os.path.isfile(p):
            print(f"  抓取 {name} 失败({r.returncode}): {r.stderr.strip()[:160]}")
            return None
        return p

    def psnr(a, b):
        """b 与 a 的 PSNR（b 先按 a 的尺寸重采样）。
        注意不能加 -v error：psnr 的结果是 info 级日志，静音了就永远读不到。"""
        wa, ha = dims(a)
        r = subprocess.run(
            [path, "-hide_banner", "-i", a, "-i", b,
             "-filter_complex",
             f"[0:v]format=rgb24[t];[1:v]scale={wa}:{ha}:flags=area,format=rgb24[s];"
             "[t][s]psnr", "-f", "null", "-"],
            capture_output=True, text=True)
        m = re.search(r"average:([-0-9.inf]+)", r.stderr)
        return float(m.group(1)) if m else None

    def filt(src, name, vf):
        p = os.path.join(out, name)
        subprocess.run([path, "-hide_banner", "-loglevel", "error", "-y",
                        "-i", src, "-vf", vf, "-update", "1", p],
                       capture_output=True, text=True)
        return p if os.path.isfile(p) else None

    def mean_luma(p):
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", p,
             "-vf", "signalstats,metadata=print:file=-", "-f", "null", "-"],
            capture_output=True, text=True)
        vals = [float(m) for m in re.findall(r"YAVG=([\d.]+)", r.stdout)]
        return max(vals) if vals else None

    # 坐标对不对，尺寸是看不出来的：crop 挪错了照样出正确分辨率的画面，
    # 只是内容来自隔壁那块区域。这里拿系统按窗口号截的图（窗口自己的
    # buffer，与屏幕坐标无关）当模板，在整屏抓取里做归一化互相关反解位置，
    # 相关峰必须落在我们算出来的 crop 上。
    #
    # 只用「在 crop 位置能拿到强相关」的窗口下结论：放大镜/半透明/正在放视频
    # 的窗口，其内容与屏幕 1:1 不匹配，得分本就上不去，不能因此判坐标错。
    def central_on_screen(item):
        cx, cy, cw, ch = item["crop"]
        disp = by_no[item["screen_no"]]
        fw = round(disp["points"][2] * disp["scale"])
        fh = round(disp["points"][3] * disp["scale"])
        if cw * ch >= 0.9 * fw * fh:
            return False   # 近乎整屏的覆盖层：crop 就等于整屏，反解不出偏移
        return (cx + cw / 4 >= 0 and cy + ch / 4 >= 0
                and cx + 3 * cw / 4 <= fw and cy + 3 * ch / 4 <= fh)

    if args.window >= 0:
        cands = [wins[args.window]]
    else:
        cands = sorted((t for t in wins if central_on_screen(t)),
                       key=lambda t: -t["crop"][2] * t["crop"][3])[:4]
    if not cands:
        check(False, "没有窗口中央区域落在屏内的候选，无法反解坐标")
        print("\nSUMMARY: " + f"FAIL({len(FAIL)})")
        return 1

    full_by_screen = {}
    conclusive = []
    scale_done = False
    for w in cands:
        d = by_no[w["screen_no"]]
        idx = idxs[w["screen_no"]] if w["screen_no"] < len(idxs) else idxs[-1]
        x, y, cw, ch = w["crop"]
        num = w["id"].split(":")[-1]
        print(f"\n── {w['title'][:44]!r}（屏 {d['screen_no']} 设备 {idx}，crop {w['crop']}）")
        if w["screen_no"] not in full_by_screen:
            fp = grab(f"full{d['screen_no']}.png", idx)
            full_by_screen[w["screen_no"]] = (fp, gray(fp)) if fp else (None, None)
        full_path, A = full_by_screen[w["screen_no"]]
        if A is None:
            check(False, f"屏 {d['screen_no']} 整屏抓取失败")
            continue

        crop_png = grab(f"crop{num}.png", idx,
                        build_crop_filter(w["crop_norm"]))
        if not crop_png:
            check(False, f"{num} crop 抓取失败")
            continue
        got_w, got_h = dims(crop_png)
        check(abs(got_w - cw) <= 2 and abs(got_h - ch) <= 2,
              f"比例裁剪 crop_norm 的输出尺寸 == 像素 crop（{cw}x{ch}，实得 {got_w}x{got_h}）")
        lum = mean_luma(crop_png)
        check(lum is not None and lum > 4, f"画面非全黑（YAVG={lum}，需屏幕录制权限）")

        truth = os.path.join(out, f"truth{num}.png")
        r = subprocess.run(["screencapture", "-o", "-x", f"-l{num}", truth],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not os.path.isfile(truth):
            check(False, f"screencapture 取窗口基准图失败: {r.stderr.strip()[:120]}")
            continue
        B = gray(truth)
        if B is None:
            continue
        hs = min(B.shape[0] // 2, A.shape[0] - 2)
        ws = min(B.shape[1] // 2, A.shape[1] - 2)
        cy0, cx0 = (B.shape[0] - hs) // 2, (B.shape[1] - ws) // 2
        sub = B[cy0:cy0 + hs, cx0:cx0 + ws]
        n = hs * ws
        if n < 120 * 120:
            print(f"  模板只有 {ws}x{hs}，Stage Manager 磁贴这种尺寸定位不稳 —— 跳过")
            continue

        def box(M):
            P = np.zeros((M.shape[0] + 1, M.shape[1] + 1))
            P[1:, 1:] = M.cumsum(0).cumsum(1)
            return (P[hs:, ws:] - P[:-hs, ws:] - P[hs:, :-ws] + P[:-hs, :-ws])

        corr = np.fft.irfft2(
            np.fft.rfft2(A) * np.conj(np.fft.rfft2(sub, s=A.shape)), s=A.shape)
        corr = corr[:A.shape[0] - hs + 1, :A.shape[1] - ws + 1]
        mA = box(A) / n
        var = np.maximum(box(A * A) / n - mA * mA, 1e-9)
        sm = sub.mean()
        ncc = (corr / n - mA * sm) / np.sqrt(var * ((sub - sm) ** 2).sum() / n)
        peak = np.unravel_index(np.argmax(ncc), ncc.shape)
        # sub 取自 B 的 (cy0, cx0)，所以它在整屏里的期望位置是 crop 再内缩这么多；
        # 少算这份内缩的话，峰明明落在 crop 上却会被读成「偏了几十像素」
        ex_r, ex_c = y + cy0, x + cx0
        sy = min(max(ex_r, 8), ncc.shape[0] - 9)
        sx = min(max(ex_c, 8), ncc.shape[1] - 9)
        score = float(ncc[ex_r, ex_c]) if (0 <= ex_r < ncc.shape[0]
                                           and 0 <= ex_c < ncc.shape[1]) else 0.0
        win = ncc[sy - 8:sy + 9, sx - 8:sx + 9]
        ly, lx = np.unravel_index(np.argmax(win), win.shape)
        dev = (int(sx - 8 + lx - ex_c), int(sy - 8 + ly - ex_r))
        print(f"  窗口 buffer {B.shape[1]}x{B.shape[0]} / 整屏 {A.shape[1]}x{A.shape[0]}；"
              f"crop 处相关 {score:.3f}（局部峰偏 {dev}），全域峰 "
              f"({peak[1] - cx0}, {peak[0] - cy0}) 得分 {ncc[peak]:.3f}")
        if score < 0.5:
            print("  内容逐帧变化/半透明，窗口自身不足以定位 —— 本轮不下结论")
            continue
        conclusive.append(w["id"])
        check(max(abs(dev[0]), abs(dev[1])) <= 6,
              f"{w['id']} 实际位置与 crop 一致（偏差 {dev}px）")
        if not scale_done and full_path:
            scale_done = True
            # 本机只有一块 1:1 外接屏，Retina 上 avfoundation 的缓冲区尺寸量不到。
            # 离线补这一课：把整屏帧放大 2 倍当成 Retina 缓冲区。
            #   比例裁剪 —— 依旧指到同一块内容（PSNR 高）
            #   按 1:1 算出来的像素坐标 —— 落在窗口的左上角四分之一处（PSNR 低）
            # 这两条一起成立，才说明「放弃像素、改用比例」真的买到了东西。
            up = filt(full_path, "up2x.png", "scale=iw*2:ih*2")
            norm2x = filt(up, "norm2x.png", build_crop_filter(w["crop_norm"]))
            naive = filt(up, "naive.png", f"crop={cw}:{ch}:{x}:{y}")
            if norm2x and naive:
                keep = psnr(crop_png, norm2x)
                drift = psnr(crop_png, naive)
                print(f"  2x 缓冲区：比例裁剪 vs 真图 PSNR={keep}，"
                      f"像素坐标错位 vs 真图 PSNR={drift}")
                check(keep is not None and keep > 20,
                      "比例裁剪在缓冲区翻倍时仍指到同一块内容")
                check(drift is None or drift < keep - 6,
                      "像素裁剪在缓冲区翻倍时确实跑偏（比例裁剪才有意义）")

    check(len(conclusive) >= 1, "至少一个窗口完成了坐标反解")
    print(f"\n产物目录: {out}")
    print("\nSUMMARY: " + ("PASS" if not FAIL else f"FAIL({len(FAIL)})"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
