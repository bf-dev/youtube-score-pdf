# -*- coding: utf-8 -*-
"""
Copy protection. Kmong customer 1775529, order 7590116.

What the customer asked for, exactly:

  1. a PC where the INSTALLER was run works normally;
  2. copying the installed folder to another PC and running it there does NOT work;
  3. running the installer on that other PC installs and works normally;
  4. NO limit on how many PCs. Three, five, ten, all fine.

So this is deliberately NOT a licence, NOT a machine lock, NOT a seat count. There
is no server, no activation key, no network call. The only question asked is
"did the installer run on THIS machine, for THIS user?", and the only way to fail
it is to have never run the installer here.

How that question is answered, in order. Both pieces of evidence live OUTSIDE the
install folder, which is the whole point: a marker inside the program directory
travels with the copy and proves nothing.

  1. HKCU\\Software\\youtube-score-pdf\\InstallToken, written by the installer
     ([Registry] in ci/installer.iss), holding sha256("<salt>|<machine id>").
     The machine id is HKLM MachineGuid, so the token a copier carries along with
     the folder does not verify on their machine.
  2. Inno Setup's own uninstall record for our AppId (HKCU, because the installer
     is PrivilegesRequired=lowest), whose InstallLocation must be the folder the
     running exe actually sits in. This is the self-heal path: it covers a v1.0.2
     user whose marker predates this feature, and a marker a cleaner wiped. When
     it fires, the marker is rewritten so the next launch takes path 1.

Anything else refuses. Nothing here is bypass-proof and it is not meant to be:
whoever has the installer can install, which is the customer's own design.

Never raises. A protection check that crashes the app would be worse than no
protection at all, so every failure of the machinery itself resolves to "allow".
"""

from __future__ import annotations

import hashlib
import os
import sys
import time

from ytscore import config

# The salt only exists so the stored value is not a bare, recognisable machine
# GUID sitting in the registry under our own key.
_SALT = "ytscore-activation-v1"

REG_KEY = r"Software\youtube-score-pdf"
REG_VALUE = "InstallToken"
# Inno's AppId from ci/installer.iss, plus the _is1 suffix Inno appends.
UNINSTALL_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
                 r"\{7E5B1C64-3F8A-4C51-9E2D-1775529A0001}_is1")

# What the customer sees. One message for every refusal reason: the difference
# between "no marker" and "marker from another PC" is our diagnostic, not theirs,
# and the fix is the same sentence either way.
NOTICE_TITLE = "설치 확인"
NOTICE = ("정상 설치된 프로그램이 아닙니다. 설치 파일로 다시 설치해 주세요.")
NOTICE_DETAIL = (
    "이 폴더는 다른 컴퓨터에서 복사해 온 것으로 보입니다.\n"
    "설치 파일(youtube-score-pdf-setup-x.x.x.exe)을 받아서 이 컴퓨터에 설치하시면\n"
    "바로 사용하실 수 있습니다. 설치 가능한 PC 대수에는 제한이 없습니다.")

OK_STATES = ("ok-marker", "ok-installed-here", "ok-not-frozen", "ok-not-windows",
             "ok-build-key", "ok-check-failed")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("ascii", "ignore")).hexdigest()


