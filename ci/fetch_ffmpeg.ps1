# Bring ffmpeg.exe / ffprobe.exe into bin\ so the app carries its own decoder.
# A customer PC has no ffmpeg, and OpenCV's bundled one cannot decode the AV1
# that YouTube serves for many of these videos (it silently returns 0 frames,
# which is how the pipeline lost a whole afternoon once).
#
# ASCII ONLY (Windows PowerShell 5.1 reads this file as ANSI).
$ErrorActionPreference = "Stop"
$dst = Join-Path $PWD "bin"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
if ((Test-Path "$dst\ffmpeg.exe") -and (Test-Path "$dst\ffprobe.exe")) {
  Write-Host "ffmpeg already present"; exit 0
}
$urls = @(
  "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
  "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
)
$zip = Join-Path $env:TEMP "ffmpeg.zip"
$got = $false
foreach ($url in $urls) {
  try {
    Write-Host "downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    $got = $true; break
  } catch { Write-Host "failed: $($_.Exception.Message)" }
}
if (-not $got) { throw "could not download ffmpeg from any mirror" }

$tmp = Join-Path $env:TEMP "ffmpeg-extract"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $tmp
foreach ($n in @("ffmpeg.exe", "ffprobe.exe")) {
  $src = Get-ChildItem $tmp -Recurse -Filter $n | Select-Object -First 1
  if (-not $src) { throw "$n not found in the archive" }
  Copy-Item $src.FullName (Join-Path $dst $n) -Force
  "{0}  {1:N1} MB" -f $n, ((Get-Item (Join-Path $dst $n)).Length / 1MB)
}
& "$dst\ffmpeg.exe" -hide_banner -version | Select-Object -First 1
$dec = & "$dst\ffmpeg.exe" -hide_banner -decoders 2>&1 | Out-String
$av1 = ($dec -split "`n") | Where-Object { $_ -match "\bav1\b|libdav1d" }
if (-not $av1) { throw "this ffmpeg has no AV1 decoder: YouTube AV1 videos would decode 0 frames" }
Write-Host "AV1 decoders present:"
$av1 | ForEach-Object { Write-Host "  $($_.Trim())" }
