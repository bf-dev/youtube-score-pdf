#!/usr/bin/env python3
"""
Rebuild the gate fixtures from a batch directory.

`ci/repeat_check.py` and `ci/playhead_check.py` both replay a pipeline pass on
preserved evidence rather than on a video, so a gate run costs milliseconds and
does not need the 30MB source. This is what turns a batch's by-products into
those fixtures. Run it after a full `ci/run_batch.sh` on a tree you trust.

    python3 ci/make_fixtures.py out/v172                       # candidate fixtures
    python3 ci/make_fixtures.py out/v172fx --phx caseF=d3t9j6DObN0 caseE=KsSlNq-ciko

The candidate dump (`<name>_cands.pkl`, written under `YTSCORE_DIAG`) is taken
BEFORE the fade and repeat passes, which is the input those passes actually see.
The playhead track comes out of the `<name>_dist_slot0.tsv` written under
`YTSCORE_DUMP_DIST`.
"""
from __future__ import annotations

import argparse
import csv
import os
import pickle
import sys

import numpy as np

# batch name -> the YouTube id the fixture is named for
CANDS = {
    "caseE-repeat2": "KsSlNq-ciko",
    "case0-original": "2RIsnf--0VY",
    "caseC-repeat": "YkjcWb63v0o",
}


def write_cands(src: str, name: str, vid: str, outdir: str) -> str:
    with open(os.path.join(src, f"{name}_cands.pkl"), "rb") as fh:
        d = pickle.load(fh)
    path = os.path.join(outdir, f"cands-{vid}.npz")
    np.savez_compressed(
        path,
        t=np.array([c["t"] for c in d], np.float32),
        si=np.array([c["si"] for c in d], np.int16),
        core=np.stack([c["core"] for c in d]),
        box=np.stack([c["box"] for c in d]),
        head=np.stack([c["head"] for c in d]),
        strength=np.array([c["strength"] for c in d], np.float32),
        cov=np.array([c.get("cov", 1.0) for c in d], np.float32),
    )
    return path


def write_phx(src: str, name: str, vid: str, outdir: str) -> str:
    rows = list(csv.DictReader(open(os.path.join(src, f"{name}_dist_slot0.tsv")),
                               delimiter="\t"))
    path = os.path.join(outdir, f"phx-{vid}.npz")
    np.savez_compressed(path,
                        t=np.array([float(r["t"]) for r in rows], np.float32),
                        phx=np.array([float(r["playhead"]) for r in rows], np.float32))
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "fixtures"))
    ap.add_argument("--phx", nargs="*", default=[],
                    help="name=videoid pairs to take the playhead track from")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    for name, vid in CANDS.items():
        if not os.path.exists(os.path.join(a.src, f"{name}_cands.pkl")):
            print(f"skip {name}: no candidate dump in {a.src}")
            continue
        p = write_cands(a.src, name, vid, a.out)
        print(f"{p}  {os.path.getsize(p)} bytes")
    for pair in a.phx:
        name, vid = pair.split("=", 1)
        p = write_phx(a.src, name, vid, a.out)
        print(f"{p}  {os.path.getsize(p)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
