#!/bin/bash
# 用法: tools/install-macos-artifact.sh <version> <dmg 路径>
# 例:   tools/install-macos-artifact.sh 2.3.1 /tmp/HanCast-2.3.1-macos-arm64.dmg
#
# 装的是 CI 产物本身，不做任何补丁（重签/替换文件都不许）——装完直接跑
# tools/accept-macos-artifact.sh 验收。
#
# 全程不提权：/Applications 属 root:admin 且组可写，当前用户在 admin 组就能直接装。
# （2026-10-14 之前这里走 osascript 弹管理员授权框，根因是最早的手搓脚本多写了一行
#   `chown -R root:wheel`，把包做成只有 root 能动，于是每轮装包、每轮清理都要弹框。
#   那行已删，遗留归属也已一次性 chown 回当前用户。）
set -euo pipefail

VER="${1:?用法: $0 <version> <dmg>}"
DMG="${2:?用法: $0 <version> <dmg>}"
APP=/Applications/HanCast.app

[ -f "$DMG" ] || { echo "FAIL dmg 不存在: $DMG"; exit 1; }

MNT=$(hdiutil attach -nobrowse -readonly "$DMG" | awk '/\/Volumes\//{print $NF}' | head -1)
[ -n "$MNT" ] || { echo "FAIL 挂载 dmg 失败"; exit 1; }
trap 'hdiutil detach "$MNT" -quiet 2>/dev/null || true' EXIT

SRC=$(find "$MNT" -maxdepth 2 -name '*.app' -print -quit)
[ -n "$SRC" ] || { echo "FAIL dmg 里找不到 .app（${MNT}）"; exit 1; }

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

# 回滚备份只保留最近一份：/tmp 的系统清扫只删「atime/mtime/ctime 全 >3 天」的普通文件，
# .app 这种目录包要分多轮才啃得动，等于不回收（2026-10-15 实测：一份 184M 的回滚包
# 在「系统会自行清扫」的错误注释下躺满一天原样）。每次装包前先把历轮遗留的清掉。
for old in /tmp/HanCast_pre_*.app; do
  [ -e "$old" ] || continue
  [ "$old" = "$ASIDE" ] && continue
  rm -rf "$old" && echo "清除历轮回滚备份: $old"
done

if [ -e "$APP" ] && [ ! -w "$APP" ]; then
  echo "FAIL $APP 当前用户不可写（root:wheel 遗留）。一次性修好后再来：sudo chown -R $(id -un) \"$APP\""
  exit 1
fi

[ ! -d "$APP" ] || mv "$APP" "$ASIDE"
ditto "$SRC" "$APP"

OUT=$(plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist")
OWNER=$(stat -f '%Su:%Sg' "$APP")
# $ASIDE 是本次唯一回滚备份，下次装包时会被上面的循环清掉，别把它当长期备份
echo "已安装: ${OUT}（原 $OLD_VER 移到 ${ASIDE}，归属 ${OWNER}）"
[ "$OUT" = "$VER" ] || { echo "FAIL 安装后版本不符"; exit 1; }
[ "$OWNER" = "$(id -un):staff" ] || echo "WARN 归属不是 $(id -un):staff，下次装包会要管理员授权"
codesign -dvvv "$APP" 2>&1 | grep -E '^Identifier=|linker-signed' | head -2
