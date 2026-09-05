#!/usr/bin/env python3
"""
Build gate for `FADE_RATIO`, the 1.7.1 fix.

`YkjcWb63v0o` printed its Intro system TWICE at the top of page 1 (t=7.5s and
t=8.0s, the second a partial fade-in with three of its five bars washed out to
grey). `drop_fade_copies` missed it by 0.005: ink strengths 0.784 and 0.616, so
ratio 0.785 against the 0.78 cut, picture match 0.957. Nothing else could take
it either: `drop_adjacent_repeats` missed the same pair by 0.003 (picture 0.957
against REPEAT_SAME 0.96), and the ink-share floor correctly leaves it alone
because 0.376 of the video's median really is ink.

The fix is the cut moving 0.78 -> 0.80, which is a DELETE threshold getting
wider, i.e. the class of change that produced the worst defect on this project
(d3t9j6DObN0 silently dropping four real systems because five near-identical
systems were all genuine). So this gate is deliberately two-sided and one of
its two sides is "and nothing else went away".

  caseC (YkjcWb63v0o)  the t=8.0s Intro copy MUST be gone from the survivors.
                       Under the 1.7.0 cut it is not -> --assert-legacy is the
                       proof this gate can go red.
  case0 (2RIsnf--0VY)  the survivor list must not move by one system. This is
                       the video whose measures 57-60 were deleted from the PDF
                       once already, and its opening pair sits at ratio 0.791,
                       i.e. INSIDE the band the new cut opens; it is kept only
                       by the match gate (0.474). If a future widening starts
                       taking it, this says so.
  caseE (KsSlNq-ciko)  same, on the video whose blank-panel and repeat defects
                       were fixed on other axes; the fade pass must stay out.

Both passes are replayed, not just the fade one: the fade pass feeds the repeat
pass, and the whole question on caseC is which of the two removes what. The
t=15.75s candidate the wider cut also takes is not a new deletion, 1.7.0 already
dropped it in the repeat pass, and this gate asserts the SURVIVOR LIST rather
than a drop count so that distinction cannot be faked.

    python3 ci/fade_check.py
    python3 ci/fade_check.py --assert-legacy
    python3 ci/fade_check.py --corpus out/v176     # the 21-run no-delete sweep
"""
from __future__ import annotations

import argparse
import glob
import os
import pickle
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ytscore.pipeline as P   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

LEGACY_RATIO = 0.78         # the 1.7.0 cut, which printed the Intro twice

# fixture -> the system times that must survive fade + repeat under the shipped
# rule. Written out in full on purpose: a count cannot tell "dropped the fade
# copy" from "dropped a real system and kept the fade copy", and that confusion
# is exactly how this project lost four systems on d3t9j6DObN0.
EXPECTED: dict[str, list[float]] = {
    "cands-YkjcWb63v0o.npz": [7.5, 16.5, 30.8, 46.0, 60.0, 75.5, 89.5, 103.5,
                              117.2, 131.2, 145.0, 160.8, 174.5, 188.5, 202.5,
                              216.2, 230.2, 244.2, 258.2, 277.8],
    "cands-2RIsnf--0VY.npz": [6.2, 16.2, 26.5, 36.8, 47.0, 57.2, 67.5, 77.5,
                              87.8, 98.0, 108.2, 118.5, 128.8, 138.8, 149.5,
                              159.2, 169.5, 179.8, 190.0],
    # its t=6.0s opening frame is a genuine fade copy (ink 0.27 against 0.49,
    # match 0.96) and is dropped under BOTH cuts, so the survivor list starts
    # at 6.2s: this video does not move at all when the cut widens.
    "cands-KsSlNq-ciko.npz": [6.2, 19.8, 47.2, 74.5, 88.2, 102.0, 115.8, 129.5,
                              136.2, 143.2, 157.2, 170.5, 211.8],
}
# ...and what the 1.7.0 cut left behind. caseC differs by the one copy at 8.0s;
# the other two are identical under both cuts, which is the no-collateral half.
LEGACY: dict[str, list[float]] = {
    "cands-YkjcWb63v0o.npz": [7.5, 8.0] + EXPECTED["cands-YkjcWb63v0o.npz"][1:],
    "cands-2RIsnf--0VY.npz": EXPECTED["cands-2RIsnf--0VY.npz"],
    "cands-KsSlNq-ciko.npz": EXPECTED["cands-KsSlNq-ciko.npz"],
}


