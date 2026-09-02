# youtube-score-pdf

Windows desktop app: paste one or more YouTube links, get one A4 PDF per video
containing the sheet music that appears on screen.

The conversion samples frames, throws away everything without a drawn staff,
groups the frames that show the same system, takes a per-pixel median of each
group (which removes the playhead and anything that transiently crosses the
score), reconstructs black-on-white notation whatever the source looked like,
and lays the systems out on A4 with a Korean-capable title header.

## Build

Windows only (PyInstaller does not cross-compile). CI does this on every push to
`main`; see `.github/workflows/build.yml`.

```
pip install -r requirements.txt pyinstaller
powershell -File ci/fetch_ffmpeg.ps1
pyinstaller --onedir --noconsole --name youtube-score-pdf ^
  --version-file ci/version_info.txt ^
  --add-data "assets/NanumGothic.ttf;assets" ^
  --add-binary "bin/ffmpeg.exe;bin" --add-binary "bin/ffprobe.exe;bin" ^
  --collect-all yt_dlp --hidden-import tkinter --noconfirm main.py
ISCC ci/installer.iss
```

## Modes

`main.py` with no arguments opens the GUI, which is the only thing an end user
runs. `--guiselftest`, `--guidemo`, `--selftest`, `--artifacts-test` and
`--console` exist for CI and diagnostics.

## Licence

NanumGothic (`assets/`) is distributed under the SIL Open Font License 1.1.
