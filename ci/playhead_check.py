#!/usr/bin/env python3
"""
Build gate for `playhead_resets`, the axis that is not picture similarity.

The customer's `d3t9j6DObN0` engraves the same groove for five systems running.
`group_lines` compares what a frame LOOKS like, so it merged those five screens
(t=151.8s..191.5s) into one group of 160 frames where every other content group
on that video holds exactly 32, and **four whole systems never became
candidates**: they were silently missing from his PDF, and the one system that
did print was a median of all five, with a broken ghost fill in its last bar.

Similarity cannot be the discriminator here and this is the third time that has
been measured on this project:

  * the four merged screens sit 0.19-0.28 apart binary and 0.21-0.37 graded
    against a 0.30 cut that cannot move (below it, sample case 0's cymbal splits
    one system into nine);
  * the five recovered strips correlate **0.84 to 0.96** with each other, i.e.
    inside the band where a02's genuinely different bars live;
  * their headers are 0.02-0.33 apart against a 0.45 cut, because this engraving
    prints no measure numbers at all.

The playhead does not care. It sweeps left to right across the system that is
sounding and jumps back to the left margin when the next one starts, so it
separates two identical systems exactly as well as two different ones.

This gate replays `playhead_resets` on the preserved per-frame track (t and the
playhead column, nothing else) and checks both directions at once:

  caseF  `d3t9j6DObN0`. The track must yield a reset at each of the four
         boundaries the picture rule missed. `--assert-legacy` replays the 1.6.0
         rule, which never looked at the playhead: 0 resets, four systems lost.
  caseE  `KsSlNq-ciko` draws a pale blue highlight at saturation 30-40, under
         the pipeline's 50 cut, so the playhead is invisible to it. The track
         must yield **no** resets at all: a rule that starts splitting videos it
         cannot actually see would print duplicates, which is the defect this
         whole file exists to remove.

    python3 ci/playhead_check.py
    python3 ci/playhead_check.py --assert-legacy
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ytscore.pipeline as P   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# the four boundaries `group_lines` missed inside its 160-frame group, in seconds
CASEF_MISSED = [159.75, 167.75, 175.75, 183.75]


def load(path: str) -> tuple[np.ndarray, list[P.SlotFrame]]:
    z = np.load(path)
    t, phx = z["t"], z["phx"]
    frames = [P.SlotFrame(t=float(a), gray=None, key=None, fp=None, phx=float(b))
              for a, b in zip(t, phx)]
    return t, frames


def resets_at(path: str, back: float | None = None, seen: float | None = None) -> list[float]:
    t, frames = load(path)
    keep = (P.PLAY_BACK, P.PLAY_SEEN)
    try:
        if back is not None:
            P.PLAY_BACK = back
        if seen is not None:
            P.PLAY_SEEN = seen
        idx = P.playhead_resets(frames)
    finally:
        P.PLAY_BACK, P.PLAY_SEEN = keep
    return sorted(float(t[i]) for i in idx)


def recovered(got: list[float]) -> list[float]:
    """Which of the four missed boundaries this track does NOT put a reset at."""
    return [b for b in CASEF_MISSED if not any(abs(b - g) < 0.13 for g in got)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default=os.path.join(HERE, "fixtures"))
    ap.add_argument("--assert-legacy", action="store_true")
    a = ap.parse_args()

    f_path = os.path.join(a.fixtures, "phx-d3t9j6DObN0.npz")
    e_path = os.path.join(a.fixtures, "phx-KsSlNq-ciko.npz")
    for p in (f_path, e_path):
        if not os.path.exists(p):
            print(f"PLAYHEAD_FAIL missing fixture {p}")
            return 1

    if a.assert_legacy:
        # Two ways of getting this wrong, one in each direction, both replayed on
        # the same tracks the shipped rule reads.
        #   1.6.0: no playhead axis at all -- PLAY_BACK out of reach. caseF's
        #          four systems stay lost, which is the defect the customer has.
        #   first cut: no PLAY_SEEN guard, so a video whose highlight is mostly
        #          invisible still gets split on the handful of frames where it
        #          shows. caseE picks up a spurious boundary that way, i.e. a
        #          system printed twice.
        old = resets_at(f_path, back=2.0)
        blind = recovered(old)
        print(f"playhead: legacy 1.6.0 (no playhead axis) on caseF: {len(old)} reset(s), "
              f"{len(blind)} of the 4 boundaries still missed -> the four systems are lost")
        loose = resets_at(e_path, seen=0.0)
        print(f"playhead: first cut (no PLAY_SEEN guard) on caseE: {len(loose)} reset(s) "
              f"at {[round(x, 2) for x in loose[:6]]} -> would split a system it cannot see")
        ok = len(blind) == 4 and len(loose) > 0
        print("PLAYHEAD_LEGACY_OK" if ok else "PLAYHEAD_LEGACY_FAIL")
        return 0 if ok else 1

    rc = 0
    got = resets_at(f_path)
    missed = recovered(got)
    print(f"playhead: caseF d3t9j6DObN0: {len(got)} reset(s) in the track; "
          f"of the 4 boundaries group_lines missed, {4 - len(missed)} recovered"
          + (f", MISSING {missed}" if missed else ""))
    rc |= 1 if missed else 0

    e_got = resets_at(e_path)
    print(f"playhead: caseE KsSlNq-ciko: {len(e_got)} reset(s) "
          f"(want 0, its highlight is under the saturation cut)")
    rc |= 1 if e_got else 0

    print("PLAYHEAD_OK" if rc == 0 else "PLAYHEAD_FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(main())
