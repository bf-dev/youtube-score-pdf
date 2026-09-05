#!/usr/bin/env python3
"""
Build gate: no system strip may be CUT THROUGH by its own crop boundary.

The 1.5.0 defect on the customer's LtNIc3oinEs was a crop boundary landing
inside the one real system, so every strip started in the middle of the beam row
and ended in the middle of the measure number. A system count could not see it
(21 systems either way) and neither could a strip-to-strip comparison (the
strips really were all different). What DOES see it is the strip's own edge: if
the boundary fell inside the notation, ink runs right up to the first and last
row of the strip, where a whole system always has paper.

Measured over the 1.4.0 corpus (`out/v140b/*_systems`), share of strips carrying
ink on an edge row:

    a01 a02 a03 a05 a07 a08 a10 case0 case1 case2 case3 ling   0.00
    a04 0.04   a09 0.10   a06 0.13        <- the three NOTES already calls
                                             usable-with-defects, for exactly
                                             this (a06 bakes a video strip in
                                             above a system, a09 prints a
                                             lyric-only fragment)
    LtNIc3oinEs before the fix             0.90   <- fails
    LtNIc3oinEs after  the fix             0.00

So the cut at 0.25 sits in an empty gap between the known-good baseline and the
defect. It is a REGRESSION gate, not a perfection gate: it holds the three
known-defective videos where they are and refuses anything worse.

    python3 ci/clip_check.py out/v150/caseA-fixed_systems [--max-share 0.25]

Exit 0 = pass, 1 = fail.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import cv2
import numpy as np

INK = 160          # a strip is polarity-normalised, so ink is dark
EDGE_MIN = 0.02    # share of an edge row that has to be ink before it counts


def edge_ink(path: str) -> tuple[float, float]:
    g = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if g is None or g.size == 0:
        return 0.0, 0.0
    return float((g[0] < INK).mean()), float((g[-1] < INK).mean())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("systems_dir")
    ap.add_argument("--max-share", type=float, default=0.25)
    ap.add_argument("--edge-min", type=float, default=EDGE_MIN)
    a = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(a.systems_dir, "*.png")))
    if not paths:
        print(f"CLIP_FAIL no system strips in {a.systems_dir}")
        return 1

    clipped = []
    for p in paths:
        top, bot = edge_ink(p)
        if max(top, bot) > a.edge_min:
            clipped.append((os.path.basename(p), round(top, 3), round(bot, 3)))

    share = len(clipped) / len(paths)
    print(f"clip: {len(clipped)}/{len(paths)} strips carry ink on an edge row "
          f"(share {share:.2f}, limit {a.max_share})")
    for name, top, bot in clipped[:8]:
        print(f"  {name}: top {top} bottom {bot}")
    if share > a.max_share:
        print(f"CLIP_FAIL {a.systems_dir}: a crop boundary is cutting through the notation")
        return 1
    print("CLIP_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
