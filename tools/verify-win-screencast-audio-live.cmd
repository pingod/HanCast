@echo off
REM Live acceptance for screen-cast audio on Windows: drive the SHIPPED sidecar
REM over its real stdin/stdout JSON protocol, so the ffmpeg command line is built
REM by product code (screencast/capture.py). A hand-written ffmpeg line can only
REM prove an argument layout; this proves the layout the app actually emits.
REM
REM Why a request FILE: device names on this machine are Chinese, and UTF-8
REM Chinese inside a .cmd breaks cmd's byte-offset re-read of the file. Every
REM request line is therefore generated as pure ASCII (json.dumps ensure_ascii,
REM plus ( ) & < > | escaped as \uXXXX) by tools/gen-win-screencast-live-req.py.
REM
REM How to run (gdigrab needs an interactive desktop, so it must be a task, not ssh):
REM   perl -pe 's/\r?\n/\r\n/' tools/verify-win-screencast-audio-live.cmd > /tmp/hcsl.cmd
REM   scp /tmp/hcsl.cmd administrator@192.168.1.68:D:/Temp/hcsl.cmd
REM   ssh administrator@192.168.1.68 "schtasks /create /f /tn HANCAST_HCSL /tr D:\Temp\hcsl.cmd /sc once /sd 2999-01-01 /st 00:00 /ru Administrator /it"
REM   ssh administrator@192.168.1.68 "schtasks /run /tn HANCAST_HCSL"
REM Result: D:\Temp\hcsl\out.txt  -> expect (rounds: 3/6/9 = mic+system, system, mic)
REM   every start "ok":true         (capture started and the playlist landed)
REM   every status "running":true   (still alive ~3s later, i.e. no EINVAL early exit)
REM   SIDECAR_EXIT=0                (no 0xc0000005 on the stop path)
REM   no "Option vf cannot be applied to input url"
REM   D:\Temp\hcsl\seg.txt       stamped segment snapshots taken WHILE each round ran
REM Cleanup afterwards: schtasks /delete /tn HANCAST_HCSL /f  and  rmdir /s /q D:\Temp\hcsl
setlocal
set ROOT=C:\Users\Administrator\AppData\Local\HanCast
set WORK=D:\Temp\hcsl
set REQ=%WORK%\req.txt
set OUT=%WORK%\out.txt
set HLS=C:\Users\Administrator\AppData\Local\Temp\hancast_screencast_hls

if not exist "%REQ%" (
  echo [fail] missing %REQ% - generate it with tools/gen-win-screencast-live-req.py 1>&2
  exit /b 2
)

REM One instance at a time: the GUI app owns the same command loop and ports.
taskkill /IM HanCast.exe /F >nul 2>&1
taskkill /IM hancast-sidecar.exe /F >nul 2>&1
rmdir /s /q "%HLS%" >nul 2>&1
if not exist "%WORK%" mkdir "%WORK%"
del "%OUT%" >nul 2>&1
del "%WORK%\seg*.txt" >nul 2>&1

REM Snapshot the HLS dir every ~5s for ~100s, stamped with the wall clock: three
REM rounds run back to back (mic+system, system only, mic only) and each round
REM blocks in start_hls until its playlist lands, so a 60s window can stop before
REM the last round produces segments. A bare "before/after" listing cannot tell
REM which round made which segments; stop and cleanup delete the session dir,
REM hence sampling WHILE the capture runs.
start /b cmd /c "for /l %%i in (1,1,20) do (ping -n 6 127.0.0.1 >nul & call echo SNAP %%TIME%% & dir /b %HLS%)" > "%WORK%\seg.txt" 2>&1

REM 3s after each request. start_hls blocks until the playlist exists, so an
REM "ok" response already means ffmpeg produced output with the audio input
REM attached; the sleeps then keep each case running long enough for status.
(
  for /f "usebackq tokens=* delims=" %%L in ("%REQ%") do (
    echo %%L
    ping -n 4 127.0.0.1 >nul
  )
) | "%ROOT%\hancast-sidecar.exe" > "%OUT%" 2>&1

echo SIDECAR_EXIT=%errorlevel% >> "%OUT%"
ping -n 3 127.0.0.1 >nul
taskkill /IM hancast-sidecar.exe /F >nul 2>&1
endlocal
