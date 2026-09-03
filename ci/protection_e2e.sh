#!/usr/bin/env bash
# Reproduce the customer's failure case for real: take the folder the installer
# put on disk, copy it somewhere else, and run it in an environment that never
# ran the installer. It must refuse, in the window, in Korean.
#
# Run ci/installed_e2e.sh first: this script copies whatever that installed.
#
# "An environment that never ran the installer" is a second local Windows account
# on the builder, `ytcopy`. That account's HKCU has no marker and no Inno
# uninstall record, which is bit for bit the state a fresh PC is in: the app
# cannot tell the two apart, because the only things it looks at are those two
# registry values and neither exists in either case. We have no second Windows
# machine of our own (external-6 and external-260720 are CUSTOMERS' PCs and are
# not ours to install test software on), so this is the honest reproduction.
#
# Four checks:
#   P1  copied folder, fresh account            -> refuse, state=no-marker
#   P2  the same, through the real GUI          -> Korean notice on screen,
#                                                  변환 시작 dead, zero PDFs written
#   P3  copied folder + a marker carried over from another machine
#                                               -> refuse, state=other-machine
#   P4  control: the same copy, run by the account that DID install, same PC
#                                               -> allowed (per the customer's
#                                                  spec only ANOTHER computer fails)
#
# YTSCORE_BUILD_KEY is deliberately never set here.
set -euo pipefail

HOST=bfdev@windows-builder
SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 $HOST"
REMOTE='C:\builds\ytscore'
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/out/protection"
WINPWD='cho28670!!server'
COPYPWD='Copy!Test#2026'
WAIT=${WAIT:-150}

log() { echo "[protection] $*"; }
push() {
  printf '\xEF\xBB\xBF' | cat - "$1" > "$1.bom"
  scp -q -o StrictHostKeyChecking=no "$1.bom" "$HOST:C:/builder/$2"
  rm -f "$1.bom"
}

