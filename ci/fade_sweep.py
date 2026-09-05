#!/usr/bin/env python3
"""
The corpus sweep that has to happen BEFORE the fade cut is allowed to move.

`drop_fade_copies` deletes a system when its neighbour is the same notation
(core/box match >= CORE_SAME) drawn in ink that never reached full black
(min strength / max strength < FADE_RATIO). YkjcWb63v0o's Intro pair sits at
ratio 0.785 against the 0.78 cut and prints twice, so the one-line fix is to
raise the cut to 0.80.

Raising a DELETE threshold is the single most dangerous class of change on this
project: d3t9j6DObN0 had four whole systems silently missing because five
near-identical systems were all genuine. So the cut may only move if the band
it opens up, ratio in [0.78, 0.80) with match >= CORE_SAME, is empty everywhere
in the corpus except for the one pair we are trying to take.

Why this cannot be read out of the run logs: the `fade?:` DIAG line in
`drop_fade_copies` is printed AFTER the ratio gate has already `continue`d, so
a pair in [0.78, 0.80) is never logged by construction. This reads the
`<name>_cands.pkl` that the pipeline dumps under YTSCORE_DIAG, which is the
exact list `drop_fade_copies` is called with (t, si, core, box, strength), and
re-evaluates every adjacent pair with the pipeline's own `core_match`.

    python3 ci/fade_sweep.py out/v176            # the band table + the verdict
    python3 ci/fade_sweep.py out/v176 --all      # every adjacent pair, not just the band

Exit 0 when the band holds nothing but caseC's Intro pair (the cut can move),
exit 1 when anything else is in there (it cannot, and the pair must be
separated on a narrower axis instead).
"""
import argparse
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ytscore.pipeline import CORE_SAME, core_match   # noqa: E402

BAND_LO = 0.78          # the cut in 1.7.0
BAND_HI = 0.80          # the cut 1.7.1 proposes
# The pair we are trying to take: YkjcWb63v0o's Intro, printed twice at the top
# of page 1 (strips 000 and 001).
TARGET = ("caseC-repeat", 7.5, 8.0)


def pairs(cands: list[dict]):
    """Every adjacent pair, scored exactly the way drop_fade_copies scores it."""
    for i in range(len(cands) - 1):
        a, b = cands[i], cands[i + 1]
        hi = max(a["strength"], b["strength"])
        lo = min(a["strength"], b["strength"])
        if hi <= 0:
            continue
        ratio = lo / hi
        # Within one slot the two pictures are the same crop of the same box;
        # only across slots is the staff-core crop needed to align them.
        m = (core_match(a["box"], b["box"]) if a["si"] == b["si"]
             else core_match(a["core"], b["core"]))
        yield i, a, b, ratio, m


