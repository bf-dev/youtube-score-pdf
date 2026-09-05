#!/usr/bin/env bash
# Build, test and package the Windows deliverable on the Azure `windows-builder`
# VM, and pull everything back into out/win/.
#
# Why not the shared ~/workspace/scripts/winbuild:
#   * it assumes --onefile (it looks for dist\*.exe and flattens the zip it pulls
#     back), and this app ships --onedir + an Inno Setup installer, because a
#     onefile exe unpacks 200MB into %TEMP% on every launch;
#   * it does `rm -rf <project>/out` before pulling, which would delete the
#     acceptance PDFs that live in this project's out/.
# The one thing it does that nothing else can, capturing a GUI in the builder's
# interactive session 1 through a `schtasks /it` task, is reproduced here.
#
# Why the proxy: YouTube answers both GitHub's and Azure's IP ranges with "Sign
# in to confirm you're not a bot", so the end-to-end test goes out through a
# Korean house SOCKS node, which windows-builder reaches over Tailscale. The
# customer's own PC needs none of this.
set -euo pipefail

HOST=bfdev@windows-builder
SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 $HOST"
REMOTE='C:\builds\ytscore'
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/out/win"
E2E_URL="${E2E_URL:-https://youtu.be/638BDPkSpf8}"
# The customer's horizontally scrolling link. The scroll guard must REFUSE this
# one, and must still convert E2E_URL above. Both sides run on the packaged exe.
SCROLL_URL="${SCROLL_URL:-https://youtu.be/rsUfI3EKAj4}"
PROXY="${YTSCORE_PROXY:-$(head -1 "$HOME/workspace/scripts/proxy-pool/output/proxies.txt")}"
WINPWD='cho28670!!server'                 # session-1 task account; also in winbuild
# Copy protection (ytscore/activation.py) refuses any copy that was not put there
# by the installer, and everything in THIS script runs the un-installed build
# tree, which is exactly that. Only its sha256 is compiled into the exe
# (config.BUILD_KEY_SHA256). Never set in ci/installed_e2e.sh or in
# ci/protection_e2e.sh: those two exist to exercise the check, not to skip it.
BUILD_KEY='e4f334903a6524ba9c84df09f45b6a66'

log() { echo "[winbuilder] $*"; }

# ---- 0. protection logic, before spending 30 minutes on a build ---------------
log "activation branch tests"
python3 "$ROOT/ci/test_activation.py" || { log "activation logic is broken"; exit 1; }

push() {                                  # push <local.ps1> <remote name>
  printf '\xEF\xBB\xBF' | cat - "$1" > "$1.bom"     # PS 5.1 reads .ps1 as ANSI without a BOM
  scp -q -o StrictHostKeyChecking=no "$1.bom" "$HOST:C:/builder/$2"
  rm -f "$1.bom"
}

# ---- 1. ship the buildable subset -------------------------------------------
log "uploading source"
TAR=$(mktemp /tmp/ytscore-src-XXXX.tar.gz)
tar czf "$TAR" -C "$ROOT" --exclude=__pycache__ \
    main.py requirements.txt README.md readme-ko.txt ytscore ci assets
scp -q -o StrictHostKeyChecking=no "$TAR" "$HOST:C:/ytscore-src.tar.gz"
rm -f "$TAR"
$SSH "New-Item -ItemType Directory -Force -Path '$REMOTE' | Out-Null
      Remove-Item '$REMOTE\\main.py','$REMOTE\\ytscore','$REMOTE\\ci','$REMOTE\\assets' -Recurse -Force -ErrorAction SilentlyContinue
      tar xzf C:\\ytscore-src.tar.gz -C '$REMOTE'
      Remove-Item C:\\ytscore-src.tar.gz" > /dev/null

