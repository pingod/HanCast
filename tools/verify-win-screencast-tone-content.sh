#!/bin/bash
# 用法: tools/verify-win-screencast-tone-content.sh [phase-1输出] [系统声音名字正则] [麦克风名字正则]
#       默认在 .68 上自选：系统声音 = 枚举里标着「当前出声」（default）的那块环回，
#       麦克风 = '麦克风 .*Realtek'；不给 phase-1 输出就自己跑一轮 list_screen_sources 枚举。
#       为什么系统声音不再按名字硬挑：装了虚拟音效设备（FxSound 等）的机器上，
#       默认渲染端点根本不是 Realtek，按 'Realtek.*Loopback' 挑中的是静音镜像，
#       分片里只剩补出来的数字静音，会被读成「系统声音没采到」。
#       MIC=off → 不接麦克风，只起「单独系统声音」那一路（判据最干净：分片里任何
#       3.5kHz 能量都只能来自系统声音，不必再跟麦克风底噪争辩）。
#       TONE=off → 静音对照，用来量同一台机器、同一副麦克风、同一路投屏的地板。
#
# 回答协议判据答不了的问题：start 返回 ok、status 返回 running、退出码 0，全都只说明
# 「FFmpeg 还活着」；声音到底有没有进 HLS 分片，只能把分片拿回来量。
# 做法：让 .68 循环播放「440Hz 与 3500Hz 同时发声」的提示音，用产品协议起一路投屏，
# 从产品自己的 HTTP 通道（http://<host>:8765/hls/<会话>/）拉回两段中途分片，
# 用打包内同一份 ffmpeg 分别做 420–460Hz / 3300–3700Hz 带通 + volumedetect：
#   3.5kHz 带通有能量 → 提示音真被混进了输出，系统声音这条路通
#   3.5kHz 带通 = -91 dB（数字静音）→ 输出里一点提示音都没有
# 为什么是 3.5kHz：单频 440Hz 判不准，风扇与麦克风底噪的能量就堆在低频
#   （实测静音对照的 420–460Hz 带通已有 -35dB，跟放音时只差 3dB）。3.5kHz 这一段
#   麦克风给不出来，只可能来自被采集的系统声音流，判据才立得住；440Hz 那一路留着
#   当「低频参考」，两频段一高一低对比才能同时看出路由和增益是否正常。
# 两个频率要「同时」发声而不是前后拼接：分片只有几秒长，串播的话抓到哪段全看运气，
#   一段里没有 3.5kHz 会被读成「系统声音没进来」。
set -u
HOST="${HOST:-192.168.1.68}"
SSH="ssh -o ConnectTimeout=8 administrator@$HOST"
INSTALL='C:\Users\Administrator\AppData\Local\HanCast'
HLS='C:\Users\Administrator\AppData\Local\Temp\hancast_screencast_hls'
WORK='D:\Temp\hctone'
FFMPEG_BIN="${FFMPEG_BIN:-/Applications/HanCast.app/Contents/Resources/ffmpeg/ffmpeg}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
GEN="$REPO/tools/gen-win-screencast-live-req.py"
NOPROXY=(env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy
         -u ALL_PROXY -u all_proxy)
TMPD=$(mktemp -d /tmp/hctone.XXXXXX)
cleanup() {
  stop_tone >/dev/null 2>&1
  rm -rf "$TMPD"
}
trap cleanup EXIT
rssh() { $SSH "$*" 2>&1 | grep -v -i 'quantum\|store now\|upgraded' | tr -d '\r'; }
put() { "${NOPROXY[@]}" scp -q "$1" "administrator@$HOST:$2" 2>&1 |
        grep -v -i 'quantum\|store now\|upgraded'; }
get() { "${NOPROXY[@]}" scp -q "administrator@$HOST:$1" "$2" 2>&1 |
        grep -v -i 'quantum\|store now\|upgraded'; }

