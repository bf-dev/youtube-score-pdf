#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression gate: filled noteheads must print as filled discs.

The customer found this defect twice on his own PDFs and our verification
could not see it either time, because a system count, a page count and a
strip-to-strip comparison are all blind to ink disappearing from INSIDE a
glyph. So it gets its own gate.

    python3 ci/notehead_check.py out/v140/ling140.pdf

Renders every page at 300dpi and measures the fill ratio of every notehead in
the snare space (2nd from the top) and the bass space (bottom) with
src/diag_notehead.py. Fails when the median drops below --min-median, or when
more than --max-damaged of the heads fall into the damaged cluster: on 1.3.0
the two populations were cleanly separated, damaged at 0.15-0.43 against
healthy at 0.85-0.96, so anything under 0.60 is damage and not measurement
noise.

Two traps live in the measurement itself, both of which produced a false
"clean" before they were caught. They are documented in src/diag_notehead.py;
do not reimplement this against a fresh measurement.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

# by path, not as a package: src/ is a folder of standalone diagnostics and the
# shipped app never imports from it.
_spec = importlib.util.spec_from_file_location(
    "diag_notehead", Path(__file__).resolve().parents[1] / "src" / "diag_notehead.py")
_dn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dn)
measure = _dn.measure


def render(pdf: Path, dpi: int, into: Path) -> list[Path]:
    subprocess.run(["pdftoppm", "-r", str(dpi), "-png", str(pdf), str(into / "p")],
                   check=True)
    return sorted(into.glob("p-*.png"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--min-median", type=float, default=0.77)
    ap.add_argument("--max-damaged", type=float, default=0.10,
                    help="allowed share of heads below --damaged-below")
    ap.add_argument("--damaged-below", type=float, default=0.60)
    ap.add_argument("--min-heads", type=int, default=40,
                    help="fewer than this and the measurement itself is suspect")
    ap.add_argument("--spaces", default="snare,bass")
    a = ap.parse_args()

    pdf = Path(a.pdf)
    if not pdf.is_file():
        print(f"NOTEHEAD FAIL: no such pdf {pdf}")
        return 2
    with tempfile.TemporaryDirectory() as td:
        pages = render(pdf, a.dpi, Path(td))
        rows: list[tuple[str, int, float]] = []
        for p in pages:
            rows.extend(measure(str(p)))

    bad = []
    for space in a.spaces.split(","):
        vals = np.array([f for name, _, f in rows if name == space])
        if vals.size == 0:
            print(f"NOTEHEAD FAIL: {space}: no noteheads found at all")
            bad.append(space)
            continue
        med = float(np.median(vals))
        dmg = float((vals < a.damaged_below).mean())
        hist, edges = np.histogram(vals, bins=10, range=(0.0, 1.0))
        print(f"{space}: n={vals.size} median={med:.3f} min={vals.min():.3f} "
              f"below{a.damaged_below:g}={int((vals < a.damaged_below).sum())} "
              f"({100 * dmg:.1f}%) below0.77={int((vals < 0.77).sum())}")
        print("   hist " + " ".join(f"{edges[i]:.1f}:{hist[i]}" for i in range(10)))
        if vals.size < a.min_heads:
            print(f"NOTEHEAD FAIL: {space}: only {vals.size} heads measured, "
                  f"expected at least {a.min_heads}: the measurement is not "
                  f"finding the notation, do not read this as a pass")
            bad.append(space)
        if med < a.min_median:
            print(f"NOTEHEAD FAIL: {space}: median fill {med:.3f} < {a.min_median}")
            bad.append(space)
        if dmg > a.max_damaged:
            print(f"NOTEHEAD FAIL: {space}: {100 * dmg:.1f}% of heads are hollow "
                  f"(below {a.damaged_below:g}), limit {100 * a.max_damaged:.0f}%")
            bad.append(space)

    if bad:
        print(f"NOTEHEAD FAIL: {pdf.name}")
        return 1
    print(f"NOTEHEAD_OK {pdf.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
