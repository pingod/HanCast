# Verify the PACKAGED sidecar's FFmpeg dependency chain on a real Windows box.
#
# Why a script instead of pytest: detection order (manual -> system -> managed dir)
# and the on-demand download both depend on the machine's own PATH / winget layout,
# and the download sources were never fetched for real. Source-level tests can only
# prove the code shape; this proves the artifact on hardware.
#
# Run from the install dir of the packaged app (not from source):
#   powershell -NoProfile -ExecutionPolicy Bypass -File verify-ffmpeg-detect.ps1
#   powershell ... -File verify-ffmpeg-detect.ps1 -Download          # also test download
#   powershell ... -File verify-ffmpeg-detect.ps1 -Exe D:\path\hancast-sidecar.exe
#
# Exit codes: 0 = expectations met, 1 = detection/download failed, 2 = setup problem.
# Keep this file ASCII-only: PS 5.1 reads BOM-less UTF-8 as GBK and mangles the next line.

param(
    [string]$Exe = "$env:LOCALAPPDATA\HanCast\hancast-sidecar.exe",
    [switch]$Download,
    # Sandbox mode: point APPDATA/LOCALAPPDATA/USERPROFILE at a throwaway dir.
    # On nt the winget candidate dirs are built FROM LOCALAPPDATA (ffmpeg_deps
    # _extra_dirs), so redirecting it removes the machine's real FFmpeg from the
    # search order without touching the user's winget install or settings. That is
    # the only way to reproduce "a machine with no FFmpeg" on a machine that has it.
    [switch]$TempAppData,
    # Cut the inherited PATH down to Windows itself. The machine's winget FFmpeg is
    # reachable through PATH (that is the evidence for the "system" rung), so the
    # only way to exercise the lower rungs of the ladder is to take PATH away too.
    [switch]$BarePath,
    # Full path to some real ffmpeg.exe to drop into the managed dir. Together with
    # -TempAppData -BarePath this proves the third rung (source=downloaded) without
    # waiting on a 114 MB download over a 18 KB/s line.
    [string]$SeedDownloaded,
    [int]$TimeoutSec = 900
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Exe)) {
    "FAIL: sidecar executable not found: $Exe"
    exit 2
}
"sidecar: $Exe"

$psi = New-Object System.Diagnostics.ProcessStartInfo($Exe)
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
# stderr deliberately NOT redirected: the pipe buffer would eventually fill and
# deadlock the sidecar mid-download. Let it print to the console / ssh channel.
$psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$psi.WorkingDirectory = Split-Path -Parent $Exe

$sandbox = $null
if ($TempAppData) {
    $sandbox = Join-Path $env:TEMP ("hc-ffmpeg-probe-" + (Get-Date -Format 'yyyyMMddHHmmss'))
    New-Item -ItemType Directory -Force -Path $sandbox | Out-Null
    $psi.EnvironmentVariables['APPDATA'] = $sandbox
    $psi.EnvironmentVariables['LOCALAPPDATA'] = $sandbox
    $psi.EnvironmentVariables['USERPROFILE'] = $sandbox
    "sandbox: $sandbox (APPDATA/LOCALAPPDATA/USERPROFILE redirected)"
}
if ($BarePath) {
    # winget's Links dir is on the user PATH, so PATH has to go as well or the
    # "system" rung always wins and the lower rungs stay unobservable.
    $psi.EnvironmentVariables['PATH'] = 'C:\Windows\system32;C:\Windows'
    "PATH reduced to Windows itself"
}
$proc = [System.Diagnostics.Process]::Start($psi)

$script:Id = 0

