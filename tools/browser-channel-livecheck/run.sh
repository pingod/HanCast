#!/usr/bin/env bash
# 浏览器通道（host.render / host.sniff / WebView Cookie）真机验收。
#
# 干的事：把验收包放进应用的规则目录 → 起一个假站点（播放地址只由页面 JS 现挂，
# HTTP 抓页永远看不到）→ 等你把地址粘进 HanCast 点「粘贴」→ 从应用日志里读判据。
#
#   用法:  bash tools/browser-channel-livecheck/run.sh [--port 8099] [--host 192.168.1.7]
#          [--wait 180] [--no-serve] [--keep]
#   --host 追加一个主机名进包的 hosts（在另一台机器上验收时要加，比如 Windows 测试机
#          要访问本机的局域网地址）
#   --no-serve 不自起假站点（已经在别处跑着 serve.py 时用）
#   --keep 验收完保留规则包目录（默认删干净，不在用户机器上留测试包）
#
# 退出码 0 = 四条判据全中；1 = 等超时或判据不齐（日志里能看到卡在哪一段）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACK=hancast-browser-livecheck
PORT=8099
EXTRA_HOST=""
WAIT=180
NO_SERVE=0
KEEP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --host) EXTRA_HOST="$2"; shift 2 ;;
    --wait) WAIT="$2"; shift 2 ;;
    --no-serve) NO_SERVE=1; shift ;;
    --keep) KEEP=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

case "$(uname -s)" in
  Darwin) RULES="$HOME/Library/Application Support/HanCast/share_rules/plugins"
          LOG="$HOME/Library/Application Support/HanCast/logs/hancast.log" ;;
  Linux)  RULES="$HOME/.config/hancast/share_rules/plugins"
          LOG="$HOME/.config/hancast/logs/hancast.log" ;;
  *) echo "本脚本面向 macOS/Linux；Windows 上按同样三步手工做" >&2; exit 2 ;;
esac
PACK_DIR="$RULES/$PACK"

if [ ! -d "$RULES" ]; then
  echo "规则目录不存在: $RULES（应用没跑过？先启动一次 HanCast 再验）" >&2
  exit 2
fi

cleanup() {
  [ -n "${SRV_PID:-}" ] && kill "$SRV_PID" 2>/dev/null || true
  if [ "$KEEP" -ne 1 ] && [ "${INSTALLED:-0}" -eq 1 ] && [ -d "$PACK_DIR" ]; then
    # 只删本次自己建的那一个目录，别的路径一律不碰
    rm -rf "$PACK_DIR"
    echo "[livecheck] 已移除测试包 $PACK_DIR"
  fi
}
trap cleanup EXIT

mkdir -p "$PACK_DIR"
INSTALLED=1
cp "$HERE/pack/main.js" "$PACK_DIR/main.js"
cp "$HERE/pack/manifest.json" "$PACK_DIR/manifest.json"
if [ -n "$EXTRA_HOST" ]; then
  # hosts 写在规则里（manifest 只有包元数据），追加验收机要访问的主机名就改脚本
  EXTRA_HOST_lc=$(printf '%s' "$EXTRA_HOST" | tr 'A-Z' 'a-z')
  python3 - "$PACK_DIR/main.js" "$EXTRA_HOST_lc" <<'PY'
import sys
path, extra = sys.argv[1], sys.argv[2]
src = open(path, encoding="utf-8").read()
old = "hosts: ['127.0.0.1', 'localhost']"
if old not in src:
    raise SystemExit(f"{path}: 找不到 hosts 行，追加主机名失败")
open(path, "w", encoding="utf-8").write(
    src.replace(old, "hosts: ['127.0.0.1', 'localhost', '%s']" % extra))
PY
  echo "[livecheck] 包 hosts 追加: $EXTRA_HOST_lc"
fi
echo "[livecheck] 测试包就位: $PACK_DIR"

if [ "$NO_SERVE" -eq 0 ]; then
  PYBIN="$HERE/../../hancast-backend/.venv/bin/python"
  [ -x "$PYBIN" ] || PYBIN=python3
  "$PYBIN" "$HERE/serve.py" --port "$PORT" &
  SRV_PID=$!
  sleep 1
fi

OFFSET=$(wc -c <"$LOG" 2>/dev/null || echo 0)
echo "[livecheck] 把 http://127.0.0.1:$PORT/ 粘进 HanCast 主界面，点「粘贴」"
echo "[livecheck] 判据行（最多等 ${WAIT}s）："

waited=0
while [ "$waited" -lt "$WAIT" ]; do
  if [ -f "$LOG" ] && tail -c "+$((OFFSET + 1))" "$LOG" | grep -q "LIVECHECK"; then
    break
  fi
  sleep 2
  waited=$((waited + 2))
done

LINES=$(tail -c "+$((OFFSET + 1))" "$LOG" 2>/dev/null | grep "LIVECHECK" || true)
echo "$LINES"
if [ -z "$LINES" ]; then
  echo "[livecheck] FAIL：日志里没有 LIVECHECK 行——规则没被调用，或渲染一直超时" >&2
  exit 1
fi

pass=1
grep -q "sniff=.*master\.m3u8" <<<"$LINES" || { echo "[livecheck] FAIL：没嗅到 JS 现挂的播放地址" >&2; pass=0; }
grep -q "httponly=1" <<<"$LINES" || { echo "[livecheck] FAIL：WebView 的 HttpOnly Cookie 没搬进解析会话" >&2; pass=0; }
grep -q "uaMatchesHost=true" <<<"$LINES" || { echo "[livecheck] FAIL：WebView UA 与 host.userAgent 不同源" >&2; pass=0; }
grep -q "render title=HanCast 浏览器通道验收页" <<<"$LINES" || { echo "[livecheck] FAIL：没取回渲染后的标题/正文" >&2; pass=0; }

if [ "$pass" -eq 1 ]; then
  echo "[livecheck] PASS：渲染、嗅探、正文回收与 Cookie 双罐同步全部成立"
  exit 0
fi
exit 1
