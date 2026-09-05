#!/usr/bin/env python3
"""
Build gate for the 1.7.2 fix: the output folder is the customer's, and it is
not ASCII.

On 2026-09-05 the customer moved to a new PC and pointed the app at
`C:\\Users\\Administrator\\Desktop\\유튜브악보`. Every conversion then died,
five in a row, each after 3 to 7 minutes of work that had already SUCCEEDED:

    pipeline.py, in build_pdf
    pymupdf.FileNotFoundError:
        No such file: 'C:\\Users\\Administrator\\Desktop\\유튜브악보\\_page_15624.png'

`_page_15624.png` is `_page_{os.getpid()}.png`, not a collapsed title. The
mechanism is `cv2.imwrite`: OpenCV opens the destination with a narrow-char
`fopen`, so the UTF-8 path the Python binding hands it is re-read in the process
ANSI code page, the write silently goes nowhere, and `imwrite` returns False
with no exception. `page.insert_image(filename=...)` is then the first thing to
notice, and it is the thing that gets blamed.

This is NOT a 1.7.1 regression. `tmp = out_pdf.parent / f"_page_{os.getpid()}.png"`
has been there since the first commit (74ac8f1, 1.0.0), and 1.7.1 ran green twice
on 2026-09-04 -- on `C:\\builds\\ytscore\\installed-run`, which is ASCII. Every
green run in this project's whole artifact history used an ASCII outdir and every
run in a Korean one failed. FADE_RATIO is not implicated in any way.

WHAT THIS GATE DOES

Linux filenames are bytes, so `cv2.imwrite` to a Korean path works here and the
defect cannot reproduce natively. The gate therefore installs the Windows
behaviour explicitly -- `imwrite` into a path outside the ANSI code page writes
nothing and returns False -- and runs the REAL `build_pdf` under it.

    python3 ci/unicode_path_check.py                 # the shipped code must pass
    python3 ci/unicode_path_check.py --assert-legacy # the 1.7.1 code must fail

`--assert-legacy` replays the 1.7.1 page loop (imwrite to a temp PNG in the
outdir, then insert_image BY NAME) and asserts it raises. A gate nobody has
watched go red is not a gate. The same red direction is reproducible against the
genuine shipped source rather than this replica:

    git worktree add /tmp/yt171 9a4dcdd
    cp ci/unicode_path_check.py /tmp/yt171/ci/ && (cd /tmp/yt171 && python3 ci/unicode_path_check.py)

...which fails with the customer's exact FileNotFoundError.

Both directions also assert OUTPUT EQUIVALENCE: the PDF built in the Korean
folder must render pixel-identical to the same strips built in an ASCII folder.
The fix is allowed to move the failure, never the pixels.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

try:
    import pymupdf
except ImportError:                                     # older wheel name
    import fitz as pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ytscore.pipeline as P   # noqa: E402

# The folder the customer actually chose, and the one the app defaults to.
KOREAN_DIR = "유튜브악보"
DEFAULT_DIR = "유튜브악보PDF"        # ytscore.paths.default_output_dir()


def windows_narrow_path_imwrite(real):
    """
    `cv2.imwrite` as it behaves on Windows: a path the process ANSI code page
    cannot represent is mangled by `fopen`, so nothing is written and the return
    value is False. No exception -- that silence is the whole defect.
    """
    def shim(filename, img, *a, **kw):
        if not str(filename).isascii():
            return False
        return real(filename, img, *a, **kw)
    return shim


def strips(n: int = 7) -> list[np.ndarray]:
    """A few system strips with real ink, so a blank page cannot pass."""
    rng = np.random.default_rng(1775529)
    out = []
    for i in range(n):
        s = np.full((260 + 30 * i, 1780, 3), 255, np.uint8)
        for r in range(60, 200, 34):                    # five staff lines
            s[r:r + 3, 40:1740] = 20
        s[70:190, 100 + 20 * i:1700] = np.minimum(
            s[70:190, 100 + 20 * i:1700],
            rng.integers(0, 255, (120, 1600 - 20 * i, 3), dtype=np.uint8))
        out.append(s)
    return out


def legacy_build_pdf(strips_: list[np.ndarray], out_pdf: Path, title: str) -> int:
    """The 1.7.1 page loop, verbatim in the part that matters."""
    usable_w = P.A4_W - 2 * P.MARGIN
    scaled = []
    for s in strips_:
        h, w = s.shape[:2]
        nh = max(1, int(round(h * usable_w / w)))
        scaled.append(cv2.resize(s, (usable_w, nh), interpolation=cv2.INTER_AREA))
    doc = pymupdf.open()
    tmp = out_pdf.parent / f"_page_{os.getpid()}.png"
    canvas = np.full((P.A4_H, P.A4_W, 3), 255, np.uint8)
    y = P.MARGIN
    for s in scaled:
        if y + s.shape[0] > P.A4_H - P.MARGIN:
            break
        canvas[y:y + s.shape[0], P.MARGIN:P.MARGIN + s.shape[1]] = s
        y += s.shape[0] + 46
    cv2.imwrite(str(tmp), canvas)                       # <- silently False
    page = doc.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(0, 0, 595, 842), filename=str(tmp))
    doc.save(str(out_pdf), deflate=True)
    doc.close()
    return 1


def render(pdf: Path, dpi: int = 96) -> list[np.ndarray]:
    d = pymupdf.open(str(pdf))
    out = []
    for i in range(d.page_count):
        pm = d[i].get_pixmap(dpi=dpi)
        out.append(np.frombuffer(pm.samples, np.uint8)
                   .reshape(pm.height, pm.width, pm.n).copy())
    d.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assert-legacy", action="store_true",
                    help="replay the 1.7.1 page loop and require it to FAIL")
    a = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="ytscore-unicode-"))
    real_imwrite = cv2.imwrite
    fails: list[str] = []
    try:
        ascii_dir = root / "ascii-out"
        ascii_dir.mkdir()
        title = "테스트 악보 (customer 1775529)"
        ref_pdf = ascii_dir / "ref.pdf"
        ref_pages = P.build_pdf(strips(), ref_pdf, title)
        ref = render(ref_pdf)
        print(f"[ref ] ASCII outdir, real cv2.imwrite: {ref_pages} page(s), "
              f"{ref_pdf.stat().st_size} bytes")

        cv2.imwrite = windows_narrow_path_imwrite(real_imwrite)
        P.cv2.imwrite = cv2.imwrite

        for label, folder in (("customer", KOREAN_DIR), ("app default", DEFAULT_DIR)):
            out = root / folder
            out.mkdir()
            pdf = out / "IweNtfTT8PI.pdf"

            if a.assert_legacy:
                try:
                    legacy_build_pdf(strips(), pdf, title)
                except Exception as exc:                # the shipped 1.7.1 crash
                    print(f"[red ] {label:<11} {folder}/: 1.7.1 page loop raised "
                          f"{type(exc).__name__}: {exc}")
                    continue
                fails.append(f"{folder}: the 1.7.1 page loop did NOT fail; "
                             f"this gate cannot go red and is worthless")
                continue

            try:
                npages = P.build_pdf(strips(), pdf, title)
            except Exception as exc:
                fails.append(f"{folder}: build_pdf raised "
                             f"{type(exc).__name__}: {exc}")
                continue
            if not pdf.is_file() or pdf.stat().st_size < 1024:
                fails.append(f"{folder}: no PDF written")
                continue
            if npages != ref_pages:
                fails.append(f"{folder}: {npages} pages, ASCII run gave {ref_pages}")
                continue
            got = render(pdf)
            for i, (x, y) in enumerate(zip(ref, got)):
                if x.shape != y.shape or not np.array_equal(x, y):
                    fails.append(f"{folder}: page {i + 1} is not pixel-identical "
                                 f"to the ASCII-outdir render")
            stray = sorted(p.name for p in out.glob("_page_*.png"))
            if stray:
                fails.append(f"{folder}: left {stray} behind in the customer's folder")

            # ...and the strip dump, which writes into the same folder right
            # after build_pdf and used to go silently missing there.
            sd = out / "IweNtfTT8PI_systems"
            sd.mkdir()
            s0 = strips()[0]
            if not P.imwrite(sd / "000.png", s0) or not (sd / "000.png").is_file():
                fails.append(f"{folder}: the system strip dump wrote nothing")
            else:
                back = cv2.imdecode(np.frombuffer((sd / "000.png").read_bytes(),
                                                  np.uint8), cv2.IMREAD_COLOR)
                if back is None or not np.array_equal(back, s0):
                    fails.append(f"{folder}: the strip round-tripped wrong")
            if not any(f.startswith(folder) for f in fails):
                print(f"[green] {label:<11} {folder}/: {npages} page(s), "
                      f"{pdf.stat().st_size} bytes, every page pixel-identical to "
                      f"the ASCII run, no temp file left, strip dump intact")
    finally:
        cv2.imwrite = real_imwrite
        P.cv2.imwrite = real_imwrite
        shutil.rmtree(root, ignore_errors=True)

    if fails:
        for f in fails:
            print(f"FAIL {f}")
        print("UNICODE_PATH_FAIL")
        return 1
    print("UNICODE_PATH_OK" + (" (legacy proved red)" if a.assert_legacy else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
