@echo off
REM Verify ffmpeg argument ORDER for "screen + mic / system sound" casting on Windows.
REM
REM Why a script: when -vf sits between two -i, ffmpeg assigns it to the NEXT input
REM (the microphone) and dies with
REM   Option vf cannot be applied to input url audio=... / Invalid argument
REM Unit tests can only assert the order; only a real machine + the bundled
REM ffmpeg 6.1.1 can prove it. Keep this file ASCII-only: cmd mis-slices byte
REM offsets when a .cmd carries UTF-8 Chinese.
REM
REM Three cases:
REM   OLD  -vf between inputs   -> must FAIL (reproduces v2.2.22 and earlier)
REM   A    -vf after all inputs, mic only                -> must PASS
REM   B    -vf after all inputs, mic + loopback s16le +
REM        -filter_complex amix (loopback fed from a silence file; what is under
REM        test is option position, not the data source)  -> must PASS
REM
REM Usage (file must be CRLF on the wire; repo copy is LF):
REM   perl -pe 's/\r?\n/\r\n/' tools/verify-screencast-audio-order.cmd > /tmp/hcvf.cmd
REM   scp /tmp/hcvf.cmd administrator@192.168.1.68:D:/Temp/hcvf.cmd
REM   ssh administrator@192.168.1.68 "schtasks /create /f /tn HANCAST_VFT /tr D:\Temp\hcvf.cmd /sc once /sd 2999-01-01 /st 00:00 /ru Administrator /it"
REM   ssh administrator@192.168.1.68 "schtasks /run /tn HANCAST_VFT"
REM   (must run as a task: gdigrab needs an interactive desktop, ssh-spawned
REM    processes have none)
REM Result: D:\Temp\hcvf-out.txt  -> OLD_EXIT non-zero, A_EXIT/B_EXIT zero, and each
REM of a.m3u8 / b.m3u8 reporting one video + one aac stream. Afterwards delete the
REM task and D:\Temp\hcvf*.
setlocal
set FF=C:\Users\Administrator\AppData\Local\HanCast\ffmpeg\ffmpeg.exe
set OUT=D:\Temp\hcvf
set MIC=audio=@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{965FC79E-DC31-4CFD-86F0-7C2E01B067EB}
set VF=scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2
rmdir /s /q %OUT% >nul 2>&1
mkdir %OUT%
powershell -NoProfile -Command "[IO.File]::WriteAllBytes('D:\Temp\hcvf\silence.raw',(New-Object byte[] 600000))"

echo === OLD: -vf between the two inputs ===
"%FF%" -hide_banner -loglevel error -nostdin -f gdigrab -framerate 15 -i desktop -vf "%VF%" -f dshow -i "%MIC%" -c:v libx264 -preset veryfast -profile:v main -level 42 -pix_fmt yuv420p -c:a aac -t 3 -f hls -hls_time 1 -hls_list_size 6 -hls_flags delete_segments+independent_segments+temp_file -hls_segment_filename %OUT%\old%%03d.ts %OUT%\old.m3u8
echo OLD_EXIT=%errorlevel%

echo === A: -vf after all inputs, mic only ===
"%FF%" -hide_banner -loglevel error -nostdin -f gdigrab -framerate 15 -i desktop -f dshow -i "%MIC%" -vf "%VF%" -c:v libx264 -preset veryfast -tune zerolatency -profile:v main -level 42 -pix_fmt yuv420p -g 45 -r 15 -c:a aac -b:a 128k -ar 48000 -ac 2 -t 3 -f hls -hls_time 1 -hls_list_size 6 -hls_flags delete_segments+independent_segments+temp_file -hls_segment_filename %OUT%\a%%03d.ts %OUT%\a.m3u8
echo A_EXIT=%errorlevel%

echo === B: mic + loopback s16le + filter_complex, -vf still after inputs ===
"%FF%" -hide_banner -loglevel error -nostdin -f gdigrab -framerate 15 -i desktop -f dshow -i "%MIC%" -f s16le -ar 48000 -ac 2 -i %OUT%\silence.raw -filter_complex "[1:a]aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo[m0];[2:a]aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo[m1];[m0][m1]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0" -vf "%VF%" -c:v libx264 -preset veryfast -tune zerolatency -profile:v main -level 42 -pix_fmt yuv420p -g 45 -r 15 -c:a aac -b:a 128k -ar 48000 -ac 2 -t 3 -f hls -hls_time 1 -hls_list_size 6 -hls_flags delete_segments+independent_segments+temp_file -hls_segment_filename %OUT%\b%%03d.ts %OUT%\b.m3u8
echo B_EXIT=%errorlevel%

echo === C: same as B but the loopback really comes from stdin pipe:0, with -nostdin ===
"%FF%" -hide_banner -loglevel error -nostdin -f gdigrab -framerate 15 -i desktop -f dshow -i "%MIC%" -f s16le -ar 48000 -ac 2 -i pipe:0 -filter_complex "[1:a]aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo[m0];[2:a]aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo[m1];[m0][m1]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0" -vf "%VF%" -c:v libx264 -preset veryfast -tune zerolatency -profile:v main -level 42 -pix_fmt yuv420p -g 45 -r 15 -c:a aac -b:a 128k -ar 48000 -ac 2 -t 3 -f hls -hls_time 1 -hls_list_size 6 -hls_flags delete_segments+independent_segments+temp_file -hls_segment_filename %OUT%\c%%03d.ts %OUT%\c.m3u8< %OUT%\silence.raw
echo C_EXIT=%errorlevel%

echo === outputs ===
dir /b %OUT%
echo === stream info ===
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Temp\hcvf-check.ps1
echo DONE
