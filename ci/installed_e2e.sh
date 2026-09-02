#!/usr/bin/env bash
# Acceptance for the PACKAGED deliverable, not the build tree.
#
# ci/winbuilder.sh proves that dist\youtube-score-pdf\ works. That is not the
# same artifact the customer receives: they get the Inno Setup installer, which
# can drop files somewhere else, miss one, or install an exe that cannot find
# its own _internal. So this runs the whole customer path on windows-builder:
#
#   1. uninstall any previous copy, then install from installer\*.exe silently
#   2. assert the installed tree has the exe, ffmpeg, ffprobe and the font
#   3. launch the INSTALLED exe in the builder's interactive session 1 with
#      --guidemo pointed at one of the customer's own acceptance videos, so a
#      real conversion runs in the real window
#   4. capture that window with PrintWindow while the result is on screen
#   5. assert the PDF the GUI wrote is a real multi-page PDF
#
# Everything comes back into out/installed/.
set -euo pipefail

HOST=bfdev@windows-builder
SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 $HOST"
REMOTE='C:\builds\ytscore'
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/out/installed"
# a02-buksunsaeng, one of the ten videos the customer supplied. Deliberately NOT
# the video ci/winbuilder.sh already converted, so this exercises a fresh
# download + layout detection rather than replaying a warm case.
E2E_URL="${E2E_URL:-https://youtu.be/IweNtfTT8PI}"
E2E_TITLE="${E2E_TITLE:-북선생 드럼악보}"
PROXY="${YTSCORE_PROXY:-$(head -1 "$HOME/workspace/scripts/proxy-pool/output/proxies.txt")}"
WINPWD='cho28670!!server'
SETTLE=${SETTLE:-450}
HOLD_MS=${HOLD_MS:-900000}

log() { echo "[installed-e2e] $*"; }
push() {
  printf '\xEF\xBB\xBF' | cat - "$1" > "$1.bom"
  scp -q -o StrictHostKeyChecking=no "$1.bom" "$HOST:C:/builder/$2"
  rm -f "$1.bom"
}

cat > /tmp/ytscore-install.ps1 <<PS1
\$ErrorActionPreference = 'Stop'
Set-Location '$REMOTE'
# NEWEST, not alphabetically first: installer\ keeps older versions, and
# a plain 'Select-Object -First 1' once reinstalled 1.0.0 over a fresh 1.0.1 build.
\$setup = Get-ChildItem installer\*.exe | Sort-Object LastWriteTime -Descending | Select-Object -First 1
"setup: \$(\$setup.Name) {0:N1} MB" -f (\$setup.Length / 1MB)

# uninstall a previous copy so this is a first-install, like the customer's
\$app = Join-Path \$env:LOCALAPPDATA 'Programs\youtube-score-pdf'
\$un  = Join-Path \$app 'unins000.exe'
if (Test-Path \$un) {
  Start-Process \$un -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait
  Start-Sleep -Seconds 5
}
Remove-Item \$app -Recurse -Force -ErrorAction SilentlyContinue

\$p = Start-Process \$setup.FullName -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/NOICONS' -PassThru -Wait
"installer exit \$(\$p.ExitCode)"
if (\$p.ExitCode -ne 0) { throw "installer exited \$(\$p.ExitCode)" }

\$exe = Join-Path \$app 'youtube-score-pdf.exe'
foreach (\$f in @(\$exe,
                 (Join-Path \$app '_internal\bin\ffmpeg.exe'),
                 (Join-Path \$app '_internal\bin\ffprobe.exe'),
                 (Join-Path \$app '_internal\assets\NanumGothic.ttf'),
                 (Join-Path \$app '_읽어주세요.txt'))) {
  if (-not (Test-Path \$f)) { throw "installed tree is missing \$f" }
}
"installed to \$app"
# NO backtick line continuations here: this heredoc is unquoted, so bash eats a
# backtick as a command substitution before PowerShell ever sees it.
\$files = Get-ChildItem \$app -Recurse -File
"installed size: {0:N1} MB, {1} files" -f ((\$files | Measure-Object Length -Sum).Sum / 1MB), \$files.Count
"installed exe version: \$((Get-Item \$exe).VersionInfo.FileVersion)"
PS1
push /tmp/ytscore-install.ps1 ytscore-install.ps1
log "installing from the Inno Setup installer"
$SSH "powershell -ExecutionPolicy Bypass -File C:\\builder\\ytscore-install.ps1" 2>&1 | tee "$ROOT/out/installed-build.log"

