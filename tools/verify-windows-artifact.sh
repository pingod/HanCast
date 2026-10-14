#!/bin/bash
# 用法: tools/verify-windows-artifact.sh <version> [rel ...]
# 例:   tools/verify-windows-artifact.sh 2.3.6
#
# 验收装在 .68（administrator@192.168.1.68）上的 CI 产物本身：
# 1) 注册表 DisplayVersion 必须等于 <version>；
# 2) 安装目录里那份**源码级 .py** 逐个与 `git show v<version>:…` 比对。
#    Windows 包里的 .py 全是 CRLF，所以两边都 `tr -d '\r'` 后再比哈希
#    （直接 certutil 比 tag 哈希必然「不一致」，会把装对的版本判成装错）。
# 不碰 UI、不打任何补丁。任一项 FAIL = 这版产物在他手上不可信，别继续让他测。
set -uo pipefail

VER="${1:?用法: $0 <version> [rel ...]}"
shift
TAG="v$VER"
WIN=administrator@192.168.1.68
SIDE='C:\Users\Administrator\AppData\Local\HanCast\hancast_sidecar'

# 默认清单：本轮及最近几轮「判修复在不在」的关键文件
RELS=("$@")
if [ ${#RELS[@]} -eq 0 ]; then
  RELS=(protocol/dlna.py protocol/server.py params.py media/failures.py
        utils/identity.py media/rules/jsruntime.py media/rules/jsplugins.py
        media/share_resolver.py media/server.py screencast/capture.py
        screencast/loopback.py main.py)
fi

fails=0
say() { printf '%s\n' "$*"; }

git rev-parse -q --verify "$TAG" >/dev/null || { say "FAIL 本地没有 tag $TAG"; exit 9; }

REG=$(ssh -o ConnectTimeout=10 "$WIN" \
  "reg query HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\HanCast /v DisplayVersion" \
  2>&1 | tr -d '\r' | sed -n 's/.*DisplayVersion *REG_SZ *//p')
if [ "$REG" = "$VER" ]; then say "PASS 注册表 DisplayVersion ($VER)"; else
  say "FAIL 注册表 DisplayVersion: got '$REG' want '$VER'"; fails=$((fails+1)); fi

# 远端一次算完：逐文件去 CRLF 后取 SHA256（脚本体只写 ASCII，PS 5.1 会把无 BOM
# 的 UTF-8 当 GBK 读，中文注释会吞掉下一行首字符）
PS1_REMOTE='D:\Temp\hc_verify.ps1'
ssh -o ConnectTimeout=10 "$WIN" "if not exist D:\Temp md D:\Temp" >/dev/null 2>&1
LIST_FILE=$(mktemp)
printf '%s\n' "${RELS[@]}" > "$LIST_FILE"
scp -q "$LIST_FILE" "$WIN:D:/Temp/hc_verify.txt" 2>/dev/null
rm -f "$LIST_FILE"
cat > /tmp/hc_verify.ps1 <<'EOF'
$base = 'C:\Users\Administrator\AppData\Local\HanCast\hancast_sidecar'
Get-Content 'D:\Temp\hc_verify.txt' | ForEach-Object {
  $rel = $_.Trim()
  if ($rel -eq '') { return }
  $f = Join-Path $base ($rel -replace '/', '\')
  if (-not (Test-Path $f)) { Write-Output ($rel + ' MISSING'); return }
  $t = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($f)).Replace("`r`n", "`n")
  $b = [Text.Encoding]::UTF8.GetBytes($t)
  $h = (Get-FileHash -Algorithm SHA256 -InputStream (New-Object IO.MemoryStream(,$b))).Hash
  Write-Output ($rel + ' ' + $h.ToLower())
}
EOF
scp -q /tmp/hc_verify.ps1 "$WIN:D:/Temp/hc_verify.ps1"
rm -f /tmp/hc_verify.ps1
REMOTE_OUT=$(ssh -o ConnectTimeout=10 "$WIN" \
  "powershell -NoProfile -ExecutionPolicy Bypass -File D:\Temp\hc_verify.ps1" 2>&1 | tr -d '\r')

for rel in "${RELS[@]}"; do
  want=$(git show "$TAG:hancast-backend/hancast_sidecar/$rel" 2>/dev/null \
         | tr -d '\r' | shasum -a 256 | cut -d' ' -f1)
  if [ -z "$want" ]; then say "SKIP $rel（$TAG 的 git 树里没有这个文件）"; continue; fi
  got=$(printf '%s\n' "$REMOTE_OUT" | awk -v r="$rel" '$1==r{print $2; exit}')
  if [ "$got" = "$want" ]; then say "PASS $rel (${want:0:12})"
  else say "FAIL $rel: got '${got:-无输出}' want '${want:0:12}…'"; fails=$((fails+1)); fi
done

# 在跑的进程版本（判他测的到底是哪版：StartTime 早于安装时间 = 还没重启）
ssh -o ConnectTimeout=10 "$WIN" \
  "powershell -NoProfile -Command \"Get-Process HanCast,hancast-sidecar -ErrorAction SilentlyContinue | Select-Object Name,Id,StartTime,@{n='Ver';e={\$_.MainModule.FileVersionInfo.ProductVersion}} | Format-Table -AutoSize | Out-String -Width 200\"" \
  2>&1 | tr -d '\r' | grep -vE 'decrypt later|openssh.com/pq|^\*\*|^$'

ssh -o ConnectTimeout=10 "$WIN" "del D:\Temp\hc_verify.ps1 D:\Temp\hc_verify.txt 2>nul" >/dev/null 2>&1
if [ $fails -gt 0 ]; then say "--- $fails 项 FAIL"; exit 1; fi
say "--- ALL PASS ($VER @ .68)"
