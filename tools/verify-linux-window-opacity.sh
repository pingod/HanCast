#!/usr/bin/env bash
# Linux(Wayland + Hyprland) 窗口不透明判据。
#
# 测的是**像素**而不是「窗口在不在」：光标扫过界面之后，窗口矩形里不该有任何一行
# 与身后的裸桌面一致。一致 = 那块 surface 从没提交过、alpha 保持 0，合成器把桌面
# 透出来（用户表述：「鼠标晃一晃就出现透明洞」）。机制与取证记录见
# docs/故障排查.md §8.4 与 docs/构建与发布.md §6.7。
#
# 为什么不用环境变量组合去救：v2.3.17 起 Linux 在 Wayland 下必须带
# WEBKIT_DISABLE_DMABUF_RENDERER=1（否则启动即被合成器踢掉连接，§10.6），
# 而关掉 dmabuf 之后透明窗口就会漏提交 damage。上游 tauri#14924 的结论是
# 「只有不透明窗口免疫」，所以判据只有一条：**扫完不该有透明行**。
#
# 用法：
#   tools/verify-linux-window-opacity.sh              # 起 $HC_BIN，扫，量，收尾开回来
#   tools/verify-linux-window-opacity.sh --sweeps 5   # 多扫几轮（默认 3）
#   tools/verify-linux-window-opacity.sh --max-pct 2  # 放宽阈值（默认 0%）
#   tools/verify-linux-window-opacity.sh --workspace auto  # 挪到一个空 workspace 再量
#   tools/verify-linux-window-opacity.sh --no-relaunch
#   HC_BIN=… HC_CLASS=… THRESHOLD=… tools/verify-linux-window-opacity.sh
#
# 退出码：0 = 透明行占比在 --max-pct 内；1 = 超了；2 = 环境不满足。
#
# ⚠️ 它会**真的启动/杀掉目标 GUI**（默认结束时再开回来）：跑之前确认这台机器空闲、
#    用户没在操作该窗口。窗口身后必须是裸桌面——脚本会在每次抓背景前检查同一
#    workspace 上没有别的窗口压到这块矩形，有就 exit 2，否则会拿「别的窗口」当背景，
#    把洞量成 PASS。桌面正被占用时用 `--workspace N` 挑一个空工作区。
set -uo pipefail

HC_BIN="${HC_BIN:-/usr/bin/HanCast}"
HC_CLASS="${HC_CLASS:-HanCast}"
SWEEPS=3
MAX_PCT=0
TARGET_WS=""
THRESHOLD="${THRESHOLD:-6.0}"   # 单行三通道平均绝对差小于它 = 与桌面一致
RELAUNCH=1
WORK="$(mktemp -d "${TMPDIR:-/tmp}/hc-opacity.XXXXXX")"

cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

while [ $# -gt 0 ]; do
  case "$1" in
    --sweeps) SWEEPS="${2:?--sweeps 需要数值}"; shift ;;
    --max-pct) MAX_PCT="${2:?--max-pct 需要百分比}"; shift ;;
    --bin) HC_BIN="${2:?--bin 需要路径}"; shift ;;
    --class) HC_CLASS="${2:?--class 需要值}"; shift ;;
    --workspace) TARGET_WS="${2:?--workspace 需要工作区编号}"; shift ;;
    --no-relaunch) RELAUNCH=0 ;;
    -h|--help) sed -n '1,25p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
  shift
done

fail_env() { echo "环境不满足：$*" >&2; exit 2; }

UID_NUM="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$UID_NUM}"
# 环境变量必须在启动 GUI 之前导出：缺 WAYLAND_DISPLAY 时 tao 直接 panic
# `Failed to initialize gtk backend!`，看起来像「装完还是崩」。
[ -n "${WAYLAND_DISPLAY:-}" ] || for d in "$XDG_RUNTIME_DIR"/wayland-*; do
  [ -e "$d" ] && export WAYLAND_DISPLAY="$(basename "$d")" && break
done
[ -n "${WAYLAND_DISPLAY:-}" ] || fail_env "找不到 WAYLAND_DISPLAY（这台机器没在跑 Wayland 会话？）"
[ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ] || {
  hd="$XDG_RUNTIME_DIR/hypr"
  [ -d "$hd" ] && HYPRLAND_INSTANCE_SIGNATURE="$(ls "$hd" 2>/dev/null | head -1)" && export HYPRLAND_INSTANCE_SIGNATURE
}
[ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ] || fail_env "不是 Hyprland（本判据要用 hyprctl 摆光标）"

