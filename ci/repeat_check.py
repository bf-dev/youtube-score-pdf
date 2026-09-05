#!/usr/bin/env python3
"""
Build gate for `REPEAT_CERTAIN`, the second half of the last-page repeat fix.

The customer reported "마지막장 같은마디 반복" on two videos and they fail
differently:

  YkjcWb63v0o  the page SLIDES out of the band at the end and three frames caught
               in the slide print the last bar three extra times. Fixed by
               `sliding_groups`; its gate is `ci/slide_check.py`.
  KsSlNq-ciko  no slide at all. The last frame is a single-frame group whose
               picture matches the 58-frame group before it at 0.999, and it was
               printed anyway because `drop_adjacent_repeats` also demands the
               two headers agree, and the header came back 1.000 apart. That is
               not two different measure numbers: it is one number the fixed ink
               threshold could not read on a frame that is already fading, so an
               empty band against a band with "61" in it scores 1.0 by
               construction. Fixed by letting a picture match of >= 0.985 waive
               the header veto.

This gate replays `drop_adjacent_repeats` on preserved candidate fixtures (the
box fingerprint and header signature of every candidate, which is all that pass
reads) and checks it in both directions at once:

  caseE  the t=226.2s copy MUST be dropped. Under the 1.6.0 rule, where the
         header always has a veto, it is not -> --assert-legacy catches it.
  case0  its measures 53-56 and 57-60 are four bars of rest under one lyric,
         the pair that was deleted from the PDF once already. NOTHING may be
         dropped there beyond what 1.6.0 dropped, under either rule.
  caseC  the three sliding copies correlate only 0.910, well under both cuts, so
         this pass must leave them alone. They are `sliding_groups`' business,
         and if a future widening of REPEAT_CERTAIN starts taking them this gate
         says so.

    python3 ci/repeat_check.py
    python3 ci/repeat_check.py --assert-legacy
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ytscore.pipeline as P   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# fixture -> how many adjacent repeats the shipped rule must drop
#
# caseC went 2 -> 1 in 1.7.1, and NOT because this pass got weaker. The fade cut
# moved 0.78 -> 0.80, so t=15.75s (picture 0.971 against the Intro) is now taken
# by `drop_fade_copies` one pass earlier and never reaches this one. The end
# state is what matters and it is unchanged apart from the defect the release
# fixes: 21 survivors before, 20 after, the difference being the second copy of
# the Intro at t=8.0s. `ci/fade_check.py` asserts that survivor list in full,
# precisely so this count can never quietly stand in for it.
EXPECTED = {
    "cands-KsSlNq-ciko.npz": 1,
    "cands-2RIsnf--0VY.npz": 0,
    "cands-YkjcWb63v0o.npz": 1,
}
# ...and how many the 1.6.0 rule (header always vetoes) dropped
LEGACY = {
    "cands-KsSlNq-ciko.npz": 0,
    "cands-2RIsnf--0VY.npz": 0,
    "cands-YkjcWb63v0o.npz": 1,
}


def replay(path: str, legacy: bool) -> int:
    z = np.load(path)
    cands = [P.Cand(t=float(t), si=int(si), strip=None, core=core, box=box,
                    head=head.astype(bool), strength=float(s), cov=float(c))
             for t, si, core, box, head, s, c
             in zip(z["t"], z["si"], z["core"], z["box"], z["head"],
                    z["strength"], z["cov"])]
    keep = P.REPEAT_CERTAIN
    try:
        if legacy:
            P.REPEAT_CERTAIN = 2.0      # unreachable: the header keeps its veto
        # The fade pass runs first in the pipeline and its result is what the
        # repeat pass actually sees, so replay both or the pairing is not the
        # one that shipped.
        cands, _ = P.drop_fade_copies(cands)
        _, dropped = P.drop_adjacent_repeats(cands)
    finally:
        P.REPEAT_CERTAIN = keep
    return len(dropped)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default=os.path.join(HERE, "fixtures"))
    ap.add_argument("--assert-legacy", action="store_true")
    a = ap.parse_args()

    want = LEGACY if a.assert_legacy else EXPECTED
    rc = 0
    for fx, n in want.items():
        path = os.path.join(a.fixtures, fx)
        if not os.path.exists(path):
            print(f"REPEAT_FAIL missing fixture {path}")
            return 1
        got = replay(path, a.assert_legacy)
        ok = got == n
        rc |= 0 if ok else 1
        print(f"repeat: {fx}: {'legacy' if a.assert_legacy else 'shipped'} rule drops "
              f"{got}, expected {n} -> {'ok' if ok else 'MISMATCH'}")

    if a.assert_legacy:
        # the whole point: the legacy rule must differ from the shipped one
        differs = LEGACY != EXPECTED
        print("REPEAT_LEGACY_OK" if rc == 0 and differs else "REPEAT_LEGACY_FAIL")
        return 0 if (rc == 0 and differs) else 1
    print("REPEAT_OK" if rc == 0 else "REPEAT_FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(main())