function Invoke-Sidecar([string]$Cmd, [hashtable]$Params, [int]$WaitMs) {
    $script:Id = $script:Id + 1
    $body = if ($Params) { $Params } else { @{} }
    $payload = [ordered]@{ id = $script:Id; cmd = $Cmd; params = $body } |
        ConvertTo-Json -Compress
    $proc.StandardInput.WriteLine($payload)
    $proc.StandardInput.Flush()

    $deadline = (Get-Date).AddMilliseconds($WaitMs)
    while ($true) {
        # One ReadLineAsync task reused across waits: issuing a second pending read
        # while the first is still running would silently swallow lines.
        if (-not $script:ReadTask) { $script:ReadTask = $proc.StandardOutput.ReadLineAsync() }
        if (-not $script:ReadTask.Wait(2000)) {
            if ((Get-Date) -gt $deadline) {
                "TIMEOUT waiting for response to '$Cmd' ($WaitMs ms)"
                return $null
            }
            continue
        }
        $line = $script:ReadTask.Result
        $script:ReadTask = $null
        if ($null -eq $line) { "EOF on sidecar stdout while waiting for '$Cmd'"; return $null }
        if ($line.Trim().Length -eq 0) { continue }
        $obj = $line | ConvertFrom-Json
        if ($obj.PSObject.Properties['id'] -and $obj.id -eq $script:Id) { return $obj }
        "event> $line"
    }
}

function Show-Status($data) {
    "  ok            : $($data.ok)"
    "  source        : $($data.source)"
    "  path          : $($data.path)"
    "  version       : $($data.version)"
    "  ffprobe_path  : $($data.ffprobe_path)"
    "  managed_dir   : $($data.managed_dir)"
    "  platform/arch : $($data.platform) / $($data.arch)"
    "  missing       : $($data.missing -join ', ')"
    "  optional      : $($data.optional_missing -join ', ')"
    "  download      : $($data.download)"
    "  error         : $($data.error)"
    foreach ($item in $data.items) {
        "  item $($item.key): found=$($item.found) required=$($item.required) " +
            "source=$($item.source) path=$($item.path)"
    }
    if ($data.install_commands) {
        foreach ($c in $data.install_commands) { "  install hint  : $c" }
    }
}

Write-Host "== 1. ffmpeg_status (refresh=true) =="
$status = Invoke-Sidecar 'ffmpeg_status' @{ refresh = $true } 60000
if (-not $status) { $proc.Kill(); exit 2 }
if (-not $status.success) { "FAIL: ffmpeg_status error: $($status.error)"; $proc.Kill(); exit 2 }
Show-Status $status.data

if ($SeedDownloaded) {
    Write-Host "== 1b. seed the managed dir, expect source=downloaded =="
    if (-not (Test-Path -LiteralPath $SeedDownloaded)) {
        "FAIL: seed binary not found: $SeedDownloaded"
        try { $proc.StandardInput.Close() } catch {}
        exit 2
    }
    $managed = $status.data.managed_dir
    if (-not $managed) {
        "FAIL: ffmpeg_status returned no managed_dir"
        try { $proc.StandardInput.Close() } catch {}
        exit 2
    }
    New-Item -ItemType Directory -Force -Path $managed | Out-Null
    Copy-Item -LiteralPath $SeedDownloaded -Destination (Join-Path $managed 'ffmpeg.exe') -Force
    $siblingProbe = Join-Path (Split-Path -Parent $SeedDownloaded) 'ffprobe.exe'
    if (Test-Path -LiteralPath $siblingProbe) {
        Copy-Item -LiteralPath $siblingProbe -Destination (Join-Path $managed 'ffprobe.exe') -Force
    }
    $seeded = Invoke-Sidecar 'ffmpeg_status' @{ refresh = $true } 60000
    if (-not $seeded -or -not $seeded.success) {
        "FAIL: status after seeding did not answer: $($seeded.error)"
        try { $proc.StandardInput.Close() } catch {}
        exit 1
    }
    Show-Status $seeded.data
    if ($seeded.data.source -eq 'downloaded') {
        "PASS: managed-dir copy adopted (source=downloaded, path=$($seeded.data.path))"
    } else {
        "FAIL: expected source=downloaded after seeding, got '$($seeded.data.source)' ($($seeded.data.path))"
        try { $proc.StandardInput.Close() } catch {}
        exit 1
    }
    # $status keeps the clean-machine reading: section 5 judges the missing payload.
}