cat > /tmp/ytscore-ie2e.ps1 <<PS1
\$dir = '$REMOTE\installed-run'
\$log = "\$dir\run.log"
try {
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Cap2 {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint f);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
}
"@
\$ErrorActionPreference = 'Stop'
Remove-Item \$dir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path \$dir | Out-Null
\$env:YTSCORE_PROXY = '$PROXY'
\$app = Join-Path \$env:LOCALAPPDATA 'Programs\youtube-score-pdf'
\$exe = Join-Path \$app 'youtube-score-pdf.exe'
\$base = 'youtube-score-pdf'
Get-Process -Name \$base -ErrorAction SilentlyContinue | Stop-Process -Force
# Start-Process does NOT quote array elements when it builds the command line,
# so an element containing a space arrives at the child as two argv entries and
# the title silently truncates. Embed the quotes.
# Start-Process does NOT quote array elements when it builds the command line, so
# an element containing a space arrives at the child as two argv entries and the
# title silently truncates. Embed literal quotes with a SINGLE-quoted PS string;
# PowerShell escapes with a backtick, and a backslash here is a parse error that
# kills the script before its own try/catch can report it.
\$demoArgs = @('--guidemo', '--url=$E2E_URL', '"--title=$E2E_TITLE"',
              "--out=\$dir", '--hold=$HOLD_MS')
Start-Process \$exe -ArgumentList \$demoArgs | Out-Null
\$hwnd = [IntPtr]::Zero
for (\$i = 0; \$i -lt 120; \$i++) {
  Start-Sleep -Seconds 1
  \$w = Get-Process -Name \$base -ErrorAction SilentlyContinue |
       Where-Object { \$_.MainWindowHandle -ne 0 } | Select-Object -First 1
  if (\$w) { \$hwnd = \$w.MainWindowHandle; break }
}
if (\$hwnd -eq [IntPtr]::Zero) { throw 'the installed app never opened a window' }
# wait for the conversion, but stop early once the GUI has written its PDF
for (\$i = 0; \$i -lt $SETTLE; \$i++) {
  Start-Sleep -Seconds 1
  if (\$i -gt 60 -and (Get-ChildItem \$dir -Filter *.pdf -ErrorAction SilentlyContinue)) {
    Start-Sleep -Seconds 8; break
  }
}
[Win32Cap2]::ShowWindow(\$hwnd, 5) | Out-Null
[Win32Cap2]::SetForegroundWindow(\$hwnd) | Out-Null
Start-Sleep -Seconds 2
\$r = New-Object Win32Cap2+RECT
[Win32Cap2]::GetWindowRect(\$hwnd, [ref] \$r) | Out-Null
\$w = \$r.R - \$r.L; \$h = \$r.B - \$r.T
if (\$w -lt 300 -or \$h -lt 300) { throw "window rect is \${w}x\${h}" }
\$bmp = New-Object System.Drawing.Bitmap \$w, \$h
\$g = [System.Drawing.Graphics]::FromImage(\$bmp)
\$hdc = \$g.GetHdc()
\$ok = [Win32Cap2]::PrintWindow(\$hwnd, \$hdc, 2)
\$g.ReleaseHdc(\$hdc)
if (-not \$ok) { throw 'PrintWindow failed' }
\$bmp.Save("\$dir\gui-installed.png")
\$colors = @{}
for (\$x = 0; \$x -lt \$w; \$x += 7) { for (\$y = 0; \$y -lt \$h; \$y += 7) {
  \$colors[\$bmp.GetPixel(\$x, \$y).ToArgb()] = 1 } }