cat > /tmp/ytscore-prot.ps1 <<PS1
\$dir = 'C:\prot-logs'
\$log = "\$dir\prot.log"
try {
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class Win32Prot {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint f);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  public static IntPtr Find(string needle) {
    IntPtr found = IntPtr.Zero;
    EnumWindows(delegate(IntPtr h, IntPtr p) {
      if (!IsWindowVisible(h)) return true;
      StringBuilder sb = new StringBuilder(512);
      GetWindowTextW(h, sb, 512);
      if (sb.ToString().Contains(needle)) { found = h; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@
\$ErrorActionPreference = 'Stop'
\$lines = New-Object System.Collections.ArrayList
function Say(\$m) { [void]\$lines.Add(\$m); }

Remove-Item \$dir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path \$dir | Out-Null
# the fresh account has to be able to write its own logs and output here
icacls \$dir /grant 'Users:(OI)(CI)F' | Out-Null

# ---- 0. the copy the customer would make: the installed folder, nothing else --
\$app = Join-Path \$env:LOCALAPPDATA 'Programs\youtube-score-pdf'
if (-not (Test-Path \$app)) { throw 'nothing is installed; run ci/installed_e2e.sh first' }
\$copied = 'C:\copied\youtube-score-pdf'
Remove-Item 'C:\copied' -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path 'C:\copied' | Out-Null
Copy-Item \$app 'C:\copied' -Recurse -Force
icacls 'C:\copied' /grant 'Users:(OI)(CI)RX' | Out-Null
\$exe = Join-Path \$copied 'youtube-score-pdf.exe'
if (-not (Test-Path \$exe)) { throw 'the copy has no exe' }
\$srcCount = @(Get-ChildItem \$app -Recurse -File).Count
\$dstCount = @(Get-ChildItem \$copied -Recurse -File).Count
Say "copied \$srcCount files from \$app to \$copied (\$dstCount files)"
if (\$srcCount -ne \$dstCount) { throw 'the copy is incomplete' }

# ---- 1. an account that never ran the installer ------------------------------
\$u = 'ytcopy'
if (-not (Get-LocalUser -Name \$u -ErrorAction SilentlyContinue)) {
  \$sp = ConvertTo-SecureString '$COPYPWD' -AsPlainText -Force
  New-LocalUser -Name \$u -Password \$sp -PasswordNeverExpires -AccountNeverExpires | Out-Null
  Add-LocalGroupMember -Group 'Users' -Member \$u -ErrorAction SilentlyContinue
  Say "created local account \$u"
} else { Say "reusing local account \$u" }
\$cred = New-Object System.Management.Automation.PSCredential(\$u,
        (ConvertTo-SecureString '$COPYPWD' -AsPlainText -Force))

function RunAs(\$who, \$argv, \$tag) {
  \$o = "\$dir\\\$tag.out"; \$e = "\$dir\\\$tag.err"
  Remove-Item \$o, \$e -Force -ErrorAction SilentlyContinue
  if (\$who) {
    \$p = Start-Process \$exe -Credential \$who -ArgumentList \$argv -WorkingDirectory \$dir \`
         -RedirectStandardOutput \$o -RedirectStandardError \$e -PassThru -Wait
  } else {
    \$p = Start-Process \$exe -ArgumentList \$argv -WorkingDirectory \$dir \`
         -RedirectStandardOutput \$o -RedirectStandardError \$e -PassThru -Wait
  }
  \$txt = ''
  if (Test-Path \$o) { \$txt = (Get-Content \$o -Raw) }
  Say "--- \$tag exit=\$(\$p.ExitCode)"
  foreach (\$l in (\$txt -split "\`r?\`n")) { if (\$l.Trim()) { Say "    \$l" } }
  return \$p.ExitCode
}

# ---- P1: copied folder, fresh account ----------------------------------------
\$rc = RunAs \$cred @('--protection-status') 'p1-fresh-account'
if (\$rc -ne 3) { throw "P1: a copy on a machine that never installed was ALLOWED (exit \$rc)" }
Say 'P1 PASS: copied folder + never-installed account -> refused'

# ---- P2: the same thing through the real window ------------------------------
\$outdir = "\$dir\p2-out"
New-Item -ItemType Directory -Force -Path \$outdir | Out-Null
icacls \$outdir /grant 'Users:(OI)(CI)F' | Out-Null
\$demo = @('--guidemo', '--url=https://youtu.be/6PbwedZDFfQ', "--out=\$outdir", '--hold=900000')
\$proc = Start-Process \$exe -Credential \$cred -ArgumentList \$demo -WorkingDirectory \$dir -PassThru
\$hwnd = [IntPtr]::Zero
for (\$i = 0; \$i -lt 90; \$i++) {
  Start-Sleep -Seconds 1
  \$hwnd = [Win32Prot]::Find('유튜브 악보 PDF 변환기')
  if (\$hwnd -ne [IntPtr]::Zero) { break }
}
if (\$hwnd -eq [IntPtr]::Zero) { throw 'P2: the copied app never opened a window' }
Say "P2 window found after \$i s"
# --guidemo presses 변환 시작 at 1.5s. Give it far longer than a conversion needs
# to start writing anything, then prove nothing was written.
Start-Sleep -Seconds $WAIT

function Shoot(\$h, \$name) {
  [Win32Prot]::ShowWindow(\$h, 5) | Out-Null
  [Win32Prot]::SetForegroundWindow(\$h) | Out-Null
  Start-Sleep -Seconds 2
  \$r = New-Object Win32Prot+RECT
  [Win32Prot]::GetWindowRect(\$h, [ref] \$r) | Out-Null
  \$w = \$r.R - \$r.L; \$hh = \$r.B - \$r.T
  if (\$w -lt 200 -or \$hh -lt 100) { Say "\$name rect \${w}x\${hh}, skipped"; return 0 }
  \$bmp = New-Object System.Drawing.Bitmap \$w, \$hh
  \$g = [System.Drawing.Graphics]::FromImage(\$bmp)
  \$hdc = \$g.GetHdc()
  \$ok = [Win32Prot]::PrintWindow(\$h, \$hdc, 2)
  \$g.ReleaseHdc(\$hdc)
  if (-not \$ok) { Say "\$name PrintWindow failed"; return 0 }
  \$bmp.Save("\$dir\\\$name.png")
  \$colors = @{}
  for (\$x = 0; \$x -lt \$w; \$x += 7) { for (\$y = 0; \$y -lt \$hh; \$y += 7) {
    \$colors[\$bmp.GetPixel(\$x, \$y).ToArgb()] = 1 } }
  Say "\$name captured \${w}x\${hh}, \$(\$colors.Count) colours"
  return \$colors.Count
}

\$dlg = [Win32Prot]::Find('설치 확인')
if (\$dlg -ne [IntPtr]::Zero) { Shoot \$dlg 'p2-dialog' | Out-Null; Say 'P2: the Korean notice dialog is on screen' }
\$c = Shoot \$hwnd 'p2-window'
if (\$c -lt 12) { throw 'P2: the window capture is blank' }

\$pdfs = @(Get-ChildItem \$outdir -Filter *.pdf -Recurse -ErrorAction SilentlyContinue)
Say "P2 PDFs written by the copied app: \$(\$pdfs.Count)"
if (\$pdfs.Count -ne 0) { throw 'P2: the copied app CONVERTED something' }
Stop-Process -Id \$proc.Id -Force -ErrorAction SilentlyContinue
Get-Process -Name 'youtube-score-pdf' -ErrorAction SilentlyContinue |
  Where-Object { \$_.Path -eq \$exe } | Stop-Process -Force -ErrorAction SilentlyContinue
Say 'P2 PASS: Korean notice shown, 변환 시작 refused, no PDF produced'

# ---- P3: they copied the registry marker too ---------------------------------
# A marker computed for a DIFFERENT machine id, planted in the fresh account's
# HKCU. Carrying the source PC's key across does not help: the token is bound to
# the source machine, and this machine hashes to something else.
\$foreign = 'ffffffff-1111-2222-3333-444444444444'
\$sha = [System.Security.Cryptography.SHA256]::Create()
\$bytes = [System.Text.Encoding]::ASCII.GetBytes("ytscore-activation-v1|\$foreign")
\$tok = -join (\$sha.ComputeHash(\$bytes) | ForEach-Object { \$_.ToString('x2') })
Say "planting a marker for machine \$foreign (\$tok)"
\$p = Start-Process 'reg.exe' -Credential \$cred -WorkingDirectory \$dir -PassThru -Wait \`
     -ArgumentList @('add', 'HKCU\Software\youtube-score-pdf', '/v', 'InstallToken',
                     '/t', 'REG_SZ', '/d', \$tok, '/f')
if (\$p.ExitCode -ne 0) { throw "could not plant the foreign marker (exit \$(\$p.ExitCode))" }
\$rc = RunAs \$cred @('--protection-status') 'p3-foreign-marker'
if (\$rc -ne 3) { throw "P3: a marker from another machine was ACCEPTED (exit \$rc)" }
Say 'P3 PASS: a marker carried over from another machine is refused'
Start-Process 'reg.exe' -Credential \$cred -WorkingDirectory \$dir -Wait \`
  -ArgumentList @('delete', 'HKCU\Software\youtube-score-pdf', '/f') -ErrorAction SilentlyContinue

# ---- P4: control. Same copy, the account that DID install, same PC ------------
# Per the customer's spec only ANOTHER computer must fail; moving the folder
# around on the PC you installed on is not what they asked us to stop.
\$rc = RunAs \$null @('--protection-status') 'p4-installer-account'
if (\$rc -ne 0) { throw "P4: the machine that DID install was refused (exit \$rc)" }
Say 'P4 PASS: on the PC that ran the installer, the same copy is allowed'

Say 'ALL PROTECTION CHECKS PASSED'
\$lines -join "\`r\`n" | Out-File \$log -Encoding utf8
} catch {
  New-Item -ItemType Directory -Force -Path \$dir | Out-Null
  ((\$lines -join "\`r\`n") + "\`r\`nFAILED: \$(\$_.Exception.Message)") | Out-File \$log -Encoding utf8
}
PS1
push /tmp/ytscore-prot.ps1 ytscore-prot.ps1

# Delete the previous log from THIS side, before the task starts: a task that
# never starts would otherwise let the poll read the last run and report a stale
# pass. That has happened on this project before.
$SSH "Remove-Item 'C:\\prot-logs\\prot.log' -Force -ErrorAction SilentlyContinue" > /dev/null 2>&1 || true
log "running the copied-folder checks in session 1"
$SSH "schtasks /create /tn ytscore-prot /tr \"powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\\builder\\ytscore-prot.ps1\" /sc once /st 00:00 /ru bfdev /rp '$WINPWD' /it /f | Out-Null
      schtasks /run /tn ytscore-prot | Out-Null" > /dev/null
RES=""
deadline=$((SECONDS + WAIT + 600))
while [ $SECONDS -lt $deadline ]; do
  sleep 20
  RES=$($SSH "Get-Content 'C:\\prot-logs\\prot.log' -Raw -ErrorAction SilentlyContinue" 2>/dev/null || true)
  if [ -n "$RES" ]; then break; fi
done
$SSH "schtasks /delete /tn ytscore-prot /f | Out-Null" > /dev/null 2>&1 || true

mkdir -p "$OUT"
printf '%s\n' "$RES" | tr -d '\r' > "$OUT/protection.log"
$SSH "Remove-Item C:\\builds\\ytscore-prot.zip -ErrorAction SilentlyContinue
      Compress-Archive -Path 'C:\\prot-logs\\*' -DestinationPath C:\\builds\\ytscore-prot.zip -Force" > /dev/null 2>&1 || true
scp -q -o StrictHostKeyChecking=no "$HOST:C:/builds/ytscore-prot.zip" "$OUT/_p.zip" 2>/dev/null || true
if [ -f "$OUT/_p.zip" ]; then
  python3 - "$OUT/_p.zip" "$OUT" <<'PY'
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
  rm -f "$OUT/_p.zip"
fi
cat "$OUT/protection.log"
find "$OUT" -type f -printf '%10s  %p\n' | sort -k2
case "$RES" in
  *"ALL PROTECTION CHECKS PASSED"*) log "done";;
  *) log "PROTECTION GATE FAILED"; exit 1;;
esac
