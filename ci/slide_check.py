#!/usr/bin/env python3
"""
Build gate for the last-page repeat (`YkjcWb63v0o`, "마지막장 같은마디 반복 깨짐").

What the defect was
-------------------
That video ends by scrolling its page up out of the score band over three
quarters of a second. Three sampled frames were caught inside that slide, each
looked like a brand new line to `group_lines` (staff at row 103, then 65, then
14, against 106 held for the previous 45 frames), and the last system of the
chart came out of the pipeline FOUR times: three copies labelled bar 77 plus one
unlabelled, all four ending "F.O".

Why this gate is not a similarity check
---------------------------------------
Because similarity is measured dead on this defect, three times over:

* the duplicate copies correlate 0.910 with the original, while a02's bars
  26/30/34/38 are genuinely different music that correlates 0.90-0.91;
* inside this very video bars 57/65/73 correlate 0.93-0.99 with each other,
  because it is a repetitive drum groove;
* a `same measure number + same notation` pairing on the rendered strips was
  built and thrown away: a03-atthe prints no measure numbers at all and repeats
  a bar of notation exactly (strips 004 and 012 correlate 0.9999 with different
  lyrics under them), so that gate fires on a clean video.

What separates the copies from real systems is not what they look like, it is
that the page was MOVING when they were sampled. This gate therefore tests the
discriminator itself, `travelling_flags` + `sliding_groups`, against preserved
frame evidence from two videos that pull in opposite directions:

  slide-YkjcWb63v0o.npz  groups 21..29, the real bar 77 (45 frames) followed by
                         the three slide samples and the outro. Groups 23,24,25
                         MUST come out sliding: those are the three extra
                         printed copies. Nothing else may.
  slide-WzDkB5al_Ik.npz  a09-kimyongtae groups 5..11 and 29..33. This video
                         flips to its next line inside a single sample period,
                         so five of its groups are one all-travelling frame each
                         -- and they are printed today. NOTHING here may come
                         out sliding, or that video loses five systems and the
                         no-regression promise breaks.

Each fixture holds only the row-ink profile of each frame (one float per pixel
row), which is all the discriminator reads.

    python3 ci/slide_check.py            # both fixtures
    python3 ci/slide_check.py --list     # show the per-group verdicts
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import namedtuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ytscore.pipeline import sliding_groups, travelling_flags   # noqa: E402

Frame = namedtuple("Frame", "t travelling")
HERE = os.path.dirname(os.path.abspath(__file__))

# fixture -> the group numbers that MUST be called sliding, and nothing else.
EXPECTED = {
    "slide-YkjcWb63v0o.npz": {23, 24, 25},
    "slide-WzDkB5al_Ik.npz": set(),
}


def verdict(path: str, rule: str = "shipped"):
    z = np.load(path)
    profs = [z["profs"][i] for i in range(z["profs"].shape[0])]
    times = list(z["times"])
    gap, dt = float(z["gap"]), float(z["dt"])
    bounds = list(z["bounds"])
    gnums = list(z["groups"])

    flags = travelling_flags(profs, times, gap, dt)
    groups, start = [], 0
    for b in bounds:
        groups.append([Frame(times[i], flags[i]) for i in range(start, b)])
        start = b
    if rule == "none":              # what 1.6.0 did: motion was never looked at
        slides = set()
    elif rule == "allmoving":       # the first cut of the rule, without the RUN
        slides = {i for i, g in enumerate(groups)
                  if g and all(f.travelling for f in g)}
    else:
        slides = sliding_groups(groups, dt)
    rows = [(int(gnums[i]), len(groups[i]), sum(1 for f in groups[i] if f.travelling),
             i in slides) for i in range(len(groups))]
    return rows, {int(gnums[i]) for i in slides}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--fixtures", default=os.path.join(HERE, "fixtures"))
    ap.add_argument("--assert-legacy", action="store_true",
                    help="prove this gate can fail: replay the two rules that get "
                         "it wrong and require BOTH of them to be caught")
    a = ap.parse_args()

    if a.assert_legacy:
        bad = 0
        for rule, fx in (("none", "slide-YkjcWb63v0o.npz"),
                         ("allmoving", "slide-WzDkB5al_Ik.npz")):
            _, got = verdict(os.path.join(a.fixtures, fx), rule)
            caught = got != EXPECTED[fx]
            bad += 0 if caught else 1
            print(f"slide: legacy rule {rule!r} on {fx}: {sorted(got)} vs expected "
                  f"{sorted(EXPECTED[fx])} -> {'CAUGHT' if caught else 'NOT CAUGHT'}")
        print("SLIDE_LEGACY_OK" if bad == 0 else "SLIDE_LEGACY_FAIL")
        return 1 if bad else 0

    rc = 0
    for fx, want in EXPECTED.items():
        path = os.path.join(a.fixtures, fx)
        if not os.path.exists(path):
            print(f"SLIDE_FAIL missing fixture {path}")
            return 1
        rows, got = verdict(path)
        if a.list:
            for gi, n, mv, sl in rows:
                print(f"    {fx} #{gi:<3d} n={n:<4d} travelling={mv:<4d} "
                      f"{'SLIDING' if sl else 'settled'}")
        ok = got == want
        rc |= 0 if ok else 1
        print(f"slide: {fx}: sliding groups {sorted(got)}, expected {sorted(want)} "
              f"-> {'ok' if ok else 'MISMATCH'}")

    print("SLIDE_OK" if rc == 0 else "SLIDE_FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(main())
