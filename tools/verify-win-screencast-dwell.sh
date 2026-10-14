#!/bin/bash
# 用法: tools/verify-win-screencast-dwell.sh [host] [时长秒]
# 配合 tools/verify-win-screencast-audio-live.cmd 的加长驻留请求（tools/gen-win-… 的
# --dwell）：跑批的同时从本机拉产品自己的 HTTP 通道
# http://<host>:8765/hls/<session>/live.m3u8，按秒记录最大分片号。
# 为什么还要这一路：协议里的 screen_cast_status 只说「进程还在」，说不出
# 「画面还在往前走」；喂料线程/环流饿死时进程可以活着但分片不再增长。
set -u
HOST="${1:-192.168.1.68}"
SECONDS_TOTAL="${2:-60}"
HLS_ROOT='C:\Users\Administrator\AppData\Local\Temp\hancast_screencast_hls'
SSH="ssh -o ConnectTimeout=6 administrator@$HOST"
sid=""
end=$((SECONDS + SECONDS_TOTAL))
while [ $SECONDS -lt $end ]; do
  if [ -z "$sid" ]; then
    sid=$($SSH "dir /b /o:d \"$HLS_ROOT\" 2>nul" 2>/dev/null | tr -d '\r' | tail -1)
    [ -n "$sid" ] && echo "SID=$sid"
  fi
  if [ -n "$sid" ]; then
    pl=$(curl -s --noproxy '*' --max-time 4 \
         "http://$HOST:8765/hls/$sid/live.m3u8" 2>/dev/null)
    last=$(printf '%s' "$pl" | grep -o 'seg[0-9]*\.ts' | tail -1)
    n=$(printf '%s' "$pl" | grep -c '^#EXTINF')
    echo "$(date +%H:%M:%S) extinf=$n last=$last"
  else
    echo "$(date +%H:%M:%S) 等待会话目录"
  fi
  sleep 3
done
