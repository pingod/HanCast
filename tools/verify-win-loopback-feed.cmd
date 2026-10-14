@echo off
REM Why the product screencast command produced no playlist in 8s on .68 (2.2.23,
REM 2026-10-14) when the same layout worked in verify-screencast-audio-order.cmd:
REM the only difference is WHERE pipe:0 gets its bytes. These two cases separate
REM "silent-but-open stdin" from "realtime trickle".
REM
REM Run as an interactive task (gdigrab needs a desktop):
REM   perl -pe 's/\r?\n/\r\n/' tools/verify-win-loopback-feed.cmd > /tmp/hcfeed.cmd
REM   scp /tmp/hcfeed.cmd administrator@192.168.1.68:D:/Temp/hcfeed.cmd
REM   ssh administrator@192.168.1.68 "schtasks /create /f /tn HANCAST_FEED /tr D:\Temp\hcfeed.cmd /sc once /sd 2999-01-01 /st 00:00 /ru Administrator /it"
REM   ssh administrator@192.168.1.68 "schtasks /run /tn HANCAST_FEED"
REM Result: D:\Temp\hcfeed-out.txt  -> D_* shows whether the playlist appears with an
REM empty stdin; E_* shows the realtime-trickle baseline. loglevel is info here, not
REM error, so the stall reason is visible.
setlocal
set FF=C:\Users\Administrator\AppData\Local\HanCast\ffmpeg\ffmpeg.exe
set OUT=D:\Temp\hcfeed
set RES=D:\Temp\hcfeed-out.txt
set MIC=audio=@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{965FC79E-DC31-4CFD-86F0-7C2E01B067EB}
set MIX=[1:a]aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo[m0];[2:a]aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo[m1];[m0][m1]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0
set VF=scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2
rmdir /s /q %OUT% >nul 2>&1
mkdir %OUT%
del %RES% >nul 2>&1

echo === D: stdin open but SILENT (no loopback bytes at all) === >> %RES%
REM powershell sleeps 20s writing nothing: stdin stays open, zero bytes delivered
powershell -NoProfile -Command "Start-Sleep 20" | "%FF%" -hide_banner -loglevel info -f gdigrab -framerate 15 -i desktop -f dshow -i "%MIC%" -f s16le -ar 48000 -ac 2 -i pipe:0 -filter_complex "%MIX%" -vf "%VF%" -c:v libx264 -preset veryfast -tune zerolatency -profile:v main -level 42 -pix_fmt yuv420p -g 15 -r 15 -c:a aac -b:a 128k -ar 48000 -ac 2 -t 8 -f hls -hls_time 1 -hls_list_size 6 -hls_flags delete_segments+independent_segments+temp_file -hls_segment_filename %OUT%\d%%03d.ts %OUT%\d.m3u8 2>> %RES%
echo D_EXIT=%errorlevel% >> %RES%
for %%F in (%OUT%\d.m3u8) do echo D_M3U8_AGE_SECS_SEE_DIR >> %RES%
dir %OUT%\d*.ts >> %RES% 2>&1

echo === E: stdin trickled at realtime (192000 B/s of zeros) === >> %RES%
powershell -NoProfile -Command "$s=[Console]::OpenStandardOutput();$b=New-Object byte[] 3840;1..420|%%{if($s.CanWrite){$s.Write($b,0,3840);$s.Flush()};Start-Sleep -m 20}" | "%FF%" -hide_banner -loglevel info -f gdigrab -framerate 15 -i desktop -f dshow -i "%MIC%" -f s16le -ar 48000 -ac 2 -i pipe:0 -filter_complex "%MIX%" -vf "%VF%" -c:v libx264 -preset veryfast -tune zerolatency -profile:v main -level 42 -pix_fmt yuv420p -g 15 -r 15 -c:a aac -b:a 128k -ar 48000 -ac 2 -t 8 -f hls -hls_time 1 -hls_list_size 6 -hls_flags delete_segments+independent_segments+temp_file -hls_segment_filename %OUT%\e%%03d.ts %OUT%\e.m3u8 2>> %RES%
echo E_EXIT=%errorlevel% >> %RES%
dir %OUT%\e*.ts >> %RES% 2>&1