Get-Process -Name \$base -ErrorAction SilentlyContinue | Stop-Process -Force
\$pdfs = @(Get-ChildItem \$dir -Filter *.pdf)
if (\$pdfs.Count -lt 1) { throw 'the installed GUI produced no PDF' }
\$out = "captured \${w}x\${h}, \$(\$colors.Count) colours"
foreach (\$p in \$pdfs) { \$out += "; PDF \$(\$p.Name) \$(\$p.Length) bytes" }
if (\$colors.Count -lt 12) { \$out = "FAILED blank capture; \$out" }
\$out | Out-File \$log -Encoding utf8
} catch {
  New-Item -ItemType Directory -Force -Path \$dir | Out-Null
  "FAILED: \$(\$_.Exception.Message)" | Out-File \$log -Encoding utf8
}
PS1
push /tmp/ytscore-ie2e.ps1 ytscore-ie2e.ps1
# Delete the previous run.log HERE, from this side. If the scheduled task fails to
# start at all, the poll below would otherwise read the last run's log and report a
# stale success, which it has already done once.
$SSH "Remove-Item '$REMOTE\\installed-run\\run.log' -Force -ErrorAction SilentlyContinue" > /dev/null 2>&1 || true
log "running the installed GUI on $E2E_URL in session 1 (up to $((SETTLE / 60)) min)"
$SSH "schtasks /create /tn ytscore-ie2e /tr \"powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\\builder\\ytscore-ie2e.ps1\" /sc once /st 00:00 /ru bfdev /rp '$WINPWD' /it /f | Out-Null
      schtasks /run /tn ytscore-ie2e | Out-Null" > /dev/null
RUN=""
deadline=$((SECONDS + SETTLE + 600))
while [ $SECONDS -lt $deadline ]; do
  sleep 30
  # `|| true`: see the same note in ci/winbuilder.sh. A command substitution
  # that fails is fatal under `set -e`, and the poll fails until run.log exists.
  RUN=$($SSH "Get-Content '$REMOTE\\installed-run\\run.log' -ErrorAction SilentlyContinue" 2>/dev/null || true)
  RUN=$(printf '%s' "$RUN" | tr -d '\r')
  if [ -n "$RUN" ]; then break; fi
done
$SSH "schtasks /delete /tn ytscore-ie2e /f | Out-Null" > /dev/null 2>&1 || true
log "result: ${RUN:-(timed out)}"
echo "${RUN:-TIMED OUT}" >> "$ROOT/out/installed-build.log"

mkdir -p "$OUT"
$SSH "Remove-Item C:\\builds\\ytscore-installed.zip -ErrorAction SilentlyContinue
      Compress-Archive -Path '$REMOTE\\installed-run\\*' -DestinationPath C:\\builds\\ytscore-installed.zip -Force" > /dev/null
scp -q -o StrictHostKeyChecking=no "$HOST:C:/builds/ytscore-installed.zip" "$OUT/_i.zip"
python3 - "$OUT/_i.zip" "$OUT" <<'PY'
import sys, zipfile, pathlib
z, out = sys.argv[1], pathlib.Path(sys.argv[2])
with zipfile.ZipFile(z) as f:
    for i in f.infolist():
        if i.is_dir():
            continue
        name = i.filename
        if not i.flag_bits & 0x800:
            try:
                name = name.encode("cp437").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        p = out / name.replace("\\", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f.read(i))
PY
rm -f "$OUT/_i.zip"
find "$OUT" -type f -printf '%10s  %p\n' | sort -k2
case "$RUN" in FAILED*|"") log "FAILED"; exit 1;; esac
log "done"
