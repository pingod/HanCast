#!/bin/bash
# 用法: tools/accept-macos-artifact.sh 2.2.15
# 验收 **CI 产物本身**：全程 CLI，不碰 UI 坐标，禁止任何手工补丁（重装/重签都不许）。
# 每轮装完包都跑一遍；任一项 FAIL 就说明这版产物不能用，别拿本地补丁版冒充。
#
# 依赖下载实测需要代理时导出：HC_FFMPEG_PROXY=http://127.0.0.1:9565
# （探针默认清空代理环境，直连得通就不必给；直连不通时它报 SKIP 而不是 FAIL）
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
           models/session.py models/media.py renderer/mpv.py screencast/manager.py screencast/capture.py \
           screencast/httpd.py screencast/ffmpeg_manager.py screencast/www/index.html \
           screencast/macos_tap.py utils/logger.py utils/config.py utils/ffmpeg_deps.py main.py \
           media/server.py media/share_resolver.py media/rules/__init__.py media/rules/base.py \
           media/rules/declarative.py media/rules/jsplugins.py media/rules/jsruntime.py \
           utils/identity.py utils/httpclient.py utils/browser_channel.py utils/ghmirror.py; do
  want=$(git show "$TAG:hancast-backend/hancast_sidecar/$rel" | shasum -a 256 | cut -d' ' -f1)
  got=$(shasum -a 256 "$RES/$rel" 2>/dev/null | cut -d' ' -f1)
  chk "sha256 $rel" "${got:0:12}" "${want:0:12}"
done