echo === F: mic only, no audio stdin, sanity for gdigrab itself === >> %RES%
"%FF%" -hide_banner -loglevel info -f gdigrab -framerate 15 -i desktop -vf "%VF%" -c:v libx264 -preset veryfast -profile:v main -pix_fmt yuv420p -g 15 -r 15 -t 6 -an -f hls -hls_time 1 -hls_list_size 6 -hls_flags delete_segments+independent_segments+temp_file -hls_segment_filename %OUT%\f%%03d.ts %OUT%\f.m3u8 >> %RES% 2>&1
echo F_EXIT=%errorlevel% >> %RES%
dir %OUT%\f*.ts >> %RES% 2>&1

REM G/H add the product's "-nostdin" on top of E/D, and stamp when the playlist
REM file was FIRST created (dir /tc), not last rewritten. G decides whether the
REM sidecar's pipe is being read at all under -nostdin; H is the negative control
REM for "input starved => no playlist inside the product's 8s window".
set TRICKLE=powershell -NoProfile -Command "$s=[Console]::OpenStandardOutput();$b=New-Object byte[] 3840;1..420|%%{if($s.CanWrite){$s.Write($b,0,3840);$s.Flush()};Start-Sleep -m 20}"

echo === G: E + -nostdin (product stdin handling) === >> %RES%
echo G_START=%TIME% >> %RES%
%TRICKLE% | "%FF%" -hide_banner -loglevel info -nostdin -f gdigrab -framerate 15 -i desktop -f dshow -i "%MIC%" -f s16le -ar 48000 -ac 2 -i pipe:0 -filter_complex "%MIX%" -vf "%VF%" -c:v libx264 -preset veryfast -tune zerolatency -profile:v main -level 42 -pix_fmt yuv420p -g 15 -r 15 -c:a aac -b:a 128k -ar 48000 -ac 2 -t 8 -f hls -hls_time 1 -hls_list_size 6 -hls_flags delete_segments+independent_segments+temp_file -hls_segment_filename %OUT%\g%%03d.ts %OUT%\g.m3u8 2>> %RES%
echo G_EXIT=%errorlevel% >> %RES%
for /f "tokens=1-4" %%a in ('dir /tc %OUT%\g.m3u8 2^>^&1 ^| findstr /i "g.m3u8"') do echo G_M3U8_CREATED=%%a %%b %%c %%d >> %RES%
dir %OUT%\g*.ts >> %RES% 2>&1

echo === H: D + -nostdin (starved pipe, expect no playlist before exit) === >> %RES%
echo H_START=%TIME% >> %RES%
powershell -NoProfile -Command "Start-Sleep 20" | "%FF%" -hide_banner -loglevel info -nostdin -f gdigrab -framerate 15 -i desktop -f dshow -i "%MIC%" -f s16le -ar 48000 -ac 2 -i pipe:0 -filter_complex "%MIX%" -vf "%VF%" -c:v libx264 -preset veryfast -tune zerolatency -profile:v main -level 42 -pix_fmt yuv420p -g 15 -r 15 -c:a aac -b:a 128k -ar 48000 -ac 2 -t 8 -f hls -hls_time 1 -hls_list_size 6 -hls_flags delete_segments+independent_segments+temp_file -hls_segment_filename %OUT%\h%%03d.ts %OUT%\h.m3u8 2>> %RES%
echo H_EXIT=%errorlevel% >> %RES%
for /f "tokens=1-4" %%a in ('dir /tc %OUT%\h.m3u8 2^>^&1 ^| findstr /i "h.m3u8"') do echo H_M3U8_CREATED=%%a %%b %%c %%d >> %RES%
dir %OUT%\h*.ts >> %RES% 2>&1
endlocal