command -v hyprctl >/dev/null || fail_env "缺 hyprctl"
command -v grim >/dev/null || fail_env "缺 grim（Wayland 截图）"
command -v ffmpeg >/dev/null || fail_env "缺 ffmpeg（PNG → rawvideo rgba）"
command -v python3 >/dev/null || fail_env "缺 python3"

# 读自家窗口几何（含所在 workspace），并回答「同一 workspace 上有没有别的窗口压在这块矩形里」
# ——跨 workspace 的矩形重叠不算，背景必须看的是同一张工作区
hypr_state() {  # $1 = geometry | overlap，overlap 时另带 X Y W H WS
  hyprctl clients -j 2>/dev/null | HC_CLASS="$HC_CLASS" MODE="$1" \
    X="${2:-0}" Y="${3:-0}" W="${4:-0}" HH="${5:-0}" WS="${6:-}" python3 -c '
import json, os, sys
want, mode = os.environ["HC_CLASS"], os.environ["MODE"]
clients = json.load(sys.stdin)
if mode == "geometry":
    for c in clients:
        if (c.get("class") or "") == want:
            a, s = c["at"], c["size"]
            if s[0] > 1 and s[1] > 1:
                print(a[0], a[1], s[0], s[1], c["workspace"]["id"]); sys.exit(0)
    sys.exit(1)
x, y, w, h, ws = (int(os.environ[k]) for k in ("X", "Y", "W", "HH", "WS"))
for c in clients:
    if (c.get("class") or "") == want or c["workspace"]["id"] != ws:
        continue
    a, s = c["at"], c["size"]
    if a[0] < x + w and a[0] + s[0] > x and a[1] < y + h and a[1] + s[1] > y:
        print("ws%s %s %s" % (ws, c.get("class"), (c.get("title") or "")[:40])); sys.exit(0)
sys.exit(1)
'
}

launch() {  # $1 = 日志后缀
  setsid nohup "$HC_BIN" >"$WORK/app-$1.log" 2>&1 </dev/null &
  sleep 9
}
stop_app() { pkill -TERM -f "^$HC_BIN\$" 2>/dev/null; sleep 1.5; }

move_cursor() { hyprctl dispatch "hl.dsp.cursor.move({x=$1, y=$2})" >/dev/null 2>&1; }
sweep() {
  # 蛇形扫过整个客户区：洞是「damage 矩形漏提交」，只有让光标划过才复现得出来
  for row in 1 2 3 4 5 6; do
    yy=$((Y + 20 + (H - 40) * (row - 1) / 5))
    for i in $(seq 0 20); do
      xx=$((X + 20 + (W - 40) * i / 20))
      [ $((row % 2)) -eq 0 ] && xx=$((X + 20 + (W - 40) * (20 - i) / 20))
      move_cursor "$xx" "$yy"
      sleep 0.05
    done
  done
  sleep 0.8
}