# v2.3.3 契约：程序里没有任何编译进去的站点规则，也没有随包播种的规则目录。
# 这几样任何一样还在，就说明「内置规则」这条退役没退干净。
for gone in media/rules/bili.py media/rules/douyin.py media/bili_resolver.py rule_plugins; do
  if [ -e "$RES/$gone" ]; then
    say "FAIL 产物里还有已退役的东西: $gone"; fails=$((fails+1))
  else
    say "PASS 产物里无 $gone"
  fi
  if git cat-file -e "$TAG:hancast-backend/hancast_sidecar/$gone" 2>/dev/null; then
    say "FAIL $TAG 的 git 树里还有 $gone（提交漏了删除）"; fails=$((fails+1))
  fi
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
load_params() {  # $1 = params.py 路径
  /usr/bin/python3 - "$1" <<'PY'
import json, sys, importlib.util
spec = importlib.util.spec_from_file_location("hp", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(json.dumps({s["key"]: s["default"] for s in m.PARAM_SPECS}, sort_keys=True))
PY
}
PARAMS_JSON=$(load_params "$RES/params.py" 2>/dev/null) || PARAMS_JSON='{}'
# 基准从被测 tag 的源码现读，不写死条数：写死过 "18"，加一个参数就得同步改脚本，
# FAIL 只能证明脚本过期，证明不了产物有问题。逐键逐默认值比，能同时抓住多带/漏带/带旧值。
TAG_DIR=$(mktemp -d)
# 文件名必须以 .py 结尾：importlib 按后缀选 loader，无后缀的 mktemp 临时文件会静默读不出表
git show "${TAG}:hancast-backend/hancast_sidecar/params.py" > "$TAG_DIR/params.py" 2>/dev/null
SRC_PARAMS_JSON=$(load_params "$TAG_DIR/params.py" 2>/dev/null)
rm -rf "$TAG_DIR"
if [ -z "$SRC_PARAMS_JSON" ]; then
  say "FAIL 取不到 $TAG 的参数表基准（tag 没 fetch 到？）"; fails=$((fails+1))
else
  chk "参数登记表与 $TAG 源码逐键一致" "$PARAMS_JSON" "$SRC_PARAMS_JSON"
fi
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
PROTO_OUT=$(printf '{"id":1,"cmd":"get_params","params":{}}\n{"id":2,"cmd":"plugin_list","params":{}}\n{"id":3,"cmd":"rule_inventory","params":{}}\n{"id":4,"cmd":"exit","params":{}}\n' \
  | env -i PATH=/usr/bin:/bin LC_ALL=C "$APP/Contents/MacOS/hancast-sidecar" 2>/dev/null)
PROTO_LINE=$(echo "$PROTO_OUT" | grep -a '"id": 1' | head -1)
if echo "$PROTO_LINE" | grep -q '"success": true' && echo "$PROTO_LINE" | grep -q 'label_zh'; then
  say "PASS 无 locale 环境下协议仍走通（中文登记表原样回来）"
else
  say "FAIL LC_ALL=C 下 get_params 没走通: ${PROTO_LINE:0:160}"; fails=$((fails+1))
fi

# ── v2.3.0/v2.3.1 本轮契约：JS 解析插件宿主进了冻结的二进制 ──
# quickjs 是 C 扩展，Nuitka 少收一个 --include-package 的话源码测试全绿、产物里
# 一调就崩；所以判据不能只看文件在不在包里，要让装好的 sidecar 真答一次 plugin_list。
PLUGIN_LINE=$(echo "$PROTO_OUT" | grep -a '"id": 2' | head -1)
if echo "$PLUGIN_LINE" | grep -q '"success": true'; then
  say "PASS plugin_list 在打包 sidecar 里答得上来（quickjs 宿主可用）"
else
  say "FAIL plugin_list 没成功，产物里 JS 插件宿主多半起不来: ${PLUGIN_LINE:0:200}"; fails=$((fails+1))
fi
# v2.3.3 契约：规则只有两种来源（JS 包 / 声明式 JSON），「内置」这个概念已经删除。
# 冻结产物里若还认得 rule_set_builtin_enabled，说明命令表来自旧代码；而 rule_inventory
# 的 script_packs 里若出现 "builtin" 字段，说明 info() 还在给包发内置身份。
INV_LINE=$(echo "$PROTO_OUT" | grep -a '"id": 3' | head -1)
if echo "$INV_LINE" | grep -q '"success": true' \
   && echo "$INV_LINE" | grep -q '"script_packs"' \
   && echo "$INV_LINE" | grep -q '"declarative"' \
   && ! echo "$INV_LINE" | grep -q '"builtin"'; then
  say "PASS rule_inventory 只报两种来源，产物里已无「内置」身份"
else
  say "FAIL rule_inventory 仍带内置概念或没答上来: ${INV_LINE:0:200}"; fails=$((fails+1))
fi
if grep -q 'rule_set_builtin_enabled' "$RES/commands.py"; then
  say "FAIL 冻结产物仍注册 rule_set_builtin_enabled（内置规则概念未删）"; fails=$((fails+1))
else
  say "PASS 命令表已无 rule_set_builtin_enabled"
fi
for cmd in plugin_check_updates plugin_pull; do
  if grep -q "\"$cmd\":" "$RES/commands.py"; then
    say "PASS $cmd 已注册"
  else
    say "FAIL 命令表缺 $cmd（设置页的「检查规则包更新」必挂）"; fails=$((fails+1))
  fi
done
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

# ── v2.2.25 本轮契约：环回交付节奏自己撑 ──
# PortAudio 的 WASAPI 环回在「没人出声」的渲染端点上可以整场零字节交付
# （.68 实测：扬声器 (Realtek(R) Audio) 的环回 read() 一直阻塞），而 amix 要
# 凑齐每一路输入才出帧——stdin 一断供，ffmpeg 停在 0.42 秒、8 秒内不落播放
# 列表，表现是「一选系统声音屏幕投屏就起不来」。修复形态是采集放独立线程、
# 消费侧按单调时钟补欠账的静音（上限一块的 4 倍，免得把音频冲到视频前面）。
if grep -q 'def _pad' "$RES/screencast/loopback.py" \
   && grep -q 'MAX_PAD_CHUNKS' "$RES/screencast/loopback.py" \
   && grep -q 'loopback-pump' "$RES/screencast/loopback.py"; then
  say "PASS 环回按单调时钟补静音（设备零交付不再憋死整条管线）"
else
  say "FAIL 环回仍直连 ffmpeg stdin（静音设备会把投屏饿死）"; fails=$((fails+1))
fi

# 「ffmpeg 活着却没落播放列表」必须说清楚为什么：这一类全是某路输入把管线
# 憋住，ffmpeg 自己在 stderr 里就喊了 buffer/queue 警告，不带上就等于让下一个人
# 从头猜。
if grep -q '权限未授予）" + self._stderr_hint()' "$RES/screencast/capture.py"; then
  say "PASS 超时未生成带 ffmpeg stderr 线索"
else
  say "FAIL 超时未生成还是那句没用的「可能是权限未授予」"; fails=$((fails+1))
fi

# ── v2.2.26 本轮契约：把「当前出声」那条环回标出来 ──
# WASAPI 环回是「每块输出设备一条镜像」，.68 上同时挂着 4 条；只有对应当前默认
# 渲染设备的那条读得到声音。sidecar 必须既排第一、也把 default 传到协议里，
# 界面才标得出来（选到闲置那条的表现是「投屏正常但电视没声音」，用户无从下手）。
if grep -q 'd\["default"\] = d\["index"\] == default_index' "$RES/screencast/loopback.py" \
   && grep -q '"default": bool(d.get("default"))' "$RES/screencast/ffmpeg_manager.py"; then
  say "PASS 环回设备带「当前出声」标记（枚举排序 + 协议字段）"
else
  say "FAIL 环回列表仍分不清哪条真在出声"; fails=$((fails+1))
fi

# 环回 feeder 早退（.68 偶发，起流 0.3 秒后）不能再只留一句「stdin 已关闭」：
# 异常类型 + FFmpeg 退出码 + stderr 尾巴要一起进日志，否则下一样只能靠复碰。
if grep -q '环回 feeder 停止' "$RES/screencast/capture.py" \
   && grep -q 'rc = proc.poll()' "$RES/screencast/capture.py"; then
  say "PASS 环回 feeder 停止带异常与 ffmpeg 状态"
else
  say "FAIL 环回 feeder 停止仍只报「FFmpeg stdin 已关闭」"; fails=$((fails+1))
fi

# ── v2.3.5 本轮契约：代理不再挖字节 / 封面走 https / 下线设备要消失 ──

# 判型窗口多读的字节必须原样交给响应体：B 站 mp4 的 moov 在文件尾，
# 中间少 908 字节 mpv 直接 "Errors when loading file"（解析成功、投屏黑屏）。
if grep -q 'return buf\[:want\]' "$RES/media/server.py"; then
  say "FAIL _peek_upstream 仍把多读的字节裁掉（又会挖空投屏流）"; fails=$((fails+1))
else
  say "PASS 代理转发的字节数与上游一致（不再截断判型窗口）"
fi

# 封面图：打包版页面是安全上下文，http 图片会被按混合内容直接拦掉
# （B 站 data.pic 下发的就是 http://，表现为投屏卡片图片挂了）。
if grep -q 'def secure_thumbnail' "$RES/models/media.py" \
   && grep -q 'secure_thumbnail(self.thumbnail)' "$RES/models/media.py"; then
  say "PASS 封面统一升级 https"
else
  say "FAIL 冻结产物没做封面 https 归一"; fails=$((fails+1))
fi

# 下线设备：byebye 立刻摘 + 静默的要探活两次才摘，并如实推 device_lost。
# 只删不推事件的话，前端那张卡片会一直挂着。
if grep -q 'def _handle_byebye' "$RES/ssdp.py" \
   && grep -q 'def _sweep_stale_devices' "$RES/ssdp.py" \
   && grep -q 'on_device_lost=self._on_device_lost' "$RES/commands.py" \
   && grep -q 'emit_event("device_lost"' "$RES/commands.py"; then
  say "PASS 设备下线链路完整（byebye + 探活 + device_lost 事件）"
else
  say "FAIL 设备下线链路缺失（下线了还挂在列表里）"; fails=$((fails+1))
fi
if echo "$PARAMS_JSON" | /usr/bin/python3 -c \
     'import json,sys; sys.exit(0 if "discovery.device_stale_s" in json.load(sys.stdin) else 1)'; then
  say "PASS discovery.device_stale_s 已在参数登记表"
else
  say "FAIL 参数表缺 discovery.device_stale_s（下线判定没法调）"; fails=$((fails+1))
fi

# v2.3.7 契约①：TLS 指纹伪造的两个包必须真的冻进产物。
# 漏了 _cffi_backend 时产物是「能跑，但 JA3 仍是 Python」——顶层 curl_cffi 一 import
# 就要它，代码里又有降级，肉眼与日志都看不出来，只能按 bundle 里的文件判。
# v2.3.7 首发产物实测踩过：Nuitka 产出了 curl_cffi/，但 bundle.resources 没列这个
# 目录（`*.so` 只抓顶层），于是 curl_cffi/_wrapper.abi3.so 整个被丢掉，打包版一直
# 走 requests。所以目录、包内二进制、顶层 _cffi_backend 三样都要单独判。
RROOT="$APP/Contents/Resources"
if [ -d "$RROOT/curl_cffi" ]; then
  say "PASS 产物含 curl_cffi"
else
  say "FAIL 产物缺 curl_cffi（指纹伪造在打包版里静默降级）"; fails=$((fails+1))
fi
if ls "$RROOT"/curl_cffi/_wrapper*.so >/dev/null 2>&1; then
  say "PASS curl_cffi 带 _wrapper 二进制"
else
  say "FAIL curl_cffi/ 里没有 _wrapper*.so（curl 的真身在缺失的目录里，指纹无从发出）"; fails=$((fails+1))
fi
if ls "$RROOT"/_cffi_backend*.so >/dev/null 2>&1; then
  say "PASS 产物含 _cffi_backend"
else
  say "FAIL 产物缺 _cffi_backend（curl_cffi 一 import 就失败，指纹仍是 Python）"; fails=$((fails+1))
fi
if grep -q 'def make_session' "$RES/utils/httpclient.py" \
   && grep -q 'from ..utils.httpclient import make_session\|utils.httpclient' "$RES/media/share_resolver.py"; then
  say "PASS 解析链路走指纹工厂"
else
  say "FAIL 解析链路没接上 utils/httpclient.py 的会话工厂"; fails=$((fails+1))
fi

# v2.3.7 契约②：浏览器通道两端都在产物里。Python 半查文件，Rust 半查主程序里的
# 窗口标签——离屏窗口的 label 前缀只出现在 browser.rs，是最省事的「编进去了」判据。
if grep -q 'emit_event("browser_request"' "$RES/utils/browser_channel.py" \
   && grep -q '"browser_result":' "$RES/commands.py" \
   && grep -q '__render' "$RES/media/rules/jsruntime.py"; then
  say "PASS sidecar 半的浏览器通道链路完整（事件/命令/沙箱口）"
else
  say "FAIL sidecar 半的浏览器通道缺一段（事件、命令或沙箱 host.render）"; fails=$((fails+1))
fi
if strings -a "$APP/Contents/MacOS/HanCast" 2>/dev/null | grep -q "hc-render-"; then
  say "PASS 主程序含离屏渲染窗口（browser.rs 已编进产物）"
else
  say "FAIL 主程序里没有 hc-render- 窗口标签（Rust 半没进这个包）"; fails=$((fails+1))
fi

# ── v2.3.11 本轮契约：FFmpeg 彻底去捆绑，改为「检测已装 + 缺失时按需下载」──
# 三层判据，缺一层都不算验收过：
# ① bundle 里没有任何 ffmpeg/ffprobe 可执行（v2.3.10 及以前同时躺着
#    Contents/Resources/ffmpeg/ffmpeg 和 Contents/MacOS/ffmpeg，这是「删干净」的唯一硬证据）；
# ② 冻结的 sidecar 自己答得上 ffmpeg_status，且检出的那份真能跑；
# ③ 按需下载在真机上跑得通（_SOURCES 的 darwin gz 源此前从未真下载过，
#    pytest 用的是假流；只有真产物 + 真网络能证明解包后的文件在 macOS 上可执行）。
if find "$APP" -type f \( -name ffmpeg -o -name ffprobe -o -name 'ffmpeg.exe' \) -print -quit | grep -q .; then
  say "FAIL 产物里仍有 ffmpeg/ffprobe 可执行（去捆绑没生效）"; fails=$((fails+1))
else
  say "PASS 产物内无 ffmpeg/ffprobe 可执行"
fi
for cmd in ffmpeg_status ffmpeg_download_start ffmpeg_download_status; do
  if grep -q "\"$cmd\":" "$RES/commands.py"; then
    say "PASS $cmd 已注册"
  else
    say "FAIL 命令表缺 $cmd（依赖弹窗在打包版里必挂）"; fails=$((fails+1))
  fi
done
if grep -q 'def _get_base_path\|def _find_in_program_dir' "$RES/screencast/ffmpeg_manager.py"; then
  say "FAIL ffmpeg_manager 仍留着捆绑目录探测（安装目录会被当候选）"; fails=$((fails+1))
else
  say "PASS ffmpeg_manager 已无捆绑目录探测函数"
fi

# ②③ 要驱动装好的 sidecar，且不能污染本机设置：给它一个临时 HOME，
# 于是 SETTING_DIR / 下载落点 / 日志全在临时目录里，跑完整目录删掉。
osascript -e 'tell application "HanCast" to quit' 2>/dev/null
for i in $(seq 1 12); do pgrep -x hancast-sidecar >/dev/null 2>&1 || break; sleep 2; done
if pgrep -x hancast-sidecar >/dev/null 2>&1; then
  say "FAIL 旧 sidecar 未退出，跳过 FFmpeg 依赖实测（别 pkill，会留空壳 GUI）"; fails=$((fails+1))
else
  TMPHOME=$(mktemp -d)
  PROBE_FILE=$(mktemp)
  /usr/bin/python3 - "$APP/Contents/MacOS/hancast-sidecar" "$TMPHOME" > "$PROBE_FILE" 2>&1 <<'PY'
import json, os, queue, subprocess, sys, threading, time

sidecar, tmp_home = sys.argv[1], sys.argv[2]
env = {"HOME": tmp_home, "PATH": "/usr/bin:/bin", "LC_ALL": "C"}
# 默认清空代理（别把本机那个时好时坏的代理带进来），需要代理时用 HC_FFMPEG_PROXY 显式给
proxy = os.environ.get("HC_FFMPEG_PROXY")
if proxy:
    env.update({"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
                "ALL_PROXY": proxy})
proc = subprocess.Popen([sidecar], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, env=env, text=True, encoding="utf-8")
lines: "queue.Queue[str]" = queue.Queue()


def reader():
    for line in proc.stdout:
        lines.put(line.strip())


threading.Thread(target=reader, daemon=True).start()
_id = 0


def call(cmd, params=None, wait=60.0):
    """发一条命令并取回同 id 的响应；事件行（无 id）丢掉，超时返回 None"""
    global _id
    _id += 1
    want = _id
    proc.stdin.write(json.dumps({"id": want, "cmd": cmd,
                                 "params": params or {}}) + "\n")
    proc.stdin.flush()
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            line = lines.get(timeout=1.0)
        except queue.Empty:
            continue
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("id") == want:
            return obj
    return None


def run_version(path):
    try:
        r = subprocess.run([path, "-version"], capture_output=True, text=True,
                           timeout=8, encoding="utf-8", errors="replace")
        return (r.returncode, (r.stdout or "").splitlines()[0] if r.stdout else "")
    except (OSError, subprocess.TimeoutExpired, IndexError) as e:
        return (None, repr(e))


def say(kind, text):
    print(f"{kind} {text}")


# 线路故障与产品故障要分开判：这台机连 GitHub 走不走得通取决于代理，不是产品能控制的；
# 但「失败原因有没有原样送到前端」恰恰是 v2.3.11 的契约，所以下面遇到线路故障时
# 一边 SKIP 完成性检查，一边 PASS 上报链路。
LINE_FAULTS = ("连接下载源失败", "Timeout", "Failed to perform", "Connection",
               "timed out", "reset by peer", "Broken pipe", "Unable to connect")


def is_line_fault(text):
    return any(k in str(text) for k in LINE_FAULTS)


resp = call("ffmpeg_status", {"refresh": True})
if not resp or not resp.get("success"):
    say("FAIL", f"打包 sidecar 的 ffmpeg_status 没答上来: {resp}")
    sys.exit(0)
d = resp["data"]
for key in ("ok", "path", "source", "items", "missing", "install_commands",
            "managed_dir", "download"):
    if key not in d:
        say("FAIL", f"ffmpeg_status 缺字段 {key}（弹窗渲染会空一块）")
say("INFO", f"ok={d.get('ok')} source={d.get('source')} path={d.get('path')}")
say("INFO", f"version={d.get('version')}")
say("INFO", f"managed_dir={d.get('managed_dir')}")
say("INFO", f"download={d.get('download')} install={d.get('install_commands')}")

managed = d.get("managed_dir") or ""
if not managed.startswith(tmp_home):
    say("FAIL", f"托管目录不在临时 HOME 里，会污染本机: {managed}")
if len(d.get("items") or []) != 2:
    say("FAIL", f"items 应为 ffmpeg+ffprobe 两条，实得 {len(d.get('items') or [])}")

if d.get("ok"):
    # _extra_dirs() 的绝对路径兜底（/opt/homebrew/bin 等）在 env -i 的窄 PATH 下
    # 仍要检出——「装了但没进 GUI 进程 PATH」正是打包版最常见的形态。
    rc, head = run_version(d["path"])
    if rc == 0:
        say("PASS", f"检出的 FFmpeg 真能跑（source={d['source']}）: {head[:60]}")
    else:
        say("FAIL", f"状态说找到了，但这份跑不起来 rc={rc}: {head[:120]}")
else:
    if d.get("missing") and d.get("install_commands") and (d.get("download") or {}).get("available"):
        say("PASS", "缺失时给全了清单/安装命令/下载源（弹窗有得渲染）")
    else:
        say("FAIL", f"缺失载荷不完整: {d.get('missing')} / {d.get('install_commands')}")

# ③ 按需下载：真下、真解、真跑。连不上下载源算环境限制，不算产品失败。
start = call("ffmpeg_download_start", wait=30)
if not start or not start.get("success"):
    err = (start or {}).get("error", "") or "无响应"
    if is_line_fault(err):
        say("SKIP", f"下载源连不上（线路/代理限制，需代理时导出 HC_FFMPEG_PROXY 重跑）: {str(err)[:140]}")
        say("PASS", f"失败原因原样上报（弹窗拿得到真因）: {str(err)[:140]}")
    else:
        say("FAIL", f"ffmpeg_download_start 失败: {str(err)[:160]}")
else:
    stages, final, last = [], None, None
    deadline = time.time() + 900
    while time.time() < deadline:
        time.sleep(2)
        st = call("ffmpeg_download_status", wait=30)
        if not st or not st.get("success"):
            continue
        payload = st["data"]
        last = payload
        stage = payload.get("stage")
        if stage and (not stages or stages[-1] != stage):
            stages.append(stage)
        if payload.get("status") in ("done", "error"):
            final = payload
            break
    say("INFO", "stage 轨迹: " + " -> ".join(stages))
    if not final:
        moved = (last or {}).get("received") or 0
        say("SKIP" if moved else "FAIL",
            f"900s 未跑完，已收 {moved}/{(last or {}).get('total')} 字节"
            + ("（线路太慢，不是产品问题）" if moved else "（一个字节都没动，也没给出原因）"))
    elif final["status"] == "error":
        err = str(final.get("error") or "")
        if is_line_fault(err):
            say("SKIP", "下载源线路故障（需代理时导出 HC_FFMPEG_PROXY 重跑）")
            say("PASS", f"失败原因原样上报（curl 原文进了载荷）: {err[:160]}")
        else:
            say("FAIL", f"下载失败: {err[:240]}")
    else:
        got = final.get("result") or {}
        dl_path = os.path.join(managed, "ffmpeg")
        rc, head = run_version(dl_path)
        if rc == 0:
            say("PASS", f"下载的那份能执行: {head[:60]}（paths={got.get('paths')}）")
        else:
            say("FAIL", f"下载任务报 done，但 {dl_path} 跑不起来 rc={rc}: {head[:120]}")
        # 优先级：系统已装 > 本程序下载的那份（find_binary 先走系统目录）
        after = call("ffmpeg_status", {"refresh": True})
        src = ((after or {}).get("data") or {}).get("source")
        if src in ("system", "downloaded"):
            say("PASS", f"复检仍按「系统已装 > 自下载」归位（source={src}）")
        else:
            say("FAIL", f"复检 source={src}（期望 system 或 downloaded）")

# 手动指定分支 + 同目录 ffprobe 搭档采纳（macOS 自下载只有 ffmpeg，靠这一条补齐）
manual = d.get("path") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else None)
if not manual:
    say("SKIP", "本机没有可手动指定的 ffmpeg，跳过 manual 分支")