# ---- 2. build + every headless check ----------------------------------------
cat > /tmp/ytscore-build.ps1 <<PS1
\$ErrorActionPreference = 'Stop'
\$py = 'C:\Program Files\Python312\python.exe'
Set-Location '$REMOTE'
# The build tree was not put here by the installer, so without this every mode
# below would (correctly) refuse. See the note on BUILD_KEY in this script.
\$env:YTSCORE_BUILD_KEY = '$BUILD_KEY'
& \$py -m pip install -q --upgrade pip
& \$py -m pip install -q -r requirements.txt pyinstaller
powershell -ExecutionPolicy Bypass -File ci\fetch_ffmpeg.ps1
Remove-Item -Recurse -Force dist,build -ErrorAction SilentlyContinue
& \$py -m PyInstaller --onedir --noconsole --name youtube-score-pdf \`
  --version-file ci/version_info.txt \`
  --add-data "assets/NanumGothic.ttf;assets" \`
  --add-binary "bin/ffmpeg.exe;bin" --add-binary "bin/ffprobe.exe;bin" \`
  --collect-all yt_dlp --collect-all pymupdf --hidden-import tkinter --hidden-import socks \`
  --noconfirm main.py
if (\$LASTEXITCODE -ne 0) { throw 'pyinstaller failed' }

\$exe = Get-Item 'dist\youtube-score-pdf\youtube-score-pdf.exe'
"exe: {0:N1} MB" -f (\$exe.Length / 1MB)
\$fs = [System.IO.File]::OpenRead(\$exe.FullName); \$b = New-Object byte[] 2
\$fs.Read(\$b, 0, 2) | Out-Null; \$fs.Close()
if (\$b[0] -ne 0x4D -or \$b[1] -ne 0x5A) { throw 'not a PE binary' }
"version resource: \$(\$exe.VersionInfo.ProductName) / \$(\$exe.VersionInfo.FileVersion)"
foreach (\$p in @('_internal\bin\ffmpeg.exe', '_internal\bin\ffprobe.exe',
                 '_internal\assets\NanumGothic.ttf')) {
  if (-not (Test-Path "dist\youtube-score-pdf\\\$p")) { throw "missing \$p" }
}
"onedir total: {0:N1} MB" -f ((Get-ChildItem dist\youtube-score-pdf -Recurse | Measure-Object Length -Sum).Sum / 1MB)

foreach (\$mode in @('--version', '--guiselftest')) {
  \$p = Start-Process \$exe.FullName -ArgumentList \$mode -PassThru -Wait
  "\$mode -> exit \$(\$p.ExitCode)"
  if (\$p.ExitCode -ne 0) { throw "\$mode failed" }
}

# The refusal below asks "was this machine ever installed on", and the answer has
# to come from THIS build, not from whatever a previous lane left on the builder.
# ci/installed_e2e.sh performs a real install as this same account, so its marker
# and Inno uninstall record survive into the next build and made the check pass a
# copy that should have been refused (seen on the 1.3.0 build: state=ok-marker,
# exit 0, where exit 3 was required). Clear both first. This is our own CI state;
# installed_e2e re-creates it properly a few minutes later.
Remove-Item -Path 'HKCU:\Software\youtube-score-pdf' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E5B1C64-3F8A-4C51-9E2D-1775529A0001}_is1' -Recurse -Force -ErrorAction SilentlyContinue

# Copy protection, both directions, on the build tree (which the installer never
# touched). With the CI key it must allow; with the key removed the very same exe
# in the very same folder must refuse with exit 3. That is the whole mechanism in
# two lines, ~15 seconds, before anything expensive runs.
\$p = Start-Process \$exe.FullName -ArgumentList '--protection-status' \`
     -RedirectStandardOutput protection-allow.log -PassThru -Wait
Get-Content protection-allow.log
if (\$p.ExitCode -ne 0) { throw 'protection refused a run that carried the CI key' }
\$saved = \$env:YTSCORE_BUILD_KEY
\$env:YTSCORE_BUILD_KEY = ''
\$p = Start-Process \$exe.FullName -ArgumentList '--protection-status' \`
     -RedirectStandardOutput protection-refuse.log -PassThru -Wait
Get-Content protection-refuse.log
\$env:YTSCORE_BUILD_KEY = \$saved
if (\$p.ExitCode -ne 3) { throw "an uninstalled copy was allowed to run (exit \$(\$p.ExitCode))" }

\$p = Start-Process \$exe.FullName -ArgumentList '--artifacts-test' -RedirectStandardOutput artifacts.log -PassThru -Wait
Get-Content artifacts.log
if (\$p.ExitCode -ne 0) { throw 'artifacts reporter did not land' }

\$env:YTSCORE_PROXY = '$PROXY'
Remove-Item -Recurse -Force e2e -ErrorAction SilentlyContinue
\$p = Start-Process \$exe.FullName -ArgumentList '--selftest','--url=$E2E_URL','--out=e2e' \`
     -RedirectStandardOutput selftest.log -PassThru -Wait
Get-Content selftest.log -Tail 25
if (\$p.ExitCode -ne 0) { throw 'end-to-end conversion failed' }
\$pdf = Get-Item e2e\selftest.pdf
"E2E PDF: {0:N0} bytes" -f \$pdf.Length
if (\$pdf.Length -lt 100000) { throw 'PDF too small to be real' }

# The scroll guard, on the packaged exe, both directions. The step above is the
# negative side (a normal video still converts); this is the positive one: the
# customer's horizontally scrolling link must be REFUSED with the Korean notice
# and must leave no PDF behind. See pipeline.ScrollingScore.
Remove-Item -Recurse -Force scrolltest -ErrorAction SilentlyContinue
\$p = Start-Process \$exe.FullName -ArgumentList '--scrolltest','--url=$SCROLL_URL','--out=scrolltest' \`
     -RedirectStandardOutput scrolltest.log -PassThru -Wait
Get-Content scrolltest.log -Tail 12
if (\$p.ExitCode -ne 0) { throw 'the scroll guard did not refuse the scrolling video' }
if (Get-ChildItem scrolltest\*.pdf -ErrorAction SilentlyContinue) { throw 'a PDF was written for a video the guard refused' }

\$iscc ="\${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
& \$iscc ci\installer.iss | Select-Object -Last 5
if (\$LASTEXITCODE -ne 0) { throw 'ISCC failed' }
# NEWEST, not alphabetically first: installer\ keeps older versions, and
# A plain "Select-Object -First 1" once reinstalled 1.0.0 over a fresh 1.0.1
# build. (No backticks in this comment: the heredoc is unquoted, so bash would
# run them as a command substitution.)
\$setup = Get-ChildItem installer\*.exe | Sort-Object LastWriteTime -Descending | Select-Object -First 1
"installer: {0}  {1:N1} MB  sha256 {2}" -f \$setup.Name, (\$setup.Length / 1MB), \`
   (Get-FileHash \$setup.FullName -Algorithm SHA256).Hash.ToLower()
if (\$setup.Length -lt 20MB) { throw 'installer suspiciously small' }
# The ZIP wraps the INSTALLER now, not the program folder: an extracted program
# folder is by definition a copy and the protection would refuse it.
& \$py ci\package_zip.py \$setup.FullName out
PS1
push /tmp/ytscore-build.ps1 ytscore-build.ps1
log "building (pip, pyinstaller, selftests, real end-to-end, installer)"
$SSH "powershell -ExecutionPolicy Bypass -File C:\\builder\\ytscore-build.ps1" 2>&1 | tee "$ROOT/out/win-build.log" || {
  log "build failed, see out/win-build.log"; exit 1; }

# ---- 3. GUI screenshot with a real conversion on screen, in session 1 --------
# PrintWindow, not CopyFromScreen: the builder's console framebuffer goes stale
# after a reboot and a screen grab silently returns a frozen desktop.
HOLD_MS=${HOLD_MS:-1200000}
SETTLE=${SETTLE:-480}        # the conversion itself measures ~165s on this VM
cat > /tmp/ytscore-shot.ps1 <<PS1
\$log = '$REMOTE\screenshots\shot.log'
try {
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
\$ErrorActionPreference = 'Stop'
\$dir = '$REMOTE\screenshots'
New-Item -ItemType Directory -Force -Path \$dir | Out-Null
Remove-Item "\$dir\gui.png" -ErrorAction SilentlyContinue
\$env:YTSCORE_PROXY = '$PROXY'
\$env:YTSCORE_BUILD_KEY = '$BUILD_KEY'
Set-Location '$REMOTE'
\$exe = '$REMOTE\dist\youtube-score-pdf\youtube-score-pdf.exe'
\$base = 'youtube-score-pdf'
Get-Process -Name \$base -ErrorAction SilentlyContinue | Stop-Process -Force
\$demoArgs = @('--guidemo', '--url=$E2E_URL', '--title=드럼악보샘플',
           '--out=$REMOTE\demo-out', '--hold=$HOLD_MS')
Start-Process \$exe -ArgumentList \$demoArgs | Out-Null
\$hwnd = [IntPtr]::Zero
for (\$i = 0; \$i -lt 120; \$i++) {
  Start-Sleep -Seconds 1
  \$win = Get-Process -Name \$base -ErrorAction SilentlyContinue |
         Where-Object { \$_.MainWindowHandle -ne 0 } | Select-Object -First 1
  if (\$win) { \$hwnd = \$win.MainWindowHandle; break }
}
if (\$hwnd -eq [IntPtr]::Zero) { throw 'the app never opened a window' }
Start-Sleep -Seconds $SETTLE
[Win32Cap]::ShowWindow(\$hwnd, 5) | Out-Null
[Win32Cap]::SetForegroundWindow(\$hwnd) | Out-Null
Start-Sleep -Seconds 2
\$r = New-Object Win32Cap+RECT
[Win32Cap]::GetWindowRect(\$hwnd, [ref] \$r) | Out-Null
\$w = \$r.R - \$r.L; \$h = \$r.B - \$r.T
if (\$w -lt 300 -or \$h -lt 300) { throw "window rect is \${w}x\${h}" }
\$bmp = New-Object System.Drawing.Bitmap \$w, \$h
\$g = [System.Drawing.Graphics]::FromImage(\$bmp)
\$hdc = \$g.GetHdc()
\$ok = [Win32Cap]::PrintWindow(\$hwnd, \$hdc, 2)
\$g.ReleaseHdc(\$hdc)
if (-not \$ok) { throw 'PrintWindow failed' }
\$bmp.Save("\$dir\gui.png")
\$colors = @{}
for (\$x = 0; \$x -lt \$w; \$x += 7) { for (\$y = 0; \$y -lt \$h; \$y += 7) {
  \$colors[\$bmp.GetPixel(\$x, \$y).ToArgb()] = 1 } }
if (\$colors.Count -lt 12) { throw "capture looks blank (\$(\$colors.Count) colours)" }
Get-Process -Name \$base -ErrorAction SilentlyContinue | Stop-Process -Force
"captured \${w}x\${h}, \$(\$colors.Count) colours" | Out-File \$log -Encoding utf8
} catch {
  New-Item -ItemType Directory -Force -Path '$REMOTE\screenshots' | Out-Null
  "FAILED: \$(\$_.Exception.Message)" | Out-File \$log -Encoding utf8
}
PS1
push /tmp/ytscore-shot.ps1 ytscore-shot.ps1
# Delete the previous shot.log and gui.png from HERE, before the task is started.
# If the scheduled task fails to start at all, the poll below would otherwise read
# the LAST build's log and report a stale success with no gui.png to show for it.
# That has happened; do not remove this.
$SSH "Remove-Item '$REMOTE\\screenshots\\shot.log','$REMOTE\\screenshots\\gui.png' -Force -ErrorAction SilentlyContinue" > /dev/null 2>&1 || true
log "capturing the GUI in session 1 (a real conversion runs first, ~$((SETTLE / 60)) min)"
$SSH "schtasks /create /tn ytscore-shot /tr \"powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\\builder\\ytscore-shot.ps1\" /sc once /st 00:00 /ru bfdev /rp '$WINPWD' /it /f | Out-Null
      schtasks /run /tn ytscore-shot | Out-Null" > /dev/null
deadline=$((SECONDS + SETTLE + 600))
while [ $SECONDS -lt $deadline ]; do
  sleep 30
  # `|| true` is NOT decoration. Under `set -e` a bare assignment from a command
  # substitution inherits that command's exit status, and powershell-over-ssh
  # returns non-zero while shot.log does not exist yet, so the FIRST poll killed
  # this script and left the capture running on the VM with nobody to collect it.
  SHOT=$($SSH "Get-Content '$REMOTE\\screenshots\\shot.log' -ErrorAction SilentlyContinue" 2>/dev/null || true)
  SHOT=$(printf '%s' "$SHOT" | tr -d '\r')
  # NOT `[ -n "$SHOT" ] && break`: under `set -e` a false test as the last
  # statement of the loop body kills the whole script, which is how a finished
  # build once threw its own artifacts away.
  if [ -n "$SHOT" ]; then break; fi
done
$SSH "schtasks /delete /tn ytscore-shot /f | Out-Null" > /dev/null 2>&1 || true
log "capture: ${SHOT:-(timed out)}"
case "${SHOT:-}" in
  ""|FAILED*) log "GUI capture did not produce a screenshot; treat this build as unverified";;
esac

# ---- 4. pull everything ------------------------------------------------------
mkdir -p "$OUT"
$SSH "\$ErrorActionPreference='Stop'
      Set-Location '$REMOTE'
      Remove-Item C:\\builds\\ytscore-out.zip -ErrorAction SilentlyContinue
      \$items = @('installer','screenshots','out','e2e','selftest.log','artifacts.log',
                 'protection-allow.log','protection-refuse.log') |
                Where-Object { Test-Path \$_ }
      Compress-Archive -Path \$items -DestinationPath C:\\builds\\ytscore-out.zip -Force" > /dev/null
scp -q -o StrictHostKeyChecking=no "$HOST:C:/builds/ytscore-out.zip" "$OUT/_out.zip"
python3 - "$OUT/_out.zip" "$OUT" <<'PY'
import sys, zipfile, pathlib
z, out = sys.argv[1], pathlib.Path(sys.argv[2])
with zipfile.ZipFile(z) as f:
    for i in f.infolist():
        name = i.filename
        if not i.flag_bits & 0x800:
            try:
                name = name.encode("cp437").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        p = out / name.replace("\\", "/")
        if i.is_dir():
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f.read(i))
PY
rm -f "$OUT/_out.zip"
find "$OUT" -type f | sort | while read -r f; do
  printf '%10s  %s\n' "$(du -h "$f" | cut -f1)" "${f#$ROOT/}"
done
for e in "$OUT"/installer/*.exe; do file "$e" | grep -q PE32 || { log "NOT a PE: $e"; exit 1; }; done
log "done"
