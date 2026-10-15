#!/bin/bash
# dyrs 采集站分片防盗链矩阵：同一分片，换不同的头/Range/Cookie，看到底哪种给真 TS
# 用法: tools/probe-dyrs-headers.sh <m3u8_url> [页面 referer]
#
# 为什么要矩阵：代理对每条分片请求最终拿到的是 image/png（302 到 gimg1.baidu.com
# 的推广图），而播放列表本身是 200 正常 m3u8 —— 说明拒绝发生在分片这一层，
# 需要搞清它到底认哪一个字段（页面 Referer？Set-Cookie 会话？Sec-Fetch-*？Range？）
set -u
U="$1"
PAGE="${2:-https://www.4kcz.com/v_play/bXZfMjM5ODktbm1fMQ==.html}"
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
# 产物落仓库 .tmp-win/（可见、可清）——临时目录别散在 /tmp 或 $TMPDIR
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/.tmp-win/dyrs-headers"
rm -rf "$OUT"; mkdir -p "$OUT"
JAR="$OUT/cookies.txt"

echo "== 播放列表响应头（看有没有 Set-Cookie）=="
curl -s --noproxy '*' -m 25 -A "$UA" -H "Referer: $PAGE" -D "$OUT/m3u8.hdr" \
  -c "$JAR" -o "$OUT/main.m3u8" -w 'http=%{http_code} type=%{content_type} bytes=%{size_download}\n' "$U"
grep -iE '^(set-cookie|via|server|x-)' "$OUT/m3u8.hdr" | head -8
echo "-- cookie jar --"; grep -v '^#' "$JAR" | head -5

SEG=$(grep -v '^#' "$OUT/main.m3u8" | grep -v '^[[:space:]]*$' | head -1)
[ -z "$SEG" ] && { echo "没有分片行，停止"; exit 1; }
FULL="${SEG#/}"; FULL="https://$(printf '%s' "$U" | sed -E 's#^https?://([^/]+).*#\1#')/$FULL"
echo "== 分片 =="; echo "$FULL"

try() {  # try <标题> <额外 curl 参数...>
  local title="$1"; shift
  local f="$OUT/body.bin" h="$OUT/body.hdr"
  local code
  code=$(curl -s --noproxy '*' -m 40 -A "$UA" -D "$h" -o "$f" \
    -w '%{http_code} %{content_type} %{size_download} %{url_effective}' "$@" "$FULL")
  printf '%-34s → %s\n' "$title" "$code" | cut -c1-200
  /usr/bin/python3 - "$f" <<'PY'
import sys
d = open(sys.argv[1], 'rb').read()
SYNC, PKT = 0x47, 188
kind = ("PNG" if d[:8] == b"\x89PNG\r\n\x1a\n" else
        "TS" if d[:1] == b"\x47" else
        "HTML" if d[:5].lower() in (b"<html", b"<!doc") else "?")
offs = [o for o in range(min(len(d), 20000))
        if all(d[o + PKT * i] == SYNC for i in range(6))][:3]
print(f"     首字节 kind={kind} len={len(d)} 对齐偏移={offs} head={d[:16].hex()}")
PY
}

echo
echo "== 矩阵 =="
try "裸 UA（无任何站点头）"
try "页面 Referer" -H "Referer: $PAGE"
try "页面 Referer + Origin" -H "Referer: $PAGE" -H "Origin: https://www.4kcz.com"
try "m3u8 同域 Referer" -H "Referer: ${U%/*}/"
try "带 m3u8 会话 Cookie" -b "$JAR" -H "Referer: $PAGE"
try "带会话 Cookie + Range 闭合" -b "$JAR" -H "Referer: $PAGE" -H "Range: bytes=0-4294967295"
try "带会话 Cookie + Sec-Fetch" -b "$JAR" -H "Referer: $PAGE" \
  -H 'Sec-Fetch-Dest: video' -H 'Sec-Fetch-Mode: no-cors' -H 'Sec-Fetch-Site: cross-site'
try "跟随 302（看推广图长什么样）" -L -H "Referer: $PAGE"
echo "产物: $OUT"
