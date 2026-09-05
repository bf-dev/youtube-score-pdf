#!/usr/bin/env python3
"""
Build gate: a COLOURED notehead must survive `normalise_ink`.

The 1.5.0 defect on the customer's umZcjiNpEOw: its crash cymbals are red X
noteheads, and the chrome gate in `playhead_mask` measured brightness as HSV V,
which is max(B,G,R). A red head reads V=196 and scored as a translucent
playhead, while its actual luma is 96, i.e. plainly dark ink on white paper.
77-84% of every red head was whitened to paper, so the PDF printed a couple of
dash fragments where the X belongs.

The gate takes real frames from the video, finds the coloured notation itself
(saturated, notehead-sized, dark in luma), and asserts that the shipped
`normalise_ink` still leaves that ink on the page. It prints the LEGACY score
alongside, computed here from the pre-fix rule, so the number the fix moved is
visible in the build log and the gate is demonstrably able to fail.

    python3 ci/colour_check.py work/caseB/video.mp4 --band 141 1041 --gap 16

Exit 0 = pass, 1 = fail.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ytscore.pipeline import normalise_ink, playhead_mask, probe_video

SAT = 110          # the sat_thresh the pipeline runs with
KEEP_MIN = 0.70    # a coloured head must keep this share of its ink
MIN_HEADS = 4      # ... measured over at least this many heads, or the test is blind


def frame_at(video: Path, t: float, w: int, h: int) -> np.ndarray | None:
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        capture_output=True).stdout
    if len(raw) < w * h * 3:
        return None
    return np.frombuffer(raw, np.uint8)[:w * h * 3].reshape(h, w, 3).copy()


def coloured_glyphs(bgr: np.ndarray, gap: float) -> list[np.ndarray]:
    """Ground truth: saturated, notehead-sized, dark-in-luma components."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    luma = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    m = ((hsv[:, :, 1] > 140) & (luma < 140)).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, 8)
    out = []
    for i in range(1, n):
        w, h = st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT]
        if st[i, cv2.CC_STAT_AREA] < 0.25 * gap * gap:
            continue
        if w > 2.0 * gap or h > 2.0 * gap or w < 0.4 * gap or h < 0.4 * gap:
            continue
        out.append(lab == i)
    return out


def kept(norm: np.ndarray, mask: np.ndarray) -> float:
    """Share of a glyph's pixels that are still ink after normalisation."""
    return float((norm[mask] < 200).mean()) if mask.any() else 1.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--band", nargs=2, type=int, required=True, metavar=("Y0", "Y1"))
    ap.add_argument("--gap", type=float, required=True)
    ap.add_argument("--keep-min", type=float, default=KEEP_MIN)
    ap.add_argument("--assert-legacy", action="store_true",
                    help="judge the PRE-FIX rule instead, to prove this gate can fail")
    a = ap.parse_args()

    video = Path(a.video)
    w, h, dur = probe_video(video)
    y0, y1 = a.band
    now, legacy = [], []
    for f in (0.15, 0.30, 0.45, 0.60, 0.75, 0.90):
        bgr = frame_at(video, dur * f, w, h)
        if bgr is None:
            continue
        band = bgr[y0:y1]
        glyphs = coloured_glyphs(band, a.gap)
        if not glyphs:
            continue
        shipped = normalise_ink(band, None, "dark_ink", SAT, a.gap)
        # the pre-fix rule, reproduced here so the gate is provably discriminating
        old = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY).copy()
        old[playhead_mask(band, SAT, 140)] = 255
        for g in glyphs:
            now.append(kept(shipped, g))
            legacy.append(kept(old, g))

    if len(now) < MIN_HEADS:
        print(f"COLOUR_FAIL found only {len(now)} coloured glyphs; the test would be blind")
        return 1

    med, lmed = float(np.median(now)), float(np.median(legacy))
    print(f"colour: {len(now)} coloured noteheads sampled | shipped keeps median "
          f"{med:.2f} of each head, legacy V-gate kept {lmed:.2f}")
    if a.assert_legacy:
        now, med = legacy, lmed
        print("colour: judging the PRE-FIX rule (--assert-legacy)")
    bad = sum(1 for v in now if v < a.keep_min)
    print(f"colour: {bad} head(s) below the {a.keep_min} floor")
    if med < a.keep_min or bad > 0.1 * len(now):
        print("COLOUR_FAIL coloured noteheads are being erased as overlay chrome")
        return 1
    print("COLOUR_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
