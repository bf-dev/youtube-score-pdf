# -*- coding: utf-8 -*-
"""
유튜브 악보 PDF 변환기 - entry point. Kmong customer 1775529, order 7589200.

The customer's entry point is the GUI, with no arguments. Everything below with
a flag is OURS (CI, diagnostics) and is never part of what they run:

    --guiselftest     build the window, close it, exit 0 (proves tkinter bundled)
    --guidemo         fill the window in, run a real conversion, hold it for the
                      screenshot; --url=, --title=, --out=, --hold= tune it
    --selftest        headless end-to-end: convert --url= and assert the PDF
    --scrolltest      the other side of that gate: run --url= and assert the
                      scroll guard REFUSES it, printing the Korean refusal the
                      customer would see and failing if a PDF appeared anyway
    --artifacts-test  post one report and print the server's answer
    --console         run one conversion on the console, for a shell on the builder
    --protection-status
                      print the copy-protection verdict for this copy, exit 0 when
                      it would run and 3 when it would refuse. Reports only; it
                      neither writes a marker nor bypasses one.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

if getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(sys.executable).parent))
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from ytscore import activation, bridge, config, paths          # noqa: E402


def _arg(name: str, default: str | None = None) -> str | None:
    pre = f"--{name}="
    for a in sys.argv[1:]:
        if a.startswith(pre):
            return a[len(pre):]
    return default


def _hook(exc_type, exc, tb) -> None:
    try:
        bridge.send_crash(exc)
    except Exception:
        pass
    try:
        sys.__excepthook__(exc_type, exc, tb)
    except Exception:
        pass


def gui_selftest() -> int:
    from tkinter import Tk
    from ytscore.gui import App
    root = Tk()
    App(root)
    root.after(1500, root.destroy)
    root.mainloop()
    return 0


def protection_status() -> int:
    """Read-only verdict, for the copied-folder test. Writes nothing, bypasses nothing."""
    v = activation.check()
    print(activation.summary(v), flush=True)
    print(f"exe={sys.executable}", flush=True)
    print(f"machine_id={activation.machine_id()}", flush=True)
    print(f"expected_token={activation.expected_token()}", flush=True)
    print(f"stored_token={activation.stored_token()}", flush=True)
    print(f"install_record={activation.installed_here()}", flush=True)
    print("PROTECTION_ALLOW" if v["ok"] else "PROTECTION_REFUSE", flush=True)
    return 0 if v["ok"] else 3


def artifacts_test() -> int:
    r = bridge.send(text="artifacts wire test from the build pipeline",
                    parts={"diagnostics.json": __import__("json").dumps(
                        bridge.diagnostics(), ensure_ascii=False, indent=2)},
                    tag="selftest", blocking=True)
    print("artifacts response:", r, flush=True)
    ok = bool(r) and r.get("status") == 200 and (r.get("data") or {}).get("matched") is True
    print("ARTIFACTS_OK" if ok else "ARTIFACTS_FAIL", flush=True)
    return 0 if ok else 1


def _gated() -> bool:
    """True when this copy must not convert anything. Printed, never silent."""
    v = activation.check()
    if v["ok"]:
        return False
    print(activation.summary(v), flush=True)
    print(activation.NOTICE, flush=True)
    return True


def selftest() -> int:
    """Headless end-to-end on a real YouTube link, then report the run."""
    import json
    import time
    from ytscore import pipeline

    if _gated():
        return 3

    url = _arg("url") or os.environ.get("YTSCORE_TEST_URL") or "https://youtu.be/2RIsnf--0VY"
    out = Path(_arg("out") or (paths.app_data_dir() / "selftest"))
    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    pipeline.set_log_sink(lambda s: (lines.append(s), print(s, flush=True)))
    t0 = time.time()
    try:
        res = pipeline.run(url=url, workdir=out / "work", outdir=out, fps=4.0,
                           dedup_thresh=0.30, proxy=os.environ.get("YTSCORE_PROXY") or None,
                           name="selftest", title_override=None, keep_video=False)
    except Exception as exc:
        bridge.send(text=f"SELFTEST FAILED url={url}: {exc}",
                    parts={"run.log": "\n".join(lines),
                           "traceback.txt": traceback.format_exc(),
                           "diagnostics.json": json.dumps(bridge.diagnostics(),
                                                          ensure_ascii=False, indent=2)},
                    tag="selftest", blocking=True)
        print(f"SELFTEST_FAIL {exc}", flush=True)
        return 2
    pdf = Path(res["pdf"])
    ok = pdf.is_file() and pdf.stat().st_size > 20_000 and res["systems"] >= 1
    r = bridge.send(text=(f"SELFTEST {'OK' if ok else 'FAIL'} url={url}\n"
                          f"systems={res['systems']} pages={res['pages']} "
                          f"bytes={pdf.stat().st_size if pdf.is_file() else 0} "
                          f"elapsed={time.time() - t0:.1f}s"),
                    parts={"run.log": "\n".join(lines),
                           "run.json": json.dumps(res, ensure_ascii=False, indent=2,
                                                  default=str),
                           "diagnostics.json": json.dumps(bridge.diagnostics(),
                                                          ensure_ascii=False, indent=2)},
                    tag="selftest", blocking=True)
    print("artifacts response:", r, flush=True)
    print(f"PDF {pdf} {pdf.stat().st_size if pdf.is_file() else 0} bytes, "
          f"{res['systems']} systems, {res['pages']} pages", flush=True)
    matched = bool(r) and (r.get("data") or {}).get("matched") is True
    print("SELFTEST_OK" if ok else "SELFTEST_FAIL", flush=True)
    print("ARTIFACTS_OK" if matched else "ARTIFACTS_FAIL", flush=True)
    return 0 if (ok and matched) else 1


def scrolltest() -> int:
    """
    Prove the scroll guard on the packaged build: this URL must be REFUSED, with
    the Korean message, and must leave no PDF behind. A guard that only exists
    in the source tree is not a guard the customer has.
    """
    import time
    from ytscore import pipeline
    from ytscore.gui import korean_error

    if _gated():
        return 3
    url = _arg("url") or "https://youtu.be/rsUfI3EKAj4"
    out = Path(_arg("out") or (paths.app_data_dir() / "scrolltest"))
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.pdf"):
        stale.unlink()
    lines: list[str] = []
    pipeline.set_log_sink(lambda s: (lines.append(s), print(s, flush=True)))
    t0 = time.time()
    verdict, detail = "no-refusal", ""
    try:
        pipeline.run(url=url, workdir=out / "work", outdir=out, fps=4.0,
                     dedup_thresh=0.30, proxy=os.environ.get("YTSCORE_PROXY") or None,
                     name="scrolltest", title_override=None, keep_video=False)
    except pipeline.ScrollingScore as exc:
        verdict, detail = "refused", str(exc)
        print(f"guard: {detail}", flush=True)
        print(korean_error(exc), flush=True)
    except Exception as exc:                                     # noqa: BLE001
        verdict, detail = f"wrong-error:{type(exc).__name__}", str(exc)
        print(f"UNEXPECTED {type(exc).__name__}: {exc}", flush=True)
    pdfs = sorted(p.name for p in out.glob("*.pdf"))
    ok = verdict == "refused" and not pdfs
    print(f"pdfs left behind: {pdfs}", flush=True)
    bridge.send(text=(f"SCROLLTEST {'OK' if ok else 'FAIL'} url={url} "
                      f"verdict={verdict} pdfs={pdfs} "
                      f"elapsed={time.time() - t0:.1f}s\n{detail}"),
                parts={"run.log": "\n".join(lines)}, tag="scrolltest", blocking=True)
    print("SCROLLTEST_OK" if ok else "SCROLLTEST_FAIL", flush=True)
    return 0 if ok else 1


def console() -> int:
    from ytscore import pipeline
    if _gated():
        return 3
    url = _arg("url") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not url:
        print("usage: --console --url=<youtube link> [--out=<dir>]")
        return 2
    out = Path(_arg("out") or paths.default_output_dir())
    res = pipeline.run(url=url, workdir=out / "_work", outdir=out, fps=4.0,
                       dedup_thresh=0.30, proxy=os.environ.get("YTSCORE_PROXY") or None,
                       name=_arg("name", "score"), title_override=_arg("title"),
                       keep_video=False)
    print(res["pdf"])
    return 0


def _utf8_console() -> None:
    """
    Our CI modes print Korean (a YouTube title, a status line) to a redirected
    stdout, whose default encoding on a Korean/US Windows console is cp949/cp1252
    and raises UnicodeEncodeError on the first Hangul character. Guarded on both
    sides: in the shipped --noconsole build sys.stdout is None and has no
    .reconfigure, and that must not be an error either.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    _utf8_console()
    sys.excepthook = _hook
    argv = sys.argv[1:]
    if "--version" in argv:
        print(f"{config.APP_SLUG} {config.APP_VERSION} (customer {config.CUSTOMER_ID})")
        return 0
    if "--protection-status" in argv:
        return protection_status()
    if "--guiselftest" in argv:
        return gui_selftest()
    if "--artifacts-test" in argv:
        return artifacts_test()
    if "--selftest" in argv:
        return selftest()
    if "--scrolltest" in argv:
        return scrolltest()
    if "--console" in argv:
        return console()

    from ytscore import updater
    from ytscore.gui import run_gui
    demo = None
    if "--guidemo" in argv:
        demo = {"url": _arg("url", "https://youtu.be/2RIsnf--0VY"),
                "title": _arg("title", ""),
                "outdir": _arg("out", str(paths.default_output_dir())),
                "hold": int(_arg("hold", "300000")),
                "autostart": "--nostart" not in argv}
    updater.start()
    return run_gui(demo)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:                       # noqa: BLE001
        try:
            bridge.send_crash(exc)
        except Exception:
            pass
        raise