measure() {  # $1 = 轮次，打印与裸桌面一致的行数
  ffmpeg -loglevel error -y -i "$WORK/fg-$1.png" -f rawvideo -pix_fmt rgba "$WORK/fg-$1.raw" &&
  ffmpeg -loglevel error -y -i "$WORK/bg-$1.png" -f rawvideo -pix_fmt rgba "$WORK/bg-$1.raw" &&
  TH="$THRESHOLD" W="$W" H="$H" python3 -c '
import os
w, h, th = int(os.environ["W"]), int(os.environ["H"]), float(os.environ["TH"])
fg = open(os.environ["WORK_FG"], "rb").read()
bg = open(os.environ["WORK_BG"], "rb").read()
bad = 0
for y in range(h):
    o, d = y * w * 4, 0
    for x in range(0, w, 2):
        i = o + x * 4
        d += abs(fg[i] - bg[i]) + abs(fg[i + 1] - bg[i + 1]) + abs(fg[i + 2] - bg[i + 2])
    if d / ((w // 2) * 3) < th:
        bad += 1
print(bad)
' || return 1
}

read_geom() {  # 把自家窗口的 X/Y/W/H/WS 读进全局变量
  local g
  g="$(hypr_state geometry)" || return 1
  set -- $g; X=$1; Y=$2; W=$3; H=$4; WS=$5
}
# 桌面忙（别的窗口压在矩形里）时，**切到**一个空 workspace 再启动应用，让窗口自己落在那儿。
# 绝对不要用 hl.dsp.window.move：它作用于「当前焦点窗口」，而焦点未必是刚映射的自家窗口，
# 实测会把他人的终端/编辑器整窗口挪走（v2.3.18 取证时就误挪过一次），换 workspace 才是安全的做法。
ORIG_WS="$(hyprctl activeworkspace -j 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')" 2>/dev/null || ORIG_WS=""
if [ "$TARGET_WS" = auto ]; then
  # 现存最大编号 + 1 —— 按定义是空工作区
  TARGET_WS="$(hyprctl workspaces -j 2>/dev/null | python3 -c 'import json,sys
ids=[w["id"] for w in json.load(sys.stdin) if isinstance(w.get("id"), int)]
print((max(ids) if ids else 1) + 1)')"
fi
if [ -n "$TARGET_WS" ]; then
  hyprctl dispatch "hl.dsp.focus({ workspace = $TARGET_WS })" >/dev/null 2>&1
  sleep 1.5
  echo "切到 workspace $TARGET_WS（原 $ORIG_WS）"
fi
launch first
if ! read_geom; then
  echo "启动后没有窗口。日志尾部：" >&2
  tail -5 "$WORK/app-first.log" >&2
  fail_env "class=$HC_CLASS 未映射"
fi
[ -z "$TARGET_WS" ] || [ "$WS" = "$TARGET_WS" ] ||
  fail_env "窗口落在 workspace $WS 而不是 $TARGET_WS，背景不干净（手动把工作区切过去再跑）"
echo "窗口 $HC_CLASS @ $X,$Y ${W}x${H}（ws $WS，sweeps=$SWEEPS，上限 ${MAX_PCT}%）"

worst=0
for run in $(seq 1 "$SWEEPS"); do
  sweep
  grim -g "$X,$Y ${W}x${H}" "$WORK/fg-$run.png" || fail_env "grim 抓窗口失败"
  stop_app
  if blocked="$(hypr_state overlap "$X" "$Y" "$W" "$H" "$WS" 2>/dev/null)"; then
    fail_env "workspace $WS 上「$blocked」压着这块矩形，背景不是裸桌面（用 --workspace N 换到空工作区再量）"
  fi
  grim -g "$X,$Y ${W}x${H}" "$WORK/bg-$run.png" || fail_env "grim 抓裸桌面失败"
  bad="$(WORK_FG="$WORK/fg-$run.raw" WORK_BG="$WORK/bg-$run.raw" measure "$run")" || fail_env "逐行比对失败"
  pct=$((bad * 100 / H))
  [ "$pct" -gt "$worst" ] && worst=$pct
  echo "  第 $run 轮：窗口 ${W}x${H}  透明行 $bad/$H = ${pct}%"
  [ "$run" = "$SWEEPS" ] && break
  launch "$run"
  read_geom || fail_env "第 $run 轮后窗口没回来"
  [ -z "$TARGET_WS" ] || [ "$WS" = "$TARGET_WS" ] ||
    fail_env "第 $run 轮后窗口落在 workspace $WS 而不是 $TARGET_WS"
done

[ "$RELAUNCH" = 1 ] && launch last
# 用过空 workspace 就把他的视图还回原工作区（只切视图，不挪任何人的窗口）
if [ -n "$TARGET_WS" ] && [ -n "$ORIG_WS" ] && [ "$ORIG_WS" != "$TARGET_WS" ]; then
  hyprctl dispatch "hl.dsp.focus({ workspace = $ORIG_WS })" >/dev/null 2>&1
fi

if [ "$worst" -le "$MAX_PCT" ]; then
  echo "PASS：光标扫过之后没有一行露出身后桌面（最差 ${worst}% ≤ ${MAX_PCT}%）"
  exit 0
fi
echo "FAIL：最差一轮 ${worst}% 的行与裸桌面一致 = 窗口有透明洞（上限 ${MAX_PCT}%）"
exit 1
