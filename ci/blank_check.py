#!/usr/bin/env python3
"""
Build gate: no printed system may be an EMPTY BOX.

Two of the customer's open defects are the same defect, and it is this one:

* `KsSlNq-ciko`, his words "맨윗줄 공백" (blank top line). System 000 of that PDF
  is a white box carrying 0.4% ink, all of it the border rule of the uploader's
  intro panel. It got in because that border rule is a full-width dark row, so
  `staff_row_coverage` called it a staff line and read 1.000, and the
  mid-animation gate only cuts below 0.55 of the median.
* `YkjcWb63v0o`, the second half of "마지막장 같은마디 반복 깨짐". Its outro system
  prints twice, and the faint copy is broken debris: 0.27% ink against the good
  copy's 3.6%, staff snapped at the top, with a stray horizontal rule under it.
  Every similarity pass is blind to the pair because the page moved 73 rows
  between the two frames, so the two views do not line up to be compared.

`a09-kimyongtae` had the same thing twice (strips 000 and 041, both invisible
white boxes) and nobody had noticed, because a blank box changes no count and
looks like white paper on the page.

The metric is deliberately not a similarity: it is how much ink the strip has,
as a share of what THIS video's own systems carry. Measured over the whole
corpus of prepared candidates (`out/v17x/*_cands`, 630 candidates, 19 videos):

    junk   caseC t=277.5 0.029 | a09 t=241.8 0.047 | a09 t=4.8 0.059
    real   ling t=232.0 0.161 | a10 t=252.2 0.170 | a06 t=69.2 0.171
           case2 t=4.0 0.203 | case0 t=5.8 0.215 | everything else >= 0.22

so the pipeline's `INK_SHARE_FLOOR` sits at 0.10, in the middle of an empty
band. On the FINAL strips the band is wider still (junk 0.029-0.059, the lowest
real system anywhere is case0's 0.335), which is what this gate measures, so it
runs at 0.15.

    python3 ci/blank_check.py out/v175/caseE-repeat2_systems [more dirs...]
    python3 ci/blank_check.py --assert-legacy out/v172/caseC-repeat_systems

Exit 0 = every printed strip carries real ink. 1 = an empty box was printed.
`--assert-legacy` inverts it: the named dirs are pre-fix output and MUST fail,
which is what proves the gate can fail at all.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import cv2
import numpy as np

FLOOR = 0.15            # share of the video's own median strip ink


def ink_shares(systems_dir: str) -> list[tuple[str, float]]:
    out = []
    for p in sorted(glob.glob(os.path.join(systems_dir, "*.png"))):
        im = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        if im is None:
            continue
        out.append((os.path.basename(p), float((im < 128).mean())))
    return out


def check(systems_dir: str, floor: float) -> tuple[int, list[str]]:
    shares = ink_shares(systems_dir)
    if not shares:
        print(f"blank: no system strips in {systems_dir}")
        return 1, []
    med = float(np.median([s for _, s in shares]))
    if med <= 0:
        print(f"blank: {systems_dir} has no ink at all")
        return 1, [n for n, _ in shares]
    ratios = [(n, s / med) for n, s in shares]
    bad = [(n, r) for n, r in ratios if r < floor]
    lowest = sorted(ratios, key=lambda kv: kv[1])[:3]
    print(f"blank: {os.path.basename(systems_dir)}: {len(bad)}/{len(shares)} "
          f"empty (median ink {med:.4f}, floor {floor}) | lowest " +
          ", ".join(f"{n[:-4]}:{r:.3f}" for n, r in lowest))
    for n, r in bad:
        print(f"  {n}: {r:.3f} of this video's median ink -> an empty box")
    return (1 if bad else 0), [n for n, _ in bad]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("systems_dir", nargs="+")
    ap.add_argument("--floor", type=float, default=FLOOR)
    ap.add_argument("--assert-legacy", action="store_true",
                    help="the dirs are PRE-fix output and must be flagged")
    a = ap.parse_args()

    rc = 0
    for d in a.systems_dir:
        r, _ = check(d, a.floor)
        rc |= r
    if a.assert_legacy:
        if rc:
            print("BLANK_LEGACY_OK the gate fails on the pre-fix strips")
            return 0
        print("BLANK_LEGACY_FAIL the pre-fix strips passed; the gate is inert")
        return 1
    if rc:
        print("BLANK_FAIL an empty box was printed as a system")
        return 1
    print("BLANK_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
