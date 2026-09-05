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
from collections import Counter
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


class ScrollingScore(RuntimeError):
    """
    The score does not sit still: it is one continuous ribbon travelling
    sideways past a fixed playhead, so there are no discrete systems to lift.

    This tool composites each system out of the frames in which it is
    stationary, then dedups the repeats. A horizontally scrolling ribbon has no
    stationary frames at all, so the dedup INVERTS: neighbouring frames come out
    as different as frames ten seconds apart, every sampled frame survives as
    its own "system", and the result is a fat PDF of sliced-up nonsense that
    used to be reported as a success. Refusing loudly is the only honest answer
    until the horizontal-mosaic path exists.
    """


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


# --------------------------------------------------- 0. writing to the outdir

def imwrite(path: Path | str, img: np.ndarray) -> bool:
    """
    `cv2.imwrite`, but through the Python file layer.

    OpenCV opens the destination with a NARROW-char `fopen`, so on Windows the
    path it is handed (UTF-8 bytes, out of the Python binding) is re-interpreted
    in the process ANSI code page. An output folder outside that code page --
    `C:\\Users\\Administrator\\Desktop\\유튜브악보`, which is the folder this
    customer picked on 2026-09-05 -- therefore writes NOTHING and returns False,
    with no exception, and the next thing to open that file is what fails. That
    is what put `FileNotFoundError: No such file: ...\\_page_15624.png` on his
    screen after seven minutes of work, five runs running.

    Encoding in memory and letting `pathlib` write is exact, not approximate:
    `imencode` and `imwrite` share the encoder and the default parameters, so
    the bytes on disk are identical (checked byte-for-byte, colour and grey),
    and `Path.write_bytes` is wide-char on Windows.
    """
    p = Path(path)
    ok, buf = cv2.imencode(p.suffix or ".png", img)
    if not ok:
        return False
    p.write_bytes(buf.tobytes())
    return True


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
    # A cached video.mp4 is only a cache for the URL that produced it. The work
    # dir is reused across conversions, and a run that was cancelled, crashed or
    # killed leaves its video behind, so an unstamped reuse silently emitted the
    # PREVIOUS video's score under the NEW video's title. Stamp the source and
    # only reuse on an exact match; anything else is deleted, not reused.
    stamp = dest / "source.txt"
    try:
        cached_for = stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else ""
    except Exception:
        cached_for = ""
    if out.exists() and out.stat().st_size > 0:
        if cached_for == url:
            log(f"download: reusing {out} ({out.stat().st_size} bytes)")
            return out
        log(f"download: discarding a leftover video from {cached_for or 'an unknown url'}")
        for leftover in list(dest.glob("video.*")) + list(dest.glob("*.part")):
            try:
                leftover.unlink()
            except Exception:
                pass
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
    try:
        stamp.write_text(url, encoding="utf-8")
    except Exception:
        pass
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
    # -nostdin: ffmpeg reads the console for interactive keys by default, so a
    # pipeline driven from a shell loop has its own input silently eaten (the
    # acceptance batch script stopped after three videos for exactly this).
    cmd = [paths.ffmpeg(), "-v", "error", "-nostdin", "-i", str(video),
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
    # The rows actually SLICED out of each frame. `systems` stays the detection
    # layout (it is what the scroll band, the run report and the --slots picker
    # mean); `crops` may reach further out to cover a video that re-lays its
    # score out from screen to screen, and may overlap its neighbour. Everything
    # downstream that has to keep comparing the same pixels across videos is
    # offset by `crop_pad` instead of being re-measured in the wider box.
    crops: list[tuple[int, int]] = field(default_factory=list)
    crop_pad: list[tuple[int, int]] = field(default_factory=list)  # (top, bottom) grown

    def crop_boxes(self) -> list[tuple[int, int]]:
        return self.crops if self.crops else list(self.systems)


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


def frame_staff_clusters(prof: np.ndarray, h: int,
                         thresh: float = 0.40) -> tuple[list[list[int]], float]:
    """
    One frame's staff lines, grouped into systems. Returns (clusters, spacing).

    `thresh` is the share of the frame width a ridge has to cover. The default
    0.40 is what the layout pass uses and must not move. The vertical-scroll
    layout lowers it, because a score's FINAL system is often only two bars wide
    (H_uW2B5A1kE ends on a 2 bar tag covering 0.35 of the width) and would
    otherwise be dropped off the end of the PDF.
    """
    line_runs = [r for r in _runs(prof > thresh) if r[1] - r[0] <= max(6, h // 90)]
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


# Gates for deink_plate. Calibrated on the three light_ink videos we have; see
# that function's docstring for the measured numbers behind each one.
INK_PLATE_LEVEL = 235       # a plate value this bright kills the alpha outright
PLATE_CLEAN_MIN = 6         # frames of real background needed to replace it
PLATE_MIN_CHANGE = 60       # grey levels; below this the correction is noise
PLATE_BLOB_MIN_PX = 40      # saturated px a blob needs before it counts as a glyph
PLATE_BLOB_FRACTION = 0.25  # ... and the share of the blob they have to be

# A staff group whose own lines sit closer together than this share of the staff
# spacing is a beam row, not a staff. See the beam-row guard in analyse_layout;
# measured 0.93-1.00 for every real staff in the corpus, 0.25 for the beam row.
STAFF_PITCH_MIN_RATIO = 0.5

# Gates for the coloured-notehead rescue in normalise_ink. A saturated glyph
# small enough to be a notehead and DARK in luma is ink, not chrome.
CHROME_HEAD_MAX_GAPS = 1.8  # a rescued blob is at most this many staff spaces
CHROME_HEAD_MAX_LUMA = 140  # ... and darker than this (a highlight is brighter)
CHROME_HEAD_MIN_AREA = 0.15  # ... and at least this share of gap^2, so noise stays


def _odd(n: float, lo: int = 3) -> int:
    k = max(lo, int(round(n)))
    return k if k % 2 else k + 1


def plate_ink_blobs(plate: np.ndarray, gap: float,
                    lift: int = 60) -> tuple[np.ndarray, np.ndarray]:
    """
    Where a light-ink background plate has NOTATION baked into it, and what the
    background around that notation looks like.

    Returns (mask, envelope). `envelope` is a grey opening of the plate with a
    kernel wider than any glyph, i.e. the local dark background the notation is
    sitting on. `mask` is the notehead-sized bright blobs standing `lift` grey
    levels or more above it.

    Only BLOBS. A first opening with a kernel about half a staff space wide
    deletes every thin structure (staff lines are ~2px, barlines ~2px, stems
    3-4px, beams a few px thick) and keeps only things that are thick in both
    directions, which for a score means noteheads. Staff lines and barlines are
    deliberately left in the plate: the alpha recovery cancelling them and
    `static_chrome` putting them back is the calibrated behaviour of eleven
    verified videos, and this defect has nothing to do with it.
    """
    thick = cv2.morphologyEx(
        plate, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (_odd(0.45 * gap), _odd(0.45 * gap))))
    kb = _odd(2.4 * gap, 9)
    env = cv2.morphologyEx(plate, cv2.MORPH_OPEN,
                           cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kb, kb)))
    m = (thick.astype(np.int16) - env.astype(np.int16)) > lift
    # grow back over the anti-aliased rim the first opening ate
    m = cv2.dilate(m.astype(np.uint8),
                   cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                             (_odd(0.55 * gap), _odd(0.55 * gap)))) > 0
    return m, env


