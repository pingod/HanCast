<#
verify-win-capture-sources.ps1

Grabs one PNG per Windows screen-capture variant so a human can judge what
ffmpeg gdigrab actually delivers on this box:

  desktop.png        -f gdigrab -i desktop            (GetDC(NULL), DWM composite)
  monitor<N>.png     offset_x/offset_y/video_size     (region mode, per monitor)
  win<N>_hwnd.png    -i hwnd=0x...                    (window mode, GetDC(hwnd))
  win<N>_region.png  region mode over the window rect (candidate fix path)

Also dumps everything that decides the coordinate spaces in play:
  - this process' DPI awareness + per-monitor DPI (GetDpiForMonitor)
  - monitor rects as this process sees them
  - every visible window (hwnd / rect / title)
  - each produced PNG real pixel size
so the manifest.txt is usable as evidence on its own.

Run it in the INTERACTIVE session (a service/ssh session has no desktop and
gdigrab returns black). Use a one-shot scheduled task:
  schtasks /create /f /tn hc_capchk /tr "powershell -NoProfile -ExecutionPolicy Bypass -File D:\Temp\verify-win-capture-sources.ps1" /sc once /sd 2999-01-01 /st 00:00 /ru Administrator /it
  schtasks /run /tn hc_capchk

Keep the file ASCII-only: Windows PowerShell 5.1 reads BOM-less scripts as
the ANSI codepage and non-ASCII comments corrupt the following line.

Exit code = number of failed ffmpeg grabs (0 = every grab produced a PNG).
#>

param(
    [string]$Ffmpeg = "ffmpeg",
    [string]$OutDir = "D:\Temp\capture-check",
    [int]$MaxWindows = 6
)

$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Collections.Generic;

public class WCap {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)]
    public struct MONITORINFO { public int cbSize; public RECT rcMonitor; public RECT rcWork; public uint dwFlags; }

    public class MonInfo {
        public int X, Y, W, H;
        public bool Primary;
        public uint DpiX, DpiY;
    }
    public class WinInfo {
        public long Hwnd; public string Title;
        public int X, Y, W, H; public bool Iconic;
    }

    delegate bool MonitorEnumProc(IntPtr h, IntPtr hdc, ref RECT r, IntPtr d);
    delegate bool EnumWindowProc(IntPtr h, IntPtr d);

    [DllImport("user32.dll")] static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorEnumProc cb, IntPtr d);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern bool GetMonitorInfoW(IntPtr h, ref MONITORINFO mi);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowProc cb, IntPtr d);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowTextW(IntPtr h, StringBuilder sb, int max);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowTextLengthW(IntPtr h);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int index);
    [DllImport("shcore.dll")] static extern int GetDpiForMonitor(IntPtr hmon, int type, out uint x, out uint y);
    [DllImport("shcore.dll")] static extern int GetProcessDpiAwareness(IntPtr proc, out int awareness);

    public static string ProcessDpiAwareness() {
        int a;
        try {
            if (GetProcessDpiAwareness(IntPtr.Zero, out a) != 0) return "unknown";
            switch (a) {
                case 0: return "unaware";
                case 1: return "system-aware";
                case 2: return "per-monitor-aware";
                default: return "code " + a;
            }
        } catch { return "unavailable (shcore missing)"; }
    }

    public static MonInfo[] Monitors() {
        var list = new List<MonInfo>();
        EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, delegate(IntPtr h, IntPtr hdc, ref RECT r, IntPtr d) {
            var mi = new MONITORINFO();
            mi.cbSize = Marshal.SizeOf(typeof(MONITORINFO));
            if (GetMonitorInfoW(h, ref mi)) {
                var info = new MonInfo {
                    X = mi.rcMonitor.Left, Y = mi.rcMonitor.Top,
                    W = mi.rcMonitor.Right - mi.rcMonitor.Left,
                    H = mi.rcMonitor.Bottom - mi.rcMonitor.Top,
                    Primary = (mi.dwFlags & 1) != 0
                };
                uint dx, dy;
                try {
                    if (GetDpiForMonitor(h, 0, out dx, out dy) == 0) { info.DpiX = dx; info.DpiY = dy; }
                } catch { }
                list.Add(info);
            }
            return true;
        }, IntPtr.Zero);
        return list.ToArray();
    }

    public static WinInfo[] Windows() {
        var list = new List<WinInfo>();
        EnumWindows(delegate(IntPtr h, IntPtr d) {
            if (!IsWindowVisible(h)) return true;
            int len = GetWindowTextLengthW(h);
            if (len == 0) return true;
            var sb = new StringBuilder(len + 2);
            GetWindowTextW(h, sb, sb.Capacity);
            RECT r;
            if (!GetWindowRect(h, out r)) return true;
            list.Add(new WinInfo {
                Hwnd = h.ToInt64(), Title = sb.ToString(),
                X = r.Left, Y = r.Top,
                W = r.Right - r.Left, H = r.Bottom - r.Top, Iconic = IsIconic(h)
            });
            return true;
        }, IntPtr.Zero);
        return list.ToArray();
    }

    // virtual desktop rect in this process' coordinate space
    public static int[] VirtualScreen() {
        int vx = GetSystemMetrics(76), vy = GetSystemMetrics(77);
        int vw = GetSystemMetrics(78), vh = GetSystemMetrics(79);
        return new int[] { vx, vy, vw, vh };
    }
}
"@

