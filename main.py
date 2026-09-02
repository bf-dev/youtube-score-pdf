# -*- coding: utf-8 -*-
"""
유튜브 악보 PDF 변환기 - entry point. Kmong customer 1775529, order 7589200.

The customer's entry point is the GUI, with no arguments. Everything below with
a flag is OURS (CI, diagnostics) and is never part of what they run:

    --guiselftest     build the window, close it, exit 0 (proves tkinter bundled)
    --guidemo         fill the window in, run a real conversion, hold it for the
                      screenshot; --url=, --title=, --out=, --hold= tune it
    --selftest        headless end-to-end: convert --url= and assert the PDF
    --artifacts-test  post one report and print the server's answer
    --console         run one conversion on the console, for a shell on the builder
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

from ytscore import bridge, config, paths          # noqa: E402


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


def artifacts_test() -> int:
    r = bridge.send(text="artifacts wire test from the build pipeline",
                    parts={"diagnostics.json": __import__("json").dumps(
                        bridge.diagnostics(), ensure_ascii=False, indent=2)},
                    tag="selftest", blocking=True)
    print("artifacts response:", r, flush=True)
    ok = bool(r) and r.get("status") == 200 and (r.get("data") or {}).get("matched") is True
    print("ARTIFACTS_OK" if ok else "ARTIFACTS_FAIL", flush=True)
    return 0 if ok else 1


def selftest() -> int:
    """Headless end-to-end on a real YouTube link, then report the run."""
    import json
    import time
    from ytscore import pipeline

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


def console() -> int:
    from ytscore import pipeline
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


def main() -> int:
    sys.excepthook = _hook
    argv = sys.argv[1:]
    if "--version" in argv:
        print(f"{config.APP_SLUG} {config.APP_VERSION} (customer {config.CUSTOMER_ID})")
        return 0
    if "--guiselftest" in argv:
        return gui_selftest()
    if "--artifacts-test" in argv:
        return artifacts_test()
    if "--selftest" in argv:
        return selftest()
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
