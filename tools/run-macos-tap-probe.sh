#!/bin/bash
# 把 tools/probe-macos-systap.py 装进一个最小的 .app 再跑。
#
# 为什么要 .app：CoreAudio 的「音频采集」是 TCC 权限，判给的是**负责进程**
# （responsible process）的 bundle，而且要求 bundle 的 Info.plist 里有
# NSAudioCaptureUsageDescription。裸 python 既没有 bundle 也没有这句话，
# tccd 不会弹窗、也不会拒绝报错——AudioHardwareCreateProcessTap 照样返回
# noErr，但环回永远不产出数据，聚合设备上一条流都建不出来。
# HanCast 实际运行时 sidecar 是 App 拉起的，负责进程就是 HanCast.app，
# 所以这里用同一个 bundle 身份来复现产品路径。
#
# 用法：bash tools/run-macos-tap-probe.sh [ffmpeg 路径]
# 输出：探针的完整 stdout/stderr（GUI app 没有终端，落到日志文件再打印）
set -euo pipefail
cd "$(dirname "$0")/.."

PY="$PWD/hancast-backend/.venv/bin/python"
FFMPEG="${1:-$(command -v ffmpeg)}"
LOG="${TMPDIR:-/tmp}/hancast-tap-probe.log"
APP="${TMPDIR:-/tmp}/HanCast Tap Probe.app"
BUNDLE_ID="${HANCAST_TAP_BUNDLE_ID:-com.hancast.tapprobe}"
[[ -x "$PY" ]] || { echo "找不到 $PY"; exit 2; }

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>run</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleName</key><string>HanCast Tap Probe</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAudioCaptureUsageDescription</key>
  <string>HanCast 需要读取其他程序正在播放的声音，才能把系统声音投屏到电视。</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>HanCast 需要麦克风才能把解说一起投屏。</string>
</dict>
</plist>
EOF
cat > "$APP/Contents/MacOS/run" <<EOF
#!/bin/zsh
# GUI app 通过 launchd 起，终端的环境变量和 argv 都传不进去，只能写死
exec '$PY' '$PWD/tools/probe-macos-systap.py' '$FFMPEG' > '$LOG' 2>&1
EOF
chmod +x "$APP/Contents/MacOS/run"
codesign --force --sign - --timestamp=none "$APP" >/dev/null

rm -f "$LOG"
# 首次运行会弹「音频采集」授权；没人点的话这里最多等 90 秒，别让脚本挂住
open -W "$APP" &
wait_pid=$!
for _ in $(seq 1 90); do
  kill -0 "$wait_pid" 2>/dev/null || break
  sleep 1
done
if kill -0 "$wait_pid" 2>/dev/null; then
  echo "仍在运行：大概率在等「音频采集」授权弹窗，请在弹窗里点允许"
fi
wait "$wait_pid" 2>/dev/null || true
cat "$LOG"
grep -q "SUMMARY: PASS" "$LOG" && echo "→ 打包 bundle 身份下环回可用"