$log = New-Object System.Collections.Generic.List[string]
function Log([string]$m) { Write-Host $m; $script:log.Add($m) }
$script:failCount = 0

function Grab([string]$outName, [string[]]$inArgs) {
    $outPath = Join-Path $OutDir $outName
    $errPath = Join-Path $OutDir ($outName + ".stderr.txt")
    if (Test-Path $outPath) { Remove-Item $outPath -Force }
    $ff = @("-hide_banner", "-loglevel", "error", "-y", "-f", "gdigrab", "-framerate", "5") + $inArgs + @("-frames:v", "1", "-update", "1", $outPath)
    & $Ffmpeg @ff 2> $errPath
    $code = $LASTEXITCODE
    if ($code -ne 0 -or -not (Test-Path $outPath)) {
        $script:failCount++
        $err = ""
        if (Test-Path $errPath) { $err = ((Get-Content $errPath -Raw) -replace "`r?`n", " | ").Trim() }
        Log ("FAIL {0} exit={1} :: {2}" -f $outName, $code, $err)
    } else {
        Log ("OK   {0}" -f $outName)
    }
}

function ImgSize([string]$p) {
    try {
        $img = [System.Drawing.Image]::FromFile($p)
        $s = "{0}x{1}" -f $img.Width, $img.Height
        $img.Dispose()
        return $s
    } catch { return "unreadable" }
}

Log ("FFMPEG {0}" -f $Ffmpeg)
Log ("OUTDIR {0}" -f $OutDir)
Log ("PSDPI  awareness={0}" -f [WCap]::ProcessDpiAwareness())
$vs = [WCap]::VirtualScreen()
Log ("VIRTUALSCREEN x={0} y={1} w={2} h={3} (this process)" -f $vs[0], $vs[1], $vs[2], $vs[3])

$monitors = [WCap]::Monitors()
$i = 0
$monRects = @()
foreach ($m in $monitors) {
    $i++
    Log ("MONITOR {0}: x={1} y={2} w={3} h={4} primary={5} dpi={6}x{7}" -f $i, $m.X, $m.Y, $m.W, $m.H, $m.Primary, $m.DpiX, $m.DpiY)
    $monRects += ,@($m.X, $m.Y, $m.W, $m.H)
}

$wins = @([WCap]::Windows() | Where-Object { -not $_.Iconic -and $_.W -ge 200 -and $_.H -ge 120 } |
    Sort-Object { $_.W * $_.H } -Descending)
Log ("WINDOWCOUNT {0} (visible, non-iconic, >=200x120)" -f $wins.Count)
foreach ($w in $wins) {
    Log ("WINDOW hwnd=0x{0:X} rect=({1},{2},{3}x{4}) title={5}" -f $w.Hwnd, $w.X, $w.Y, $w.W, $w.H, $w.Title)
}

Log "---- grabs ----"
Grab "desktop.png" @("-i", "desktop")

$i = 0
foreach ($r in $monRects) {
    $i++
    Grab ("monitor{0}.png" -f $i) @("-offset_x", "$($r[0])", "-offset_y", "$($r[1])", "-video_size", "$($r[2])x$($r[3])", "-i", "desktop")
}

$vx = $vs[0]; $vy = $vs[1]; $vr = $vx + $vs[2]; $vb = $vy + $vs[3]
$n = 0
foreach ($w in ($wins | Select-Object -First $MaxWindows)) {
    $n++
    Log ("WINTARGET win{0}: hwnd=0x{1:X} title={2}" -f $n, $w.Hwnd, $w.Title)
    Grab ("win{0}_hwnd.png" -f $n) @("-i", ("hwnd=0x{0:X}" -f $w.Hwnd))
    # clamp window rect into the virtual screen before region grab
    $x = [Math]::Max($w.X, $vx); $y = [Math]::Max($w.Y, $vy)
    $x2 = [Math]::Min($w.X + $w.W, $vr); $y2 = [Math]::Min($w.Y + $w.H, $vb)
    $cw = $x2 - $x; $ch = $y2 - $y
    if ($cw -ge 200 -and $ch -ge 120) {
        Grab ("win{0}_region.png" -f $n) @("-offset_x", "$x", "-offset_y", "$y", "-video_size", "${cw}x${ch}", "-i", "desktop")
    } else {
        Log ("SKIP win{0}_region.png (clamped rect {1}x{2} too small)" -f $n, $cw, $ch)
    }
}

Log "---- png sizes ----"
foreach ($f in (Get-ChildItem $OutDir -Filter *.png | Sort-Object Name)) {
    Log ("PNG {0} bytes={1} size={2}" -f $f.Name, $f.Length, (ImgSize $f.FullName))
}

$log -join "`r`n" | Out-File -Encoding utf8 (Join-Path $OutDir "manifest.txt")
Write-Host ("MANIFEST {0}" -f (Join-Path $OutDir "manifest.txt"))
exit $script:failCount