def _cands_from_npz(path: str) -> list[P.Cand]:
    z = np.load(path)
    return [P.Cand(t=float(t), si=int(si), strip=None, core=core, box=box,
                   head=head.astype(bool), strength=float(s), cov=float(c))
            for t, si, core, box, head, s, c
            in zip(z["t"], z["si"], z["core"], z["box"], z["head"],
                   z["strength"], z["cov"])]


def _cands_from_pkl(path: str) -> list[P.Cand]:
    with open(path, "rb") as fh:
        d = pickle.load(fh)
    return [P.Cand(t=c["t"], si=c["si"], strip=None, core=c["core"], box=c["box"],
                   head=c["head"], strength=c["strength"], cov=c.get("cov", 1.0))
            for c in d]


def survivors(cands: list[P.Cand], ratio: float) -> list[float]:
    """Replay the two dedup passes in pipeline order and return what prints."""
    kept, _ = P.drop_fade_copies(cands, ratio=ratio)
    kept, _ = P.drop_adjacent_repeats(kept)
    return [round(c.t, 1) for c in kept]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default=os.path.join(HERE, "fixtures"))
    ap.add_argument("--assert-legacy", action="store_true",
                    help="replay the 1.7.0 cut; the gate must go RED on caseC")
    ap.add_argument("--corpus", help="a run dir of *_cands.pkl: assert the wider "
                                     "cut removes nothing outside caseC")
    a = ap.parse_args()

    ratio = LEGACY_RATIO if a.assert_legacy else P.FADE_RATIO
    want = LEGACY if a.assert_legacy else EXPECTED
    rc = 0
    for fx, times in want.items():
        path = os.path.join(a.fixtures, fx)
        if not os.path.exists(path):
            print(f"FADE_FAIL missing fixture {path}")
            return 1
        got = survivors(_cands_from_npz(path), ratio)
        ok = got == [round(t, 1) for t in times]
        rc |= 0 if ok else 1
        print(f"fade: {fx}: cut {ratio} leaves {len(got)} systems, "
              f"expected {len(times)} -> {'ok' if ok else 'MISMATCH'}")
        if not ok:
            lost = [t for t in times if t not in got]
            extra = [t for t in got if t not in times]
            print(f"      missing {lost} unexpected {extra}")

    if a.corpus:
        pkls = sorted(glob.glob(os.path.join(a.corpus, "*_cands.pkl")))
        if not pkls:
            print(f"FADE_FAIL no *_cands.pkl under {a.corpus}")
            return 1
        moved = []
        for p in pkls:
            name = os.path.basename(p)[:-len("_cands.pkl")]
            cands = _cands_from_pkl(p)
            old = survivors(cands, LEGACY_RATIO)
            new = survivors(cands, P.FADE_RATIO)
            if old != new:
                moved.append((name, old, new))
        for name, old, new in moved:
            lost = [t for t in old if t not in new]
            gained = [t for t in new if t not in old]
            print(f"fade: corpus {name}: {len(old)} -> {len(new)} systems, "
                  f"lost {lost} gained {gained}")
        bad = [m for m in moved if m[0] != "caseC-repeat"]
        okc = len(moved) == 1 and not bad and \
            [t for t in moved[0][1] if t not in moved[0][2]] == [8.0]
        print(f"fade: corpus sweep over {len(pkls)} runs: {len(moved)} video(s) "
              f"move -> {'ok' if okc else 'MISMATCH'}")
        rc |= 0 if okc else 1

    if a.assert_legacy:
        differs = LEGACY != EXPECTED
        good = rc == 0 and differs
        print("FADE_LEGACY_OK" if good else "FADE_LEGACY_FAIL")
        return 0 if good else 1
    print("FADE_OK" if rc == 0 else "FADE_FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(main())
