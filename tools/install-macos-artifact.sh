#!/bin/bash
# 用法: tools/install-macos-artifact.sh <version> <dmg 路径>
# 例:   tools/install-macos-artifact.sh 2.3.1 /tmp/HanCast-2.3.1-macos-arm64.dmg
#
# 装的是 CI 产物本身，不做任何补丁（重签/替换文件都不许）——装完直接跑
# tools/accept-macos-artifact.sh 验收。
# 本机 sudo 要密码，所以整个提权过程打包成一条 osascript，只弹一次授权框。
set -euo pipefail

VER="${1:?用法: $0 <version> <dmg>}"
DMG="${2:?用法: $0 <version> <dmg>}"
APP=/Applications/HanCast.app

[ -f "$DMG" ] || { echo "FAIL dmg 不存在: $DMG"; exit 1; }

MNT=$(hdiutil attach -nobrowse -readonly "$DMG" | awk '/\/Volumes\//{print $NF}' | head -1)
[ -n "$MNT" ] || { echo "FAIL 挂载 dmg 失败"; exit 1; }
trap 'hdiutil detach "$MNT" -quiet 2>/dev/null || true' EXIT

SRC=$(find "$MNT" -maxdepth 2 -name '*.app' -print -quit)
[ -n "$SRC" ] || { echo "FAIL dmg 里找不到 .app（$MNT）"; exit 1; }

SRC_VER=$(plutil -extract CFBundleShortVersionString raw "$SRC/Contents/Info.plist")
[ "$SRC_VER" = "$VER" ] || { echo "FAIL dmg 内版本 $SRC_VER ≠ 要求的 $VER"; exit 1; }

# 旧 sidecar 会一直占着 stdio 与端口，验收脚本见到它就拒绝继续；
# 这里也不能 pkill（会留下没有子进程的空壳 GUI），只能等它自己退。
osascript -e 'tell application "HanCast" to quit' 2>/dev/null || true
for _ in $(seq 1 12); do
  pgrep -x hancast-sidecar >/dev/null 2>&1 || break
  sleep 2
done
if pgrep -x hancast-sidecar >/dev/null 2>&1; then
  echo "FAIL hancast-sidecar 仍未退出，中止（别强杀）"; exit 1
fi

OLD_VER=$(plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist" 2>/dev/null || echo none)
ASIDE="/tmp/HanCast_pre_${OLD_VER//./}_$(date +%H%M%S).app"

SCRIPT="/tmp/hancast-install-$$.sh"
cat >"$SCRIPT" <<EOF
set -e
[ ! -d "$APP" ] || mv "$APP" "$ASIDE"
ditto "$SRC" "$APP"
chown -R root:wheel "$APP"
plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist"
EOF
chmod +x "$SCRIPT"
# 空脚本也会「授权成功」，装完还是旧版——必须在弹框前就挡住
grep -q ditto "$SCRIPT" || { echo "FAIL 生成的提权脚本不完整: $SCRIPT"; exit 1; }

OUT=$(osascript -e "do shell script \"/bin/bash $SCRIPT\" with administrator privileges with prompt \"安装 HanCast $VER\"" | tr -d '\r')
rm -f "$SCRIPT"

echo "已安装: $OUT（原 $OLD_VER 移到 $ASIDE）"
[ "$OUT" = "$VER" ] || { echo "FAIL 安装后版本不符"; exit 1; }
codesign -dvvv "$APP" 2>&1 | grep -E '^Identifier=|linker-signed' | head -2
