#!/usr/bin/env bash
# cargo check 的本地前置：打包资源在 CI 才生成，本机 cargo 会因资源 glob 落空直接失败。
# 这里按 tauri 配置里的 resources 模式建「一次性空桩」，check 完只删自己建的文件。
#
# 用法: tools/rust-check.sh [cargo check 参数...]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAURI="$ROOT/src-tauri"
CREATED=()

cleanup() {
  # 逆序删：先删文件再删空目录，绝不动仓库里已存在的路径
  for ((i=${#CREATED[@]}-1; i>=0; i--)); do
    rm -f "${CREATED[$i]}"
  done
  for ((i=${#CREATED[@]}-1; i>=0; i--)); do
    rmdir -p "$(dirname "${CREATED[$i]}")" 2>/dev/null || true
  done
}
trap cleanup EXIT

stub() {
  local path="$1"
  if [ -e "$path" ]; then
    echo "已存在，跳过: ${path#$TAURI/}" >&2
    return
  fi
  mkdir -p "$(dirname "$path")"
  : > "$path"
  CREATED+=("$path")
  echo "建桩: ${path#$TAURI/}"
}

cd "$TAURI"

# externalBin 要求 <name>-<target-triple> 可执行文件
TRIPLE="$(rustc -vV 2>/dev/null | awk -F': ' '/^host/{print $2}')"
stub "hancast-sidecar/hancast-sidecar-${TRIPLE}"
chmod +x "hancast-sidecar/hancast-sidecar-${TRIPLE}" 2>/dev/null || true

# resources 里的每一项都要至少命中一个文件
stub "mpv/mpv.app/Contents/MacOS/mpv"
stub "mpv/portable_config/mpv.conf"
stub "libdummy.so"
stub "libdummy.dylib"
for pkg in certifi lxml charset_normalizer curl_cffi hancast_sidecar; do
  stub "$pkg/__init__.py"
done

cargo check "$@"
