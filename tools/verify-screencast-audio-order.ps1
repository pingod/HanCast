# v2.3.11 起产物不再自带 ffmpeg：读上面那条 .cmd 落盘的 manifest，用依赖面板
# 下载到 %APPDATA%\HanCast\bin 的那份探流，没有就退回 PATH 上的系统 ffmpeg。
$ff = Join-Path $env:APPDATA 'HanCast\bin\ffmpeg.exe'
if (-not (Test-Path $ff)) { $ff = "ffmpeg" }
foreach ($n in @("old", "a", "b", "c")) {
  $m = "D:\Temp\hcvf\$n.m3u8"
  if (-not (Test-Path $m)) { Write-Output "MISSING $m"; continue }
  $segs = (Get-Content $m | Select-String -Pattern '^[^#].*\.ts$').Count
  $info = & $ff -hide_banner -i $m 2>&1 | Select-String -Pattern "Input #|Stream #"
  Write-Output ("CASE {0}: segs={1}" -f $n, $segs)
  foreach ($l in $info) { Write-Output ("  " + $l.ToString().Trim()) }
}
