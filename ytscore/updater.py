# -*- coding: utf-8 -*-
"""
Auto-update. A fix is a republish, not a reinstall.

This app ships as an installer (--onedir, so start-up is instant), which changes
the house hot-swap slightly: there is no single exe to copy over, so the update
is "download the new installer, run it silently, let it replace the folder and
relaunch". Inno Setup's /SILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS does
exactly that.

Filename rule (Cloudflare edge cache): every build is published under a NEW
version-suffixed name and version-ytscore.json is repointed at it. Overwriting a
served filename hands out stale bytes for hours and puts the updater in a loop.

Nothing here may ever break the running program: every path is wrapped, and the
check runs on a daemon thread.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading

from ytscore import config

MIN_INSTALLER_BYTES = 5_000_000


def _tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except Exception:
        return (0,)


class UpdaterThread(threading.Thread):
    def __init__(self, status_cb=None) -> None:
        super().__init__(daemon=True, name="updater")
        self._stop = threading.Event()
        self._status = status_cb or (lambda *_: None)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._check_once()
            except Exception:
                pass
            self._stop.wait(config.UPDATE_CHECK_SECONDS)

    def _check_once(self) -> None:
        import requests
        if not getattr(sys, "frozen", False):
            return                              # never swap a dev checkout
        try:
            r = requests.get(config.VERSION_URL, timeout=10,
                             headers={"Cache-Control": "no-cache"})
            if r.status_code != 200:
                return
            data = r.json()
        except Exception:
            return
        latest = str(data.get("version", "")).strip()
        url = data.get("exeUrl") or data.get("installerUrl")
        if not latest or not url:
            return
        if _tuple(latest) <= _tuple(config.APP_VERSION):
            return
        path = self._download(url)
        if not path:
            return
        try:
            from ytscore import bridge
            bridge.send(text=f"auto-update {config.APP_VERSION} -> {latest} ({url})",
                        tag="update")
        except Exception:
            pass
        self._status(f"새 버전({latest})을 내려받았습니다. 곧 자동으로 업데이트됩니다.")
        try:
            subprocess.Popen([path, "/SILENT", "/CLOSEAPPLICATIONS",
                              "/RESTARTAPPLICATIONS", "/NORESTART"],
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        except Exception:
            pass

    def _download(self, url: str) -> str | None:
        import requests
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix="-setup.exe")
            os.close(fd)
            total = 0
            with requests.get(url, timeout=120, stream=True,
                              headers={"Cache-Control": "no-cache"}) as r:
                if r.status_code != 200:
                    os.unlink(tmp)
                    return None
                expected = r.headers.get("Content-Length")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        if chunk:
                            f.write(chunk)
                            total += len(chunk)
            if expected and expected.isdigit() and total != int(expected):
                os.unlink(tmp)
                return None
            if total < MIN_INSTALLER_BYTES:
                os.unlink(tmp)
                return None
            return tmp
        except Exception:
            if tmp:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
            return None


def start(status_cb=None) -> UpdaterThread | None:
    try:
        t = UpdaterThread(status_cb)
        t.start()
        return t
    except Exception:
        return None
