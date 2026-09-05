#!/usr/bin/env python3
"""
Build gate: no composited system may be a DOUBLE EXPOSURE.

The 1.6.0 defect on the customer's EVprtoI_3eY was `staff_anchor` locking onto
the wrong line of the staff, so a third of a group's frames were composited 51px
(3 x gap) away from the rest and the median of the two populations printed as a
ghosted blur. His words: "2페이지 위아래 흐림?". Nothing count-based can see it:
40 systems and 4 pages before the fix, 40 and 4 after, and the two bad strips
are genuinely different from every other strip, so a strip-to-strip comparison
is blind too. The customer found it by opening his own PDF, which is now the
sixth time a defect on this order got past a count.

What DOES see it is the strip's own staff. A staff is N evenly spaced full-width
lines. Compositing two copies of it 3 staff spaces apart makes the copies agree
on the two lines where they overlap and disagree on the other three, so those
three come out grey and broken instead of black: the ladder loses rungs. On the
customer's page 2, top strip, measured off the row ink-coverage profile:

    1.5.0 (broken)  rows  84:0.96  101:0.96  119:0.27  138:0.27  (5th under 0.22)
    1.6.0 (fixed)   rows  89:0.96  106:0.96  123:0.96  140:0.96  157:0.96

So the metric is: find the strongest full-width row, walk a ladder out from it in
both directions at the best-fitting step, and count how many rungs are still at
least `--rung` of the strongest one. Compare that to the MODAL count over the
video's own strips, so nothing has to know how many lines this engraving draws.
A strip that is two or more rungs short of its own video's mode is a ghost.

Calibrated over the whole 17-video corpus (`out/v160b/*_systems`), share of
strips flagged:

    a01 a02 a03 a04 a05 a06 a07 a08 a10 case0 case1 case2 case3
    caseA caseB ling                        0.00
    a09                                     0.24   <- pre-existing, see below
    EVprtoI_3eY before the fix              0.05   <- fails
    EVprtoI_3eY after  the fix              0.00

a09-kimyongtae is the one video that scores non-zero while being unchanged: its
engraving draws the middle staff line at about 0.38 coverage against 0.99 for
its neighbours, in 10 of its 42 strips, and it measures the SAME 0.24 on the
shipped 1.5.0 output (`out/v150batch`) as at HEAD. It is a property of that
video, not a regression, and it is why this gate is pointed at one case at a
time exactly like ci/clip_check.py, rather than swept over the corpus.

    python3 ci/ghost_check.py out/v160c/D-reg2_systems [--max-share 0.02]

Exit 0 = pass, 1 = fail.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import cv2
import numpy as np

INK = 200          # a strip is polarity-normalised, so ink is dark
STRONG = 0.85      # a row this close to the best is a staff line for sure
RUNG = 0.35        # a ladder rung has to keep at least this much of the best
                   # row. The ghost rungs measure 0.27 of theirs and a09's
                   # genuinely faint middle line 0.38, so the cut sits between.
MIN_COV = 0.55     # below this there is no full-width line at all: not a staff


def ladder(path: str) -> int:
    """How many staff lines this strip still has, 0 if it has no staff."""
    g = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if g is None or g.size == 0:
        return 0
    cov = (g < INK).mean(axis=1).astype(float)
    best = float(cov.max())
    if best < MIN_COV:
        return 0

    centres: list[float] = []
    start = None
    for r, on in enumerate(cov >= STRONG * best):
        if on and start is None:
            start = r
        elif not on and start is not None:
            centres.append((start + r - 1) / 2.0)
            start = None
    if start is not None:
        centres.append((start + len(cov) - 1) / 2.0)
    if not centres:
        return 0

    anchor = max(centres, key=lambda m: cov[int(round(m))])
    rung = RUNG * best

    def lit(pos: float) -> bool:
        lo, hi = int(round(pos)) - 2, int(round(pos)) + 3
        return lo >= 0 and hi <= len(cov) and bool((cov[lo:hi] >= rung).any())

    # Try every plausible step and keep the ladder that explains the most rungs:
    # a missing rung must not be allowed to inflate the step and hide itself.
    found = 1
    for step in range(5, max(6, len(cov) // 3)):
        n = 1
        for direction in (1, -1):
            pos = anchor
            while True:
                pos += direction * step
                if not lit(pos):
                    break
                n += 1
        found = max(found, n)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("systems_dir")
    ap.add_argument("--max-share", type=float, default=0.02)
    ap.add_argument("--short-by", type=int, default=2,
                    help="rungs missing before a strip counts as ghosted")
    a = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(a.systems_dir, "*.png")))
    if not paths:
        print(f"GHOST_FAIL no system strips in {a.systems_dir}")
        return 1

    counts = [(os.path.basename(p), ladder(p)) for p in paths]
    hist: dict[int, int] = {}
    for _, n in counts:
        hist[n] = hist.get(n, 0) + 1
    mode = max(hist.items(), key=lambda kv: (kv[1], kv[0]))[0]

    # 0 rungs means no staff at all (a fade frame, a lyric-only fragment). That
    # is a different, already-known defect class and not what this gate is for.
    ghosted = [(n, c) for n, c in counts if 0 < c <= mode - a.short_by]
    share = len(ghosted) / len(counts)
    print(f"ghost: {len(ghosted)}/{len(counts)} strips are short of the "
          f"{mode}-line staff by {a.short_by}+ (share {share:.2f}, "
          f"limit {a.max_share}) | rungs {sorted(hist.items())}")
    for name, c in ghosted[:8]:
        print(f"  {name}: {c} of {mode} staff lines survive")
    if share > a.max_share:
        print(f"GHOST_FAIL {a.systems_dir}: a system was composited as a "
              f"double exposure")
        return 1
    print("GHOST_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
