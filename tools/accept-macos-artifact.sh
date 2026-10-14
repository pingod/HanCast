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
           screencast/macos_tap.py utils/logger.py utils/config.py main.py \
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

# ── v2.2.21 本轮契约：窗口选择 + 系统声音 + 麦克风 ──

# 麦克风授权声明：Info.plist 里缺 NSMicrophoneUsageDescription 时，TCC 不是
# 「弹窗被拒」而是「拒绝且不再提示」，ffmpeg 从 App 继承这条授权后抓回的样本
# 恒为 0 —— 设备照样列得出、投屏照样流畅，只是永远没声。
chk "NSMicrophoneUsageDescription" "$(plutil -extract NSMicrophoneUsageDescription raw "$PLIST" 2>/dev/null | cut -c1-7)" "HanCast"

# 勾了麦克风却没有输入权限，以前是静默降级成 -an；现在开录前试采一秒，
# 采到全零就如实报错。这条判据在打包产物里必须在。
if grep -q 'audio_input_silent' "$RES/screencast/manager.py"; then
  say "PASS 静音闸门（无录音权限时如实报错）"
else
  say "FAIL 打包产物缺 audio_input_silent 闸门（又会静默投哑）"; fails=$((fails+1))
fi

# macOS 系统声音：CoreAudio Audio Tap（14.2+）现建「HanCast 系统声音」设备，
# 不装虚拟声卡也能投。缺这个模块 = 界面里根本没有系统声音可选项。
if grep -q 'AudioHardwareCreateAggregateDevice' "$RES/screencast/macos_tap.py" 2>/dev/null; then
  say "PASS Audio Tap 系统声音模块已打包"
else
  say "FAIL 打包产物缺 macos_tap.py（macOS 系统声音没辙）"; fails=$((fails+1))
fi

# 窗口选择器：macOS 交的是「窗口号 + 该窗口的裁剪区域」，Windows 过滤掉框架
# 影子窗口（裸 EnumWindows 会把 com.han.hancast-siw 这类 16x16 隐形窗口塞进
# 列表，选到了就是黑屏）。两者都在 ffmpeg_manager 里。
if grep -q 'def darwin_windows' "$RES/screencast/ffmpeg_manager.py" \
   && grep -q 'def _castable' "$RES/screencast/ffmpeg_manager.py"; then
  say "PASS 窗口枚举与过滤（选择器不再靠手打标题）"
else
  say "FAIL 缺窗口枚举/过滤实现"; fails=$((fails+1))
fi

# Windows 麦克风列不出来的根因：现在的 dshow -list_devices 不打印
# "DirectShow audio devices" 分段头，只按分段头切段的解析永远进不了音频段。
# 产物里必须认设备行的 (audio) 后缀。
if grep -q '(audio|video|none)' "$RES/screencast/ffmpeg_manager.py"; then
  say "PASS dshow 按 (audio) 后缀认设备"
else
  say "FAIL dshow 解析仍只认分段头（Windows 一个麦克风都列不出）"; fails=$((fails+1))
fi

# ── v2.2.22 本轮契约 ──

# dshow 列设备的兜底：没有分段头时整份输出都是设备区，但每行还带着
# "Alternative name"（形如 @audio:{...}），把它当设备名交给 ffmpeg -f dshow
# 必然打不开，等于麦克风列表里全是假条目。
if grep -q 'Alternative name' "$RES/screencast/ffmpeg_manager.py"; then
  say "PASS dshow 兜底排除 Alternative name"
else
  say "FAIL dshow 兜底没排除 Alternative name（麦克风列表里是设备 ID 不是设备）"; fails=$((fails+1))
fi

# 图片皮的判型窗口：v2.2.20 用固定 400 字节，扫不到 dyrs 的 583 字节 PNG 头，
# 症状是「播放列表照转、分片全哑」（电视黑屏、mpv 报 no audio or video data）。
# 窗口现在按上游 Content-Type 分档：自称 image/* 就给 16K，其余保持 2K 不加延迟。
if grep -q 'def _disguise_peek_len' "$RES/media/server.py" \
   && grep -q '_DISGUISE_SCAN_IMAGE' "$RES/media/server.py"; then
  say "PASS 分片判型窗口按上游类型分档（image/* 给 16K）"
else
  say "FAIL 分片判型窗口还是固定长度（皮一长就又投哑）"; fails=$((fails+1))
fi

# 视频滤镜的位置：v2.2.22 及以前把 -vf 排在音频输入之前，ffmpeg 把它判给
# 「下一个输入」（麦克风），EINVAL 直接退出——表现就是「一选麦克风/系统声音
# 屏幕投屏就起不来」。修复形态是 build_video_input 只返回输入参数，-vf 由
# start_hls 放到所有 -i 之后。
if grep -q 'cmd += \["-vf", vf\]' "$RES/screencast/capture.py" \
   && ! grep -q '"-vf", vf' "$RES/screencast/ffmpeg_manager.py"; then
  say "PASS 视频滤镜排在所有输入之后（采音不再 EINVAL）"
else
  say "FAIL 视频滤镜还夹在输入段里（选麦克风/系统声音必崩）"; fails=$((fails+1))
fi

# ── v2.2.24 本轮契约：环回流的关闭权归读线程 ──
# v2.2.23 及以前，stop() 在 feeder 仍阻塞于 read() 时直接 close 流；PortAudio 的
# C 调用不允许并发，.68 事件日志是「异常代码 0xc0000005 / ntdll.dll」，sidecar
# 整个进程没了——用户看到的「选系统声音后崩溃」有两段，这段是第二段。
if grep -q 'lb.close()' "$RES/screencast/capture.py" \
   && grep -q 'is_alive()' "$RES/screencast/capture.py" \
   && grep -q 'self._closed' "$RES/screencast/loopback.py"; then
  say "PASS 环回流由读线程关闭（停止链路不再踩内存）"
else
  say "FAIL 环回流仍可能被别的线程关掉（停止/早退会打崩 sidecar）"; fails=$((fails+1))
fi

say "--- $([ $fails -eq 0 ] && echo 'ALL PASS' || echo "$fails FAIL") ---"
exit $fails
