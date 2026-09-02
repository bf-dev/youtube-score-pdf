# -*- coding: utf-8 -*-
"""
Artifacts API reporter: every run of this program tells us what it did.

Contract (no auth):
    POST https://works.insu.ng/works/api
    multipart: customerId=1775529  source=ytscore-desktop-diag  text=<summary>  file=<zip>
    200 -> {"success": true, "data": {"id", "matched", ...}}
Only customerId / source / text / file are read; any other field is discarded,
so everything else has to be folded into `text` or into the zip.

Rules this file exists to keep:
  * it must never break the customer's program. Every path is wrapped, the post
    runs on a daemon thread, and nothing here is ever awaited by the UI.
  * one report per conversion (success or failure), never a stream of them.
  * bounded: the log is truncated from the MIDDLE (head and tail are what
    matter) and the zip is capped, so a three-hour session cannot mail us 400MB.
  * no credentials. This app has none, but the redaction pass stays anyway
    because a future revision might introduce cookies.
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import sys
import threading
import time
import traceback
import zipfile
from pathlib import Path

from ytscore import config

MAX_TEXT = 8000
MAX_LOG_CHARS = 400_000
MAX_ZIP_BYTES = 4_000_000

_SECRET = re.compile(r"(?i)(password|passwd|token|api[-_ ]?key|authorization|cookie)"
                     r"(\s*[:=]\s*)(\S+)")


def _redact(text: str) -> str:
    try:
        return _SECRET.sub(r"\1\2***", text)
    except Exception:
        return text


def _truncate_middle(text: str, limit: int = MAX_LOG_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2:]
    return f"{head}\n\n... [{len(text) - limit} characters cut from the middle] ...\n\n{tail}"


def diagnostics(extra: dict | None = None) -> dict:
    d = {
        "customerId": config.CUSTOMER_ID,
        "app": config.APP_SLUG,
        "appVersion": config.APP_VERSION,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "machine": platform.machine(),
        "locale_encoding": sys.getdefaultencoding(),
        "exe": sys.executable,
        "cwd": os.getcwd(),
        "localTime": time.strftime("%Y-%m-%d %H:%M:%S %z"),
    }
    try:
        from ytscore import paths
        d["ffmpeg"] = paths.ffmpeg()
        d["font"] = paths.kr_font_path()
        d["dataDir"] = str(paths.app_data_dir())
    except Exception:
        pass
    try:
        import yt_dlp
        d["yt_dlp"] = yt_dlp.version.__version__
    except Exception:
        d["yt_dlp"] = None
    try:
        import cv2
        d["cv2"] = cv2.__version__
    except Exception:
        d["cv2"] = None
    if extra:
        d.update(extra)
    return d


def _build_zip(name: str, parts: dict[str, bytes], files: list[Path]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for fname, data in parts.items():
            zf.writestr(f"{name}/{fname}", data)
        for p in files:
            try:
                if p.is_file() and p.stat().st_size < MAX_ZIP_BYTES:
                    zf.write(p, f"{name}/{p.name}")
            except Exception:
                pass
    return buf.getvalue()


def _post(source: str, text: str, zip_name: str | None, zip_bytes: bytes | None) -> dict:
    import requests
    data = {"customerId": config.CUSTOMER_ID, "source": source,
            "text": _redact(text)[:MAX_TEXT]}
    files = None
    if zip_bytes:
        files = {"file": (zip_name, zip_bytes, "application/zip")}
    r = requests.post(config.WORKS_API, data=data, files=files, timeout=45)
    out = {"status": r.status_code}
    try:
        out.update(r.json())
    except Exception:
        out["body"] = r.text[:300]
    return out


def send(text: str, parts: dict[str, str] | None = None,
         files: list[Path] | None = None, source: str | None = None,
         tag: str = "run", blocking: bool = False) -> dict | None:
    """
    Fire one report. Returns the parsed response when blocking (CI proof),
    None otherwise. Never raises.
    """
    source = source or config.ARTIFACT_SOURCE
    stamp = time.strftime("%Y%m%d-%H%M%S")
    zip_name = f"ytscore-{config.CUSTOMER_ID}-{tag}-{stamp}.zip"
    inner = f"ytscore-{config.CUSTOMER_ID}-{tag}-{stamp}"

    def work() -> dict | None:
        try:
            blobs = {k: _truncate_middle(_redact(v)).encode("utf-8")
                     for k, v in (parts or {}).items()}
            zip_bytes = _build_zip(inner, blobs, list(files or []))
            if len(zip_bytes) > MAX_ZIP_BYTES:
                blobs.pop("page1.png", None)
                zip_bytes = _build_zip(inner, blobs, [])
            head = f"[{config.APP_SLUG} {config.APP_VERSION}] customer {config.CUSTOMER_ID}\n"
            return _post(source, head + text, zip_name, zip_bytes)
        except Exception:
            try:
                return _post(source, f"[reporter fallback] customer {config.CUSTOMER_ID}\n"
                                     f"{text}\n{traceback.format_exc()}", None, None)
            except Exception:
                return None

    if blocking:
        return work()
    threading.Thread(target=work, daemon=True, name="artifacts").start()
    return None


def send_crash(exc: BaseException, log_text: str = "") -> None:
    """One report per unhandled error, on top of the per-run report."""
    try:
        send(text=f"UNHANDLED ERROR: {type(exc).__name__}: {exc}",
             parts={"traceback.txt": "".join(traceback.format_exception(
                        type(exc), exc, exc.__traceback__)),
                    "diagnostics.json": json.dumps(diagnostics(), ensure_ascii=False, indent=2),
                    "run.log": log_text},
             tag="crash")
    except Exception:
        pass
