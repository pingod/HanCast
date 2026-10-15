#!/bin/bash
# 一次性复现：dyrs 采集站的分片到底是不是「披着 PNG 皮的 TS」，皮有多长
# 用法: tools/probe-dyrs-skin.sh <m3u8_url> [referer]
set -u
U="$1"
REF="${2:-https://www.4kcz.com/}"
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
# 产物落仓库 .tmp-win/（可见、可清）——临时目录别散在 /tmp 或 $TMPDIR
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/.tmp-win/dyrs-skin"
rm -rf "$OUT"; mkdir -p "$OUT"
echo "== 拉播放列表 =="
curl -s --noproxy '*' -m 25 -A "$UA" -H "Referer: $REF" -o "$OUT/main.m3u8" \
  -w 'http=%{http_code} type=%{content_type} bytes=%{size_download}\n' "$U"
head -c 500 "$OUT/main.m3u8"; echo
SEG=$(grep -v '^#' "$OUT/main.m3u8" | grep -v '^[[:space:]]*$' | head -1)
[ -z "$SEG" ] && { echo "没有分片行，停止"; exit 1; }
case "$SEG" in
  http*) FULL="$SEG" ;;
  /*) FULL="$(printf '%s' "$U" | sed -E 's#^(https?://[^/]+).*#\1#')$SEG" ;;
  *) FULL="${U%/*}/$SEG" ;;
esac
echo "== 分片地址 =="; echo "$FULL"
for RANGE in "bytes=0-" "bytes=0-4294967295" ""; do
  echo "== Range: '${RANGE:-（不带）}' =="
  HDR=(-A "$UA" -H "Referer: $REF")
  [ -n "$RANGE" ] && HDR+=(-H "Range: $RANGE")
  curl -s --noproxy '*' -m 40 "${HDR[@]}" -o "$OUT/seg.bin" \
    -w 'http=%{http_code} type=%{content_type} bytes=%{size_download}\n' "$FULL"
  /usr/bin/python3 - "$OUT/seg.bin" <<'PY'
import sys
d = open(sys.argv[1], 'rb').read()
print("  头 32 字节:", d[:32].hex())
print("  是否 PNG:", d[:8] == b"\x89PNG\r\n\x1a\n", "| 是否 TS 同步:", d[:1] == b"\x47")
SYNC, PKT = 0x47, 188
hits = []
for off in range(0, min(len(d), 20000)):
    if all(d[off + PKT * i] == SYNC for i in range(6)):
        hits.append(off)
        if len(hits) >= 4:
            break
print("  连续 6 包对齐的偏移(前 4 个):", hits, "| 扫描上限 20000")
PY
done
echo "产物目录: $OUT"