def _reg_read(root: int, key: str, name: str) -> str | None:
    import winreg
    try:
        with winreg.OpenKey(root, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            value, _ = winreg.QueryValueEx(k, name)
        value = str(value).strip()
        return value or None
    except Exception:
        return None


def machine_id() -> str | None:
    """
    A stable per-machine identifier, read the same way the installer reads it.

    HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid: present on every Windows
    since XP, survives reboots, updates and renames, and differs on a different
    PC. Deliberately no fallback chain: the installer would have to reproduce it
    exactly, and a mismatch there locks out a paying customer. If this is
    unreadable the marker is simply never written and evidence 2 carries the check.
    """
    if os.name != "nt":
        return None
    guid = _reg_read(_HKLM(), r"SOFTWARE\Microsoft\Cryptography", "MachineGuid")
    if not guid:
        return None
    return guid.strip("{}").lower()


def _HKLM() -> int:
    import winreg
    return winreg.HKEY_LOCAL_MACHINE


def _HKCU() -> int:
    import winreg
    return winreg.HKEY_CURRENT_USER


def expected_token() -> str | None:
    mid = machine_id()
    if not mid:
        return None
    return _sha(f"{_SALT}|{mid}")


def stored_token() -> str | None:
    if os.name != "nt":
        return None
    return _reg_read(_HKCU(), REG_KEY, REG_VALUE)


def write_marker(token: str | None = None) -> bool:
    """Write the HKCU marker for this machine. Used by the self-heal path."""
    if os.name != "nt":
        return False
    token = token or expected_token()
    if not token:
        return False
    import winreg
    try:
        with winreg.CreateKeyEx(_HKCU(), REG_KEY, 0,
                                winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as k:
            winreg.SetValueEx(k, REG_VALUE, 0, winreg.REG_SZ, token)
            winreg.SetValueEx(k, "InstalledVersion", 0, winreg.REG_SZ, config.APP_VERSION)
            winreg.SetValueEx(k, "MarkedAt", 0, winreg.REG_SZ,
                              time.strftime("%Y-%m-%d %H:%M:%S"))
        return True
    except Exception:
        return False


def _app_dir() -> str:
    """The folder the running program actually sits in."""
    from pathlib import Path
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve().parent)
    return str(Path(__file__).resolve().parent.parent)


def _same_dir(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def installed_here() -> str | None:
    """
    Inno's own uninstall record for our AppId, if its InstallLocation is the
    folder we are running from. Returns that location, or None.
    """
    if os.name != "nt":
        return None
    loc = _reg_read(_HKCU(), UNINSTALL_KEY, "InstallLocation")
    if not loc:
        return None
    if _same_dir(loc, _app_dir()):
        return loc
    return None


def check() -> dict:
    """
    The verdict. Always returns a dict, never raises.

        {"ok": bool, "state": str, "detail": str}
    """
    try:
        if not getattr(sys, "frozen", False):
            return _v(True, "ok-not-frozen", "running from source")
        if os.name != "nt":
            return _v(True, "ok-not-windows", f"os.name={os.name}")
        # Our CI runs the un-installed build tree (ci/winbuilder.sh). Never set
        # by the installer and never present on a customer machine.
        key = os.environ.get("YTSCORE_BUILD_KEY", "")
        if key and _sha(key) == config.BUILD_KEY_SHA256:
            return _v(True, "ok-build-key", "build-tree run")

        want = expected_token()
        have = stored_token()
        if want and have and have.lower() == want:
            return _v(True, "ok-marker", "installer marker matches this machine")

        loc = installed_here()
        if loc:
            # Installed on this machine by this user, but the marker is absent or
            # stale (a v1.0.2 install predating this feature, or a wiped key).
            # Refresh it and let them through: they did run the installer.
            wrote = write_marker(want)
            return _v(True, "ok-installed-here",
                      f"uninstall record at {loc}; marker rewritten={wrote}")

        if have and want and have.lower() != want:
            return _v(False, "other-machine",
                      "marker present but bound to a different machine id")
        if have and not want:
            return _v(False, "no-machine-id",
                      "marker present but this machine has no readable MachineGuid")
        return _v(False, "no-marker",
                  f"no installer marker and no install record for {_app_dir()}")
    except Exception as exc:                                     # noqa: BLE001
        # The machinery itself broke. Allowing is the only acceptable outcome:
        # a false refusal costs a paying customer their program.
        return _v(True, "ok-check-failed", f"{type(exc).__name__}: {exc}")


def _v(ok: bool, state: str, detail: str) -> dict:
    return {"ok": bool(ok), "state": state, "detail": detail}


def summary(verdict: dict | None = None) -> str:
    v = verdict or check()
    return f"activation={'OK' if v['ok'] else 'BLOCKED'} state={v['state']} ({v['detail']})"