SYS_RX="${2:-default}"
MIC_RX="${3:-麦克风 .*Realtek}"
MIC="${MIC:-on}"
SYS="${SYS:-on}"
# TONE=off 用来测底噪对照：同样起一路投屏但不放提示音，带通能量就该回到静音地板
TONE="${TONE:-on}"
# 换频率就换文件名：播放器读的是绝对路径，旧文件（单频 440Hz）留在盘上既会误导
# 「HAVE 就不重建」，也随时可能被上一轮的孤儿播放器继续循环放出来污染对照组
TONEWAV="${TONEWAV:-tone-440x3500}"

# ── 1. 提示音 + 循环播放器（这台机没有系统 python，PowerShell SoundPlayer 走默认输出设备）
rssh "if not exist $WORK mkdir $WORK" >/dev/null
if ! rssh "if exist $WORK\\$TONEWAV.wav (echo HAVE)" | grep -q HAVE; then
  rssh "$INSTALL\\ffmpeg\\ffmpeg.exe -y -v error -f lavfi -i sine=frequency=440:duration=12 -f lavfi -i sine=frequency=3500:duration=12 -filter_complex [0:a][1:a]amix=inputs=2:duration=longest:normalize=0,volume=0.5 -ar 48000 -ac 2 $WORK\\$TONEWAV.wav" >/dev/null
  rssh "if exist $WORK\\$TONEWAV.wav (echo HAVE)" | grep -q HAVE ||
    { echo "FAIL 提示音没生成"; exit 2; }
fi
PSQ="\$PID | Out-File -Encoding ascii '$WORK\\pid.txt'; while (-not (Test-Path '$WORK\\stop')) { \$p = New-Object Media.SoundPlayer '$WORK\\$TONEWAV.wav'; \$p.PlaySync(); \$p.Dispose() }"
printf '@echo off\r\ndel %s\\stop >nul 2>&1\r\npowershell -NoProfile -Command "%s"\r\n' \
  "$WORK" "$PSQ" > "$TMPD/tone.cmd"
put "$TMPD/tone.cmd" D:/Temp/hctone.cmd >/dev/null
rssh "schtasks /create /f /tn HANCAST_TONE /tr D:\\Temp\\hctone.cmd /sc once /sd 2999-01-01 /st 00:00 /ru Administrator /it" >/dev/null

# 停播放器：不能只杀 pid.txt 里那一个——schtasks /end 只结束任务实例，播放器
# powershell 会作为孤儿继续循环放音，上一轮的孤儿会把这一轮「静音对照组」也染上声音。
# 所以按命令行特征清理：凡是命令行里带 tone.wav 的 powershell 全部杀掉
# （模式串用拼接写，免得这个脚本自己匹配自己）。
cat > "$TMPD/kill.ps1" <<'PS1'
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -match ('tone' + '\.wav') } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
PS1
perl -pe 's/\r?\n/\r\n/' "$TMPD/kill.ps1" > "$TMPD/kill.win.ps1"
put "$TMPD/kill.win.ps1" D:/Temp/hctone/kill.ps1 >/dev/null
stop_tone() {
  rssh "echo stop > $WORK\\stop" >/dev/null
  rssh "powershell -NoProfile -ExecutionPolicy Bypass -File D:\\Temp\\hctone\\kill.ps1" >/dev/null
}
stop_tone   # 先清掉历史遗留的播放器

# ── 2. 设备清单（phase-1 输出没给就自己跑一轮）
PHASE1="${1:-}"
if [ -z "$PHASE1" ]; then
  python3 "$GEN" phase1 -o "$TMPD/req1.txt" >/dev/null
  rssh "rmdir /s /q D:\\Temp\\hcsl >nul 2>&1 & mkdir D:\\Temp\\hcsl" >/dev/null
  put "$TMPD/req1.txt" D:/Temp/hcsl/req.txt >/dev/null
  rssh "schtasks /run /tn HANCAST_HCSL" >/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sleep 5
    get D:/Temp/hcsl/out.txt "$TMPD/phase1.txt" >/dev/null
    grep -q audio_inputs "$TMPD/phase1.txt" 2>/dev/null && break
  done
  PHASE1="$TMPD/phase1.txt"
