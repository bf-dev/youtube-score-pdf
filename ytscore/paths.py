# -*- coding: utf-8 -*-
"""Where the app finds its bundled tools, its font, and its own writable state.

Frozen (PyInstaller --onedir) the app carries its own ffmpeg/ffprobe and the
Korean font, because a customer PC has neither. Run from source on this Linux
host it falls back to whatever is on PATH, so `src/score_pdf.py` keeps working
exactly as it did before the app was wrapped around it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Directory that holds the bundled assets (font, ffmpeg)."""
    if frozen():
        # --onedir: sys._MEIPASS is dist/<app>/_internal
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def _exe(name: str) -> str:
    if os.name == "nt":
        cand = resource_dir() / "bin" / f"{name}.exe"
        if cand.is_file():
            return str(cand)
        return f"{name}.exe"
    cand = resource_dir() / "bin" / name
    if cand.is_file():
        return str(cand)
    return name


def ffmpeg() -> str:
    return _exe("ffmpeg")


def ffprobe() -> str:
    return _exe("ffprobe")


def kr_font_path() -> str | None:
    """Korean-capable TTF for the PDF header."""
    bundled = resource_dir() / "assets" / "NanumGothic.ttf"
    if bundled.is_file():
        return str(bundled)
    for p in ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
              "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
              r"C:\Windows\Fonts\malgun.ttf"):
        if os.path.exists(p):
            return p
    return None


def app_data_dir() -> Path:
    """Per-user writable dir: settings, run logs, temp video downloads."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    d = base / "youtube-score-pdf"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_output_dir() -> Path:
    """Desktop\\유튜브악보PDF, falling back to the home dir when there is no Desktop."""
    for cand in (Path.home() / "Desktop", Path.home() / "바탕 화면", Path.home()):
        if cand.is_dir():
            return cand / "유튜브악보PDF"
    return Path.home() / "유튜브악보PDF"


# A --noconsole exe has no console, so every child process would otherwise flash
# its own black window in the customer's face, once per ffmpeg call.
CREATE_NO_WINDOW = 0x08000000


def popen_kwargs() -> dict:
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"creationflags": CREATE_NO_WINDOW, "startupinfo": si}
    return {}
