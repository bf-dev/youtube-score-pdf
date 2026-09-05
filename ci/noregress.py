#!/usr/bin/env python3
"""
No-regression gate: every baseline video must come out of the new pipeline
exactly as it came out of the shipped one.

We promised this customer by name that fixing his four videos would not touch
the ones that already worked, and he thanked us for it. The promise is only
worth something if it is checked mechanically, so this compares a fresh batch
against the shipped batch three ways, cheapest and strictest first:

1. system + page COUNTS from run.json. Necessary, never sufficient: six defects
   on this order passed a count check and four of those were found by the
   customer opening his own PDF.
2. the composited system STRIPS, byte for byte. This is the pipeline's real
   output; the PDF is only a layout of it. A strip that is bit-identical cannot
   render differently.
3. the rendered PAGES, pixel for pixel, at --dpi. This is what the customer
   actually looks at, and it catches pagination or header changes that leave
   every strip untouched. The PDF bytes themselves are NOT comparable: PyMuPDF
   stamps a creation date, so two runs of identical input differ in md5.

    python3 ci/noregress.py out/v150batch out/v160b [--dpi 110]

Exit 0 = every case identical, 1 = something moved (and it is named).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np

try:
    import pymupdf
except ImportError:                                     # older wheel name
    import fitz as pymupdf

import cv2


def counts(run_json: str) -> tuple[int, int]:
    j = json.load(open(run_json))
    return int(j["systems"]), int(j["pages"])


def strips_differ(a_dir: str, b_dir: str) -> list[str]:
    a = sorted(glob.glob(os.path.join(a_dir, "*.png")))
    b = sorted(glob.glob(os.path.join(b_dir, "*.png")))
    if len(a) != len(b):
        return [f"strip count {len(a)} -> {len(b)}"]
    out = []
    for pa, pb in zip(a, b):
        ia = cv2.imread(pa, cv2.IMREAD_GRAYSCALE)
        ib = cv2.imread(pb, cv2.IMREAD_GRAYSCALE)
        if ia is None or ib is None:
            out.append(f"{os.path.basename(pa)} unreadable")
        elif ia.shape != ib.shape:
            out.append(f"{os.path.basename(pa)} {ia.shape} -> {ib.shape}")
        elif not np.array_equal(ia, ib):
            d = np.abs(ia.astype(int) - ib.astype(int))
            out.append(f"{os.path.basename(pa)} {int((d > 40).sum())}px differ "
                       f"(max {int(d.max())})")
    return out


def pages_differ(a_pdf: str, b_pdf: str, dpi: int) -> list[str]:
    da, db = pymupdf.open(a_pdf), pymupdf.open(b_pdf)
    if da.page_count != db.page_count:
        return [f"page count {da.page_count} -> {db.page_count}"]
    out = []
    for i in range(da.page_count):
        pa = da[i].get_pixmap(dpi=dpi)
        pb = db[i].get_pixmap(dpi=dpi)
        ia = np.frombuffer(pa.samples, np.uint8).reshape(pa.height, pa.width, pa.n)
        ib = np.frombuffer(pb.samples, np.uint8).reshape(pb.height, pb.width, pb.n)
        if ia.shape != ib.shape:
            out.append(f"page {i + 1} {ia.shape} -> {ib.shape}")
            continue
        d = np.abs(ia.astype(int) - ib.astype(int))
        n = int((d > 40).sum())
        if n:
            out.append(f"page {i + 1}: {n}px differ (max {int(d.max())})")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline")
    ap.add_argument("candidate")
    ap.add_argument("--dpi", type=int, default=110)
    a = ap.parse_args()

    names = sorted(os.path.basename(p)[:-len(".run.json")]
                   for p in glob.glob(os.path.join(a.baseline, "*.run.json")))
    if not names:
        print(f"NOREGRESS_FAIL no run.json in {a.baseline}")
        return 1

    rc = 0
    print(f"{'case':24s} {'baseline':>10s} {'candidate':>10s}  strips  pages")
    for n in names:
        b_json = os.path.join(a.baseline, f"{n}.run.json")
        c_json = os.path.join(a.candidate, f"{n}.run.json")
        if not os.path.exists(c_json):
            print(f"{n:24s} {'-':>10s} {'MISSING':>10s}")
            rc = 1
            continue
        bs, bp = counts(b_json)
        cs, cp = counts(c_json)

        sdiff = strips_differ(os.path.join(a.baseline, f"{n}_systems"),
                              os.path.join(a.candidate, f"{n}_systems"))
        pdiff = pages_differ(os.path.join(a.baseline, f"{n}.pdf"),
                             os.path.join(a.candidate, f"{n}.pdf"), a.dpi)

        ok = (bs, bp) == (cs, cp) and not sdiff and not pdiff
        print(f"{n:24s} {f'{bs}/{bp}':>10s} {f'{cs}/{cp}':>10s}  "
              f"{'same' if not sdiff else str(len(sdiff)) + ' DIFFER':>6s}  "
              f"{'same' if not pdiff else str(len(pdiff)) + ' DIFFER':>6s}")
        if not ok:
            rc = 1
            for line in (sdiff + pdiff)[:6]:
                print(f"    {line}")

    print("NOREGRESS_OK" if rc == 0 else "NOREGRESS_FAIL")
    return rc


if __name__ == "__main__":
    sys.exit(main())