fi
[ -s "$PHASE1" ] || { echo "FAIL 没有设备枚举输出"; exit 2; }

# ── 3. 起投屏：MIC=off 只接系统声音，SYS=off 只接麦克风，否则两路都接；8 次状态 ≈ 24 秒驻留
REQ_GEN=(python3 "$GEN" req "$PHASE1" --dwell 8)
[ "$SYS" = off ] || REQ_GEN+=(--sys-match "$SYS_RX")
[ "$MIC" = off ] || REQ_GEN+=(--mic-match "$MIC_RX")
"${REQ_GEN[@]}" -o "$TMPD/req.txt" || { echo "FAIL 请求生成"; exit 2; }
put "$TMPD/req.txt" D:/Temp/hcsl/req.txt >/dev/null
if [ "$TONE" = on ]; then
  rssh "schtasks /run /tn HANCAST_TONE" >/dev/null
else
  stop_tone
fi
sleep 3
rssh "schtasks /run /tn HANCAST_HCSL" >/dev/null

# ── 4. 会话一起来就抓两段中途分片（stop 会连目录一起删，只能边跑边取）
sid=""
for _ in $(seq 1 15); do
  sleep 3
  sid=$(rssh "dir /b /o:d $HLS 2>nul" | tail -1)
  [ -n "$sid" ] && break
done
[ -n "$sid" ] || { echo "FAIL 没等到会话目录"; exit 3; }
echo "SID=$sid"
sleep 8
for n in 1 2; do
  seg=$(rssh "type %TEMP%\\hancast_screencast_hls\\$sid\\live.m3u8 2>nul" |
        grep -o 'seg[0-9]*\.ts' | tail -1)
  if [ -n "$seg" ]; then
    dst="$TMPD/${sid}_${n}_${seg}"
    "${NOPROXY[@]}" curl -s --noproxy '*' --max-time 8 -o "$dst" \
      "http://$HOST:8765/hls/$sid/$seg"
    echo "GOT $seg $(wc -c < "$dst") bytes"
  else
    echo "FAIL 播放列表里没有分片"
  fi
  sleep 4
done
stop_tone
sleep 2
get D:/Temp/hcsl/out.txt "$TMPD/out.txt" >/dev/null
grep -o 'SIDECAR_EXIT=[0-9-]*' "$TMPD/out.txt" 2>/dev/null
grep -o 'loopback feeder[^"]*' "$TMPD/out.txt" 2>/dev/null | head -3

# ── 5. 带通音量：提示音到底进没进输出（3.5kHz 是判据，440Hz 只做低频参考）
for f in "$TMPD"/${sid}_*_*seg*.ts; do
  [ -e "$f" ] || continue
  lo=$("$FFMPEG_BIN" -hide_banner -nostats -i "$f" -af \
        "highpass=f=420,lowpass=f=460,volumedetect" -f null - 2>&1 |
        grep -oE 'mean_volume: [-0-9.]+ dB' | head -1)
  hi=$("$FFMPEG_BIN" -hide_banner -nostats -i "$f" -af \
        "highpass=f=3300,lowpass=f=3700,volumedetect" -f null - 2>&1 |
        grep -oE '(mean|max)_volume: [-0-9.]+ dB' | tr '\n' ' ')
  full=$("$FFMPEG_BIN" -hide_banner -nostats -i "$f" -af volumedetect -f null - 2>&1 |
        grep -oE 'mean_volume: [-0-9.]+ dB')
  echo "$(basename "$f") 440[$lo] 3500[$hi] 全带[$full]"
done
