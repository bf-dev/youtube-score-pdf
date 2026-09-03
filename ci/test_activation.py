# -*- coding: utf-8 -*-
"""
Branch coverage for ytscore/activation.py without a Windows machine.

Every decision the copy protection makes is a registry read, so a fake winreg is
enough to drive all of them. This does NOT replace the real reproduction on
Windows (a build that passes here can still be wrong about how the installer
writes the marker); it exists so a broken branch is caught in seconds instead of
after a 30-minute build.

    python3 ci/test_activation.py
"""
from __future__ import annotations

import hashlib
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HKLM, HKCU = 1, 2
CRYPTO = r"SOFTWARE\Microsoft\Cryptography"
MACHINE_A = "3e81ba0c-971d-42b8-9c47-23a77308cb7a"
MACHINE_B = "11111111-2222-3333-4444-555555555555"
APP_DIR = r"C:\Users\test\AppData\Local\Programs\youtube-score-pdf"


def token_for(mid: str) -> str:
    return hashlib.sha256(f"ytscore-activation-v1|{mid}".encode("ascii")).hexdigest()


class FakeWinreg(types.ModuleType):
    HKEY_LOCAL_MACHINE = HKLM
    HKEY_CURRENT_USER = HKCU
    KEY_READ = 0x20019
    KEY_WRITE = 0x20006
    KEY_WOW64_64KEY = 0x0100
    REG_SZ = 1

    def __init__(self, store: dict) -> None:
        super().__init__("winreg")
        self.store = store

    def OpenKey(self, root, key, _res=0, _access=0):          # noqa: N802
        if (root, key) not in self.store:
            raise FileNotFoundError(key)
        return _Key(self.store, (root, key))

    def CreateKeyEx(self, root, key, _res=0, _access=0):      # noqa: N802
        self.store.setdefault((root, key), {})
        return _Key(self.store, (root, key))

    def QueryValueEx(self, k, name):                          # noqa: N802
        return k.get(name)

    def SetValueEx(self, k, name, _r, _t, value):             # noqa: N802
        k.set(name, value)


class _Key:
    def __init__(self, store, ref):
        self.store, self.ref = store, ref

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, name):
        d = self.store[self.ref]
        if name not in d:
            raise FileNotFoundError(name)
        return d[name], 1

    def set(self, name, value):
        self.store.setdefault(self.ref, {})[name] = value


def run_case(name: str, store: dict, *, frozen=True, nt=True, env=None,
             app_dir=APP_DIR, expect_ok=None, expect_state=None) -> tuple[bool, str]:
    for mod in [m for m in sys.modules if m.startswith("ytscore")]:
        del sys.modules[mod]
    sys.modules["winreg"] = FakeWinreg(store)
    from ytscore import activation

    activation.sys = types.SimpleNamespace(frozen=frozen, executable=app_dir + r"\app.exe")
    real_env = dict(os.environ)
    os.environ.clear()
    os.environ.update(env or {})
    saved_name = activation.os.name
    try:
        activation.os.name = "nt" if nt else "posix"
        activation._app_dir = lambda: app_dir
        v = activation.check()
    finally:
        activation.os.name = saved_name
        os.environ.clear()
        os.environ.update(real_env)

    ok = (v["ok"] == expect_ok) and (expect_state is None or v["state"] == expect_state)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: ok={v['ok']} state={v['state']}")
    return ok, v["state"]


def base_store(machine=MACHINE_A) -> dict:
    return {(HKLM, CRYPTO): {"MachineGuid": machine}}


def main() -> int:
    results = []
    print("copy protection, faked registry")

    # 1. installed on this machine: the installer's marker matches.
    s = base_store()
    s[(HKCU, r"Software\youtube-score-pdf")] = {"InstallToken": token_for(MACHINE_A)}
    results.append(run_case("installed here, marker matches", s,
                            expect_ok=True, expect_state="ok-marker")[0])

    # 2. the customer's case: folder copied to a PC that never ran the installer.
    results.append(run_case("copied folder, no marker anywhere", base_store(MACHINE_B),
                            expect_ok=False, expect_state="no-marker")[0])

    # 3. copied WITH the source PC's registry marker: still a different machine.
    s = base_store(MACHINE_B)
    s[(HKCU, r"Software\youtube-score-pdf")] = {"InstallToken": token_for(MACHINE_A)}
    results.append(run_case("copied folder + stolen marker", s,
                            expect_ok=False, expect_state="other-machine")[0])

    # 4. a v1.0.2 install that predates the marker: Inno's own uninstall record
    #    for THIS folder proves the installer ran here. Must self-heal, not lock out.
    s = base_store()
    s[(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
             r"\{7E5B1C64-3F8A-4C51-9E2D-1775529A0001}_is1")] = {"InstallLocation": APP_DIR}
    ok, _ = run_case("legacy install, no marker yet", s,
                     expect_ok=True, expect_state="ok-installed-here")
    healed = s.get((HKCU, r"Software\youtube-score-pdf"), {}).get("InstallToken")
    print(f"  {'PASS' if healed == token_for(MACHINE_A) else 'FAIL'}  "
          f"   marker rewritten for this machine")
    results += [ok, healed == token_for(MACHINE_A)]

    # 5. an uninstall record pointing somewhere else does NOT vouch for this copy.
    s = base_store()
    s[(HKCU, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
             r"\{7E5B1C64-3F8A-4C51-9E2D-1775529A0001}_is1")] = {
                 "InstallLocation": r"C:\Program Files\somewhere-else"}
    results.append(run_case("install record for a different folder", s,
                            expect_ok=False, expect_state="no-marker")[0])

    # 6. no seat count anywhere: three different machines, each with its own
    #    marker, all allowed. This is the requirement the customer was explicit about.
    for n, mid in enumerate(["aaaaaaaa-0000-0000-0000-000000000001",
                             "bbbbbbbb-0000-0000-0000-000000000002",
                             "cccccccc-0000-0000-0000-000000000003"], 1):
        s = base_store(mid)
        s[(HKCU, r"Software\youtube-score-pdf")] = {"InstallToken": token_for(mid)}
        results.append(run_case(f"machine {n} of 3, independently installed", s,
                                expect_ok=True, expect_state="ok-marker")[0])

    # 7. our own CI runs the un-installed build tree.
    results.append(run_case("build tree with the CI key", base_store(),
                            env={"YTSCORE_BUILD_KEY": "e4f334903a6524ba9c84df09f45b6a66"},
                            expect_ok=True, expect_state="ok-build-key")[0])
    results.append(run_case("build tree with a wrong key", base_store(),
                            env={"YTSCORE_BUILD_KEY": "nope"},
                            expect_ok=False, expect_state="no-marker")[0])

    # 8. running from source (this Linux host) is never gated.
    results.append(run_case("not frozen", base_store(), frozen=False,
                            expect_ok=True, expect_state="ok-not-frozen")[0])
    results.append(run_case("not windows", base_store(), nt=False,
                            expect_ok=True, expect_state="ok-not-windows")[0])

    # 9. the machinery itself breaking must ALLOW. A false refusal costs a
    #    paying customer their program; a false allow costs nothing we can see.
    results.append(run_case("registry unreadable entirely", {},
                            expect_ok=False, expect_state="no-marker")[0])

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
