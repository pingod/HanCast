$ff = "C:\Users\Administrator\AppData\Local\HanCast\ffmpeg\ffmpeg.exe"
foreach ($n in @("old", "a", "b", "c")) {
  $m = "D:\Temp\hcvf\$n.m3u8"
  if (-not (Test-Path $m)) { Write-Output "MISSING $m"; continue }
  $segs = (Get-Content $m | Select-String -Pattern '^[^#].*\.ts$').Count
  $info = & $ff -hide_banner -i $m 2>&1 | Select-String -Pattern "Input #|Stream #"
  Write-Output ("CASE {0}: segs={1}" -f $n, $segs)
  foreach ($l in $info) { Write-Output ("  " + $l.ToString().Trim()) }
}