def deink_plate(stack: np.ndarray, plate: np.ndarray, gap: float, q: int) -> np.ndarray:
    """
    Take the notation back out of a light-ink background plate.

    The plate is meant to be "the value each pixel takes when NO notation is on
    it", and a flat low percentile only delivers that if notation covers a pixel
    for less than (100-q)% of the video. On a repetitive groove it does not.
    Measured on `G1J0ZLF8fI8` (the customer's 검정치마 Ling Ling chart): every
    system is the same four-bar backbeat, the engraver puts the bar-1 snare head
    on the SAME pixel every time, and that pixel is brighter than 200 in **96%**
    of the layout frames. So p5=253, p10=255, p15=255: no choice of q helps.
    `normalise_ink` then computes a = (255 - 255) / 30 = 0 and erases the inside
    of the head, leaving only the rim where the heads of different systems did
    not all overlap. That rim is the hollow ring / one-sided crescent the
    customer photographed, and it is produced by a SINGLE frame, before anything
    is composited: the misregistered-median theory is not what this is.

    Fixing it per pixel rather than per frame: at the flagged pixels, take the
    same percentile over only the frames whose value there is nearer the local
    background than the glyph. A pixel that is ink in 96% of frames still has
    4% of real background to measure, and the flagged set is a few thousand
    pixels, so this costs nothing.

    Three gates decide whether a flagged pixel is actually corrected, and they
    exist because case 3 forced them. That video is white ink over a STATIC
    studio shot, so the plate legitimately contains bright drum hardware, a
    plant pot and a poster, all of them blob-shaped. Correcting the raw blob
    mask moved 106,819 px of its score band by a median of 116 grey levels,
    which would have printed the drum kit onto the customer's page:

      * `peak >= 235`  - the plate is at the saturated ink level, which is the
        only value that actually kills the alpha: a = (255-255)/30 = 0. Case 3's
        background highlights sit at a median of 114.
      * `n_clean >= 6` - we must have SEEN the background at this pixel. A
        static bright object never goes dark, so it has nothing to correct with.
      * `change >= 60` - the correction has to be worth making.

    Together they take case 3 from 106,819 px down to 469 (0.09% of its band)
    while keeping all 1,761 px of G1J0ZLF8fI8's baked-in noteheads.

    A pixel that fails a gate keeps its plate value. There is deliberately no
    spatial fallback: substituting the local envelope wherever the plate looks
    bright is exactly the thing that would repaint case 3's studio onto the page.

    Deliberately narrow. Nothing outside the flagged blobs is touched, and the
    whole function is light_ink only, so the twelve dark_ink videos in the
    acceptance set cannot move.
    """
    m, env = plate_ink_blobs(plate, gap)
    if not m.any():
        return plate
    ys, xs = np.nonzero(m)
    cols = stack[:, ys, xs].astype(np.int16)            # (N, M)
    base = env[ys, xs].astype(np.int16)
    peak = plate[ys, xs].astype(np.int16)
    cut = base + np.maximum(((peak - base) * 0.35).astype(np.int16), 12)
    clean = cols < cut[None, :]
    n_clean = clean.sum(axis=0)
    srt = np.sort(np.where(clean, cols, 255), axis=0)   # dirty samples to the top
    idx = np.clip(((q / 100.0) * np.maximum(n_clean - 1, 0)).astype(np.int64),
                  0, cols.shape[0] - 1)
    val = np.take_along_axis(srt, idx[None, :], axis=0)[0]
    hot = (peak >= INK_PLATE_LEVEL) & (n_clean >= PLATE_CLEAN_MIN) \
        & ((peak - val) >= PLATE_MIN_CHANGE)
    # Decide per GLYPH, not per pixel. A notehead's anti-aliased rim sits at
    # 150-230 and fails the saturation gate, so a per-pixel decision hollows the
    # head out of the plate and leaves a bright outline that still erases the
    # edge of every note. A blob is accepted whole when enough of its own body
    # is saturated ink, which also keeps case 3 safe for free: its background
    # blobs are large and only a few hundred scattered pixels in them are ever
    # that bright, so the fraction test throws every one of them out.
    n_cc, lab = cv2.connectedComponents(m.astype(np.uint8), 8)
    comp = lab[ys, xs]
    tot = np.bincount(comp, minlength=n_cc).astype(np.float64)
    good = np.bincount(comp, weights=hot.astype(np.float64), minlength=n_cc)
    ok = (good >= PLATE_BLOB_MIN_PX) & (good >= PLATE_BLOB_FRACTION * np.maximum(tot, 1))
    take = ok[comp] & (n_clean >= PLATE_CLEAN_MIN) & (val < peak)
    if not take.any():
        log("analyse: plate carries no baked-in notation")
        return plate
    out = plate.copy()
    out[ys[take], xs[take]] = val[take].astype(np.uint8)
    log(f"analyse: plate de-inked, {int(ok.sum()) - int(ok[0])} glyph(s) / "
        f"{int(take.sum())} px of baked-in notation removed from {int(m.sum())} "
        f"flagged (median drop {float(np.median((peak - val)[take])):.0f} grey levels)")
    return out


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
    # How far apart this group's own lines sit. A staff's lines are one staff
    # space apart by definition, so this lands on `gap`; a beam row is two
    # ridges a few px apart and lands far below it. See the beam-row guard.
    pitches: list[float] = []
    for i in range(modal):
        cl = [c[i] for c, _ in usable]
        per = [float(np.median(np.diff(c))) for c in cl if len(c) >= 2]
        pitches.append(float(np.median(per)) if per else 0.0)
    log(f"analyse: modal layout = {modal} staff group(s) in {len(usable)}/{len(grays)} frames, "
        f"line spacing {gap:.1f}px, groups {spans} with {nlines} line(s)")

    # A staff has at least two lines. Where a real multi-line staff was found, a
    # LONE horizontal rule in the same frame is never a second system: it is the
    # beam row that sits above the notation (acceptance videos 9 and 10), the row
    # of accent beams inside the system (video 8) or the player's bottom border at
    # y = h-1 (videos 4 and 8). All four were being promoted to a system slot,
    # which then overlapped the real one, so every line was printed twice: once as
    # a fragment of beams with no staff, once properly. Videos whose score really
    # is a single-line rhythm staff have no multi-line group at all and are left
    # exactly as they were.
    if max(nlines) >= 2 and min(nlines) < 2:
        keep_multi = [i for i, n in enumerate(nlines) if n >= 2]
        log(f"analyse: dropping single-rule group(s) "
            f"{[spans[i] for i, n in enumerate(nlines) if n < 2]} next to a real "
            f"{max(nlines)}-line staff")
        spans = [spans[i] for i in keep_multi]
        nlines = [nlines[i] for i in keep_multi]
        pitches = [pitches[i] for i in keep_multi]

    # A BEAM ROW is not a staff, even though it has two lines.
    #
    # The customer's LtNIc3oinEs draws its eighth notes with beams that run most
    # of the width, and the ridge detector reads the beam and its shadow as a
    # 2-line "staff group" at rows 878..882 -- 4px apart, against this video's
    # real 16px staff spacing. That phantom group was promoted to its own slot,
    # which forced the overlap split at y=908 straight THROUGH the one real
    # system: the beams and accents above the staff were cut off (every eighth
    # note printed as a bare stem) and the measure number below it was sliced in
    # half. Nineteen of the phantom slot's twenty candidates were then dropped as
    # mid-animation, because a beam row has no staff coverage to speak of.
    #
    # A staff's lines are one staff space apart by construction, so its internal
    # pitch IS `gap`. Measured over all 21 cached videos (`src/diag_group_pitch.py`,
    # out/diag2/pitch.tsv): every group with 2+ lines that is a real staff sits at
    # ratio 0.93-1.00, and this beam row is the only thing in the corpus below it,
    # at 0.25. The cut at 0.5 sits in an empty gap, not on a knife edge.
    #
    # Single-rule groups keep their own guard above; a genuine one-line rhythm
    # staff has no internal pitch to measure and is not touched here.
    if len(spans) >= 2:
        real = [i for i, p in enumerate(pitches)
                if nlines[i] < 2 or p >= STAFF_PITCH_MIN_RATIO * gap]
        if real and len(real) < len(spans):
            log(f"analyse: dropping beam-row group(s) "
                f"{[spans[i] for i in range(len(spans)) if i not in real]} "
                f"(line pitch {[round(pitches[i], 1) for i in range(len(spans)) if i not in real]}px "
                f"against a {gap:.1f}px staff spacing)")
            spans = [spans[i] for i in real]
            nlines = [nlines[i] for i in real]
            pitches = [pitches[i] for i in real]

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

    # Two slots must never share rows. Where they do, the same notation lands in
    # both crops with a different framing, the cross-slot dedup (which compares
    # equal-size signatures) cannot see they are the same, and the system is
    # printed twice. Split the overlap on the midpoint between the two STAFFS,
    # which is where the eye separates them too.
    for i in range(len(boxes) - 1):
        (y0, y1), (n0, n1) = boxes[i], boxes[i + 1]
        if y1 <= n0:
            continue
        cut = (spans[i][1] + spans[i + 1][0]) // 2
        cut = int(min(max(cut, min(y1, n0)), max(y1, n0)))
        log(f"analyse: slots {i}/{i+1} overlapped ({y0},{y1})/({n0},{n1}) -> split at {cut}")
        boxes[i] = (y0, cut)
        boxes[i + 1] = (cut, n1)
    tall = [i for i, (a, b) in enumerate(boxes) if b - a >= max(8, int(round(3.0 * gap)))]
    if not tall:
        raise ScoreNotFound("staff detected but no usable system band around it")
    boxes = [boxes[i] for i in tall]
    spans = [spans[i] for i in tall]
    nlines = [nlines[i] for i in tall]

    # ---- how far the score actually MOVES between screens.
    # `spans` is one modal layout for the whole video, but a video is free to
    # re-lay its score out from screen to screen: the customer's zDG0Tw7MDXg
    # holds four systems at a time and repositions all four on every screen, so
    # its staffs sit at 350..429 / 542..612 / 718..804 / 911..996 across six
    # different layouts. A box cut for the modal phase then clips the measure
    # number off its own system at the top and swallows the beam row of the NEXT
    # system at the bottom, which is what put a sliced repeat of bar 57 on both
    # sides of a page break in the delivered PDF.
    #
    # So the box is only the DETECTION window; the rows actually sliced out are
    # grown to cover every phase the video was seen in. Overlapping a neighbour
    # is fine and expected here, because `trim_system` cuts each finished
    # composite back to its own system at the measured ink-free line.
    # Only a TILED layout is grown. A single-slot video has no neighbouring
    # system in the frame to straddle into, its box is already cut generously
    # around the one staff, and the same measurement on acceptance video 6 (whose
    # score fades out and whose "staff" detections wander onto the video image
    # behind it) grew the slice by 68px of city skyline, diluted the staff
    # coverage every strip is judged on, and let eleven fade ghosts through.
    crops: list[tuple[int, int]] = list(boxes)
    crop_pad: list[tuple[int, int]] = [(0, 0)] * len(boxes)
    pitch_all = float(np.median(np.diff([s[0] for s in spans]))) if len(spans) >= 2 else 0.0
    grow_cap = int(round(0.5 * pitch_all))
    for i, ((y0, y1), (s0, s1)) in enumerate(zip(boxes, spans) if len(spans) >= 2 else []):
        mid, half = (s0 + s1) / 2.0, (pitch_all / 2.0 if pitch_all > 0 else 4.0 * gap)
        tops = [c[0] for cl, _ in per_frame for c in cl
                if len(c) >= 2 and abs((c[0] + c[-1]) / 2.0 - mid) < half]
        bots = [c[-1] for cl, _ in per_frame for c in cl
                if len(c) >= 2 and abs((c[0] + c[-1]) / 2.0 - mid) < half]
        if len(tops) >= 8:
            lo = int(round(float(np.percentile(tops, 2))))
            hi = int(round(float(np.percentile(bots, 98))))
        else:
            lo, hi = s0, s1
        # A staff that WANDERS further than most of a pitch is not a page that
        # was re-laid-out into a few phases, it is a page in motion, and the
        # vertical-scroll path re-reads the video for those anyway. Growing the
        # crop to cover the whole sweep would only cost memory: on H_uW2B5A1kE
        # it took the slice from 215px to 395px per slot, and the frames are
        # held in RAM on the customer's own PC.
        sweep = float(np.percentile(tops, 98) - np.percentile(tops, 2)) \
            if len(tops) >= 8 else 0.0
        if pitch_all > 0 and sweep > 0.75 * pitch_all:
            log(f"analyse: slot {i} staff sweeps {sweep:.0f}px of a {pitch_all:.0f}px "
                f"pitch: a moving page, not a re-layout -> crop left at the box")
            lo, hi = s0, s1
        up_grow = int(min(max(s0 - lo, 0), grow_cap))
        dn_grow = int(min(max(hi - s1, 0), grow_cap))
        c0, c1 = max(0, y0 - up_grow), min(h, y1 + dn_grow)
        crops[i] = (c0, c1)
        crop_pad[i] = (y0 - c0, c1 - y1)
        if up_grow or dn_grow:
            log(f"analyse: slot {i} staff moves {lo}..{hi} across screens "
                f"(modal {s0}..{s1}) -> crop {c0}..{c1}, box {y0}..{y1}")

    if force_band is not None:                    # hand-set band overrides detection
        by0, by1 = force_band
        boxes = [(by0, by1)]
        crops = [(by0, by1)]
        crop_pad = [(0, 0)]
        spans = [max(spans, key=lambda s: (by0 <= (s[0] + s[1]) // 2 < by1, -abs(s[0] - by0)))]
        nlines = nlines[:1]
        log(f"analyse: band overridden by hand -> {boxes}")
    log(f"analyse: {len(boxes)} system slot(s) {boxes}")

    # background plate for the translucent/inverted case: the value each pixel
    # takes when NO notation is on it. Low percentile for white ink, high for
    # black ink.
    q = 15 if polarity == "light_ink" else 85
    plate = np.percentile(stack, q, axis=0).astype(np.uint8)
    if polarity == "light_ink":
        plate = deink_plate(stack, plate, gap, q)
    med = np.median(stack, axis=0).astype(np.uint8)

    staff_rows = []
    for (s0, s1), n in zip(spans, nlines):
        staff_rows.append([int(round(s0 + (s1 - s0) * k / max(n - 1, 1))) for k in range(max(n, 1))])

    lay = Layout(polarity=polarity, width=w, height=h, staff_gap=gap, systems=boxes,
                 staff_spans=spans, staff_rows=staff_rows,
                 plate=plate, ridge_ref=0.0, crops=crops, crop_pad=crop_pad)

    if dump is not None:
        dump.mkdir(parents=True, exist_ok=True)
        vis = cv2.cvtColor(med, cv2.COLOR_GRAY2BGR)
        for (y0, y1), (s0, s1) in zip(boxes, spans):
            cv2.rectangle(vis, (2, y0), (w - 3, y1 - 1), (0, 0, 255), 3)
            cv2.rectangle(vis, (2, s0), (w - 3, s1), (255, 0, 0), 1)
        imwrite(dump / "layout.png", vis)
        imwrite(dump / "plate.png", plate)
        log(f"analyse: dumped {dump/'layout.png'}")
    return lay


# ------------------------------------------------------------- 4/5. per-slot

@dataclass
class SlotFrame:
    t: float
    gray: np.ndarray        # polarity-normalised: ink is always DARK here
    key: np.ndarray         # binary signature for dedup
    fp: np.ndarray          # grey fingerprint, same downscale without the threshold
    hkey: np.ndarray | None = None   # the header band (measure number, marker) on its own
    top: int = -1           # row of this frame's staff, for composite alignment
    travelling: bool = False    # the page was sliding past when this frame was taken
    phx: float = -1.0       # playhead column across the staff, 0..1 (-1 = none seen)


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


PLAY_TALL = 0.80        # a playhead covers this share of the staff's height
PLAY_BACK = 0.35        # ...and a jump back this far across it is the next system
PLAY_SEEN = 0.80        # only trust the track when this share of frames carry one


def playhead_x(bgr: np.ndarray, sat_thresh: int, polarity: str) -> float:
    """
    Where the playhead is across the staff, 0..1, or -1 when none is visible.

    `bgr` must be cropped to the STAFF ROWS, not to the slot box. On
    `d3t9j6DObN0` the box below the staff carries a dark red photograph that is
    saturated over its whole width, so measured over the full box the chrome
    centroid sits at a constant 0.41 and the playhead is invisible; measured
    over the staff rows alone the same frames give a clean sawtooth, 0.05 to
    0.88 and back, stepping +0.029 per sample.

    Only columns the chrome covers TOP TO BOTTOM count. That is what separates a
    playhead from everything else saturated in the band: a coloured notehead is
    a fifth of the staff's height, a highlighted lyric sits under it, and a
    coloured background covers the staff lines but not the gaps between them.
    """
    m = playhead_mask(bgr, sat_thresh, 140 if polarity == "dark_ink" else 0)
    if not m.any():
        return -1.0
    h, w = m.shape
    tall = m.sum(axis=0) >= PLAY_TALL * h
    if not tall.any() or w == 0:
        return -1.0
    return float(np.flatnonzero(tall).mean()) / w


def playhead_resets(frames: list[SlotFrame]) -> set[int]:
    """
    The frame indices at which the playhead jumped back to the left margin.

    This is the axis picture similarity cannot have. `group_lines` asks what a
    frame LOOKS like, and three of the customer's videos defeat that on purpose
    by engraving the same groove for four or five systems running: on
    `d3t9j6DObN0` the five screens from t=151.8s to t=191.5s collapse into ONE
    group of 160 frames (every other content group on that video holds exactly
    32), so four whole systems never became candidates and were silently missing
    from his PDF. Their distances against the running anchor are 0.19-0.28
    binary and 0.21-0.37 graded, against a 0.30 cut that has to stay where it is
    (below it, sample case 0's cymbal splits one system into nine).

    A playhead does not care what the notation looks like. It sweeps left to
    right across the system that is sounding and jumps back to the left margin
    when the next one starts, so it separates two identical systems as easily as
    two different ones. Measured on that same window: +0.029 per sample forward,
    -0.89 at each of the eight resets. There is nothing in between.

    The track is only trusted when the playhead is actually visible in
    PLAY_SEEN of the slot's frames. A video with no playhead, or one whose
    highlight is too pale to see (`KsSlNq-ciko` draws a light blue box at
    saturation 30-40, under the pipeline's 50 cut), gets an empty set and the
    picture rule decides alone, exactly as before.
    """
    if not frames:
        return set()
    seen = sum(1 for f in frames if f.phx >= 0.0)
    if seen < PLAY_SEEN * len(frames):
        return set()
    out: set[int] = set()
    prev = -1.0
    for i, f in enumerate(frames):
        if f.phx < 0.0:
            continue
        if prev >= 0.0 and f.phx - prev <= -PLAY_BACK:
            out.add(i)
        prev = f.phx
    return out


def coloured_heads(bgr: np.ndarray, chrome: np.ndarray, gap: float) -> np.ndarray:
    """
    The pixels of `chrome` that are actually COLOURED NOTATION, not overlay chrome.

    The customer's umZcjiNpEOw prints its crash cymbals as red X noteheads. Red
    is the worst case for the brightness gate in `playhead_mask`, because HSV V
    is max(B,G,R): a red head measures V=196 and passes "bright chrome" while its
    LUMA is only 96, i.e. it is plainly dark ink on white paper. 77-84% of every
    red head was being whitened away, so the PDF printed a pair of dash
    fragments where the X should be. That is the "coloured noteheads are not
    recognised" the customer reported.

    Two properties separate a coloured head from a translucent playhead, and both
    are needed (measured per connected component over the cached corpus):

    * SIZE. A head is at most a couple of staff spaces across. The real
      playheads in the corpus are nothing like it: a01 is a 6x164 column, case3's
      saturated background runs 626x94.
    * LUMA. A translucent highlight LIGHTENS the paper it covers, so it stays
      bright (a01's playhead measures luma 146-164). Ink is dark whatever its
      hue (the red heads measure 78-104).

    Deciding per connected component rather than per pixel is what keeps a head
    whole: its anti-aliased rim is neither as dark nor as saturated as its core,
    and a per-pixel rule hollows the head out exactly like the 1.4.0 plate bug.
    """
    n, lab, stats, _ = cv2.connectedComponentsWithStats(chrome.astype(np.uint8), 8)
    if n <= 1:
        return np.zeros_like(chrome)
    luma = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    lim = max(4.0, CHROME_HEAD_MAX_GAPS * gap)
    min_area = max(8.0, CHROME_HEAD_MIN_AREA * gap * gap)
    keep = np.zeros(n, bool)
    for i in range(1, n):
        w = stats[i, cv2.CC_STAT_WIDTH]
        h = stats[i, cv2.CC_STAT_HEIGHT]
        if w > lim or h > lim or stats[i, cv2.CC_STAT_AREA] < min_area:
            continue
        keep[i] = float(np.median(luma[lab == i])) < CHROME_HEAD_MAX_LUMA
    return keep[lab]


def normalise_ink(bgr: np.ndarray, plate: np.ndarray, polarity: str,
                  sat_thresh: int, gap: float = 0.0) -> np.ndarray:
    """
    Turn one cropped system into an image where the ink is dark and the paper light,
    whatever the source looked like.

    dark_ink : the band really is paper. Whiten the saturated playhead and keep
               the grey levels; the group median downstream restores what the
               playhead covered. Coloured NOTEHEADS are spared (see
               `coloured_heads`); they are saturated too, but they are ink.
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
        if gap > 0 and sat.any():
            sat = sat & ~coloured_heads(bgr, sat, gap)
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


# ---------------------------------------------------------------------------
# Horizontal-scroll detection. See ScrollingScore.
# ---------------------------------------------------------------------------

def column_ink(gray: np.ndarray) -> np.ndarray:
    """
    Per-column ink mass of a polarity-normalised (ink dark) crop.

    A 1-D signature is enough and is what makes the guard affordable: sideways
    travel is a pure translation of this profile, and collapsing the rows throws
    away exactly the dimension the motion does not use.
    """
    ink = np.clip(200.0 - gray.astype(np.float32), 0.0, None)
    return ink.sum(axis=0)


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    """Normalised cross-correlation of two equal-length 1-D windows."""
    if a.size < 16:
        return 0.0
    x = a - a.mean()
    y = b - b.mean()
    d = float(np.sqrt(float(x @ x) * float(y @ y)))
    return float(x @ y) / d if d > 1e-6 else 0.0


def row_ink(gray: np.ndarray) -> np.ndarray:
    """
    Per-ROW ink mass of a polarity-normalised (ink dark) crop.

    The vertical twin of `column_ink`. A score that scrolls up is a pure
    vertical translation of this profile, whose peaks are the staff lines, the
    beam rows and the lyric rows: a strongly structured 1-D signal that
    registers to a pixel.
    """
    ink = np.clip(200.0 - gray.astype(np.float32), 0.0, None)
    return ink.sum(axis=1)


def best_shift(a: np.ndarray, b: np.ndarray, max_shift: int,
               cap_div: int = 3) -> tuple[int, float, float]:
    """
    The integer shift that best takes profile `a` onto profile `b`.

    Returns (shift, ncc at that shift, ncc at zero shift). Positive means the
    content moved to the RIGHT (or DOWN, for a row profile) between the two
    frames. Correlation is computed on the overlap only and normalised there,
    so a large shift is not rewarded for having fewer samples to disagree on.

    `cap_div` bounds the search to `len/cap_div`, i.e. it fixes the minimum
    overlap the two windows must retain. The horizontal guard keeps the default
    3; the vertical tracker relaxes it, because one discrete scroll step can
    move the band by more than a third of its own height in a single frame gap.
    """
    w = int(min(a.size, b.size))
    m = int(max(1, min(max_shift, w // max(2, cap_div))))
    best, bestc = 0, -2.0
    zero = _ncc(a[:w], b[:w])
    for s in range(-m, m + 1):
        if s >= 0:
            c = _ncc(a[: w - s], b[s:w])
        else:
            c = _ncc(a[-s:w], b[: w + s])
        if c > bestc:
            best, bestc = s, c
    return best, bestc, zero


# A pair whose best correlation is below this is two frames that simply do not
# match at any shift (a cut, a fade, a blank), and its "shift" is noise.
SCROLL_MIN_NCC = 0.55
# Below this the frame pair is treated as stationary. One pixel of jitter at
# 1080p is compression noise, not travel.
SCROLL_STILL_PX = 2


def scroll_metrics(profiles: list[np.ndarray], fps: float,
                   max_pairs: int = 240) -> dict:
    """
    How much the content of one slot travels sideways, per consecutive pair of
    sampled frames. Everything the guard decides on is in here.

        px_per_s          median travel of the matched pairs, in px per second
        sign_consistency  share of the moving pairs that travel the same way
        still_frac        share of matched pairs that did not move at all
        matched           pairs whose best correlation cleared SCROLL_MIN_NCC
    """
    n = len(profiles)
    out = {"pairs": 0, "matched": 0, "px_per_s": 0.0, "sign_consistency": 0.0,
           "still_frac": 1.0, "median_shift_px": 0.0}
    if n < 8:
        return out
    idx = list(range(n - 1))
    if len(idx) > max_pairs:                      # sample evenly over the video
        step = len(idx) / float(max_pairs)
        idx = [idx[int(i * step)] for i in range(max_pairs)]
    width = int(profiles[0].size)
    max_shift = int(max(8, min(0.15 * width, 200)))
    shifts: list[int] = []
    for i in idx:
        s, c, _z = best_shift(profiles[i], profiles[i + 1], max_shift)
        if c >= SCROLL_MIN_NCC:
            shifts.append(s)
    out["pairs"] = len(idx)
    out["matched"] = len(shifts)
    if len(shifts) < 8:
        return out
    arr = np.asarray(shifts, dtype=np.float32)
    moving = arr[np.abs(arr) > SCROLL_STILL_PX]
    out["still_frac"] = float(np.mean(np.abs(arr) <= SCROLL_STILL_PX))
    out["median_shift_px"] = float(np.median(np.abs(arr)))
    out["px_per_s"] = float(np.median(np.abs(arr)) * fps)
    if moving.size:
        same = max(float(np.mean(moving > 0)), float(np.mean(moving < 0)))
        out["sign_consistency"] = same
    return out


# The trip conditions, all of which must hold. Calibrated on the ten acceptance
# videos plus the customer's scrolling one; the measured numbers are in NOTES.md.
SCROLL_MIN_PX_PER_S = 12.0      # a whole staff space per second, sustained
SCROLL_MAX_STILL_FRAC = 0.30    # a real score is stationary most of the time
SCROLL_MIN_SIGN_CONSISTENCY = 0.85
# Systems per minute of video, above which the run is printing the same music
# more than once. Measured: 5.1-9.6 on the fourteen verified videos, 19.0 on the
# vertically scrolling one before it was registered properly.
SANITY_MAX_PER_MIN = 16.0


def is_scrolling(m: dict) -> bool:
    """A slot travels sideways continuously, in one direction, and rarely rests."""
    return (m["matched"] >= 20
            and m["px_per_s"] >= SCROLL_MIN_PX_PER_S
            and m["still_frac"] <= SCROLL_MAX_STILL_FRAC
            and m["sign_consistency"] >= SCROLL_MIN_SIGN_CONSISTENCY)


# ------------------------------------------------------- vertical scroll track
#
# A score that jumps UP the screen in discrete steps is a completely different
# animal from the horizontal ribbon `ScrollingScore` refuses. It is stationary
# almost all of the time (which is why the horizontal guard correctly measures
# 0.0 px/s and leaves it alone), and every system IS wholly on screen for
# several seconds at a time. Nothing has to be stitched together out of partial
# views: the frames only have to be put back into the PAGE's own coordinate
# system before anything is measured, cropped or deduped.
#
# What went wrong before this existed: `analyse_layout` reduces the per-frame
# staff detections to a MODAL layout, which on a page that keeps moving is an
# average of two or three different scroll phases. On the customer's
# H_uW2B5A1kE it settled on three slots of pitch 215-221px against a real
# 247px system pitch, so every crop straddled (one staff plus the lyric row plus
# a sliver of the next system's beams) and, worse, the same system landed at a
# DIFFERENT vertical offset in each slot, so its signature never matched its own
# earlier copy and the dedup could not collapse it. 91 candidates, 1 cross-slot
# duplicate found, 75 systems and 8 pages printed for a 25 system chart, and
# `result=OK`.
#
# The fix is registration, not stitching:
#
#   1. `vscroll_segments`  the page is stationary except during short jumps, so
#      cut the frame sequence into stable segments at the moving pairs.
#   2. `vscroll_register`  register each stable segment against the next on the
#      MEDIAN row-ink profile of the whole segment, which is far cleaner than
#      any single frame pair, and keep the longest chain of confident links.
#      That chain is the score; intro cards and outros fall off it.
#   3. `vscroll_content_band`  the rows that actually travel. Static chrome (the
#      channel logo, the title bar) has to be excluded before anything is
#      cropped, or it prints as a black bar across the top of page 1.
#   4. `vscroll_layout`  map every segment's staff detections into content rows
#      (content_row = screen_row + offset) and cluster there. On a page standing
#      still the same system lands on the same content row from every segment
#      that saw it, so this yields the real system list and the real pitch,
#      including the pitch VARIATION that a single modal layout cannot express.
#   5. `vscroll_extract`  crop each system out of the one segment where it sits
#      wholly inside the moving band, from that segment's median frame.
#
# Each system is therefore lifted whole, from frames in which it is stationary,
# exactly once. There is no dedup step to get wrong.

# A frame pair whose best correlation is below this did not match at any shift
# (a cut, a fade, a blank); it ends the current stable segment.
VSCROLL_MIN_NCC = 0.45
# Segment-to-segment: the registration a content offset is built from. Held much
# higher than the per-pair threshold because one wrong link shifts every later
# system by a wrong constant. Real links on H_uW2B5A1kE measure 0.92-1.00.
VSCROLL_LINK_NCC = 0.75
# Under this many pixels a pair is stationary: a pixel or two of jitter at 1080p
# is compression noise, not a scroll.
VSCROLL_STILL_PX = 3
# What it takes to call a video a vertically scrolling one at all.
VSCROLL_MIN_STEP_PX = 24        # a step smaller than a staff is not a scroll
VSCROLL_MIN_EVENTS = 4          # a couple of jolts is an animation, not a scroll
VSCROLL_MIN_ONE_WAY = 0.80      # a page turns one way
VSCROLL_MIN_TRAVEL_BANDS = 1.5  # total travel, in heights of the score band
# Share of the frame width a ridge must cover to count as a staff line when the
# page layout is read. Lower than the layout pass's 0.40 on purpose: a final
# two-bar tag is a short staff, and the count is stable anywhere from 0.30 down
# to 0.20 on H_uW2B5A1kE (23 systems at 0.40, 24 at 0.30 and below).
VSCROLL_STAFF_COVER = 0.28


def vscroll_pair_shifts(prof: np.ndarray) -> list[int | None]:
    """
    Vertical shift between every consecutive pair of frames, or None where the
    two frames do not correlate at any shift.

    `prof` is (frames, rows): the row-ink profile of the score band per frame.
    """
    n = len(prof)
    if n < 2:
        return []
    h = int(prof.shape[1])
    max_shift = int(max(8, 0.45 * h))
    out: list[int | None] = []
    for i in range(n - 1):
        s, c, _z = best_shift(prof[i], prof[i + 1], max_shift, cap_div=2)
        out.append(s if c >= VSCROLL_MIN_NCC else None)
    return out


def vscroll_segments(shifts: list[int | None], min_len: int = 3) -> list[tuple[int, int]]:
    """
    Maximal runs of frames with no movement between them, as inclusive index
    pairs. Frames caught mid-jump end up in runs shorter than `min_len` and are
    dropped: they are motion-blurred or half-scrolled and belong to no page
    position at all.
    """
    segs: list[tuple[int, int]] = []
    start = 0
    for i, s in enumerate(shifts):
        if s is None or abs(s) > VSCROLL_STILL_PX:
            if i - start + 1 >= min_len:
                segs.append((start, i))
            start = i + 1
    if len(shifts) - start + 1 >= min_len:
        segs.append((start, len(shifts)))
    return segs


def vscroll_register(meds: list[np.ndarray], band: tuple[int, int]) -> list[tuple[int, float]]:
    """
    Shift and correlation between each stable segment's median row profile and
    the next one's, measured over `band`.

    The search window is deliberately wide (cap_div=2, i.e. half the band): this
    display jumps one, two or three of its 123px quanta at a time, and a 370px
    jump measured through a window that only reaches 210px comes back as a
    confident-looking piece of nonsense (-145px at ncc 0.44, against +370px at
    ncc 1.00 once the window is wide enough).
    """
    b0, b1 = band
    h = b1 - b0
    max_shift = int(max(8, 0.45 * h))
    out: list[tuple[int, float]] = []
    for k in range(len(meds) - 1):
        s, c, _z = best_shift(meds[k][b0:b1], meds[k + 1][b0:b1], max_shift, cap_div=2)
        out.append((s, c))
    return out


def vscroll_chain(links: list[tuple[int, float]]) -> tuple[int, int]:
    """Longest run of segments joined by confident links, as (first, last)."""
    if not links:
        return (0, 0)
    best = (0, 0)
    k = 0
    while k <= len(links):
        j = k
        while j < len(links) and links[j][1] >= VSCROLL_LINK_NCC:
            j += 1
        if j - k > best[1] - best[0]:
            best = (k, j)
        k = j + 1
    return best


def vscroll_content_band(meds: list[np.ndarray], gap: float,
                         fallback: tuple[int, int]) -> tuple[int, int]:
    """
    The rows of the frame that the page actually scrolls through.

    Static chrome (a channel logo, a fixed title bar, a player border) is ink
    that never changes from one page position to the next; scrolling notation
    changes completely. Both are separated by comparing, per row, the ink mass
    against how much that ink mass moved between segments, which needs no
    threshold tuned to a particular video: notation rows measure a change of
    0.9-2.1x their own ink, chrome rows 0.03-0.06x.

    Rows with no ink at all (the margin between two systems) are NOT evidence of
    chrome, so the classification is closed over gaps of a few staff spaces
    before the largest run is taken.
    """
    if len(meds) < 3:
        return fallback
    M = np.asarray(meds, dtype=np.float32)
    ink = M.mean(axis=0)
    change = np.abs(np.diff(M, axis=0)).mean(axis=0)
    lvl = float(np.percentile(ink, 85))
    if lvl <= 0:
        return fallback
    raw = ((change > 0.5 * ink) & (ink > 0.10 * lvl)).astype(np.float32)
    k = max(9, int(round(1.5 * gap))) | 1
    sm = np.convolve(raw, np.ones(k, np.float32) / k, mode="same") > 0.4
    close = max(9, int(round(3.0 * gap))) | 1
    sm = cv2.morphologyEx(sm.astype(np.uint8).reshape(-1, 1), cv2.MORPH_CLOSE,
                          np.ones((close, 1), np.uint8)).ravel().astype(bool)
    runs = _runs(sm)
    if not runs:
        return fallback
    r0, r1 = max(runs, key=lambda r: r[1] - r[0])
    if r1 - r0 < (fallback[1] - fallback[0]) * 0.5:
        return fallback
    return (int(r0), int(r1))


def vscroll_track(rows: np.ndarray, fps: float, band: tuple[int, int],
                  gap: float) -> dict:
    """
    Everything about the page's vertical motion, in one pass over the row-ink
    profiles collected during the main frame pass.

        scrolling   the page really does scroll, so use the scroll-aware path
        segments    stable (first, last) frame index pairs
        chain       (first, last) SEGMENT index of the longest registered run
        offsets     content_row = screen_row + offsets[k], per chain segment
        band        the rows the page scrolls through, chrome excluded
        step_px     median size of one jump
        travel_px   total travel over the registered chain
    """
    b0, b1 = band
    out = {"scrolling": False, "segments": [], "chain": [0, 0], "offsets": {},
           "band": [int(b0), int(b1)], "events": 0, "step_px": 0.0,
           "travel_px": 0.0, "one_way": 0.0, "frames": int(len(rows)),
           "links": 0, "px_per_s": 0.0}
    if len(rows) < 24:
        return out
    prof = np.asarray(rows, dtype=np.float32)
    segs = vscroll_segments(vscroll_pair_shifts(prof[:, b0:b1]))
    out["segments"] = [[int(a), int(b)] for a, b in segs]
    if len(segs) < VSCROLL_MIN_EVENTS + 1:
        return out

    meds = [np.median(prof[a:b + 1], axis=0) for a, b in segs]
    # a first, coarse chain purely to pick the segments the content band is
    # measured over: the band needs a registration and the registration wants
    # the band, so the known-good analysis band breaks the circle.
    coarse = vscroll_chain(vscroll_register(meds, (b0, b1)))
    ref = meds[coarse[0]:coarse[1] + 1] if coarse[1] > coarse[0] else meds
    cband = vscroll_content_band(ref, gap, (b0, b1))
    links = vscroll_register(meds, cband)
    k0, k1 = vscroll_chain(links)
    out["band"] = [int(cband[0]), int(cband[1])]
    out["chain"] = [int(k0), int(k1)]
    out["links"] = int(k1 - k0)
    if k1 - k0 < VSCROLL_MIN_EVENTS:
        return out

    offs: dict[int, float] = {}
    o = 0.0
    steps: list[int] = []
    for k in range(k0, k1 + 1):
        offs[k] = o
        if k < k1:
            s = links[k][0]
            steps.append(s)
            o -= s
    out["offsets"] = {int(k): float(v) for k, v in offs.items()}
    arr = np.asarray(steps, dtype=np.float32)
    moving = arr[np.abs(arr) > VSCROLL_STILL_PX]
    out["events"] = int(moving.size)
    out["step_px"] = float(np.median(np.abs(moving))) if moving.size else 0.0
    out["travel_px"] = float(np.abs(moving).sum()) if moving.size else 0.0
    if moving.size:
        out["one_way"] = max(float(np.mean(moving > 0)), float(np.mean(moving < 0)))
    span = max(1e-6, float(cband[1] - cband[0]))
    out["px_per_s"] = float(out["travel_px"] / max(len(rows) - 1, 1) * fps)
    out["travel_bands"] = round(out["travel_px"] / span, 2)
    out["scrolling"] = is_vscrolling(out)
    return out


def is_vscrolling(tr: dict) -> bool:
    """
    The page moved vertically, in one direction, in real steps, far enough that
    its own coordinate system has to be recovered before anything else is true.

    This is NOT the mirror of `is_scrolling`: it does not refuse a video, it
    switches onto the path that converts it properly, so it is allowed to fire
    on a video the ordinary path would also have survived. What it must never do
    is fire on a stationary score. The ten acceptance videos plus the four
    sample cases all measure 0 events and 0 travel (see NOTES.md), so nothing
    but a genuine scroller comes anywhere near these numbers.
    """
    return (tr["events"] >= VSCROLL_MIN_EVENTS
            and tr["step_px"] >= VSCROLL_MIN_STEP_PX
            and tr["one_way"] >= VSCROLL_MIN_ONE_WAY
            and tr.get("travel_bands", 0.0) >= VSCROLL_MIN_TRAVEL_BANDS)


def regular_staff(lines: list[int], gap: float) -> list[int]:
    """
    The longest run of evenly spaced lines in one detected cluster.

    `frame_staff_clusters` groups anything within three staff spaces, which on
    this video swallows the beam rule that sits exactly 3 x gap above the top
    staff line of some systems. The cluster's first line is then 48px too high
    on those frames and 0px too high on the others, so the same system appears
    to sit at two different content rows. Keeping only the evenly spaced run
    removes the beam rule and leaves the staff.
    """
    if len(lines) < 2:
        return list(lines)
    runs: list[list[int]] = [[lines[0]]]
    for c in lines[1:]:
        if c - runs[-1][-1] <= 1.6 * gap:
            runs[-1].append(c)
        else:
            runs.append([c])
    return max(runs, key=len)


def vscroll_layout(covs: dict[int, np.ndarray], offsets: dict[int, float],
                   band: tuple[int, int], gap: float,
                   height: int) -> list[tuple[float, float]]:
    """
    Every system of the page, as (staff top, staff bottom) in CONTENT rows.

    Each stable segment contributes the staffs it can see; they are mapped into
    content rows and clustered there. A system seen from three segments produces
    one entry, not three: that is the whole point.

    Three things about the clustering are measured, not chosen:

    * a staff CLIPPED by the edge of the moving band shows fewer lines and a
      wrong end, and one such detection drags the median top of its system 30px
      off. Detections within a staff space of either band edge are dropped.
    * a DENSE system loses lines: noteheads and beams sit on the staff and the
      ridge coverage of the covered lines falls under the threshold, so bar 70
      of H_uW2B5A1kE comes back as a 3 line staff from all three segments that
      see it. Grouping therefore tolerates ends that disagree by up to three
      staff spaces (real systems are 192px apart at their tightest, so nothing
      is ever merged that should not be), and the group's span is taken from its
      best-resolved members only.
    * a span shorter than the video's usual staff is a partial detection whose
      missing lines could be at either end, so it is re-centred on the modal
      staff height rather than trusted as measured.
    """
    b0, b1 = band
    seen: list[tuple[float, float, int]] = []
    for k, cov in covs.items():
        off = offsets[k]
        clusters, _g = frame_staff_clusters(cov, height, VSCROLL_STAFF_COVER)
        for cl in clusters:
            cl = regular_staff(cl, gap)
            if len(cl) < 3 or cl[0] < b0 + gap or cl[-1] >= b1 - gap:
                continue
            seen.append((cl[0] + off, cl[-1] + off, len(cl)))
    if not seen:
        return []
    seen.sort(key=lambda s: s[1])
    groups: list[list[tuple[float, float, int]]] = [[seen[0]]]
    for s in seen[1:]:
        if s[1] - groups[-1][-1][1] > 3.0 * gap:
            groups.append([s])
        else:
            groups[-1].append(s)
    spans: list[tuple[float, float]] = []
    for G in groups:
        best = max(g[2] for g in G)
        top = float(np.median([g[0] for g in G if g[2] == best]))
        bot = float(np.median([g[1] for g in G if g[2] == best]))
        spans.append((top, bot))
    full = float(np.median([b - a for a, b in spans])) if spans else 0.0
    out: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if full > 0 and (b - a) < 0.75 * full:
            mid = (a + b) / 2.0
            a, b = mid - full / 2.0, mid + full / 2.0
        out.append((a, b))
    return out


def vscroll_boxes(spans: list[tuple[float, float]], gap: float,
                  page: np.ndarray, origin: int) -> list[tuple[float, float]]:
    """
    One crop box per system, cut at the ink-free line between neighbours.

    `page` is the page's own row-ink profile in content rows (`origin` is the
    content row of `page[0]`), so the boundary between two systems does not have
    to be guessed at all: it is the last row of blank paper before the next
    system's beams start, which is exactly where a reader would cut.

    A fixed ratio does not work here and was tried first. The multi-slot layout
    splits the free space 60/40 (beams above, lyrics below) and on H_uW2B5A1kE
    the lyric row eats 46% of it, so every 40% cut sliced the Korean lyrics in
    half lengthwise and left the top halves sitting under the staff. Measured,
    the real boundary lands between 0.44 and 0.62 of the gap on this one video
    alone, which is precisely the range no constant covers.
    """
    if not spans:
        return []
    prof = np.convolve(np.asarray(page, dtype=np.float32),
                       np.ones(5, np.float32) / 5.0, mode="same")
    cuts: list[float] = []
    for i in range(len(spans) - 1):
        s1, s0 = spans[i][1], spans[i + 1][0]
        a = int(round(s1 + 0.5 * gap)) - origin
        b = int(round(s0 - 1.0 * gap)) - origin
        a = max(0, min(a, len(prof) - 1))
        b = max(a + 1, min(b, len(prof)))
        w = prof[a:b]
        if w.size < 2:
            cuts.append((s1 + s0) / 2.0)
            continue
        blank = _runs(w <= 0.02 * max(float(w.max()), 1.0))
        if blank:
            r0, r1 = blank[-1]
            cuts.append(float(origin + a + (r0 + r1) / 2.0))
        else:
            cuts.append(float(origin + a + int(np.argmin(w))))
    if not cuts:
        return [(spans[0][0] - 4.0 * gap, spans[0][1] + 4.0 * gap)]
    above = float(np.median([spans[i][0] - cuts[i - 1] for i in range(1, len(spans))]))
    below = float(np.median([cuts[i] - spans[i][1] for i in range(len(spans) - 1)]))
    boxes = [(spans[0][0] - above, cuts[0])]
    for i in range(1, len(spans) - 1):
        boxes.append((cuts[i - 1], cuts[i]))
    if len(spans) > 1:
        boxes.append((cuts[-1], spans[-1][1] + below))
    return boxes


def vscroll_page_profile(meds: dict[int, np.ndarray], offsets: dict[int, float],
                         band: tuple[int, int]) -> tuple[np.ndarray, int]:
    """
    The whole page's row-ink profile, in content rows, built by dropping every
    segment's own profile at its registered offset and averaging the overlaps.

    Returns (profile, origin): profile[0] is content row `origin`.
    """
    b0, b1 = band
    origin = int(np.floor(min(offsets.values()))) + b0
    end = int(np.ceil(max(offsets.values()))) + b1
    acc = np.zeros(max(end - origin, 1), np.float64)
    cnt = np.zeros_like(acc)
    for k, m in meds.items():
        r = row_ink(m)
        a = b0 + int(round(offsets[k])) - origin
        b = a + (b1 - b0)
        a, b = max(a, 0), min(b, len(acc))
        if b > a:
            acc[a:b] += r[b0:b0 + (b - a)]
            cnt[a:b] += 1
    return (acc / np.maximum(cnt, 1)).astype(np.float32), origin


def scroll_gray(bgr: np.ndarray, polarity: str) -> np.ndarray:
    """
    Ink-dark grey for the motion measurement only.

    Deliberately NOT `normalise_ink`: that recovers the compositing alpha
    against the background PLATE, and on a scrolling video the plate is a
    temporal percentile of content that never stopped moving, i.e. a smear. The
    tracker only needs a profile that translates with the page, and a plain
    (inverted, for white ink) grey does that with no dependence on the plate.
    """
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return g if polarity == "dark_ink" else (255 - g)


def vscroll_extract(video: Path, lay: Layout, tr: dict, fps: float, sat: int,
                    max_per_seg: int = 24, progress=None) -> tuple[list[np.ndarray], dict]:
    """
    Lift every system of a vertically scrolling page, once, as a whole.

    Second decode of the video: the first pass only measured the motion, and
    holding a whole video's frames to re-crop them later would cost gigabytes.
    Each stable segment collapses to ONE median frame (the playhead sits at a
    different x in every frame of the segment, so the median erases it exactly
    as it does for a stationary score), and every system is then cropped out of
    the single segment where it sits wholly inside the moving band, furthest
    from both its edges.

    Returns (composites in page order, info) where the composites are grey with
    dark ink, the same thing `composite_line` returns.
    """
    segs = [(int(a), int(b)) for a, b in tr["segments"]]
    offsets = {int(k): float(v) for k, v in tr["offsets"].items()}
    band = (int(tr["band"][0]), int(tr["band"][1]))
    want: dict[int, int] = {}                  # frame index -> segment index
    for k in sorted(offsets):
        a, b = segs[k]
        idx = np.linspace(a, b, min(max_per_seg, b - a + 1)).astype(int)
        for i in set(int(x) for x in idx):
            want[i] = k
    bufs: dict[int, list[np.ndarray]] = {k: [] for k in offsets}
    meds: dict[int, np.ndarray] = {}
    n = 0
    for i, (_t, f) in enumerate(iter_frames(video, fps, lay.width, lay.height)):
        n += 1
        k = want.get(i)
        if k is None:
            continue
        bufs[k].append(normalise_ink(f, lay.plate, lay.polarity, sat, lay.staff_gap))
        if i >= segs[k][1] - 1 and bufs[k]:
            meds[k] = np.median(np.stack(bufs[k]), axis=0).astype(np.uint8)
            bufs[k] = []
            if progress is not None:
                progress(len(meds) / max(len(offsets), 1))
    for k, buf in bufs.items():               # anything the loop ended on
        if buf and k not in meds:
            meds[k] = np.median(np.stack(buf), axis=0).astype(np.uint8)
    log(f"vscroll: {n} frames re-read, {len(meds)}/{len(offsets)} segment composites")
    if len(meds) < 3:
        return [], {"systems": 0}

    covs = {k: staff_row_coverage(m, "dark_ink") for k, m in meds.items()}
    spans = vscroll_layout(covs, offsets, band, lay.staff_gap, lay.height)
    page, origin = vscroll_page_profile(meds, {k: offsets[k] for k in meds}, band)
    boxes = vscroll_boxes(spans, lay.staff_gap, page, origin)
    pitch = [round(spans[i + 1][0] - spans[i][0], 1) for i in range(len(spans) - 1)]
    log(f"vscroll: {len(spans)} systems on the page, staff pitch "
        f"{min(pitch) if pitch else 0:.0f}..{max(pitch) if pitch else 0:.0f}px "
        f"(median {float(np.median(pitch)) if pitch else 0:.0f})")

    comps: list[np.ndarray] = []
    picked: list[dict] = []
    missed = 0
    for i, (c0, c1) in enumerate(boxes):
        best = None
        for k, off in offsets.items():
            if k not in meds:
                continue
            y0, y1 = c0 - off, c1 - off
            if y0 < band[0] or y1 > band[1]:
                continue
            margin = min(y0 - band[0], band[1] - y1)
            score = (margin, segs[k][1] - segs[k][0])
            if best is None or score > best[0]:
                best = (score, k, int(round(y0)), int(round(y1)))
        if best is None:
            missed += 1
            log(f"vscroll: system {i} (content rows {c0:.0f}..{c1:.0f}) is never "
                f"wholly inside the band, skipped")
            continue
        _s, k, y0, y1 = best
        comps.append(meds[k][y0:y1].copy())
        picked.append({"system": i, "segment": k, "content": [round(c0, 1), round(c1, 1)],
                       "screen": [y0, y1]})
    info = {"systems": len(comps), "spans": len(spans), "skipped": missed,
            "pitch_px": pitch, "picked": picked}
    return comps, info


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
    # Ink is darkness relative to THIS strip's own paper, not to 255. Acceptance
    # video 4 prints its score on a translucent panel that sits at grey 210, so
    # measuring against 255 adds a constant 45 of "ink" to every pixel of empty
    # paper; the denominator is then dominated by the blank part of the strip and
    # two completely different systems measure 0.07 instead of 0.4. Every video
    # whose paper really is white is unaffected (its 90th percentile IS ~255):
    # measured on videos 1 and 3 the count of detected line changes is identical.
    paper = max(float(np.percentile(a, 90)), float(np.percentile(b, 90)), 1.0)
    ia = np.clip(paper - a.astype(np.float32), 0.0, None)
    ib = np.clip(paper - b.astype(np.float32), 0.0, None)
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


HEAD_W, HEAD_H = 320, 24
HEAD_SAME = 0.30            # <= this, two headers carry the same marks


def head_signature(comp: np.ndarray, gap: float) -> np.ndarray:
    """
    Binary fingerprint of the rows ABOVE the staff: the measure number and the
    section marker, and nothing else.

    This is the only thing that separates two systems whose music is genuinely
    identical from one system the video simply drew twice, and both happen in
    this customer's videos:

    * sample case 0 prints four bars of rest under one lyric at measures 53-56
      and again at 57-60. The staves correlate 0.9998 and even the whole slot box
      correlates past 0.96, because the "Bridge" marker and the two digits are a
      handful of rows in a 300px box. Measure 57 was being deleted from the PDF.
    * acceptance video 3 plays every line twice, so the SAME picture (same
      measure number, same marker, same playhead-free notation) turns up in two
      separate seven-second spans. Printing it twice would double the score.

    The header band is where those two cases differ, so it is compared on its
    own, at its own scale, where two digits are a large share of the ink.
    """
    rows = np.where(staff_row_coverage(comp, "dark_ink") > 0.40)[0]
    top = int(rows.min()) if len(rows) else int(0.35 * comp.shape[0])
    cut = max(0, top - int(round(0.4 * gap)))
    if cut < 4:
        return np.zeros((HEAD_H, HEAD_W), bool)
    # a FIXED ink threshold, not an adaptive one: this band is mostly empty
    # paper, and rescaling it against its own spread turns compression noise
    # into "ink", so two composites of the same line stop matching.
    return signature(comp[:cut], HEAD_W, HEAD_H)


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


INK_SHARE_FLOOR = 0.11      # of the video's own median candidate ink share


def ink_share(strip: np.ndarray) -> float:
    """
    How much of this strip is actually inked, measured on the RENDERED strip.

    `ink_strength` asks how dark the darkest ink gets, which says nothing about
    how much of it there is: a white box with one grey border rule and a broken
    two-line staff both reach a perfectly respectable strength. This is the
    other half, and it is what separates a printed system from an empty one.
    """
    g = strip if strip.ndim == 2 else cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
    return float((g < 128).mean())


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


# --------------------------------------------------------------------------
# Frames taken while the page was SLIDING past the band.
#
# A system is a thing that stands still on screen. `YkjcWb63v0o` ends by
# scrolling its page up out of the band over three quarters of a second, and the
# three sampled frames caught in that slide each looked like a brand new line to
# `group_lines` (their staff sits at row 103, then 65, then 14, against 106 held
# for the previous 45 frames), so the last system of the score was composited
# and printed FOUR times. That is the customer's "마지막장 같은마디 반복".
#
# What the three copies have and a real system does not is not their picture:
# the copies correlate 0.910 with the original, which is inside the band where
# genuinely different bars of this repetitive drum groove live (bars 57/65/73
# correlate 0.94-0.99 with each other). It is their MOTION. Registering each
# frame's row-ink profile against its temporal neighbour separates the two
# completely: a settled frame registers at shift 0 with the correlation at zero
# shift already 1.00, and a sliding frame only correlates once it is shifted.
# --------------------------------------------------------------------------
TRAVEL_MIN_NCC = 0.50       # under this the pair is a cut or a fade, not travel
TRAVEL_MIN_PX = 2           # a pixel of registration jitter is compression noise
TRAVEL_MARGIN = 0.25        # sliding has to explain the pair better than standing still


def mark_travelling(frames: list[SlotFrame], gap: float, dt: float) -> int:
    """
    Flag every frame that sits inside a vertical slide of the page.

    A pair of consecutive frames is travelling when its row-ink profiles
    register at a non-zero shift, that registration is good, and it is
    *substantially* better than standing still. All three are needed:

    * the shift alone fires on noise, so it has to clear TRAVEL_MIN_PX;
    * the registered correlation alone fires on a cut to a blank outro, where
      nothing matches at any shift and the argmax is meaningless;
    * the margin over the zero-shift correlation is what keeps a settled frame
      out. A static pair registers at shift 0, so both numbers are the same and
      the margin is 0.00; the customer's three sliding frames measure 0.98/0.26,
      0.60/0.00 and 0.54/0.00 (registered/zero).

    A frame counts as travelling only if the pair on one side of it does, which
    deliberately keeps the LAST settled frame before a slide begins (its
    incoming pair is static) inside its own group.

    Returns how many frames were flagged. Pairs more than 2.5 sample periods
    apart are not compared at all: the frames between them were thrown out for
    having no staff, so any registration across the hole is a guess.
    """
    flags = travelling_flags([row_ink(f.gray) for f in frames],
                             [f.t for f in frames], gap, dt)
    for f, v in zip(frames, flags):
        f.travelling = v
    return sum(flags)


def travelling_flags(profs: list[np.ndarray], times: list[float],
                     gap: float, dt: float) -> list[bool]:
    """`mark_travelling` on nothing but the row-ink profiles, so a fixture can
    replay a slide without carrying the frames (see `ci/slide_check.py`)."""
    if len(profs) < 2:
        return [False] * len(profs)
    limit = max(8, int(round(6.0 * gap)))
    moved = [False] * (len(profs) - 1)
    for i in range(len(profs) - 1):
        if times[i + 1] - times[i] > 2.5 * dt:
            continue
        dy, ncc, zero = best_shift(profs[i], profs[i + 1], limit, cap_div=2)
        moved[i] = (abs(dy) >= TRAVEL_MIN_PX and ncc >= TRAVEL_MIN_NCC
                    and ncc - zero >= TRAVEL_MARGIN)
    return [(i > 0 and moved[i - 1]) or (i < len(moved) and moved[i])
            for i in range(len(profs))]


def sliding_groups(groups: list[list[SlotFrame]], dt: float) -> set[int]:
    """
    Which groups were cut out of a page that never stopped moving.

    A group every frame of which is travelling is a sample of a slide rather
    than a system -- but ONE such group sitting between two settled ones is
    ambiguous. `a09-kimyongtae` flips to its next line in a single sample
    period five times over, and each of those five transition frames is
    all-travelling; they are junk (each is a truncated redraw of the line above
    it) but they are printed today and this pass is not allowed to move that
    video's count.

    A RUN of them is not ambiguous. If the page is still moving a sample period
    later, then nothing came to rest in between, so everything the run saw was
    either already printed from a settled view or is about to be. That is what
    separates the five a09 transitions (runs of 1) from the three copies of
    YkjcWb63v0o's last bar (a run of 3, at 103 -> 65 -> 14 rows).

    Runs must be contiguous in TIME as well as in the group list: frames with no
    staff at all are thrown out before grouping, so two group indices can be
    seconds apart, and a slide either side of that hole is two events.
    """
    allmv = [bool(g) and all(f.travelling for f in g) for g in groups]
    out: set[int] = set()
    for i, mv in enumerate(allmv):
        if not mv:
            continue
        near_prev = (i > 0 and allmv[i - 1]
                     and groups[i][0].t - groups[i - 1][-1].t <= 2.5 * dt)
        near_next = (i + 1 < len(allmv) and allmv[i + 1]
                     and groups[i + 1][0].t - groups[i][-1].t <= 2.5 * dt)
        if near_prev or near_next:
            out.add(i)
    return out


HEAD_NEW = 0.45             # header this different is a new system on its own
HEAD_RUN = 3                # ...but only once it has stayed different this many frames


LAST_GROUP_TRACE: list[tuple[float, float, float, float, float, int]] = []
LAST_GROUP_RESETS = 0
LAST_GROUP_SPLITS = 0


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

    ...and a video that engraves the same groove for five systems running
    defeats both at once, because there is nothing in the picture to see. That
    is what `playhead_resets` is for, applied as a pure SUBDIVISION of the
    groups this loop produced (see `split_on_playhead`): a boundary is added
    inside a group that swallowed several systems, and no boundary this loop
    found is ever moved, so none of the thresholds calibrated above shift under
    it.

    Feeding the resets into the loop instead is measurably worse and was tried
    first: on `a10-drumtab` the reset at t=8.25s lands inside the opening
    system's 1.2s fade-in, and re-anchoring there moved the picture boundaries
    that follow it, turning that video's two fade-in copies into three.
    """
    groups: list[list[SlotFrame]] = []
    anchor: np.ndarray | None = None
    anchor_fp: np.ndarray | None = None
    anchor_head: np.ndarray | None = None
    head_run = 0
    # Every frame's two distances against the running anchor, for the diagnostic
    # that reads them back (`YTSCORE_DUMP_DIST`). A group that swallowed five
    # screens is only legible next to the numbers that let each screen through.
    trace: list[tuple[float, float, float, float, float, int]] = []
    for fi, f in enumerate(frames):
        # A changed HEADER is a new system on its own evidence. The staff picture
        # cannot always show it: sample case 0's measures 53-56 and 57-60 are four
        # bars of rest under one lyric, identical to within two digits, so on the
        # whole strip they measure 0.02 apart and were printed once instead of
        # twice. The header band is those two digits plus the section marker at
        # their own scale, where the same pair measures 0.7.
        # ...and only when it STAYS changed. A system that is on screen for ten
        # seconds picks up transients -- the slide-in smear, a highlight box, the
        # playhead crossing a rest -- and a single frame of those was enough to
        # print sample case 0's measure 57 three times over.
        hd = (jaccard(f.hkey, anchor_head)
              if (anchor_head is not None and f.hkey is not None) else 0.0)
        head_run = (head_run + 1) if hd > HEAD_NEW else 0
        head_changed = head_run >= HEAD_RUN
        bj = 1.0 if anchor is None else jaccard(f.key, anchor)
        gj = 1.0 if anchor_fp is None else soft_jaccard(f.fp, anchor_fp)
        new = (anchor is None
               or head_changed
               or (bj > thresh and gj > thresh))
        trace.append((f.t, bj, gj, hd, f.phx, int(new)))
        if new:
            groups.append([f])
            anchor = f.key.copy()
            anchor_fp = f.fp.copy()
            anchor_head = None if f.hkey is None else f.hkey.copy()
            head_run = 0
        else:
            groups[-1].append(f)
            anchor |= f.key
            anchor_fp = np.minimum(anchor_fp, f.fp)      # darkest wins: union of ink
    global LAST_GROUP_TRACE, LAST_GROUP_RESETS, LAST_GROUP_SPLITS
    LAST_GROUP_TRACE = trace
    resets = playhead_resets(frames)
    LAST_GROUP_RESETS = len(resets)
    before = len(groups)
    groups = split_on_playhead(groups, resets)
    LAST_GROUP_SPLITS = len(groups) - before
    return groups


PLAY_PIECE = 4              # frames: neither side of a split may be shorter


def split_on_playhead(groups: list[list[SlotFrame]],
                      resets: set[int]) -> list[list[SlotFrame]]:
    """
    Cut a group wherever the playhead started over inside it.

    Only INSIDE. A reset on a group's first frame is a boundary the picture rule
    already found and changes nothing, and no boundary is ever removed, so a
    video the picture rule handles correctly cannot move: the only groups this
    touches are the ones that swallowed a system whole.

    ...and only when both halves are long enough to BE a system. `a10-drumtab`
    is why: its playhead is already sweeping over the intro banner and resets at
    t=8.25s while the opening system is still 1.2 seconds into its fade-in, so
    the split lands two frames into a two-frame group and turns that video's two
    fade-in copies of system 1 into three. Measured over the corpus, the pieces
    a real system boundary produces are 15 to 32 frames and the only pieces
    under 6 are a10's pair of 1s, so PLAY_PIECE sits in a very wide empty band.

    Over the 17-video corpus the videos whose playhead this pipeline can see at
    all are a01, a04, a07, a08, a10, case1, case2 and the customer's
    d3t9j6DObN0. Every group this pass actually cuts is a defect: `a04` was
    silently missing TEN of its systems (its bar ladder ran 1, 9, 13, 21, 29 in
    steps of 8 where the engraving steps by 4), `a08-abcdrum` had composited its
    bars 21 and 25 into one strip with two lyric rows printed on top of each
    other, and `d3t9j6DObN0` had swallowed five whole systems. All three had
    been called print-clean or ghost-only on a count.
    """
    if not resets:
        return groups
    out: list[list[SlotFrame]] = []
    i = 0
    for g in groups:
        cuts: list[int] = []
        last = 0
        for k in range(1, len(g)):
            if (i + k) in resets and k - last >= PLAY_PIECE and len(g) - k >= PLAY_PIECE:
                cuts.append(k)
                last = k
        for a, b in zip([0] + cuts, cuts + [len(g)]):
            out.append(g[a:b])
        i += len(g)
    return out


@dataclass
class Cand:
    """One candidate system: the strip plus what the ordering pass judges it on."""
    t: float
    si: int
    strip: np.ndarray
    core: np.ndarray            # fixed-size grey picture of the staff core
    box: np.ndarray             # the whole slot box, same scale for every system in a slot
    head: np.ndarray            # binary mark of the rows above the staff (measure no., marker)
    strength: float             # how dark this system's ink actually gets, 0..1
    cov: float = 0.0            # staff-line coverage of the prepared strip
    sliding: bool = False       # every frame behind it was taken mid-slide


FADE_RATIO = 0.80           # below this ink ratio, the fainter of two matching
                            # neighbours is a half-drawn fade copy


def drop_fade_copies(cands: list[Cand],
                     ratio: float = FADE_RATIO) -> tuple[list[Cand], list[int]]:
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

    FADE_RATIO was 0.78 through 1.7.0 and is 0.80 from 1.7.1. It moved for
    exactly one pair, `YkjcWb63v0o`'s Intro printed twice at the top of page 1
    (t=7.5s/8.0s, strengths 0.784 and 0.616, ratio 0.785, match 0.957), and it
    moved only because the whole corpus was swept first. Raising a DELETE
    threshold is the most dangerous change on this project: d3t9j6DObN0 had four
    whole systems silently missing because five near-identical systems were all
    genuine. The measurement that made this safe, `ci/fade_sweep.py out/v176`,
    over 590 adjacent pairs on 21 runs:

    * the band [0.78, 0.80) with match >= CORE_SAME holds **exactly one pair in
      the whole corpus**, the Intro pair above;
    * five other pairs land in that ratio band but match 0.329-0.505, so the
      match gate already refuses them and would still refuse them at any ratio;
    * replaying fade + repeat end to end at both cuts, the surviving system list
      is byte-identical on 20 of 21 videos, and on caseC it loses that one copy
      and nothing else. The t=15.75s candidate the wider cut also takes is NOT a
      new deletion: 1.7.0 already dropped it one pass later in
      `drop_adjacent_repeats` (picture 0.971 against the same Intro). The two
      cuts differ in which pass removes it, not in what reaches the page.

    Do not move this cut again without re-running that sweep. `ci/fade_check.py`
    is its gate and fails in both directions.
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
            if os.environ.get("YTSCORE_DIAG"):
                log(f"fade?: t={a.t:.1f}s/{b.t:.1f}s ink {lo:.3f}/{hi:.3f} "
                    f"= {lo / hi:.3f} (need < {ratio}) match {m:.3f} (need >= {CORE_SAME})")
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


REPEAT_SAME = 0.96          # >= this, two adjacent systems are the same picture
REPEAT_CERTAIN = 0.985      # ...and above this the header no longer gets a veto


def drop_adjacent_repeats(cands: list[Cand],
                          thresh: float = REPEAT_SAME) -> tuple[list[Cand], list[int]]:
    """
    Drop a system that is simply the previous system printed again.

    Several of these videos redraw the current line part-way through it (a
    section marker appears, a highlight box switches on, the channel's overlay
    changes), which starts a new frame group and puts the SAME music on the page
    twice in a row. Acceptance video 3 had 19 such pairs out of 44 systems.

    The threshold is deliberately strict. Measured on the acceptance set, real
    consecutive lines of a repetitive drum groove reach 0.90-0.91 (a02 measures
    26/30/34/38 are different music that looks alike), while a genuine repeat of
    the same picture sits at 0.99-1.00. Only the second copy goes; a repeat that
    is really in the music is written with a repeat sign, not by printing the
    same system twice in a row.

    Compare the whole SLOT BOX, not the staff core, whenever both systems come
    from the same slot. The staff core is anchored on the top staff line and is
    only seven staff spaces tall, so it deliberately excludes the row where the
    measure number and the section marker are printed -- and that row is the
    only thing that separates two systems whose music really is identical.
    Sample case 0 pays for it: its measures 53-56 and 57-60 are four bars of
    rest under the same lyric, so their cores correlate 0.9998 and the whole of
    measures 57-60 was being deleted from the PDF. The full box, which is the
    same crop at the same scale for every system of a slot, correlates 0.30 on
    that pair and still 1.00 on a genuine redraw.

    ...but the header only gets a VETO up to REPEAT_CERTAIN. `KsSlNq-ciko` is
    the customer's second "마지막장 같은마디 반복" video and it fails here rather
    than on the slide: its last frame is a single-frame group whose picture
    matches the 58-frame group before it at **0.999**, and it was printed anyway
    because the header distance came out 1.000. That is not two different
    numbers, it is one number the fixed ink threshold could not read on a frame
    that is already fading, so the band came back empty against a band with "61"
    in it, and an empty-vs-inked Jaccard is 1.0 by construction.

    Above 0.985 the header cannot be telling the truth: two systems that are
    genuinely different bars do not correlate that hard over a box that contains
    the measure-number row (case 0's 53-56 against 57-60 sits at 0.30). The cut
    is where it is because it is measured: over 423 adjacent pairs on the
    17-video corpus the highest picture match on a pair the header vetoed is
    **0.937** (a04 at t=104.0 vs 98.2), and the only pair anywhere above 0.985
    is a10's 0.991, which the header agreed to drop anyway. So the band from
    0.937 to 0.999 is empty and the cut sits inside it.
    """
    out: list[Cand] = []
    dropped: list[int] = []
    for c in cands:
        if out:
            prev = out[-1]
            m = core_match(prev.box, c.box) if prev.si == c.si \
                else core_match(prev.core, c.core)
        else:
            m = 0.0
        hj = jaccard(out[-1].head, c.head) if out else 1.0
        if os.environ.get("YTSCORE_DIAG") and out:
            log(f"repeat?: t={c.t:.1f}s vs t={out[-1].t:.1f}s picture {m:.3f} "
                f"(need >= {thresh}) header {hj:.3f} (need <= {HEAD_SAME} "
                f"or picture >= {REPEAT_CERTAIN})")
        if out and m >= thresh and (hj <= HEAD_SAME or m >= REPEAT_CERTAIN):
            dropped.append(len(out) + len(dropped))
            why = "same header" if hj <= HEAD_SAME else "identical picture"
            log(f"repeat: system at t={c.t:.1f}s is the previous system redrawn "
                f"(picture {m:.3f}, {why}) -> dropped")
            continue
        out.append(c)
    if dropped:
        log(f"repeat: {len(dropped)} redrawn copies dropped -> {len(out)} systems")
    return out, dropped


def _blank_runs(prof: np.ndarray, gap: float) -> list[tuple[int, int]]:
    """Ink-free row runs of a smoothed row-ink profile, widest-usable first."""
    if prof.size < 3:
        return []
    sm = np.convolve(prof.astype(np.float32), np.ones(3, np.float32) / 3.0, mode="same")
    top = float(sm.max())
    if top <= 0:
        return []
    runs = _runs(sm <= 0.02 * top)
    keep = [r for r in runs if r[1] - r[0] >= max(2, int(round(0.4 * gap)))]
    return keep or runs


def system_extent(comp: np.ndarray, gap: float, target_top: int,
                  reach: float) -> tuple[int, int]:
    """
    Cut one composited system back to ITS OWN extent, at the ink-free line.

    The slot box is a rectangle of screen, not a system. Where a video re-lays
    its score out from screen to screen (zDG0Tw7MDXg does it six times) the box
    sits at the modal phase, so on the other phases it clips the measure number
    off the top of its own system and takes the beam row of the NEXT system in
    at the bottom. That beam row is then printed twice, once as a headless
    sliver under its neighbour and once properly with its own staff, and at a
    page boundary the two copies land on facing pages: bars 57, 65 and 94 of the
    customer's own PDF each appeared twice that way.

    The system's real boundary is not a ratio of the gap, it is the blank paper
    between two systems, so it is measured here rather than assumed. `reach` is
    how far out of the staff it is worth looking (half the slot pitch): past
    that we are inside the neighbour and a blank run means nothing.

    Nothing is cut on a side where no ink-free line was found. A translucent
    overlay score has no blank paper to find, and leaving that strip exactly as
    it was today is strictly better than guessing a boundary for it.
    """
    h = comp.shape[0]
    cov = staff_row_coverage(comp, "dark_ink")
    top = staff_anchor(cov, gap, target_top)
    if top < 0:
        return 0, h
    rows = np.where(cov > 0.40)[0]
    rows = rows[(rows >= top - gap) & (rows <= top + 6.0 * gap)]
    bot = int(rows.max()) if rows.size else int(round(top + 4.0 * gap))
    if bot <= top:
        bot = int(round(top + 4.0 * gap))

    prof = row_ink(comp)
    lo, hi = 0, h
    # above: the last blank band before the staff is the one over this system's
    # own measure number and beams, so cut in the middle of it.
    a = max(0, int(round(top - reach)))
    b = max(a + 1, int(round(top - 1.0 * gap)))
    if b - a >= 4:
        runs = _blank_runs(prof[a:b], gap)
        if runs:
            r0, r1 = runs[-1]
            lo = a + (r0 + r1) // 2
    # below: the LAST blank band, not the first. A lyric row sits under the
    # staff with clear paper above it, so cutting at the first blank band throws
    # the lyrics away -- case 1 lost the words under bars 20 and 24 that way.
    # The last band is the one after this system's own lyrics and before the
    # next system's beams, which is where a reader would cut.
    a = min(h - 1, int(round(bot + 0.5 * gap)))
    b = min(h, int(round(bot + reach)))
    if b - a >= 4:
        runs = _blank_runs(prof[a:b], gap)
        if runs:
            r0, r1 = runs[-1]
            hi = a + (r0 + r1) // 2
    if hi - lo < (bot - top) + int(round(2.0 * gap)):
        return 0, h
    return lo, hi


def reg_profile(gray: np.ndarray) -> np.ndarray:
    """Mean-removed row ink profile, the signal `measure_dy` registers on."""
    p = (255.0 - gray.astype(np.float32)).sum(axis=1)
    return p - float(p.mean())


REG_TIE = 0.98              # shifts scoring within this of the best are a tie
REG_FLOOR = 0.50            # below this the profiles do not match at all
REG_MIN_GAPS = 0.70         # below this the anchor is kept; see composite_line


def measure_dy(ref: np.ndarray, prof: np.ndarray, limit: int) -> tuple[int, float]:
    """
    How many rows this frame's content sits BELOW the reference frame's, read
    off the pixels rather than off the staff-line anchor.

    `staff_anchor` returns the first detected line of the staff cluster nearest
    the layout, and on EVprtoI_3eY it is not stable: whenever the top staff line
    of a frame drops under the 0.40 coverage cut, the anchor locks onto a line
    THREE rows further down and reports a staff that has moved 51px (3 x gap)
    when nothing on screen moved at all. `composite_line` then registered a
    third of the frames 51px away from the rest and the median of the two
    populations came out as a double exposure: the customer's "2페이지 위아래
    흐림". Groups 8, 9, 18 and 31 of that video are the ones that straddle it.

    Correlating the row ink profiles cannot make that mistake, because it asks
    the picture where it is instead of asking one detector twice. Ties are
    broken toward the SMALLEST shift, which is what kills the staff's own
    periodicity: a 5-line staff correlates almost as well one staff space out,
    so with a plain argmax a group could still be pulled apart by one gap.

    Returns (dy, score). A score below REG_FLOOR means the two frames are not
    the same picture at all (one is mid-slide), and the caller keeps the anchor.
    """
    n = ref.size
    best_s, best_dy = -1.0, 0
    scores: list[tuple[float, int]] = []
    for dy in range(-limit, limit + 1):
        if dy >= 0:
            a, b = ref[dy:], prof[:n - dy] if dy else prof
        else:
            a, b = ref[:n + dy], prof[-dy:]
        if a.size < max(8, n // 2):
            continue
        den = float(np.linalg.norm(a) * np.linalg.norm(b))
        s = float(a @ b / den) if den > 0 else -1.0
        scores.append((s, dy))
        if s > best_s:
            best_s, best_dy = s, dy
    if not scores:
        return 0, -1.0
    near = [dy for s, dy in scores if s >= REG_TIE * best_s]
    return min(near, key=abs), best_s


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

    The shift itself is MEASURED on the pixels (`measure_dy`), not taken from
    the staff anchor. The anchor still picks the reference frame, so the strip
    lands at the same row it always did and every downstream offset is
    untouched; it just no longer decides how far each frame moves. See
    `measure_dy` for the defect that forced it.

    The measurement only OVERRIDES the anchor when the two disagree by at least
    `REG_MIN_GAPS` of a staff space, because that is the only mistake the anchor
    actually makes: it locks onto the wrong LINE of the staff, which moves it by
    a whole number of staff spaces and nothing else. Measured over the 17-video
    corpus, every disagreement is either <= 0.50 gap (registration noise on a
    line that never moved) or >= 0.88 gap (1.0, 2.0, 3.0 gaps: the anchor
    jumping lines), with nothing in between, so the cut sits in an empty band.

    Following the sub-gap half is not harmless. It nudges frames the 1.5.0
    pipeline left alone, and on a10-drumtab that was enough to align the intro
    KakaoTalk banner into something whose staff coverage read 0.42 instead of
    0.08, so the "mid-animation" drop let it through and a junk strip appeared
    as system 1 of a print-clean video. Sub-gap disagreements keep the anchor.
    """
    frames = group
    if len(group) > 2 * trim + 1:
        frames = group[trim:len(group) - trim]
    tops = [f.top for f in frames if f.top >= 0]
    if tops and gap > 0:
        ref = int(np.median(tops))
        limit = int(round(4.0 * gap))
        anchor = [int(np.clip(ref - f.top, -limit, limit)) if f.top >= 0 else 0
                  for f in frames]
        pivot = min(range(len(frames)),
                    key=lambda i: (frames[i].top < 0, abs(frames[i].top - ref)))
        rprof = reg_profile(frames[pivot].gray)
        shifts: list[int] = []
        weak = 0
        held = 0
        for f, a in zip(frames, anchor):
            dy, sc = measure_dy(rprof, reg_profile(f.gray), limit)
            if sc < REG_FLOOR:
                weak += 1
                dy = a
            elif abs(dy - a) < REG_MIN_GAPS * gap:
                held += 1
                dy = a
            shifts.append(dy)
        if os.environ.get("YTSCORE_DIAG"):
            dis = [(a, d) for a, d in zip(anchor, shifts) if a != d]
            if dis:
                log(f"reg?: t={frames[0].t:.1f}s n={len(frames)} ref_top={ref} "
                    f"weak={weak} held={held} "
                    f"anchor!=measured on {len(dis)}/{len(frames)}: "
                    f"{sorted(set(dis))}")
        stack = np.stack([shift_rows(f.gray, d) for f, d in zip(frames, shifts)])
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
    # The page image never touches the disk. It used to be written to
    # `out_pdf.parent/_page_<pid>.png` and handed back to PyMuPDF by NAME, and
    # `cv2.imwrite` cannot write into a folder outside the Windows ANSI code
    # page (see `imwrite` above), so on a Korean output folder every run died
    # here -- after the whole 3-7 minute pipeline had already succeeded. An
    # in-memory PNG is byte-for-byte the same image (verified: the rendered page
    # is pixel-identical either way), needs no writable outdir at all, and drops
    # a 26MB round trip to disk per page.
    for pi, page_strips in enumerate(pages):
        top = MARGIN + (head_px if pi == 0 else head_px // 2)
        canvas = np.full((A4_H, A4_W, 3), 255, np.uint8)
        y = top
        for s in page_strips:
            canvas[y:y + s.shape[0], MARGIN:MARGIN + s.shape[1]] = s
            y += s.shape[0] + gap
        ok, buf = cv2.imencode(".png", canvas)
        if not ok:
            raise RuntimeError(f"page {pi + 1} could not be encoded")
        page = doc.new_page(width=595, height=842)      # A4 in points
        page.insert_image(fitz.Rect(0, 0, 595, 842), stream=buf.tobytes())
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
    # ...and the PDF itself goes out through `write_bytes` for the same reason:
    # `doc.save(str(path))` hands the name to MuPDF's own file layer, and the
    # customer's chosen folder is the one part of this path we do not control.
    out_pdf.write_bytes(doc.tobytes(deflate=True))
    doc.close()
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
        lay.crops = [lay.crops[i] for i in pick] if lay.crops else []
        lay.crop_pad = [lay.crop_pad[i] for i in pick] if lay.crop_pad else []
        lay.staff_spans = [lay.staff_spans[i] for i in pick]
        lay.staff_rows = [lay.staff_rows[i] for i in pick]
        log(f"slots: using {pick} (tail-only: {sorted(tail_only)}) -> {lay.systems}")
    sat = sat_thresh if sat_thresh is not None else (50 if lay.polarity == "dark_ink" else 110)

    # ---- full-fps pass, cropping only the system boxes
    raw_slots: list[list[SlotFrame]] = [[] for _ in lay.systems]
    ridges: list[list[float]] = [[] for _ in lay.systems]
    # Per-frame column ink profile, for the scroll guard (see ScrollingScore).
    # One float per pixel column per frame, so a 3 minute 1080p video costs
    # ~6MB per slot, which is nothing next to the frames themselves.
    colprofs: list[list[np.ndarray]] = [[] for _ in lay.systems]
    # Per-frame ROW ink profile of the whole frame, for the vertical tracker.
    # Full frame, not the slots: the tracker has to be able to tell the static
    # chrome above and below the score from the page that moves past it.
    rowprofs: list[np.ndarray] = []
    nframes = 0
    crop_boxes = lay.crop_boxes()
    pads = lay.crop_pad or [(0, 0)] * len(crop_boxes)
    plates = [lay.plate[y0:y1] for y0, y1 in crop_boxes]
    targets = [max(0, sp[0] - y0) for sp, (y0, _) in zip(lay.staff_spans, crop_boxes)]
    staff_h = [sp[1] - sp[0] for sp in lay.staff_spans]
    up = int(round(4.0 * lay.staff_gap))
    down = int(round(4.0 * lay.staff_gap))
    # The grouping/dedup pixels must stay EXACTLY the rows they were before the
    # crop was allowed to grow, or every threshold calibrated on the eleven
    # verified videos moves under it. Both windows are therefore measured in the
    # detection box and then offset by the top pad, not re-derived in the crop.
    cores_ab: list[tuple[int, int]] = []
    head_ab: list[tuple[int, int]] = []
    # The staff's own rows inside the crop, and nothing else: that is where the
    # playhead is legible (see `playhead_x`).
    staff_ab: list[tuple[int, int]] = []
    for si, ((by0, by1), sp) in enumerate(zip(lay.systems, lay.staff_spans)):
        tb = max(0, sp[0] - by0)
        bh = by1 - by0
        a = max(0, tb - up)
        b = min(bh, tb + (sp[1] - sp[0]) + down)
        pad_t = pads[si][0]
        cores_ab.append((a + pad_t, b + pad_t))
        head_ab.append((pad_t, max(0, tb - int(round(0.4 * lay.staff_gap))) + pad_t))
        staff_ab.append((tb + pad_t, min(bh, tb + (sp[1] - sp[0])) + pad_t))
    for t, f in iter_frames(video, fps, lay.width, lay.height):
        nframes += 1
        if nframes % 20 == 0:
            report(0.32 + 0.45 * (t / max(duration, 1.0)))
        rowprofs.append(row_ink(scroll_gray(f, lay.polarity)))
        for si, (y0, y1) in enumerate(crop_boxes):
            g = normalise_ink(f[y0:y1], plates[si], lay.polarity, sat, lay.staff_gap)
            prof = staff_row_coverage(g, "dark_ink")
            top = staff_anchor(prof, lay.staff_gap, targets[si])
            # Group frames on the STAFF, not on the whole box. The box is as tall
            # as the paper the score is printed on, which on some videos is three
            # times the notation (acceptance video 4's translucent panel runs from
            # y=806 to the bottom of the frame for a staff at y=950..1012). The
            # empty paper then dominates both distances -- the graded one in
            # particular, whose denominator counts every off-white pixel -- and 777
            # frames of that video collapsed into 8 "systems" instead of ~28.
            # The window is FIXED at the layout's staff position, not moved to
            # this frame's own staff: an anchor that occasionally fails and falls
            # back shifts the crop by tens of pixels between neighbouring frames,
            # and video 1 then split its 21 systems into 83 groups. The padding
            # below absorbs the 10-20px a display shifts when a line carries
            # lyrics; composite_line still registers the frames properly. The window
            # reaches four staff spaces ABOVE the top line on purpose: the
            # measure number and the section marker live there, and they are the
            # only difference between sample case 0's measures 53-56 and 57-60
            # (four bars of rest under the same lyric). At two spaces the two
            # never separated and measure 57 was missing from the PDF.
            a, b = cores_ab[si]
            b = min(g.shape[0], b)
            core = g[a:b] if b - a >= 8 else g
            h0, hcut = head_ab[si]
            hkey = signature(g[h0:hcut], HEAD_W, HEAD_H) if hcut - h0 >= 6 else None
            s0, s1 = staff_ab[si]
            crop = f[y0:y1]
            s1 = min(crop.shape[0], s1)
            phx = playhead_x(crop[s0:s1], sat, lay.polarity) if s1 - s0 >= 8 else -1.0
            raw_slots[si].append(SlotFrame(t=t, gray=g, key=signature(core),
                                           fp=fingerprint(core), hkey=hkey, top=top,
                                           phx=phx))
            ridges[si].append(float(prof.max()))
            colprofs[si].append(column_ink(core))

    # A frame is a score frame when its staff is actually drawn. The ridge value
    # has no absolute meaning across videos, so the cut is taken against this
    # video's own upper quartile: intro cards, black frames and cutaways sit an
    # order of magnitude below it.
    slots: list[list[SlotFrame]] = []
    kept_profs: list[list[np.ndarray]] = []
    rejected = 0
    for si, sf in enumerate(raw_slots):
        ref = float(np.percentile(ridges[si], 80)) if sf else 0.0
        cut = 0.35 * ref
        mask = [r >= cut for r in ridges[si]]
        keep = [f for f, m in zip(sf, mask) if m]
        rejected += len(sf) - len(keep)
        slots.append(keep)
        kept_profs.append([p for p, m in zip(colprofs[si], mask) if m])
        log(f"collect: slot {si}: staff ridge ref {ref:.2f}, cut {cut:.2f}, "
            f"{len(keep)}/{len(sf)} frames kept")
    raw_slots = []
    colprofs = []
    kept = sum(len(s) for s in slots)
    log(f"collect: {nframes} frames x {len(lay.systems)} slot(s) -> {kept} with a staff, "
        f"{rejected} rejected (intro/outro/black/no-staff)")
    if not kept:
        raise ScoreNotFound("no score frames detected")

    # ---- the scroll guard, mechanism 1: measured horizontal travel.
    # Before anything downstream gets a chance to turn a moving ribbon into 700
    # "systems" and a 45 page PDF, ask whether the band is standing still. Every
    # slot that produced a usable verdict has to say "scrolling" before we
    # refuse: a guard that blocks a working video is worse than the bug.
    scroll_stats: list[dict] = []
    for si, ps in enumerate(kept_profs):
        m = scroll_metrics(ps, fps)
        m["scrolling"] = is_scrolling(m)
        scroll_stats.append(m)
        log(f"scroll: slot {si}: {m['px_per_s']:.1f} px/s, still {m['still_frac']:.2f}, "
            f"one-way {m['sign_consistency']:.2f}, {m['matched']}/{m['pairs']} pairs "
            f"-> {'SCROLLING' if m['scrolling'] else 'stationary'}")
    kept_profs = []
    voted = [m for m in scroll_stats if m["matched"] >= 20]
    if voted and all(m["scrolling"] for m in voted):
        worst = max(voted, key=lambda m: m["px_per_s"])
        raise ScrollingScore(
            f"the score band travels {worst['px_per_s']:.0f} px/s sideways "
            f"({worst['sign_consistency']:.0%} of matched frame pairs move the same "
            f"way, only {worst['still_frac']:.0%} are stationary)")

    # ---- the vertical page-scroll path.
    # The horizontal guard just proved the band is not travelling sideways. It
    # can still be travelling UP, in discrete jumps, which is a page being
    # scrolled rather than a ribbon being pulled: every system is wholly on
    # screen and stationary for seconds at a time, it just is not always in the
    # SAME place. `analyse_layout`'s modal slot layout is an average over scroll
    # phases in that case (215-221px slots against a 247px page pitch on the
    # customer's H_uW2B5A1kE), which straddles every crop and hides every system
    # from its own earlier copy. Register the page first, then lift each system
    # once, from the frames where it is standing still.
    vband = (min(y0 for y0, _ in lay.systems), max(y1 for _, y1 in lay.systems))
    vtrack = vscroll_track(np.asarray(rowprofs, dtype=np.float32), fps, vband,
                           lay.staff_gap)
    rowprofs = []
    log(f"vscroll: {vtrack['events']} step(s), median {vtrack['step_px']:.0f}px, "
        f"{vtrack['travel_px']:.0f}px over {vtrack.get('travel_bands', 0):.1f} band "
        f"heights, one-way {vtrack['one_way']:.2f}, band {vtrack['band']} "
        f"-> {'SCROLLING PAGE' if vtrack['scrolling'] else 'stationary'}")
    vinfo: dict = {}
    candidates: list[Cand] = []
    mode = ["all"] * len(slots)

    if vtrack["scrolling"]:
        slots = []              # the scroll path re-reads the video; free these
        vcomps, vinfo = vscroll_extract(
            video, lay, vtrack, fps, sat,
            progress=lambda f: report(0.62 + 0.20 * f))
        if len(vcomps) < 4:
            raise ScrollingScore(
                f"the page scrolls vertically ({vtrack['events']} steps of "
                f"{vtrack['step_px']:.0f}px) but only {len(vcomps)} system(s) could "
                f"be lifted from it")
        for i, comp in enumerate(vcomps):
            # `t` orders the candidates, and on a scrolling page the page's own
            # row IS the reading order: a system that scrolled off the top was
            # played before one that is still below the fold, whatever segment
            # each was finally cropped from.
            candidates.append(Cand(
                t=float(i), si=0,
                strip=render_strip(comp, lay.polarity, None),
                core=core_fingerprint(comp, lay.staff_gap),
                box=cv2.resize(comp, (CORE_W, CORE_H), interpolation=cv2.INTER_AREA),
                head=head_signature(comp, lay.staff_gap),
                strength=ink_strength(comp)))
        log(f"vscroll: {len(candidates)} systems lifted from the page "
            f"({vinfo.get('skipped', 0)} never wholly visible)")
    else:
        # ---- per slot: dedup + composite + render
        chromes: list[np.ndarray | None] = []
        for si, (y0, y1) in enumerate(crop_boxes):
            if lay.polarity == "light_ink":
                span = (lay.staff_spans[si][0] - y0, lay.staff_spans[si][1] - y0)
                chromes.append(static_chrome(plates[si], span))
            else:
                chromes.append(None)

        slot_groups: list[list[list[SlotFrame]]] = []
        slot_slides: list[set[int]] = []
        dt_sample = 1.0 / max(fps, 0.01)
        for si, sf in enumerate(slots):
            nmove = mark_travelling(sf, lay.staff_gap, dt_sample)
            groups = group_lines(sf, dedup_thresh) if sf else []
            slot_groups.append(groups)
            slides = sliding_groups(groups, dt_sample)
            slot_slides.append(slides)
            seen_ph = sum(1 for f in sf if f.phx >= 0.0)
            log(f"dedupe: slot {si}: {len(sf)} frames -> {len(groups)} distinct systems "
                f"({nmove} frame(s) taken mid-slide, {len(slides)} group(s) inside a slide, "
                f"playhead seen in {seen_ph}/{len(sf)} frame(s), "
                f"{LAST_GROUP_RESETS} reset(s) in its track, "
                f"{LAST_GROUP_SPLITS} group(s) split on one)")
            if os.environ.get("YTSCORE_DIAG"):
                for gi, g in enumerate(groups):
                    tops = [f.top for f in g if f.top >= 0]
                    hist = sorted(Counter(tops).items())
                    mv = sum(1 for f in g if f.travelling)
                    log(f"group?: slot {si} #{gi} t={g[0].t:.1f}..{g[-1].t:.1f}s "
                        f"n={len(g)} moving={mv}/{len(g)} "
                        f"slide={'y' if gi in slides else 'n'} tops={hist}")
            if os.environ.get("YTSCORE_DUMP_DIST"):
                with open(outdir / f"{name}_dist_slot{si}.tsv", "w") as fh:
                    fh.write("t\tbinary\tgraded\theader\tplayhead\tnew\n")
                    for t, bj, gj, hd, px, nw in LAST_GROUP_TRACE:
                        fh.write(f"{t:.2f}\t{bj:.4f}\t{gj:.4f}\t{hd:.4f}\t{px:.4f}\t{nw}\n")
            want = os.environ.get("YTSCORE_DUMP_GROUPS")
            if want:
                import pickle
                for gi in [int(x) for x in want.split(",")]:
                    if gi < len(groups):
                        with open(outdir / f"{name}_g{si}_{gi}.pkl", "wb") as fh:
                            pickle.dump([(f.t, f.top, f.gray) for f in groups[gi]], fh)

        # Trim BEFORE the fingerprints are taken, not at render time: two copies
        # of one system that came out of different slots only compare equal once
        # both have been cut to the same extent, and it is that comparison that
        # decides whether the system is printed once or twice.
        # Tiled layouts only, for the same reason the crop is only grown there:
        # with one slot the box IS the system and there is no neighbour to cut
        # away from, so trimming can only move thresholds that eleven verified
        # videos were calibrated on.
        pitches = np.diff([sp[0] for sp in lay.staff_spans]) if len(lay.staff_spans) > 1 \
            else np.array([])
        tiled = pitches.size > 0
        reach = float(np.median(pitches)) / 2.0 if tiled else 0.0
        comps: list[list[np.ndarray]] = []
        extents: list[list[tuple[int, int]]] = []
        for si, gs in enumerate(slot_groups):
            cs = [composite_line(g, lay.staff_gap) for g in gs]
            ex = [system_extent(c, lay.staff_gap, targets[si], reach) if tiled
                  else (0, c.shape[0]) for c in cs]
            cut = [c[a:b] for c, (a, b) in zip(cs, ex)]
            nt = sum(1 for c, (a, b) in zip(cs, ex) if (b - a) != c.shape[0])
            if nt:
                log(f"trim: slot {si}: {nt}/{len(cs)} system(s) cut back to their own "
                    f"extent (box {crop_boxes[si][1] - crop_boxes[si][0]}px -> "
                    f"{sorted({b.shape[0] for b in cut})}px)")
            comps.append(cut)
            extents.append(ex)
        cores: list[list[np.ndarray]] = [[core_fingerprint(c, lay.staff_gap)
                                          for c in cs] for cs in comps]
        box_fps: list[list[np.ndarray]] = [[cv2.resize(c, (CORE_W, CORE_H),
                                                       interpolation=cv2.INTER_AREA)
                                            for c in cs] for cs in comps]
        heads: list[list[np.ndarray]] = [[head_signature(c, lay.staff_gap) for c in cs]
                                         for cs in comps]
        times: list[list[float]] = [[g[0].t for g in gs] for gs in slot_groups]
        if os.environ.get("YTSCORE_DIAG"):
            import pickle
            for si, cs in enumerate(comps):
                if not cs:
                    continue
                imwrite(outdir / f"{name}_cores_slot{si}.png",
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
                ca, cb = extents[si][i]
                ch = None if chromes[si] is None else chromes[si][ca:cb]
                strip = render_strip(comps[si][i], lay.polarity, ch)
                candidates.append(Cand(t=times[si][i], si=si, strip=strip,
                                       core=cores[si][i], box=box_fps[si][i],
                                       head=heads[si][i],
                                       strength=ink_strength(comps[si][i]),
                                       sliding=i in slot_slides[si]))

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

    if os.environ.get("YTSCORE_DIAG"):
        # every candidate as it stands BEFORE the drop passes, named by slot and
        # time: the only way to tell a system the pipeline lost from one it
        # correctly refused to print twice.
        cdir = outdir / f"{name}_cands"
        cdir.mkdir(exist_ok=True)
        for old in cdir.glob("*.png"):
            old.unlink()
        for i, c in enumerate(prepared):
            imwrite(cdir / f"{i:03d}_s{c.si}_t{c.t:.1f}.png", c.strip)

    # Drop the systems that are mid-animation. Case 0 slides each line in with a
    # tilt, so the first and last groups are a skewed copy of a line that also
    # appears settled: their staff lines are not level, which shows up as a
    # collapsed staff-line coverage (0.22 against 0.90 for a settled system).
    covcut = 0.55 * float(np.median([c.cov for c in prepared]))
    settled = [c for c in prepared if c.cov >= covcut]
    unsettled = len(prepared) - len(settled)
    for c in prepared:
        if c.cov < covcut:
            log(f"order: dropped slot {c.si} t={c.t:.1f}s as mid-animation "
                f"(staff coverage {c.cov:.2f} < {covcut:.2f})")

    # ...and the ones that are not a system at all, but an EMPTY BOX. Two of the
    # customer's complaints are this same defect:
    #
    #   KsSlNq-ciko  "맨윗줄 공백" - system 000 is the uploader's intro panel,
    #                0.4% ink, all of it the panel's border rule. The coverage
    #                gate above cannot see it: that border rule is a full-width
    #                dark row, so staff_row_coverage reads a confident 1.000.
    #   YkjcWb63v0o  the "깨짐" half of "마지막장 같은마디 반복 깨짐" - its outro
    #                system prints twice and the faint copy is debris: a staff
    #                snapped at the top with a stray rule under it, 0.27% ink
    #                against the good copy's 3.6%. Every similarity pass is
    #                blind to that pair because the page moved 73 rows between
    #                the two frames, so the two views never line up to compare.
    #
    # Neither is a dedup problem, so neither is fixed by deleting more
    # aggressively on similarity -- which is what breaks d3t9j6DObN0, where five
    # near-identical systems are all real. Ink is the axis that separates them:
    # over the corpus's 630 prepared candidates the junk sits at 0.028-0.076 of
    # its own video's median and the faintest REAL system anywhere is ling's
    # 0.161 (a10 0.170, a06 0.171, case2 0.203, case0 0.215). The floor is the
    # geometric middle of that empty band. See ci/blank_check.py.
    shares = [ink_share(c.strip) for c in settled]
    inkcut = INK_SHARE_FLOOR * float(np.median(shares)) if shares else 0.0
    empty = [c for c, sh in zip(settled, shares) if sh < inkcut]
    for c, sh in zip(settled, shares):
        if sh < inkcut:
            log(f"order: dropped slot {c.si} t={c.t:.1f}s as an empty box "
                f"(ink share {sh:.4f} < {inkcut:.4f}, this video's median "
                f"{float(np.median(shares)):.4f})")
    if empty:
        settled = [c for c, sh in zip(settled, shares) if sh >= inkcut]

    # ...and the ones lifted out of a page that never stopped moving. The
    # coverage gate above catches a line that slides in TILTED (case 0); it
    # cannot see a page that scrolls LEVEL, because a level scroll leaves the
    # staff lines perfectly straight -- straighter, measurably, than this
    # customer's real music. That is what YkjcWb63v0o does at the end, and its
    # three sliding frames were printing the last system three extra times.
    # See `mark_travelling` and `sliding_groups`.
    sliding = [c for c in settled if c.sliding]
    for c in sliding:
        log(f"order: dropped slot {c.si} t={c.t:.1f}s as a mid-slide sample "
            f"(the page was still moving a sample period later)")
    if sliding:
        settled = [c for c in settled if not c.sliding]

    # On the scroll path each system was lifted from the page exactly once, at
    # its own content row, so there is nothing to dedup and every remaining
    # repeat is a repeat the composer wrote. Running the slot-repeat passes
    # there would delete real music.
    cross_slot = len(lay.systems) > 1 and not vtrack["scrolling"]
    # The picture on its own is NOT enough to call two strips the same system.
    # Bars 57 and 61 of zDG0Tw7MDXg are the same groove written twice, so their
    # strips sit 0.071 apart, well inside the 0.30 dedup distance, and bar 61 was
    # deleted from the PDF. What separates them is the measure number, which is
    # exactly what `head_signature` isolates and what `drop_adjacent_repeats`
    # already requires; this pass simply was not asking. A real cross-slot repeat
    # (a rolling display showing one system in two slots) carries the SAME number
    # and is still dropped.
    kept_cands: list[Cand] = []
    keys: list[np.ndarray] = []
    heads_seen: list[np.ndarray] = []
    dropped_dupes = 0
    for c in settled:
        k = signature(cv2.cvtColor(c.strip, cv2.COLOR_BGR2GRAY))
        dupe = None
        if cross_slot:
            for pk, ph in zip(keys[-4:], heads_seen[-4:]):
                d = jaccard(k, pk)
                if d > dedup_thresh:
                    continue
                hd = jaccard(ph, c.head) if c.head is not None and ph is not None else 0.0
                if hd <= HEAD_SAME:
                    dupe = (d, hd)
                    break
        if dupe is not None:
            log(f"order: dropped slot {c.si} t={c.t:.1f}s as a cross-slot repeat "
                f"(picture {dupe[0]:.3f}, header {dupe[1]:.3f})")
            dropped_dupes += 1
            continue
        kept_cands.append(c)
        keys.append(k)
        heads_seen.append(c.head)
    log(f"order: {len(candidates)} candidates -> {len(kept_cands)} systems "
        f"({unsettled} mid-animation, {dropped_dupes} cross-slot repeats dropped)")

    if os.environ.get("YTSCORE_DIAG"):
        import pickle
        with open(outdir / f"{name}_cands.pkl", "wb") as fh:
            pickle.dump([{"t": c.t, "si": c.si, "core": c.core, "box": c.box,
                          "head": c.head, "strength": c.strength,
                          "cov": c.cov} for c in kept_cands], fh)

    if vtrack["scrolling"]:
        faded, repeats = [], []
    else:
        kept_cands, faded = drop_fade_copies(kept_cands)
        kept_cands, repeats = drop_adjacent_repeats(kept_cands)
    cleaned: list[np.ndarray] = [c.strip for c in kept_cands]

    if drop_idx:
        bad = sorted({i for i in drop_idx if 0 <= i < len(cleaned)}, reverse=True)
        for i in bad:
            cleaned.pop(i)
        log(f"order: dropped systems {sorted(bad)} by hand -> {len(cleaned)} systems")

    # ---- the scroll guard, mechanism 2: a sanity bound on the yield.
    # The travel measurement above is the diagnosis; this is the backstop that
    # fires on the SYMPTOM, whatever caused it. When dedup stops deduping, the
    # system count leaves the physically sensible range by an order of
    # magnitude: the ten acceptance videos run 6-11 systems per minute of video
    # and turn 700+ frames into 21-42 systems, while the customer's scrolling
    # link produced 672 systems from 728 frames (a 45 page PDF reported as OK).
    # Both bounds have to break before this refuses, and both sit ~4x outside
    # the worst real video, so a genuinely dense score is never caught by it.
    #
    # Tier 2 (systems against the DURATION of the video) is the one the vertical
    # scroll needed: H_uW2B5A1kE printed 75 systems for a 25 system chart and
    # sailed straight through tier 1, because 75 is not 672 and 0.03 is not 0.35.
    # A system is roughly four bars, so its count per minute is bounded by the
    # tempo: the fourteen verified videos run 5.1 to 9.6 systems per minute and
    # the broken run measured 19.0. The cut sits at 16.0, two thirds above the
    # worst real video and still below the failure.
    #
    # Tier 3 is exact rather than statistical, and only exists on a page that
    # really did scroll: the page cannot contain more systems than its own
    # travel plus one screen can hold. It is the bound that would have caught
    # the 3x over-count on the first run, whatever the cause.
    per_min = len(cleaned) / max(duration / 60.0, 0.1)
    yield_ratio = len(cleaned) / float(max(kept, 1))
    log(f"sanity: {len(cleaned)} systems, {per_min:.1f}/min of video, "
        f"{yield_ratio:.3f} systems per collected frame")
    if len(cleaned) >= 60 and per_min >= 45.0 and yield_ratio >= 0.35:
        raise ScrollingScore(
            f"dedup did not converge: {len(cleaned)} systems out of {kept} "
            f"collected frames ({per_min:.0f} per minute of video), which is what "
            f"a continuously moving score looks like")
    if len(cleaned) >= 30 and per_min >= SANITY_MAX_PER_MIN:
        raise ScrollingScore(
            f"{len(cleaned)} systems for {duration / 60.0:.1f} minutes of video is "
            f"{per_min:.0f} per minute, against 5-10 on every verified score: the "
            f"same music is being printed more than once")
    if vtrack["scrolling"] and vtrack["step_px"] > 0:
        span = vtrack["travel_px"] + (vtrack["band"][1] - vtrack["band"][0])
        room = span / max(vtrack["step_px"], 1.0)
        log(f"sanity: the page travelled {vtrack['travel_px']:.0f}px in "
            f"{vtrack['step_px']:.0f}px steps, so it holds about {room:.0f} systems")
        if len(cleaned) > 1.6 * room:
            raise ScrollingScore(
                f"{len(cleaned)} systems were printed from a page that only "
                f"scrolled {span:.0f}px, i.e. room for about {room:.0f}: the same "
                f"music is being printed more than once")

    report(0.88)
    if not title_override and not meta.get("title"):
        # the metadata call can lose a race with YouTube's rate limiter; the
        # download proved the network works, so it is worth exactly one retry
        meta = video_meta(url, proxy) or meta
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
        imwrite(strip_dir / f"{i:03d}.png", s)
        bars.append(count_measures(s))

    elapsed = time.time() - t0
    result = {
        "customerId": CUSTOMER_ID, "url": url, "title": title,
        "polarity": lay.polarity, "slots": len(lay.systems),
        "system_boxes": lay.systems, "staff_gap": round(lay.staff_gap, 1),
        "sampled": nframes, "score_frames": kept, "rejected": rejected,
        "candidates": len(candidates), "unsettled_dropped": unsettled,
        "sliding_dropped": len(sliding), "empty_dropped": len(empty),
        "cross_slot_dupes": dropped_dupes, "slot_mode": mode,
        "scroll": scroll_stats, "systems_per_min": round(per_min, 1),
        "vscroll": {k: v for k, v in vtrack.items() if k != "offsets"},
        "vscroll_extract": vinfo,
        "systems_per_frame": round(yield_ratio, 4),
        "fade_copies_dropped": faded, "redrawn_copies_dropped": repeats,
        "systems": len(cleaned), "dropped_by_hand": sorted(drop_idx or []),
        "video_meta": meta, "duration_sec": round(duration, 1),
        "measures_per_system": bars,
        "measures_total": int(sum(bars)), "pages": npages,
        "pdf": str(pdf), "systems_dir": str(strip_dir),
        "elapsed_sec": round(elapsed, 1), "version": APP_VERSION,
    }
    # encoding is NOT optional here: Path.write_text defaults to the locale
    # codec, which on the customer's Windows is cp949/cp1252, and a Korean
    # YouTube title in this JSON then raises UnicodeEncodeError and fails the
    # whole conversion after the PDF has already been written.
    (outdir / f"{name}.run.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
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