if ($Download) {
    Write-Host "== 2. ffmpeg_download_start =="
    $start = Invoke-Sidecar 'ffmpeg_download_start' @{} 30000
    if (-not $start) { $proc.Kill(); exit 2 }
    if (-not $start.success) {
        "FAIL: download could not start: $($start.error)"
        $proc.Kill(); exit 1
    }
    "  task: $($start.data.task_id) url: $($start.data.url)"

    Write-Host "== 3. poll ffmpeg_download_status =="
    $pollDeadline = (Get-Date).AddSeconds($TimeoutSec)
    $lastStage = ''
    $final = $null
    while ((Get-Date) -lt $pollDeadline) {
        Start-Sleep -Seconds 2
        $st = Invoke-Sidecar 'ffmpeg_download_status' @{} 30000
        if (-not $st) { continue }
        if (-not $st.success) { "FAIL: status poll error: $($st.error)"; $proc.Kill(); exit 1 }
        $d = $st.data
        $line = "  stage=$($d.stage) status=$($d.status) received=$($d.received)/$($d.total) " +
                "percent=$($d.percent) speed=$($d.speed)"
        if ($d.stage -ne $lastStage) { $line += "  <-- stage change"; $lastStage = $d.stage }
        $line
        if ($d.status -eq 'done' -or $d.status -eq 'error') { $final = $d; break }
    }
    if (-not $final) { "TIMEOUT: download did not finish in $TimeoutSec s"; $proc.Kill(); exit 1 }
    if ($final.status -eq 'error') { "FAIL: download error: $($final.error)"; $proc.Kill(); exit 1 }
    "  result: $($final.result)"

    Write-Host "== 4. ffmpeg_status after download =="
    $after = Invoke-Sidecar 'ffmpeg_status' @{ refresh = $true } 60000
    if (-not $after) { $proc.Kill(); exit 2 }
    Show-Status $after.data
    $managed = $after.data.managed_dir
    $downloadedExe = Join-Path $managed 'ffmpeg.exe'
    if (-not (Test-Path -LiteralPath $downloadedExe)) {
        "FAIL: task said done but $downloadedExe is not there"
        $proc.Kill(); exit 1
    }
    $v = & $downloadedExe -version 2>$null | Select-Object -First 1
    if (-not $v) {
        "FAIL: the downloaded ffmpeg.exe will not run: $downloadedExe"
        $proc.Kill(); exit 1
    }
    "  downloaded binary runs: $v"
    if ($after.data.source -eq 'downloaded') {
        "  source=downloaded (nothing nearer in the search order)"
    } else {
        # By design the system copy wins over the managed dir, so a machine that
        # still has an FFmpeg on disk reports source=system even after a download.
        "  source=$($after.data.source) (system/manual still outranks the managed dir - expected outside -TempAppData)"
    }
}

Write-Host "== 5. expectations =="
$data = $status.data
$expectMissing = [bool]$TempAppData
if ($expectMissing -and -not $data.ok) {
    if (-not $data.missing -or -not $data.install_commands -or -not $data.download.available) {
        "FAIL: missing-FFmpeg payload incomplete (modal would render nothing)"
        "  missing=$($data.missing) install=$($data.install_commands) download=$($data.download)"
        $proc.Kill(); exit 1
    }
    "PASS: clean machine reports missing + install hints + a download source"
} elseif ($expectMissing) {
    "NOTE: sandbox still found an FFmpeg ($($data.path)) - an absolute candidate dir"
    "      (e.g. C:\ffmpeg\bin or chocolatey) is reachable regardless of LOCALAPPDATA."
} elseif ($data.ok -and $data.source -eq 'system') {
    "PASS: system FFmpeg detected via the search order (no download needed)"
} elseif ($data.ok) {
    "PASS: FFmpeg detected (source=$($data.source), path=$($data.path))"
} else {
    "NOTE: FFmpeg not detected on this machine. That is the expected precondition " +
        "for the dependency modal; re-run with -Download to exercise the download path."
    $proc.Kill(); exit 1
}

try { $proc.StandardInput.Close() } catch {}
if (-not $proc.WaitForExit(10000)) { $proc.Kill() }
if ($sandbox -and (Test-Path -LiteralPath $sandbox)) {
    Remove-Item -LiteralPath $sandbox -Recurse -Force
    "sandbox removed: $sandbox"
}
exit 0
