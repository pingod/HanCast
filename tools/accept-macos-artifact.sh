#!/bin/bash
# 用法: tools/accept-macos-artifact.sh 2.2.15
# 验收 **CI 产物本身**：全程 CLI，不碰 UI 坐标，禁止任何手工补丁（重装/重签都不许）。
# 每轮装完包都跑一遍；任一项 FAIL 就说明这版产物不能用，别拿本地补丁版冒充。
VER="${1:?用法: $0 <version>}"
TAG="v$VER"
APP=/Applications/HanCast.app
RES="$APP/Contents/Resources/hancast_sidecar"
LOG="$HOME/Library/Application Support/HanCast/logs/hancast.log"
PLIST="$APP/Contents/Info.plist"
ME=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}' | xargs -I{} ipconfig getifaddr {})
fails=0
say() { printf '%s\n' "$*"; }
chk() { if [ "$2" = "$3" ]; then say "PASS $1 ($3)"; else say "FAIL $1: got '$2' want '$3'"; fails=$((fails+1)); fi; }

[ -n "$ME" ] || { say "FAIL 取不到本机 LAN IP"; exit 9; }

chk "版本" "$(plutil -extract CFBundleShortVersionString raw "$PLIST" 2>/dev/null)" "$VER"
chk "NSLocalNetworkUsageDescription" "$(plutil -extract NSLocalNetworkUsageDescription raw "$PLIST" 2>/dev/null | cut -c1-7)" "HanCast"
chk "NSBonjourServices[0]" "$(plutil -extract NSBonjourServices.0 raw "$PLIST" 2>/dev/null)" "_upnp._tcp"

# 本地网络门认的是代码身份：必须是 bundle id，且不能残留 linker-signed
IDT=$(codesign -dvvv "$APP" 2>&1 | sed -n 's/^Identifier=//p' | head -1)
chk "签名 Identifier" "$IDT" "com.han.hancast"
if codesign -dvvv "$APP" 2>&1 | grep -q linker-signed; then
  say "FAIL 主程序仍是 linker-signed（装完组播必被静默拒绝）"; fails=$((fails+1))
else
  say "PASS 无 linker-signed 标志"
fi

cd /Users/pavia/githome/HanCast || exit 1
for rel in ssdp.py commands.py security/guard.py protocol/dlna.py models/session.py screencast/manager.py; do
  want=$(git show "$TAG:hancast-backend/hancast_sidecar/$rel" | shasum -a 256 | cut -d' ' -f1)
  got=$(shasum -a 256 "$RES/$rel" 2>/dev/null | cut -d' ' -f1)
  chk "sha256 $rel" "${got:0:12}" "${want:0:12}"
done

# 只看新装这份跑出来的日志：旧进程写的是同一个文件
osascript -e 'tell application "HanCast" to quit' 2>/dev/null
for i in $(seq 1 12); do pgrep -x hancast-sidecar >/dev/null 2>&1 || break; sleep 2; done
if pgrep -x hancast-sidecar >/dev/null 2>&1; then
  say "FAIL 旧 sidecar 未退出，拒绝继续（别 pkill，会留空壳 GUI）"; exit 9
fi
off=$(wc -c <"$LOG" 2>/dev/null | tr -d ' '); off=${off:-0}
open -a HanCast; sleep 30
chan=$(tail -c +"$((off + 1))" "$LOG" 2>/dev/null | grep -o "通道 [a-z]*" | tail -1)
[ -z "$chan" ] && chan="(偏移 $off 之后没有『通道』行)"
chk "SSDP 心跳通道" "$chan" "通道 multicast"

chk "入站 description.xml" "$(curl -s --noproxy '*' -m 6 -o /dev/null -w '%{http_code}' "http://$ME:8080/description.xml")" "200"

pid=$(pgrep -x hancast-sidecar | head -1)
sp=$(lsof -nP -a -p "$pid" -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1{split($9,a,":"); print a[length(a)]}' | grep -vx 8080 | head -1)
if [ -n "$sp" ]; then
  chk "入站 DLNA 事件/描述服务(/r/)" "$(curl -s --noproxy '*' -m 6 -o /dev/null -w '%{http_code}' "http://$ME:$sp/r/nonexistent")" "404"
else
  say "FAIL 找不到 8080 之外的第二个监听端口"; fails=$((fails+1))
fi
# 注意：屏幕投屏的 aiohttp 服务只在会话进行中监听（manager._start 里 httpd.start()），
# 空载时不存在，不能拿它当「分享给浏览器」的判据。跨机入站的真凭据是
# Windows 侧日志里它确实拉到了 http://<本机IP>:<port>/media/... 的字节。

# 投屏到电视的开关：控制端 SOAP 信封必须带 SOAP 1.1 encodingStyle。
# TCL 85T8G Max 对不带它的 SetAVTransportURI 稳定回 HTTP 500 空正文
# （2026-10-13 交替 3 轮 12/12 分离），也就是「投屏到电视只成功过一次」的真根因。
if grep -q 'schemas.xmlsoap.org/soap/encoding/' "$RES/protocol/dlna.py"; then
  say "PASS 控制端 SOAP 信封带 encodingStyle"
else
  say "FAIL 控制端 SOAP 信封缺 encodingStyle（电视 SetAVTransportURI 必回 500）"; fails=$((fails+1))
fi

say "--- $([ $fails -eq 0 ] && echo 'ALL PASS' || echo "$fails FAIL") ---"
exit $fails