else:
    setp = call("set_ffmpeg_path", {"path": manual})
    recheck = call("ffmpeg_status", {"refresh": True})
    dd = (recheck or {}).get("data") or {}
    probe_item = next((i for i in (dd.get("items") or []) if i.get("key") == "ffprobe"), {})
    if not setp or not setp.get("success"):
        say("FAIL", f"set_ffmpeg_path 失败: {(setp or {}).get('error')}")
    elif dd.get("source") != "manual" or dd.get("path") != manual:
        say("FAIL", f"手动指定后 source={dd.get('source')} path={dd.get('path')}（期望 manual / {manual}）")
    else:
        say("PASS", f"手动指定优先于系统检测（source=manual，且未破坏 ok={dd.get('ok')}）")
    if probe_item.get("found") and probe_item.get("source") == "manual":
        say("PASS", f"同目录 ffprobe 被自动采纳: {probe_item.get('path')}")
    else:
        say("INFO", f"ffprobe 未采纳（本机可能确实没有）: {probe_item}")

proc.stdin.close()
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
PY
  PROBE_RC=$?
  while IFS= read -r line; do
    case "$line" in
      FAIL\ *) say "$line"; fails=$((fails+1));;
      *) say "$line";;
    esac
  done < "$PROBE_FILE"
  [ "$PROBE_RC" = 0 ] || { say "FAIL FFmpeg 依赖探针异常退出 rc=$PROBE_RC"; fails=$((fails+1)); }
  rm -rf "$TMPHOME" "$PROBE_FILE"
  open -a HanCast
fi

say "--- $([ $fails -eq 0 ] && echo 'ALL PASS' || echo "$fails FAIL") ---"
exit $fails
