# -*- coding: utf-8 -*-
"""Can this runner reach YouTube at all? Exit 0 = yes, 1 = no.

GitHub's IP ranges are the ones YouTube most often answers with "Sign in to
confirm you're not a bot", so the end-to-end step is gated on this instead of
turning a network block into a red build that says nothing about the code.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

URL = sys.argv[1] if len(sys.argv) > 1 else "https://youtu.be/2RIsnf--0VY"

try:
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                           "noplaylist": True}) as ydl:
        info = ydl.extract_info(URL, download=False)
    print(f"reachable: {info.get('title')} ({info.get('duration')}s, "
          f"{info.get('width')}x{info.get('height')})")
    sys.exit(0)
except Exception as exc:                                   # noqa: BLE001
    print(f"NOT reachable from this runner: {type(exc).__name__}: {str(exc)[:300]}")
    sys.exit(1)
