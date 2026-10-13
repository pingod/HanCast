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
for rel in ssdp.py commands.py params.py security/guard.py protocol/dlna.py protocol/server.py \
           models/session.py renderer/mpv.py screencast/manager.py screencast/capture.py \
           screencast/httpd.py screencast/ffmpeg_manager.py screencast/www/index.html \
           utils/logger.py utils/config.py main.py \
           media/server.py media/share_resolver.py media/rules/__init__.py media/rules/base.py \
           media/rules/bili.py media/rules/douyin.py media/rules/declarative.py; do
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

# 手机音量键链路：入站值必须按 SCPD 的 dataType/allowedValueRange 归一化并钳制，
# 否则 '45.5'/'true'/200 这类写法会直接把 SetVolume 打成 HTTP 500 或把 mpv 音量设穿。
if grep -q 'def coerce_state_value' "$RES/protocol/dlna.py"; then
  say "PASS 入站状态值归一化（音量/静音不再 500）"
else
  say "FAIL 打包产物缺 coerce_state_value（音量键仍是旧行为）"; fails=$((fails+1))
fi

# 后台参数：打包进去的那张表才是设置页真正渲染的那张表
PARAMS_JSON=$(/usr/bin/python3 - "$RES" <<'PY'
import json, sys, importlib.util
spec = importlib.util.spec_from_file_location("hp", sys.argv[1] + "/params.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(json.dumps({s["key"]: s["default"] for s in m.PARAM_SPECS}))
PY
) 2>/dev/null || PARAMS_JSON='{}'
chk "参数登记表条数" "$(echo "$PARAMS_JSON" | /usr/bin/python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')" "18"
chk "浏览器通道默认分片" "$(echo "$PARAMS_JSON" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["screencast.browser_segment_seconds"])')" "1"
chk "电视通道默认分片" "$(echo "$PARAMS_JSON" | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["screencast.dlna_segment_seconds"])')" "2"

# HLS 只能在关键帧处切片：-g 必须跟着分片时长走，否则 1 秒分片是假的
if grep -q 'fps \* segment_seconds' "$RES/screencast/capture.py"; then
  say "PASS GOP 跟随分片时长"
else
  say "FAIL capture.py 的 -g 没跟着分片时长（1 秒分片落空）"; fails=$((fails+1))
fi

# 观看页的 hls.js 配置由后端注入（改参数不用重新打包前端）
if grep -q '__PLAYER_CONFIG__' "$RES/screencast/www/index.html" \
   && grep -q '__PLAYER_CONFIG__' "$RES/screencast/httpd.py"; then
  say "PASS 观看页播放器配置注入"
else
  say "FAIL 观看页缺 __PLAYER_CONFIG__ 注入"; fails=$((fails+1))
fi

# 设置页要能读到/写回这张表
if grep -q '"get_params":' "$RES/commands.py" && grep -q '"save_params":' "$RES/commands.py"; then
  say "PASS get_params/save_params 已注册"
else
  say "FAIL 命令表缺 get_params/save_params"; fails=$((fails+1))
fi

# 协议层必须在「父进程没给 locale」时也走得通：登记表/设备名/错误文案全是中文，
# 而 stdout 就是与 Rust 的通信通道。v2.2.18 的打包版在 LC_ALL=C 下 get_params 直接
# 回 success:false（'ascii' codec can't encode），设置页整块读不到参数。
osascript -e 'tell application "HanCast" to quit' 2>/dev/null
for i in $(seq 1 12); do pgrep -x hancast-sidecar >/dev/null 2>&1 || break; sleep 2; done
PROTO_LINE=$(printf '{"id":1,"cmd":"get_params","params":{}}\n{"id":2,"cmd":"exit","params":{}}\n' \
  | env -i PATH=/usr/bin:/bin LC_ALL=C "$APP/Contents/MacOS/hancast-sidecar" 2>/dev/null \
  | grep -a '"id": 1' | head -1)
if echo "$PROTO_LINE" | grep -q '"success": true' && echo "$PROTO_LINE" | grep -q 'label_zh'; then
  say "PASS 无 locale 环境下协议仍走通（中文登记表原样回来）"
else
  say "FAIL LC_ALL=C 下 get_params 没走通: ${PROTO_LINE:0:160}"; fails=$((fails+1))
fi
open -a HanCast

# sidecar 的自报版本（UPnP SERVER/modelNumber、config dump）也是版本契约点：
# 漏改会让真机上的「版本号」不再能证明装的是哪版（v2.2.19 就漏了这一处）。
SIDE_VER=$(grep -o 'self\.version = "[^"]*"' "$RES/utils/config.py" | head -1 | sed 's/.*"\(.*\)"/\1/')
chk "sidecar 自报版本" "$SIDE_VER" "$VER"

say "--- $([ $fails -eq 0 ] && echo 'ALL PASS' || echo "$fails FAIL") ---"
exit $fails
