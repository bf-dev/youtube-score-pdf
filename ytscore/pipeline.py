#!/usr/bin/env python3
"""
YouTube sheet-music video -> A4 PDF.

Pipeline:
  1. download   : yt-dlp, best video up to 1080p (optional SOCKS proxy for KR egress)
  2. analyse    : one low-fps pass that finds, WITHOUT any per-video tuning,
                    - the ink polarity (black-on-paper vs white-on-video)
                    - every staff SYSTEM in the frame (top, bottom, several at once)
                    - a per-pixel background plate for the translucent case
  3. collect    : full-fps pass, cropping only the system boxes
  4. dedupe     : per system slot, Jaccard on a binary signature
  5. composite  : per-pixel median over each line's frames (kills playhead + intrusions)
  6. render     : polarity-specific normalisation to black-on-white
  7. order      : merge the slots by time, drop the cross-slot repeats a rolling
                  2-line display produces
  8. assemble   : A4 pages with a Korean-capable title header, one PDF

Why the layout step is generic
------------------------------
A staff line is the one thing every score in every one of these videos has: a
long, thin, perfectly horizontal ridge. Detecting that ridge per frame and then
reducing to the modal layout finds the score whether it is at the bottom (case 0),
two systems at the top (case 1), white-on-black (case 2) or painted translucently
over a moving performance (case 3), and it settles the polarity for free (a dark
ridge on a light plate vs a light ridge on a dark one).

Customer: 1775529 (영웅급히아신스2244)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ytscore import paths
from ytscore.config import APP_VERSION, CUSTOMER_ID


class Cancelled(RuntimeError):
    """The user pressed 중지 while this conversion was running."""


class ScoreNotFound(RuntimeError):
    """The video plays, but there is no sheet music in it we can lift."""


class DownloadFailed(RuntimeError):
    """yt-dlp could not fetch the video (bad link, private, region-locked...)."""


_SINK = None            # set by the GUI; None means "print", i.e. our CLI/CI modes
_CANCEL = None          # threading.Event


def set_log_sink(fn) -> None:
    """Route every pipeline log line to the caller (the GUI status pane)."""
    global _SINK
    _SINK = fn


def set_cancel_event(ev) -> None:
    global _CANCEL
    _CANCEL = ev


def check_cancel() -> None:
    if _CANCEL is not None and _CANCEL.is_set():
        raise Cancelled("사용자가 중지했습니다")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    if _SINK is not None:
        try:
            _SINK(line)
            return
        except Exception:
            pass
    try:
        print(line, flush=True)
    except Exception:
        pass        # --noconsole: sys.stdout is None


# ---------------------------------------------------------------- 1. download

class _YtdlLog:
    """yt-dlp talks to our log sink instead of a console that does not exist."""

    def debug(self, m):
        if m and not m.startswith("[debug] "):
            log(f"yt-dlp: {m.strip()}")

    def info(self, m):
        self.debug(m)

    def warning(self, m):
        log(f"yt-dlp: {str(m).strip()}")

    def error(self, m):
        log(f"yt-dlp: {str(m).strip()}")


def _ydl_opts(max_height: int, proxy: str | None, dest: Path | None) -> dict:
    opts = {
        "format": f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YtdlLog(),
        "noplaylist": True,
        "ffmpeg_location": str(Path(paths.ffmpeg()).parent) if os.path.dirname(paths.ffmpeg()) else None,
    }
    if dest is not None:
        opts["outtmpl"] = str(dest / "video.%(ext)s")
    if proxy:
        opts["proxy"] = proxy
    return {k: v for k, v in opts.items() if v is not None}


def download(url: str, dest: Path, max_height: int = 1080, proxy: str | None = None,
             progress=None) -> Path:
    """
    Fetch the video with yt-dlp. Returns the muxed mp4 path.

    The Python module is used when it is importable (that is the shipped Windows
    build: nothing external to install on the customer's PC) and the `yt-dlp`
    binary otherwise, which is how this file has always run on the Linux host.
    """
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "video.mp4"
    if out.exists() and out.stat().st_size > 0:
        log(f"download: reusing {out} ({out.stat().st_size} bytes)")
        return out
    log(f"download: {url} (<= {max_height}p)" + (" via proxy" if proxy else ""))
    try:
        import yt_dlp
    except ImportError:
        yt_dlp = None

    if yt_dlp is not None:
        opts = _ydl_opts(max_height, proxy, dest)
        if progress is not None:
            def hook(d):
                check_cancel()
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    got = d.get("downloaded_bytes") or 0
                    if total:
                        progress(min(0.999, got / total))
            opts["progress_hooks"] = [hook]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Cancelled:
            raise
        except Exception as exc:
            raise DownloadFailed(str(exc)) from exc
    else:
        cmd = ["yt-dlp", "--no-warnings",
               "-f", f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]",
               "--merge-output-format", "mp4",
               "-o", str(dest / "video.%(ext)s")]
        if proxy:
            cmd += ["--proxy", proxy]
        cmd.append(url)
        p = subprocess.run(cmd, capture_output=True, text=True, **paths.popen_kwargs())
        if p.returncode != 0:
            raise DownloadFailed((p.stderr or p.stdout or "").strip()[-500:])

    if not out.exists():
        # yt-dlp can land on .mkv/.webm when the mp4 merge is not possible
        alt = sorted(dest.glob("video.*"), key=lambda q: q.stat().st_size, reverse=True)
        alt = [a for a in alt if a.suffix.lower() in (".mkv", ".webm", ".mp4", ".m4v")]
        if not alt:
            raise DownloadFailed("yt-dlp finished but no video file was written")
        out = alt[0]
    log(f"download: ok {out.name} {out.stat().st_size} bytes")
    return out


def video_meta(url: str, proxy: str | None = None) -> dict:
    """Metadata only (no download). Used for the default PDF title."""
    try:
        import yt_dlp
    except ImportError:
        yt_dlp = None
    if yt_dlp is not None:
        try:
            with yt_dlp.YoutubeDL(_ydl_opts(1080, proxy, None)) as ydl:
                info = ydl.extract_info(url, download=False)
            return {k: info.get(k) for k in
                    ("id", "title", "duration", "width", "height", "uploader",
                     "channel", "upload_date", "webpage_url", "format_id", "ext")}
        except Exception as exc:
            log(f"title: yt-dlp metadata lookup failed ({exc})")
            return {}
    try:
        p = subprocess.run(["yt-dlp", "--no-warnings", "--skip-download",
                            "--print", "%(title)s", url],
                           capture_output=True, text=True, timeout=90,
                           **paths.popen_kwargs())
        t = (p.stdout or "").strip().splitlines()
        if t and t[0].strip():
            return {"title": t[0].strip()}
    except Exception as exc:                                # never fatal
        log(f"title: yt-dlp lookup failed ({exc})")
    return {}


def video_title(url: str, fallback: str, meta: dict | None = None) -> str:
    t = (meta or {}).get("title") or video_meta(url).get("title")
    if t and str(t).strip():
        # yt-dlp can hand back NFD Hangul; NanumGothic has no combining jamo,
        # so an un-normalised title prints as loose jamo instead of syllables.
        return unicodedata.normalize("NFC", str(t).strip())
    return fallback


# ------------------------------------------------------------------ 2. decode

def probe_video(video: Path) -> tuple[int, int, float]:
    """(width, height, duration) via ffprobe."""
    out = subprocess.run(
        [paths.ffprobe(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "json", str(video)],
        capture_output=True, text=True, check=True, **paths.popen_kwargs()).stdout
    d = json.loads(out)
    s = d["streams"][0]
    return int(s["width"]), int(s["height"]), float(d["format"]["duration"])


def iter_frames(video: Path, fps: float, w: int, h: int):
    """
    Yield (t, bgr) decoded through a system-ffmpeg raw pipe.

    cv2.VideoCapture is deliberately not used: YouTube serves AV1 for many of
    these videos and OpenCV's bundled FFmpeg has no AV1 decoder, so it silently
    returns zero frames. The system ffmpeg (libdav1d) handles it.
    """
    cmd = [paths.ffmpeg(), "-v", "error", "-i", str(video),
           "-vf", f"fps={fps}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=10 ** 8, **paths.popen_kwargs())
    fsize = w * h * 3
    i = 0
    try:
        while True:
            check_cancel()
            buf = proc.stdout.read(fsize)
            if not buf or len(buf) < fsize:
                break
            yield i / fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            i += 1
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        proc.wait()
    if i == 0:
        err = proc.stderr.read().decode(errors="replace").strip()
        raise RuntimeError(f"ffmpeg decoded 0 frames: {err[:400]}")


# ----------------------------------------------------------------- 3. analyse

@dataclass
class Layout:
    polarity: str                       # "dark_ink" | "light_ink"
    width: int
    height: int
    staff_gap: float                    # px between adjacent staff lines
    systems: list[tuple[int, int]] = field(default_factory=list)   # box (y0, y1)
    staff_spans: list[tuple[int, int]] = field(default_factory=list)  # (first line, last line)
    staff_rows: list[list[int]] = field(default_factory=list)      # line centres per system
    plate: np.ndarray | None = None     # per-pixel background estimate, full frame
    ridge_ref: float = 0.0              # staff-line ridge strength on the median frame


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    out, start = [], None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(mask)))
    return out


RIDGE_THRESH = 8            # grey levels; a staff line clears this in every case
RIDGE_RUN_DIV = 32          # a ridge must run at least width/32 to count


def staff_ridge(gray: np.ndarray, polarity: str) -> np.ndarray:
    """
    Binary map of long, thin, horizontal ridges: the staff lines and nothing much else.

    Two settings here are load-bearing and were both measured, not guessed:

    * a FIXED grey-level threshold. Scaling it to the frame's own contrast breaks
      case 0, where the video above the score contains far harder edges than the
      staff and drags a relative threshold up past the staff lines.
    * a short run length (width/32). A staff line is interrupted wherever a beam
      or a bright patch of background sits on it, so opening with a wide kernel
      deletes whole lines. Case 2 loses four of its five lines at width/16 and
      keeps all five at width/32.
    """
    w = gray.shape[1]
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 9))
    op = cv2.MORPH_BLACKHAT if polarity == "dark_ink" else cv2.MORPH_TOPHAT
    resp = cv2.morphologyEx(gray, op, vk)
    binr = (resp > RIDGE_THRESH).astype(np.uint8)
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(24, w // RIDGE_RUN_DIV), 1))
    return cv2.morphologyEx(binr, cv2.MORPH_OPEN, hk)


def staff_row_coverage(gray: np.ndarray, polarity: str) -> np.ndarray:
    """
    Per row: the fraction of the width covered by a staff-line ridge.

    A staff line covers 0.5-0.9 of the frame width; the small inset preview score
    case 0 paints over the video covers ~0.25, and ordinary picture detail covers
    almost nothing. Coverage is what separates them.
    """
    return staff_ridge(gray, polarity).mean(axis=1)


def frame_staff_clusters(prof: np.ndarray, h: int) -> tuple[list[list[int]], float]:
    """One frame's staff lines, grouped into systems. Returns (clusters, spacing)."""
    line_runs = [r for r in _runs(prof > 0.40) if r[1] - r[0] <= max(6, h // 90)]
    centres = [int(round((a + b - 1) / 2)) for a, b in line_runs]
    if not centres:
        return [], 0.0
    gaps = np.diff(centres)
    intra = gaps[gaps <= max(8, h * 0.06)] if len(gaps) else np.array([])
    gap = float(np.median(intra)) if len(intra) else h / 70.0
    clusters: list[list[int]] = [[centres[0]]]
    for c in centres[1:]:
        if c - clusters[-1][-1] > 3.0 * gap:
            clusters.append([c])
        else:
            clusters[-1].append(c)
    return clusters, gap


def analyse_layout(video: Path, max_frames: int = 140, dump: Path | None = None,
                   force_band: tuple[int, int] | None = None,
                   force_polarity: str | None = None) -> Layout:
    """
    Low-fps pass: ink polarity, one box per staff SYSTEM, and a background plate.

    Everything here is derived from the video, not from the URL.

    The staff lines are detected PER FRAME and then reduced to the modal layout,
    not detected once on the temporal median. Case 1 is why: its rolling two-line
    display re-lays-out whenever a line carries lyrics, so the staff sits 10-20px
    higher or lower depending on the line, and the median smears every line into
    an unusable grey band. Per-frame detection sees a crisp staff in every frame.

    A long horizontal rule is not necessarily a staff: case 1 also draws a title
    card whose border runs half the frame width in every single frame. Candidate
    systems are therefore kept only if their content actually CHANGES over the
    video, which no piece of static chrome does.
    """
    w, h, dur = probe_video(video)
    afps = max(0.25, min(2.0, max_frames / max(dur, 1.0)))
    log(f"analyse: {w}x{h}, {dur:.1f}s, layout pass at {afps:.2f}fps")

    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for _, f in iter_frames(video, afps, w, h)]
    if not grays:
        raise RuntimeError("layout pass decoded no frames")
    stack = np.stack(grays)
    log(f"analyse: {len(grays)} analysis frames")

    dprofs = [staff_row_coverage(g, "dark_ink") for g in grays]
    lprofs = [staff_row_coverage(g, "light_ink") for g in grays]
    dsum = float(np.sum([p.sum() for p in dprofs]))
    lsum = float(np.sum([p.sum() for p in lprofs]))
    polarity = force_polarity or ("dark_ink" if dsum >= lsum else "light_ink")
    profs = dprofs if polarity == "dark_ink" else lprofs
    log(f"analyse: polarity {polarity} (dark ridge {dsum:.0f} vs light {lsum:.0f})")

    per_frame = [frame_staff_clusters(p, h) for p in profs]
    counts = [len(c) for c, _ in per_frame if c]
    if not counts:
        raise ScoreNotFound("no staff lines found anywhere in the frame")
    modal = int(np.bincount(counts).argmax())
    usable = [(c, g) for c, g in per_frame if len(c) == modal]
    gap = float(np.median([g for _, g in usable if g > 0])) or h / 70.0
    spans = [(int(np.median([c[i][0] for c, _ in usable])),
              int(np.median([c[i][-1] for c, _ in usable]))) for i in range(modal)]
    nlines = [int(np.median([len(c[i]) for c, _ in usable])) for i in range(modal)]
    log(f"analyse: modal layout = {modal} staff group(s) in {len(usable)}/{len(grays)} frames, "
        f"line spacing {gap:.1f}px, groups {spans} with {nlines} line(s)")

    # keep only the groups whose content changes: a score does, a title-card rule
    # or a UI border does not. The probe is restricted to the COLUMNS the ridge
    # actually occupies -- case 1's title-card border passes a full-width probe
    # only because the video inset happens to sit to the right of it.
    usable_idx = [j for j, (c, _) in enumerate(per_frame) if len(c) == modal]
    ridge_idx = usable_idx[:: max(1, len(usable_idx) // 8)][:8]
    ridges = [staff_ridge(grays[j], polarity) for j in ridge_idx]
    keep: list[int] = []
    extents: list[tuple[int, int]] = []
    probe_idx = np.linspace(0, len(grays) - 1, min(14, len(grays))).astype(int)
    for i, (s0, s1) in enumerate(spans):
        band = np.zeros(w, np.float32)
        for r in ridges:
            band += r[max(0, s0 - 2):min(h, s1 + 3)].max(axis=0)
        cols = np.where(band > 0.4 * band.max())[0] if band.max() > 0 else np.array([0, w - 1])
        x0, x1 = int(cols.min()), int(cols.max()) + 1
        extents.append((x0, x1))
        y0 = max(0, int(s0 - 2.5 * gap))
        y1 = min(h, int(s1 + 2.5 * gap))
        keys = []
        for j in probe_idx:
            crop = grays[j][y0:y1, x0:x1]
            keys.append(signature(crop if polarity == "dark_ink" else 255 - crop))
        best = max(jaccard(keys[a], keys[b])
                   for a in range(len(keys)) for b in range(a + 1, len(keys)))
        steps = sum(1 for a in range(len(keys) - 1)
                    if jaccard(keys[a], keys[a + 1]) > 0.30) / max(len(keys) - 1, 1)
        # A staff has at least two lines. A single rule that also turns over its
        # content repeatedly is accepted anyway (a genuine one-line rhythm staff),
        # but case 1's title card, which changes exactly twice in four minutes, is not.
        ok = (nlines[i] >= 2 and best >= 0.25) or (nlines[i] < 2 and steps >= 0.40)
        log(f"analyse: group {i} rows {s0}..{s1} cols {x0}..{x1}: {nlines[i]} line(s), "
            f"max change {best:.2f}, changed in {steps:.0%} of probe steps -> "
            f"{'keep' if ok else 'drop'}")
        if ok:
            keep.append(i)
    if not keep:
        raise ScoreNotFound("staff lines found but none of them carry changing notation")
    spans = [spans[i] for i in keep]
    nlines = [nlines[i] for i in keep]

    # For dark ink the score sits on real paper, so the paper itself bounds the
    # box; that is what keeps case 0's strip from swallowing the video above it.
    # The staff lines are dark rows INSIDE the paper, so the mask has to be
    # closed over them before the run is taken -- walking outwards row by row
    # stops dead on the first staff line and yields a 1px box.
    paper_runs: list[tuple[int, int]] = []
    if polarity == "dark_ink":
        bright = (np.mean([(g > 200).mean(axis=1) > 0.5 for g in grays], axis=0) > 0.5)
        k = int(max(3, round(3.0 * gap))) | 1
        closed = cv2.morphologyEx(bright.astype(np.uint8).reshape(-1, 1), cv2.MORPH_CLOSE,
                                  np.ones((k, 1), np.uint8)).ravel().astype(bool)
        paper_runs = _runs(closed)

    boxes: list[tuple[int, int]] = []
    if len(spans) >= 2:
        # tile: consecutive systems share the whole pitch between them, weighted
        # towards the space ABOVE the staff (beams and measure numbers live there,
        # only lyrics live below)
        pitch = float(np.median(np.diff([s[0] for s in spans])))
        for s0, s1 in spans:
            free = max(pitch - (s1 - s0), 4 * gap)
            boxes.append((int(round(s0 - 0.60 * free)), int(round(s1 + 0.40 * free))))
    else:
        s0, s1 = spans[0]
        wide = 12.0 * gap if paper_runs else 7.0 * gap
        boxes.append((int(round(s0 - wide)), int(round(s1 + wide))))

    final: list[tuple[int, int]] = []
    for (y0, y1), (s0, s1) in zip(boxes, spans):
        y0, y1 = max(0, y0), min(h, y1)
        mid = (s0 + s1) // 2
        for a, b in paper_runs:
            if a <= mid < b:
                y0, y1 = max(y0, a), min(y1, b)
                break
        final.append((y0, y1))
    boxes = final
    if force_band is not None:                    # hand-set band overrides detection
        by0, by1 = force_band
        boxes = [(by0, by1)]
        spans = [max(spans, key=lambda s: (by0 <= (s[0] + s[1]) // 2 < by1, -abs(s[0] - by0)))]
        nlines = nlines[:1]
        log(f"analyse: band overridden by hand -> {boxes}")
    log(f"analyse: {len(boxes)} system slot(s) {boxes}")

    # background plate for the translucent/inverted case: the value each pixel
    # takes when NO notation is on it. Low percentile for white ink, high for
    # black ink.
    q = 15 if polarity == "light_ink" else 85
    plate = np.percentile(stack, q, axis=0).astype(np.uint8)
    med = np.median(stack, axis=0).astype(np.uint8)

    staff_rows = []
    for (s0, s1), n in zip(spans, nlines):
        staff_rows.append([int(round(s0 + (s1 - s0) * k / max(n - 1, 1))) for k in range(max(n, 1))])

    lay = Layout(polarity=polarity, width=w, height=h, staff_gap=gap, systems=boxes,
                 staff_spans=spans, staff_rows=staff_rows,
                 plate=plate, ridge_ref=0.0)

    if dump is not None:
        dump.mkdir(parents=True, exist_ok=True)
        vis = cv2.cvtColor(med, cv2.COLOR_GRAY2BGR)
        for (y0, y1), (s0, s1) in zip(boxes, spans):
            cv2.rectangle(vis, (2, y0), (w - 3, y1 - 1), (0, 0, 255), 3)
            cv2.rectangle(vis, (2, s0), (w - 3, s1), (255, 0, 0), 1)
        cv2.imwrite(str(dump / "layout.png"), vis)
        cv2.imwrite(str(dump / "plate.png"), plate)
        log(f"analyse: dumped {dump/'layout.png'}")
    return lay


# ------------------------------------------------------------- 4/5. per-slot

@dataclass
class SlotFrame:
    t: float
    gray: np.ndarray        # polarity-normalised: ink is always DARK here
    key: np.ndarray         # binary signature for dedup
    fp: np.ndarray          # grey fingerprint, same downscale without the threshold
    top: int = -1           # row of this frame's staff, for composite alignment


def playhead_mask(bgr: np.ndarray, sat_thresh: int, min_value: int = 0) -> np.ndarray:
    """
    Pixels that are overlay chrome rather than notation: the sweeping playhead,
    a coloured highlight, a callout border.

    `min_value` is what keeps this from eating the music. On a paper score the
    playhead is a translucent highlight, so it is both saturated and BRIGHT
    (measured on sample case 0: value 225-230). Near-black ink, on the other
    hand, reports a wild saturation because S = (max-min)/max is meaningless at
    V=3, and acceptance video 1 pays for it: a third of its notation pixels
    report saturation above 50 and were being whitened out, which is why its
    noteheads came out as hollow smudges. Requiring brightness too keeps 98% of
    case 0's real playhead and none of video 1's ink.
    """
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    m = hsv[:, :, 1] > sat_thresh
    if min_value > 0:
        m &= hsv[:, :, 2] > min_value
    return m


def normalise_ink(bgr: np.ndarray, plate: np.ndarray, polarity: str,
                  sat_thresh: int) -> np.ndarray:
    """
    Turn one cropped system into an image where the ink is dark and the paper light,
    whatever the source looked like.

    dark_ink : the band really is paper. Whiten the saturated playhead and keep
               the grey levels; the group median downstream restores what the
               playhead covered.
    light_ink: the ink is white and the "paper" is whatever the video happens to
               show. Recover the compositing alpha against the background plate,
               a = (pixel - bg) / (255 - bg), which is flat in the ink and zero in
               the background no matter how bright or busy the background is.
    """
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    # dark ink: chrome has to be bright as well as saturated, or near-black
    # notation (whose hue is numerical noise) gets whitened away with it.
    sat = playhead_mask(bgr, sat_thresh, 140 if polarity == "dark_ink" else 0)
    if polarity == "dark_ink":
        out = g.copy()
        out[sat] = 255
        return out
    bg = plate.astype(np.float32)
    a = (g.astype(np.float32) - bg) / np.maximum(255.0 - bg, 30.0)
    a[sat] = 0.0
    return (255.0 * (1.0 - np.clip(a, 0.0, 1.0))).astype(np.uint8)


def staff_present(gray: np.ndarray) -> float:
    """
    How strongly a staff is drawn anywhere in this crop.

    `gray` is always polarity-normalised (ink dark) by the time it gets here, so
    one blackhat covers every case. The best row is taken rather than a fixed set
    of rows because case 1's display shifts the staff by 10-20px depending on
    whether the current line carries lyrics. The absolute value has no meaning
    across videos, so the caller thresholds it against this video's own spread.
    """
    return float(staff_row_coverage(gray, "dark_ink").max())


def staff_anchor(prof: np.ndarray, gap: float, target_top: int) -> int:
    """
    Row of the staff this frame is showing, or -1 when there is no staff.

    The anchor is the staff CLUSTER nearest the layout's staff, not the topmost
    staff row: two lines are on screen during a slide and the neighbour's staff
    flickers in and out of the row profile, so "topmost row" jumps 50px back and
    forth inside one single line.

    The clustering is done here against the known staff spacing rather than
    through `frame_staff_clusters`, whose thresholds are derived from the FRAME
    height and collapse when handed a 226px band (every staff line came out as
    its own cluster and every frame returned -1).
    """
    limit = max(6, int(round(0.6 * gap)))
    runs = [r for r in _runs(prof > 0.40) if r[1] - r[0] <= limit]
    centres = [int((a + b - 1) // 2) for a, b in runs]
    if not centres:
        return -1
    clusters: list[list[int]] = [[centres[0]]]
    for c in centres[1:]:
        if c - clusters[-1][-1] > 3.0 * gap:
            clusters.append([c])
        else:
            clusters[-1].append(c)
    tops = [c[0] for c in clusters if len(c) >= 3]
    if not tops:
        return -1
    return int(min(tops, key=lambda x: abs(x - target_top)))


def shift_rows(gray: np.ndarray, dy: int) -> np.ndarray:
    """Translate vertically, filling with paper white."""
    if dy == 0:
        return gray
    out = np.full_like(gray, 255)
    if dy > 0:
        out[dy:] = gray[:-dy]
    else:
        out[:dy] = gray[-dy:]
    return out


def signature(gray: np.ndarray, w: int = 320, h: int = 64, thresh: int = 165) -> np.ndarray:
    """Binary fingerprint of the notation, insensitive to the playhead."""
    small = cv2.resize(gray, (w, h), interpolation=cv2.INTER_AREA)
    return small < thresh


def fingerprint(gray: np.ndarray, w: int = 320, h: int = 64) -> np.ndarray:
    """Grey fingerprint: the same downscale as `signature`, without the threshold."""
    return cv2.resize(gray, (w, h), interpolation=cv2.INTER_AREA)


def soft_jaccard(a: np.ndarray, b: np.ndarray) -> float:
    """
    Fraction of the notation that changed, on GREY fingerprints:
    sum|a-b| / sum(max(a,b)), taking ink as 255-value.

    This is the same quantity `jaccard` measures and on binary input it is the
    same number, but it does not put a hard threshold across an antialiased
    stroke. That threshold is what broke the first acceptance video: its
    notation is thin, and downscaling puts those strokes within a few grey
    levels of the cut, so ordinary h.264 noise flipped them frame to frame and
    the binary distance between two frames of ONE line drifted to 0.33 (over the
    0.30 grouping threshold) while the frames were provably identical
    (phase correlation dx=dy=0.00, NCC 0.997). The same pairs measure 0.02-0.14
    here, and a genuinely different line still measures 0.42-0.55.
    """
    ia = 255.0 - a.astype(np.float32)
    ib = 255.0 - b.astype(np.float32)
    denom = float(np.maximum(ia, ib).sum())
    if denom <= 0:
        return 0.0
    return float(np.abs(ia - ib).sum() / denom)


def jaccard(a: np.ndarray, b: np.ndarray) -> float:
    """
    Fraction of the notation that changed: |A xor B| / |A or B|.

    A plain pixel-diff does NOT work here. A sparse line (a bridge of whole-bar
    rests) differs from the next sparse line by only ~0.006 of pixels, barely
    above the ~0.003 same-line compression noise, so any global pixel threshold
    either merges real lines or splits identical ones. Normalising by the amount
    of ink present makes the metric scale-invariant: on case 0 same-line pairs
    land <= 0.18 and different-line pairs >= 0.52.
    """
    union = int((a | b).sum())
    if union == 0:
        return 0.0
    return float((a ^ b).sum()) / union


def adaptive_signature(gray: np.ndarray, w: int = 320, h: int = 48) -> np.ndarray:
    """Binary fingerprint thresholded against THIS strip's own ink/paper spread."""
    small = cv2.resize(gray, (w, h), interpolation=cv2.INTER_AREA).astype(np.float32)
    lo = float(np.percentile(small, 2))
    hi = float(np.percentile(small, 96))
    if hi - lo < 12:                       # a blank strip has no fingerprint
        return np.zeros((h, w), bool)
    return small < (hi - 0.55 * (hi - lo))


CORE_W, CORE_H = 640, 96
CORE_SAME = 0.80            # >= this is the same notation


def core_crop(comp: np.ndarray, gap: float) -> np.ndarray:
    """
    The staff CORE of one composite: the staff itself plus the beam space above
    it, and nothing below the bottom staff line.

    Two things make this the right crop for comparing one system against another:

    * The lyric row underneath a system is a large share of its ink, and it is
      exactly the part that differs between the two copies a rolling display
      makes of the same line (the preview copy carries no lyrics yet). A
      full-strip comparison therefore reports two different systems.
    * The crop is found IN THIS composite, not from the modal layout. Case 1
      re-lays-out whenever a line carries lyrics, so its staff sits 10-20px
      (about one staff space) higher or lower from line to line.

    The crop is anchored on the TOP staff line and takes a FIXED number of staff
    spaces, so two copies of the same system always come out the same pixel
    height. Cropping "top line to bottom line" instead lets one copy come out
    60px tall and the other 100px, and the fixed-size resize then compares two
    differently stretched pictures, which never matches (measured: 0.89-0.96 on
    case 1's identical pairs).
    """
    rows = np.where(staff_row_coverage(comp, "dark_ink") > 0.40)[0]
    if len(rows) < 2:
        return comp
    top = int(rows.min())
    r0 = max(0, int(round(top - 1.6 * gap)))
    r1 = min(comp.shape[0], r0 + int(round(7.0 * gap)))
    return comp if r1 - r0 < 8 else comp[r0:r1]


def core_fingerprint(comp: np.ndarray, gap: float) -> np.ndarray:
    """Fixed-size grey picture of one system's staff core, for system-vs-system."""
    return cv2.resize(core_crop(comp, gap), (CORE_W, CORE_H), interpolation=cv2.INTER_AREA)


def core_match(a: np.ndarray, b: np.ndarray, pad: int = 8) -> float:
    """
    How strongly two systems are the same notation, 1.0 = identical.

    Normalised cross-correlation, not a Jaccard on thresholded ink, for two
    reasons that both showed up in the sample videos:

    * it is invariant to a linear intensity change, so a half-drawn FADE of a
      system still correlates ~0.95 with the finished system it copies, while a
      binary fingerprint of the same pair falls apart (the fade's ink never
      crosses the threshold).
    * matching over a small shift window absorbs the few pixels of vertical
      offset left after the crop, and the video's fixed channel watermark, which
      sits at a different place inside each slot's box, only depresses the score
      instead of destroying it.

    Measured on case 1 (rolling display, 29 lines x 2 slots): the same line seen
    in both slots scores 0.86-0.99, unrelated lines have a median of 0.45 and a
    99th percentile of 0.85.
    """
    t = b[pad:CORE_H - pad, pad:CORE_W - pad]
    r = cv2.matchTemplate(a.astype(np.float32), t.astype(np.float32), cv2.TM_CCOEFF_NORMED)
    return float(r.max())


def ink_strength(comp: np.ndarray) -> float:
    """
    How dark this composite's ink actually gets, 0..1, measured on the composite
    and NOT on the rendered strip: render_strip auto-stretches each strip on its
    own percentiles, which partly hides exactly the difference we are after.
    """
    lo = float(np.percentile(comp, 1))
    hi = float(np.percentile(comp, 96))
    return max(0.0, (hi - lo) / 255.0)


def _rolling_hits(src_cores: list[np.ndarray], src_t: list[float],
                  dst_cores: list[np.ndarray], dst_t: list[float]) -> float:
    """Fraction of src systems that reappear LATER in dst."""
    hits = 0
    for j, c in enumerate(src_cores):
        best, bi = -1.0, -1
        for i, d in enumerate(dst_cores):
            v = core_match(d, c)
            if v > best:
                best, bi = v, i
        if best >= CORE_SAME and dst_t[bi] > src_t[j]:
            hits += 1
    return hits / max(1, len(src_cores))


def detect_rolling(group_times: list[list[float]], cores: list[list[np.ndarray]],
                   min_frac: float = 0.55, margin: float = 0.20) -> tuple[int, int] | None:
    """
    Is this a rolling two-line display?

    Such a video shows every system twice: once in the preview slot as the "next"
    line and once, a few seconds later, in the current slot. Automating this is
    what removes the last hand-set flag from the customer-facing app (case 1 was
    run with `--slots 0 --tail-slots 1`; without it the PDF came out with 57
    systems instead of 29, every line printed twice).

    The test is asymmetric on purpose: nearly every system of the PREVIEW slot
    turns up again later in the CURRENT slot, and hardly any goes the other way.
    A genuine two-system layout, where both slots carry their own music, matches
    in neither direction and is left alone.

    Returns (current_slot, preview_slot) or None.
    """
    if len(cores) != 2 or min(len(c) for c in cores) < 4:
        return None
    f10 = _rolling_hits(cores[1], group_times[1], cores[0], group_times[0])
    f01 = _rolling_hits(cores[0], group_times[0], cores[1], group_times[1])
    log(f"rolling: slot 1 -> slot 0 later {f10:.0%}, slot 0 -> slot 1 later {f01:.0%}")
    if max(f10, f01) < min_frac or abs(f10 - f01) < margin:
        return None
    return (0, 1) if f10 > f01 else (1, 0)


def group_lines(frames: list[SlotFrame], thresh: float) -> list[list[SlotFrame]]:
    """
    One group per distinct line, comparing each frame to the running anchor.

    A frame starts a new line only when BOTH distances say it changed. The two
    disagree in opposite directions and each one covers the other's blind spot:

    * binary alone splits ONE line into three on a video whose notation is thin
      (acceptance video 1): downscaling lands those strokes within a few grey
      levels of the 165 cut, so h.264 noise flips them and the distance drifts to
      0.33 while the frames are provably identical (phase correlation dx=dy=0.00).
      The graded distance calls those pairs 0.02-0.14.
    * graded alone splits one line into nine on sample case 0, where the
      performer's cymbal swings into the band mid-line: a big transient grey
      change that the binary cut clips away and the graded metric does not.

    A genuinely new line clears both (measured: 0.42-0.55 graded, 0.52-0.92
    binary), so the AND costs nothing on a real line change.
    """
    groups: list[list[SlotFrame]] = []
    anchor: np.ndarray | None = None
    anchor_fp: np.ndarray | None = None
    for f in frames:
        new = (anchor is None
               or (jaccard(f.key, anchor) > thresh
                   and soft_jaccard(f.fp, anchor_fp) > thresh))
        if new:
            groups.append([f])
            anchor = f.key.copy()
            anchor_fp = f.fp.copy()
        else:
            groups[-1].append(f)
            anchor |= f.key
            anchor_fp = np.minimum(anchor_fp, f.fp)      # darkest wins: union of ink
    return groups


@dataclass
class Cand:
    """One candidate system: the strip plus what the ordering pass judges it on."""
    t: float
    si: int
    strip: np.ndarray
    core: np.ndarray            # fixed-size grey picture of the staff core
    box: np.ndarray             # the whole slot box, same scale for every system in a slot
    strength: float             # how dark this system's ink actually gets, 0..1
    cov: float = 0.0            # staff-line coverage of the prepared strip


def drop_fade_copies(cands: list[Cand], ratio: float = 0.78) -> tuple[list[Cand], list[int]]:
    """
    Remove the half-drawn copies a fade-in / fade-out leaves behind.

    Cases 2 and 3 open by fading system 1 up from nothing and close by fading the
    last system out, and each fade holds still long enough to survive the frame
    grouping as its own "system": the PDF then opened and closed with a grey
    ghost of a line that is already on the page. Those two videos were delivered
    as samples with a hand-written `--drop 0,27,28`; this is what replaces it.

    A fade copy is the same notation as the neighbour it sits next to (core
    correlation >= CORE_SAME, which survives the intensity difference) drawn in
    ink that never reaches full black (clearly lower strength). BOTH conditions
    are required: neighbours that look alike AND are equally dark are a real
    musical repeat and are kept, and a genuinely different line is never touched
    however faint it is.

    Discriminators tried on the sample runs that do NOT work: the 2nd-percentile
    grey level alone (case 0 has legitimate sparse systems at p2=123) and the
    dark/any ink ratio (case 2's fade sits at 0.252 against a real 0.277).
    """
    out = list(cands)
    dropped: list[int] = []
    changed = True
    while changed and len(out) > 1:
        changed = False
        for i in range(len(out) - 1):
            a, b = out[i], out[i + 1]
            hi = max(a.strength, b.strength)
            lo = min(a.strength, b.strength)
            if hi <= 0 or lo / hi >= ratio:
                continue
            # Within one slot the two pictures are the same crop of the same box,
            # so the whole box is the safest thing to correlate: a fade that is
            # too faint for its own staff to be detected still lines up exactly.
            # Only across slots is the staff-core crop needed to align them.
            m = core_match(a.box, b.box) if a.si == b.si else core_match(a.core, b.core)
            if m < CORE_SAME:
                continue
            weak = i if a.strength < b.strength else i + 1
            log(f"fade: system {weak} (t={out[weak].t:.1f}s, ink {out[weak].strength:.2f}) "
                f"is a fade copy of its neighbour (ink {hi:.2f}, match {m:.2f}) -> dropped")
            dropped.append(weak)
            out.pop(weak)
            changed = True
            break
    if dropped:
        log(f"fade: {len(dropped)} fade-in/fade-out copies dropped -> {len(out)} systems")
    return out, dropped


def composite_line(group: list[SlotFrame], gap: float = 0.0, trim: int = 1) -> np.ndarray:
    """
    Merge every frame of one line into one strip, via per-pixel MEDIAN.

    A single frame is not usable: the playhead is a translucent highlight drawn
    ON TOP of the notation, so blanking it also erases the notes under it. It
    sits at a different x in every frame, so the notation is recoverable by
    combining frames.

    Median, not minimum. Minimum (darkest wins) recovers the notation but also
    permanently records any transient dark thing that ever entered the band; on
    case 0 the performer's cymbal dips into the band mid-line and min-blend baked
    those blobs onto the page. Median keeps only what is present in most frames.

    The frames are first aligned on their own staff. Several of these videos
    animate a line INTO the band or nudge it a few pixels when it carries
    lyrics, and the median of an unaligned group keeps only what the two
    positions have in common: the notation comes out as hollow outlines, which
    is what acceptance video 1 produced before this. Alignment is deliberately
    done HERE and not before the grouping: registering frames before they are
    grouped changes what counts as a line change and split the verified samples
    into 27 and 31 systems instead of 19 and 29.
    """
    frames = group
    if len(group) > 2 * trim + 1:
        frames = group[trim:len(group) - trim]
    tops = [f.top for f in frames if f.top >= 0]
    if tops and gap > 0:
        ref = int(np.median(tops))
        limit = int(round(4.0 * gap))
        stack = np.stack([shift_rows(f.gray, int(np.clip(ref - f.top, -limit, limit)))
                          if f.top >= 0 else f.gray for f in frames])
    else:
        stack = np.stack([f.gray for f in frames])
    return np.median(stack, axis=0).astype(np.uint8)


# ------------------------------------------------------------------ 6. render

def static_chrome(plate_box: np.ndarray, span: tuple[int, int]) -> np.ndarray:
    """
    The part of the score that never changes: staff lines and the standing
    barlines. For white-on-video scores these live in the background plate (they
    are on screen in every single frame), so the alpha recovery cancels them out
    and they have to be put back. Returns an alpha map in [0,1].
    """
    h, w = plate_box.shape
    bg = plate_box.astype(np.float32)
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 9))
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(64, w // 3), 1))
    horiz = cv2.morphologyEx(cv2.morphologyEx(plate_box, cv2.MORPH_TOPHAT, vk),
                             cv2.MORPH_OPEN, hk)

    hkv = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 1))
    span_h = max(8, int(round((span[1] - span[0]) * 0.8)))
    vkv = cv2.getStructuringElement(cv2.MORPH_RECT, (1, span_h))
    vert = cv2.morphologyEx(cv2.morphologyEx(plate_box, cv2.MORPH_TOPHAT, hkv),
                            cv2.MORPH_OPEN, vkv)
    # barlines only make sense inside the staff itself
    band = np.zeros((h, w), np.uint8)
    band[max(0, span[0] - 2):min(h, span[1] + 3), :] = 1
    vert = vert * band

    chrome = np.maximum(horiz, vert).astype(np.float32)
    return np.clip(chrome / np.maximum(255.0 - bg, 30.0), 0.0, 1.0)


def render_strip(comp: np.ndarray, polarity: str, chrome: np.ndarray | None) -> np.ndarray:
    """Composite -> clean black-on-white BGR strip."""
    if polarity == "dark_ink":
        return cv2.cvtColor(comp, cv2.COLOR_GRAY2BGR)

    a = 1.0 - comp.astype(np.float32) / 255.0          # back to alpha
    if chrome is not None:
        a = np.maximum(a, chrome)
    lo = float(np.percentile(a, 55)) + 0.05
    hi = max(lo + 0.12, float(np.percentile(a, 99.6)) * 0.80)
    a = np.clip((a - lo) / (hi - lo), 0.0, 1.0)
    g = (255.0 * (1.0 - a)).astype(np.uint8)
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


def ink_mask(bgr: np.ndarray, thresh: int = 150) -> np.ndarray:
    g = bgr if bgr.ndim == 2 else cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return g < thresh


def deskew(bgr: np.ndarray, max_deg: float = 2.0) -> np.ndarray:
    """Rotate so staff lines are level. Only acts on small, confident angles."""
    ink = ink_mask(bgr).astype(np.uint8) * 255
    lines = cv2.HoughLinesP(ink, 1, np.pi / 720, threshold=200,
                            minLineLength=int(bgr.shape[1] * 0.35), maxLineGap=12)
    if lines is None or len(lines) == 0:
        return bgr
    segs = lines.reshape(-1, 4)                # cv2 4 gives (N,1,4), cv2 5 gives (N,4)
    angles = []
    for x1, y1, x2, y2 in segs:
        if x2 == x1:
            continue
        a = np.degrees(np.arctan2(float(y2) - y1, float(x2) - x1))
        if abs(a) < max_deg:
            angles.append(a)
    if len(angles) < 5:
        return bgr
    angle = float(np.median(angles))
    if abs(angle) < 0.15:
        return bgr
    h, w = bgr.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(bgr, M, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))


def autocrop(bgr: np.ndarray, pad: int = 10) -> np.ndarray:
    """Trim margin down to the ink bounding box."""
    ink = ink_mask(bgr)
    ink = cv2.morphologyEx(ink.astype(np.uint8), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    rows = np.where(ink.sum(axis=1) > 0)[0]
    cols = np.where(ink.sum(axis=0) > 0)[0]
    if len(rows) == 0 or len(cols) == 0:
        return bgr
    y0, y1 = max(0, rows.min() - pad), min(bgr.shape[0], rows.max() + pad + 1)
    x0, x1 = max(0, cols.min() - pad), min(bgr.shape[1], cols.max() + pad + 1)
    return bgr[y0:y1, x0:x1]


def flatten_paper(bgr: np.ndarray) -> np.ndarray:
    """Flatten paper to true white and deepen the ink, keeping notation legible."""
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    bg = cv2.morphologyEx(g, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    flat = cv2.divide(g, bg, scale=255)
    flat = cv2.normalize(flat, None, 0, 255, cv2.NORM_MINMAX)
    lut = np.clip((np.arange(256) - 40) * (255 / 190), 0, 255).astype(np.uint8)
    flat = cv2.LUT(flat, lut)
    return cv2.cvtColor(flat, cv2.COLOR_GRAY2BGR)


def count_measures(bgr: np.ndarray) -> int:
    """
    Count barlines in a rendered system: full-height dark vertical runs inside
    the staff. Used for the delivery report, not by the pipeline itself.
    """
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    ink = (g < 140).astype(np.uint8)
    rowsum = ink.sum(axis=1)
    staff = np.where(rowsum > 0.5 * ink.shape[1])[0]
    if len(staff) < 2:
        return 0
    y0, y1 = int(staff.min()), int(staff.max())
    if y1 - y0 < 8:
        return 0
    col = ink[y0:y1 + 1, :].mean(axis=0)
    bar = col > 0.92
    return len(_runs(bar))


# ----------------------------------------------------------- 7. PDF assembly

A4_W, A4_H = 2480, 3508          # px at 300dpi
MARGIN = 110
HEADER_PT = 62                   # points reserved on page 1 for the title


def kr_font() -> str | None:
    return paths.kr_font_path()


def build_pdf(strips: list[np.ndarray], out_pdf: Path, title: str) -> int:
    """Lay the ordered strips onto A4 pages, top to bottom, and write the PDF."""
    import fitz

    usable_w = A4_W - 2 * MARGIN
    scaled = []
    for s in strips:
        h, w = s.shape[:2]
        nh = max(1, int(round(h * usable_w / w)))
        scaled.append(cv2.resize(s, (usable_w, nh), interpolation=cv2.INTER_AREA))

    gap = 46
    head_px = int(round(HEADER_PT * A4_H / 842.0))
    pages: list[list[np.ndarray]] = []
    cur, cur_h = [], 0
    limit = A4_H - 2 * MARGIN - head_px                 # page 1 carries the title
    for s in scaled:
        need = s.shape[0] + (gap if cur else 0)
        if cur and cur_h + need > limit:
            pages.append(cur)
            cur, cur_h = [s], s.shape[0]
            limit = A4_H - 2 * MARGIN - head_px // 2    # smaller running header
        else:
            cur.append(s)
            cur_h += need
    if cur:
        pages.append(cur)

    font = kr_font()
    doc = fitz.open()
    tmp = out_pdf.parent / f"_page_{os.getpid()}.png"
    for pi, page_strips in enumerate(pages):
        top = MARGIN + (head_px if pi == 0 else head_px // 2)
        canvas = np.full((A4_H, A4_W, 3), 255, np.uint8)
        y = top
        for s in page_strips:
            canvas[y:y + s.shape[0], MARGIN:MARGIN + s.shape[1]] = s
            y += s.shape[0] + gap
        cv2.imwrite(str(tmp), canvas)
        page = doc.new_page(width=595, height=842)      # A4 in points
        page.insert_image(fitz.Rect(0, 0, 595, 842), filename=str(tmp))
        if font:
            page.insert_font(fontname="kr", fontfile=font)
            size, box = (17, fitz.Rect(40, 30, 555, 76)) if pi == 0 else \
                        (10, fitz.Rect(40, 26, 555, 50))
            text = title if pi == 0 else f"{title}  ({pi + 1}/{len(pages)})"
            while size >= 7:
                rc = page.insert_textbox(box, text, fontname="kr", fontsize=size,
                                         align=fitz.TEXT_ALIGN_CENTER)
                if rc >= 0:
                    break
                size -= 1
    doc.set_metadata({"title": title,
                      "producer": f"score_pdf {APP_VERSION} / customer {CUSTOMER_ID}"})
    doc.save(str(out_pdf), deflate=True)
    doc.close()
    tmp.unlink(missing_ok=True)
    log(f"pdf: {out_pdf} ({len(pages)} pages, {len(strips)} systems)")
    return len(pages)


# ------------------------------------------------------------------- driver

def run(url: str, workdir: Path, outdir: Path, fps: float, dedup_thresh: float,
        proxy: str | None, name: str, title_override: str | None = None,
        sat_thresh: int | None = None, dump: bool = False,
        use_slots: list[int] | None = None, band: tuple[int, int] | None = None,
        polarity: str | None = None, tail_slots: list[int] | None = None,
        drop_idx: list[int] | None = None, progress=None,
        keep_video: bool = True) -> dict:
    t0 = time.time()
    workdir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)

    def report(f: float) -> None:
        if progress is not None:
            try:
                progress(max(0.0, min(1.0, f)))
            except Exception:
                pass

    report(0.01)
    meta = video_meta(url, proxy) if not title_override else {}
    video = download(url, workdir, proxy=proxy,
                     progress=(lambda f: report(0.02 + 0.20 * f)))
    report(0.24)
    _vw, _vh, duration = probe_video(video)
    lay = analyse_layout(video, dump=(outdir / f"{name}_debug") if dump else None,
                         force_band=band, force_polarity=polarity)
    report(0.32)
    tail_only: set[int] = set()
    if use_slots or tail_slots:
        want = sorted(set(use_slots or []) | set(tail_slots or []))
        pick = [i for i in want if 0 <= i < len(lay.systems)]
        if not pick:
            raise RuntimeError(f"--slots {use_slots} selects nothing; "
                               f"{len(lay.systems)} slot(s) were detected")
        tail_only = {pick.index(i) for i in (tail_slots or []) if i in pick}
        lay.systems = [lay.systems[i] for i in pick]
        lay.staff_spans = [lay.staff_spans[i] for i in pick]
        lay.staff_rows = [lay.staff_rows[i] for i in pick]
        log(f"slots: using {pick} (tail-only: {sorted(tail_only)}) -> {lay.systems}")
    sat = sat_thresh if sat_thresh is not None else (50 if lay.polarity == "dark_ink" else 110)

    # ---- full-fps pass, cropping only the system boxes
    raw_slots: list[list[SlotFrame]] = [[] for _ in lay.systems]
    ridges: list[list[float]] = [[] for _ in lay.systems]
    nframes = 0
    plates = [lay.plate[y0:y1] for y0, y1 in lay.systems]
    targets = [max(0, sp[0] - y0) for sp, (y0, _) in zip(lay.staff_spans, lay.systems)]
    for t, f in iter_frames(video, fps, lay.width, lay.height):
        nframes += 1
        if nframes % 20 == 0:
            report(0.32 + 0.45 * (t / max(duration, 1.0)))
        for si, (y0, y1) in enumerate(lay.systems):
            g = normalise_ink(f[y0:y1], plates[si], lay.polarity, sat)
            prof = staff_row_coverage(g, "dark_ink")
            raw_slots[si].append(SlotFrame(t=t, gray=g, key=signature(g), fp=fingerprint(g),
                                           top=staff_anchor(prof, lay.staff_gap, targets[si])))
            ridges[si].append(float(prof.max()))

    # A frame is a score frame when its staff is actually drawn. The ridge value
    # has no absolute meaning across videos, so the cut is taken against this
    # video's own upper quartile: intro cards, black frames and cutaways sit an
    # order of magnitude below it.
    slots: list[list[SlotFrame]] = []
    rejected = 0
    for si, sf in enumerate(raw_slots):
        ref = float(np.percentile(ridges[si], 80)) if sf else 0.0
        cut = 0.35 * ref
        keep = [f for f, r in zip(sf, ridges[si]) if r >= cut]
        rejected += len(sf) - len(keep)
        slots.append(keep)
        log(f"collect: slot {si}: staff ridge ref {ref:.2f}, cut {cut:.2f}, "
            f"{len(keep)}/{len(sf)} frames kept")
    raw_slots = []
    kept = sum(len(s) for s in slots)
    log(f"collect: {nframes} frames x {len(lay.systems)} slot(s) -> {kept} with a staff, "
        f"{rejected} rejected (intro/outro/black/no-staff)")
    if not kept:
        raise ScoreNotFound("no score frames detected")

    # ---- per slot: dedup + composite + render
    chromes: list[np.ndarray | None] = []
    for si, (y0, y1) in enumerate(lay.systems):
        if lay.polarity == "light_ink":
            span = (lay.staff_spans[si][0] - y0, lay.staff_spans[si][1] - y0)
            chromes.append(static_chrome(plates[si], span))
        else:
            chromes.append(None)

    slot_groups: list[list[list[SlotFrame]]] = []
    for si, sf in enumerate(slots):
        groups = group_lines(sf, dedup_thresh) if sf else []
        slot_groups.append(groups)
        log(f"dedupe: slot {si}: {len(sf)} frames -> {len(groups)} distinct systems")

    comps: list[list[np.ndarray]] = [[composite_line(g, lay.staff_gap) for g in gs]
                                     for gs in slot_groups]
    cores: list[list[np.ndarray]] = [[core_fingerprint(c, lay.staff_gap)
                                      for c in cs] for cs in comps]
    box_fps: list[list[np.ndarray]] = [[cv2.resize(c, (CORE_W, CORE_H),
                                                   interpolation=cv2.INTER_AREA)
                                        for c in cs] for cs in comps]
    times: list[list[float]] = [[g[0].t for g in gs] for gs in slot_groups]
    if os.environ.get("YTSCORE_DIAG"):
        import pickle
        for si, cs in enumerate(comps):
            if not cs:
                continue
            cv2.imwrite(str(outdir / f"{name}_cores_slot{si}.png"),
                        np.vstack([np.vstack([c, np.zeros((4, CORE_W), np.uint8)])
                                   for c in cores[si]]))
        with open(outdir / f"{name}_slots.pkl", "wb") as fh:
            pickle.dump({"cores": cores, "times": times, "gap": lay.staff_gap,
                         "strength": [[ink_strength(c) for c in cs] for cs in comps]}, fh)

    # "all" = every system of this slot, "tail" = only its final settled one.
    mode = ["all"] * len(slots)
    for i in tail_only:
        mode[i] = "tail"
    if not (use_slots or tail_slots):
        roll = detect_rolling(times, cores)
        if roll:
            cur, prev = roll
            mode = ["none"] * len(slots)
            mode[cur], mode[prev] = "all", "tail"
            log(f"rolling: slot {cur} is the current line, slot {prev} only previews it "
                f"-> slot {cur} in full + slot {prev}'s last system")

    candidates: list[Cand] = []
    for si, groups in enumerate(slot_groups):
        if not groups or mode[si] == "none":
            continue
        picks = list(range(len(groups)))
        if mode[si] == "tail":
            # A rolling display previews the next line in the preview slot, so
            # that slot repeats what the current slot shows a beat later --
            # except for the very last system, which the video ends on before it
            # ever rolls up. Take only that one, and only if it is a real system:
            # the actual last group is usually a handful of frames of the outro
            # fade, which medians into a ghosted copy of the line underneath it.
            floor = 0.35 * float(np.median([len(g) for g in groups]))
            solid = [i for i, g in enumerate(groups) if len(g) >= floor]
            picks = (solid or picks)[-1:]
            log(f"dedupe: slot {si} is tail-only, keeping its final settled system "
                f"({len(groups[picks[0]])} frames, from t={times[si][picks[0]]:.1f}s)")
        for i in picks:
            strip = render_strip(comps[si][i], lay.polarity, chromes[si])
            candidates.append(Cand(t=times[si][i], si=si, strip=strip,
                                   core=cores[si][i], box=box_fps[si][i],
                                   strength=ink_strength(comps[si][i])))

    # ---- order across slots, then drop the repeats a rolling display produces.
    # A rolling display shows each system twice: once in the lower slot as the
    # "next" line, then again in the upper slot as the current one. Sorting by
    # (start time, slot) puts the two copies next to each other, and the clean
    # copy -- the one with no playhead on it yet -- comes first.
    # With a single slot this pass can only ever destroy real systems that the
    # per-slot grouping already separated, so it is skipped.
    candidates.sort(key=lambda c: (c.t, c.si))
    prepared: list[Cand] = []
    for c in candidates:
        s = deskew(c.strip)
        s = autocrop(s)
        if lay.polarity == "dark_ink":
            s = flatten_paper(s)
        if s.shape[0] <= 8 or s.shape[1] <= 80:
            continue
        c.strip = s
        c.cov = float(staff_row_coverage(cv2.cvtColor(s, cv2.COLOR_BGR2GRAY), "dark_ink").max())
        prepared.append(c)
    if not prepared:
        raise ScoreNotFound("every candidate system was rejected as unusable")

    # Drop the systems that are mid-animation. Case 0 slides each line in with a
    # tilt, so the first and last groups are a skewed copy of a line that also
    # appears settled: their staff lines are not level, which shows up as a
    # collapsed staff-line coverage (0.22 against 0.90 for a settled system).
    covcut = 0.55 * float(np.median([c.cov for c in prepared]))
    settled = [c for c in prepared if c.cov >= covcut]
    unsettled = len(prepared) - len(settled)

    cross_slot = len(lay.systems) > 1
    kept_cands: list[Cand] = []
    keys: list[np.ndarray] = []
    dropped_dupes = 0
    for c in settled:
        k = signature(cv2.cvtColor(c.strip, cv2.COLOR_BGR2GRAY))
        if cross_slot and any(jaccard(k, prev) <= dedup_thresh for prev in keys[-4:]):
            dropped_dupes += 1
            continue
        kept_cands.append(c)
        keys.append(k)
    log(f"order: {len(candidates)} candidates -> {len(kept_cands)} systems "
        f"({unsettled} mid-animation, {dropped_dupes} cross-slot repeats dropped)")

    if os.environ.get("YTSCORE_DIAG"):
        import pickle
        with open(outdir / f"{name}_cands.pkl", "wb") as fh:
            pickle.dump([{"t": c.t, "si": c.si, "core": c.core, "strength": c.strength,
                          "cov": c.cov} for c in kept_cands], fh)

    kept_cands, faded = drop_fade_copies(kept_cands)
    cleaned: list[np.ndarray] = [c.strip for c in kept_cands]

    if drop_idx:
        bad = sorted({i for i in drop_idx if 0 <= i < len(cleaned)}, reverse=True)
        for i in bad:
            cleaned.pop(i)
        log(f"order: dropped systems {sorted(bad)} by hand -> {len(cleaned)} systems")

    report(0.88)
    title = unicodedata.normalize("NFC", title_override) if title_override \
        else video_title(url, name, meta)
    pdf = outdir / f"{name}.pdf"
    npages = build_pdf(cleaned, pdf, title)
    report(0.95)

    strip_dir = outdir / f"{name}_systems"
    strip_dir.mkdir(exist_ok=True)
    for old in strip_dir.glob("*.png"):
        old.unlink()
    bars = []
    for i, s in enumerate(cleaned):
        cv2.imwrite(str(strip_dir / f"{i:03d}.png"), s)
        bars.append(count_measures(s))

    elapsed = time.time() - t0
    result = {
        "customerId": CUSTOMER_ID, "url": url, "title": title,
        "polarity": lay.polarity, "slots": len(lay.systems),
        "system_boxes": lay.systems, "staff_gap": round(lay.staff_gap, 1),
        "sampled": nframes, "score_frames": kept, "rejected": rejected,
        "candidates": len(candidates), "unsettled_dropped": unsettled,
        "cross_slot_dupes": dropped_dupes, "slot_mode": mode,
        "fade_copies_dropped": faded,
        "systems": len(cleaned), "dropped_by_hand": sorted(drop_idx or []),
        "video_meta": meta, "duration_sec": round(duration, 1),
        "measures_per_system": bars,
        "measures_total": int(sum(bars)), "pages": npages,
        "pdf": str(pdf), "systems_dir": str(strip_dir),
        "elapsed_sec": round(elapsed, 1), "version": APP_VERSION,
    }
    (outdir / f"{name}.run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if not keep_video:
        # the customer's disk is not a video cache: a 4 minute 1080p video is
        # 20-40MB and they will run this dozens of times.
        for leftover in list(workdir.glob("video.*")) + list(workdir.glob("*.part")):
            try:
                leftover.unlink()
            except Exception:
                pass
    report(1.0)
    log(f"done in {elapsed:.1f}s -> {pdf}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="YouTube sheet-music video -> A4 PDF")
    ap.add_argument("url")
    ap.add_argument("--work", default="work")
    ap.add_argument("--out", default="out")
    ap.add_argument("--fps", type=float, default=4.0)
    ap.add_argument("--dedup-thresh", type=float, default=0.30)
    ap.add_argument("--proxy", default=None, help="socks5h:// proxy if YouTube blocks this IP")
    ap.add_argument("--name", default="score")
    ap.add_argument("--title", default=None,
                    help="header printed at the top of the PDF; defaults to the YouTube title")
    ap.add_argument("--sat-thresh", type=int, default=None,
                    help="saturation above which a pixel is treated as overlay chrome")
    ap.add_argument("--dump", action="store_true", help="write layout/plate debug images")
    ap.add_argument("--slots", default=None,
                    help="comma-separated slot indexes to keep, e.g. '0' for a rolling "
                         "two-line display where the lower slot only previews the next line")
    ap.add_argument("--band", default=None,
                    help="hand-set score band 'y0,y1', overriding automatic detection")
    ap.add_argument("--polarity", default=None, choices=["dark_ink", "light_ink"],
                    help="force the ink polarity instead of detecting it")
    ap.add_argument("--drop", default=None,
                    help="comma-separated indexes of ordered systems to remove before "
                         "assembly, for a fade-in frame the detector still calls a system")
    ap.add_argument("--tail-slots", default=None,
                    help="slots that contribute only their FINAL system, for the last "
                         "line of a rolling display that never rolls up")
    a = ap.parse_args()
    slots = [int(x) for x in a.slots.split(",")] if a.slots else None
    tails = [int(x) for x in a.tail_slots.split(",")] if a.tail_slots else None
    drops = [int(x) for x in a.drop.split(",")] if a.drop else None
    band = tuple(int(x) for x in a.band.split(",")) if a.band else None
    r = run(a.url, Path(a.work), Path(a.out), a.fps, a.dedup_thresh, a.proxy, a.name,
            a.title, a.sat_thresh, a.dump, slots, band, a.polarity, tails, drops)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