def simulate(cands: list[dict], ratio_cut: float) -> list[tuple[float, int]]:
    """Replay drop_fade_copies' loop and return what it deletes, as (t, si)."""
    out = list(cands)
    dropped: list[tuple[float, int]] = []
    changed = True
    while changed and len(out) > 1:
        changed = False
        for i in range(len(out) - 1):
            a, b = out[i], out[i + 1]
            hi = max(a["strength"], b["strength"])
            lo = min(a["strength"], b["strength"])
            if hi <= 0 or lo / hi >= ratio_cut:
                continue
            m = (core_match(a["box"], b["box"]) if a["si"] == b["si"]
                 else core_match(a["core"], b["core"]))
            if m < CORE_SAME:
                continue
            weak = i if a["strength"] < b["strength"] else i + 1
            dropped.append((out[weak]["t"], out[weak]["si"]))
            out.pop(weak)
            changed = True
            break
    return dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", help="a corpus run dir holding <name>_cands.pkl")
    ap.add_argument("--all", action="store_true", help="print every adjacent pair")
    ap.add_argument("--lo", type=float, default=BAND_LO)
    ap.add_argument("--hi", type=float, default=BAND_HI)
    args = ap.parse_args()

    run = Path(args.run)
    pkls = sorted(run.glob("*_cands.pkl"))
    if not pkls:
        print(f"FAIL: no *_cands.pkl in {run} (was the run made with YTSCORE_DIAG=1?)")
        return 1

    band: list[tuple] = []
    near: list[tuple] = []
    total_pairs = 0
    print(f"fade sweep over {len(pkls)} videos in {run}, "
          f"band [{args.lo}, {args.hi}) with match >= {CORE_SAME}")
    print()
    for pkl in pkls:
        name = pkl.name[:-len("_cands.pkl")]
        cands = pickle.load(open(pkl, "rb"))
        vid_band = 0
        for i, a, b, ratio, m in pairs(cands):
            total_pairs += 1
            hit = args.lo <= ratio < args.hi and m >= CORE_SAME
            if hit:
                band.append((name, i, a["t"], b["t"], ratio, m))
                vid_band += 1
            elif args.lo <= ratio < args.hi:
                near.append((name, i, a["t"], b["t"], ratio, m))
            if args.all:
                flag = "  <== BAND" if hit else ""
                print(f"  {name:22s} {i:3d}/{i+1:<3d} t={a['t']:7.1f}s/{b['t']:7.1f}s "
                      f"si={a['si']}/{b['si']} ratio={ratio:.3f} match={m:.3f}{flag}")
        print(f"  {name:22s} {len(cands):3d} candidates, "
              f"{len(cands) - 1:3d} adjacent pairs, {vid_band} in band")

    print()
    print(f"{total_pairs} adjacent pairs swept over {len(pkls)} videos.")
    print(f"IN BAND [{args.lo}, {args.hi}) with match >= {CORE_SAME}: {len(band)}")
    for name, i, ta, tb, ratio, m in band:
        print(f"    {name:22s} pair {i}/{i+1}  t={ta:.1f}s/{tb:.1f}s  "
              f"ratio={ratio:.4f}  match={m:.4f}")
    print(f"in the ratio band but match < {CORE_SAME} (the match gate already "
          f"protects these): {len(near)}")
    for name, i, ta, tb, ratio, m in near:
        print(f"    {name:22s} pair {i}/{i+1}  t={ta:.1f}s/{tb:.1f}s  "
              f"ratio={ratio:.4f}  match={m:.4f}")

    print()
    print(f"what actually changes if the cut moves {args.lo} -> {args.hi}:")
    deltas = []
    for pkl in pkls:
        name = pkl.name[:-len("_cands.pkl")]
        cands = pickle.load(open(pkl, "rb"))
        old = simulate(cands, args.lo)
        new = simulate(cands, args.hi)
        extra = [d for d in new if d not in old]
        gone = [d for d in old if d not in new]
        if extra or gone:
            deltas.append((name, old, new, extra, gone))
            print(f"    {name:22s} drops {len(old)} -> {len(new)}: "
                  f"newly deleted {[f'{t:.1f}s(si{si})' for t, si in extra]}"
                  + (f", no longer deleted {[f'{t:.1f}s' for t, _ in gone]}" if gone else ""))
    if not deltas:
        print("    nothing anywhere in the corpus")

    expected = [b for b in band if b[0] == TARGET[0]
                and abs(b[2] - TARGET[1]) < 0.3 and abs(b[3] - TARGET[2]) < 0.3]
    others = [b for b in band if b not in expected]
    print()
    if not expected:
        print("SWEEP_FAIL: the caseC Intro pair is NOT in the band; the measurement "
              "this fix rests on did not reproduce.")
        return 1
    if others:
        print(f"SWEEP_FAIL: {len(others)} other pair(s) are in the band. The cut must "
              "NOT move; separate the Intro pair on a narrower axis instead.")
        return 1
    print("SWEEP_OK: the band holds the caseC Intro pair and nothing else. "
          f"Moving the fade cut to {args.hi} is safe by measurement.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
