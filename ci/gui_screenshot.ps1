# GUI screenshot capture (GitHub Actions windows-latest).
#
# ASCII ONLY IN THIS FILE. Windows PowerShell 5.1 reads a .ps1 as ANSI unless it
# has a UTF-8 BOM, so any non-ASCII character here turns into mojibake and the
# parser dies on an unterminated string.
#
# PrintWindow, not CopyFromScreen: a stale/empty framebuffer makes CopyFromScreen
# return a black image that passes every blankness heuristic. PrintWindow asks the
# window to paint itself, so it is correct even when the desktop is not rendered.
param(
  [string] $DemoArgs = "--guidemo,--hold=600000",
  [string] $Out = "gui.png",
  [int] $SettleSeconds = 240,
  [string] $Exe = "dist\youtube-score-pdf\youtube-score-pdf.exe"
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Cap {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint f);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
}
"@

$dir = Join-Path $PWD "screenshots"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

$path = Join-Path (Get-Location).Path $Exe
if (-not (Test-Path -LiteralPath $path)) { throw "no exe at $path" }
$exe = Get-Item -LiteralPath $path
$base = [System.IO.Path]::GetFileNameWithoutExtension($exe.Name)
if (-not $base) { throw "could not derive a process name from $path" }
Write-Host "launching $($exe.FullName) (process name $base)"

Get-Process -Name $base -ErrorAction SilentlyContinue | Stop-Process -Force

$argList = $DemoArgs.Split(",")
Write-Host "demo args: $($argList -join ' ')"
$p = Start-Process $exe.FullName -ArgumentList $argList -PassThru

$hwnd = [IntPtr]::Zero
for ($i = 0; $i -lt 180; $i++) {
  Start-Sleep -Seconds 1
  $win = Get-Process -Name $base -ErrorAction SilentlyContinue |
         Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
  if ($win) { $hwnd = $win.MainWindowHandle; Write-Host "window found after $i s"; break }
}
if ($hwnd -eq [IntPtr]::Zero) { throw "app never opened a window within 180s" }

# let the real conversion finish so the success banner is on screen
Start-Sleep -Seconds $SettleSeconds

[Win32Cap]::ShowWindow($hwnd, 5) | Out-Null
[Win32Cap]::SetForegroundWindow($hwnd) | Out-Null
Start-Sleep -Seconds 2

$r = New-Object Win32Cap+RECT
[Win32Cap]::GetWindowRect($hwnd, [ref] $r) | Out-Null
$w = $r.R - $r.L; $h = $r.B - $r.T
Write-Host "window rect ${w}x${h}"
if ($w -lt 200 -or $h -lt 200) { throw "window rect is ${w}x${h}" }

$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
$ok = [Win32Cap]::PrintWindow($hwnd, $hdc, 2)
$g.ReleaseHdc($hdc)
if (-not $ok) { throw "PrintWindow failed" }

$out = Join-Path $dir $Out
$bmp.Save($out)

$colors = @{}
for ($x = 0; $x -lt $w; $x += 7) {
  for ($y = 0; $y -lt $h; $y += 7) {
    $colors[$bmp.GetPixel($x, $y).ToArgb()] = 1
  }
}
Write-Host "distinct colours: $($colors.Count)"
if ($colors.Count -lt 12) { throw "capture looks blank ($($colors.Count) colours)" }

try { Get-Process -Name $base -ErrorAction SilentlyContinue | Stop-Process -Force } catch {}
Write-Host "saved $out (${w}x${h}, $($colors.Count) colours)"
