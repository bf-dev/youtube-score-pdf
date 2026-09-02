# -*- coding: utf-8 -*-
"""
The window the customer uses. tkinter, Korean throughout.

Everything the customer can change is on this screen: the links, the PDF title,
the output folder. There is no config file to edit and no flag to type. The
conversion runs on a worker thread so the window never freezes, and every button
handler is wrapped so a bug shows a dialog instead of killing the program.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import webbrowser
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, SUNKEN, StringVar, Tk, X, Y, filedialog, messagebox
from tkinter import ttk
import tkinter as tk

from ytscore import bridge, config, paths
from ytscore.pipeline import Cancelled, DownloadFailed, ScoreNotFound

BG = "#f4f5f7"
CARD = "#ffffff"
INK = "#1c1f23"
MUTED = "#6b7280"
ACCENT = "#1f6feb"
OK = "#137333"
BAD = "#b3261e"

YT_RE = re.compile(r"(?:youtube\.com/(?:watch\?[^ ]*v=|shorts/|live/|embed/)|youtu\.be/)"
                   r"([A-Za-z0-9_-]{6,})")


def _font(size: int = 10, bold: bool = False) -> tuple:
    fam = "맑은 고딕" if os.name == "nt" else "DejaVu Sans"
    return (fam, size, "bold") if bold else (fam, size)


def safe_name(text: str, fallback: str = "score") -> str:
    """A filename Windows will accept, keeping Hangul."""
    text = unicodedata.normalize("NFC", text or "").strip()
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text or fallback)[:80]


class Settings:
    """The customer's own choices, remembered between runs."""

    def __init__(self) -> None:
        self.path = paths.app_data_dir() / "settings.json"
        self.data = {"outdir": str(paths.default_output_dir())}
        try:
            if self.path.is_file():
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            pass

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except Exception:
            pass


def korean_error(exc: BaseException) -> str:
    """Every failure the customer can hit, in plain Korean."""
    if isinstance(exc, Cancelled):
        return "사용자가 중지했습니다."
    if isinstance(exc, DownloadFailed):
        m = str(exc).lower()
        if "private" in m or "sign in" in m or "login" in m or "age" in m:
            return "비공개 또는 로그인이 필요한 영상이라 내려받을 수 없습니다."
        if "unavailable" in m or "not available" in m or "removed" in m:
            return "삭제되었거나 볼 수 없는 영상입니다. 링크를 확인해 주세요."
        if "not a valid url" in m or "unsupported url" in m or "no video" in m:
            return "유튜브 링크가 아닌 것 같습니다. 주소를 다시 확인해 주세요."
        if "timed out" in m or "connection" in m or "network" in m or "resolve" in m:
            return "인터넷 연결이 불안정해 영상을 내려받지 못했습니다. 잠시 후 다시 시도해 주세요."
        return "영상을 내려받지 못했습니다. 링크가 맞는지, 비공개 영상이 아닌지 확인해 주세요."
    if isinstance(exc, ScoreNotFound):
        return "이 영상에서는 악보를 찾지 못했습니다. 악보가 화면에 보이는 영상인지 확인해 주세요."
    msg = str(exc)
    if "ffmpeg" in msg.lower():
        return "영상을 읽지 못했습니다(영상 형식 오류). 다른 화질이나 다른 영상으로 시도해 주세요."
    return f"변환 중 오류가 발생했습니다: {msg[:160]}"


class App:
    def __init__(self, root: Tk, demo: dict | None = None) -> None:
        self.root = root
        self.settings = Settings()
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel = threading.Event()
        self.done_paths: list[Path] = []
        self.demo = demo or {}
        self.session_log: list[str] = []

        root.title(f"{config.APP_NAME} v{config.APP_VERSION}")
        root.configure(bg=BG)
        root.geometry("980x760")
        root.minsize(880, 700)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=INK, font=_font(10))
        style.configure("Card.TLabel", background=CARD, foreground=INK, font=_font(10))
        style.configure("Hint.TLabel", background=CARD, foreground=MUTED, font=_font(9))
        style.configure("H1.TLabel", background=BG, foreground=INK, font=_font(16, True))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=_font(9))
        style.configure("TButton", font=_font(10), padding=6)
        style.configure("Go.TButton", font=_font(11, True), padding=(18, 9))
        style.configure("TEntry", fieldbackground="#ffffff")
        style.configure("Bar.Horizontal.TProgressbar", troughcolor="#e5e7eb",
                        background=ACCENT, thickness=14)

        self._build()
        self.root.after(120, self._pump)

    # ------------------------------------------------------------------ layout
    def _card(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent, style="Card.TFrame", padding=14)
        f.pack(fill=X, pady=(0, 12))
        return f

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill=BOTH, expand=True)

        head = ttk.Frame(outer)
        head.pack(fill=X, pady=(0, 14))
        ttk.Label(head, text="유튜브 악보 PDF 변환기", style="H1.TLabel").pack(anchor="w")
        ttk.Label(head, text="유튜브 영상에 나오는 악보를 모아 A4 PDF 한 개로 만들어 드립니다.",
                  style="Sub.TLabel").pack(anchor="w", pady=(3, 0))

        # links
        c1 = self._card(outer)
        ttk.Label(c1, text="유튜브 링크", style="Card.TLabel", font=_font(11, True)).pack(anchor="w")
        ttk.Label(c1, text="한 줄에 하나씩 붙여넣으세요. 여러 개를 넣으면 위에서부터 차례대로 변환합니다.",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 8))
        box = ttk.Frame(c1, style="Card.TFrame")
        box.pack(fill=X)
        self.links = tk.Text(box, height=6, font=_font(10), relief="solid", bd=1,
                             highlightthickness=0, wrap="none", bg="#ffffff", fg=INK,
                             insertbackground=INK)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.links.yview)
        self.links.configure(yscrollcommand=sb.set)
        self.links.pack(side=LEFT, fill=X, expand=True)
        sb.pack(side=RIGHT, fill=Y)

        # title + folder
        c2 = self._card(outer)
        ttk.Label(c2, text="PDF 제목 (선택)", style="Card.TLabel",
                  font=_font(11, True)).pack(anchor="w")
        ttk.Label(c2, text="비워두면 유튜브 영상 제목을 그대로 사용합니다.",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 6))
        self.title_var = StringVar()
        ttk.Entry(c2, textvariable=self.title_var, font=_font(10)).pack(fill=X)

        ttk.Label(c2, text="저장 폴더", style="Card.TLabel",
                  font=_font(11, True)).pack(anchor="w", pady=(14, 0))
        row = ttk.Frame(c2, style="Card.TFrame")
        row.pack(fill=X, pady=(6, 0))
        self.out_var = StringVar(value=self.settings.data["outdir"])
        ttk.Entry(row, textvariable=self.out_var, font=_font(10)).pack(side=LEFT, fill=X,
                                                                      expand=True)
        ttk.Button(row, text="폴더 선택", command=self.h(self.pick_dir)).pack(side=LEFT, padx=(8, 0))

        # actions
        act = ttk.Frame(outer)
        act.pack(fill=X, pady=(0, 10))
        self.go = ttk.Button(act, text="변환 시작", style="Go.TButton",
                             command=self.h(self.start))
        self.go.pack(side=LEFT)
        self.stop = ttk.Button(act, text="중지", command=self.h(self.request_stop),
                               state="disabled")
        self.stop.pack(side=LEFT, padx=(10, 0))
        self.open_btn = ttk.Button(act, text="폴더 열기", command=self.h(self.open_dir),
                                   state="disabled")
        self.open_btn.pack(side=LEFT, padx=(10, 0))

        # progress
        c3 = self._card(outer)
        self.status = StringVar(value="유튜브 링크를 붙여넣고 [변환 시작]을 눌러 주세요.")
        self.status_lbl = ttk.Label(c3, textvariable=self.status, style="Card.TLabel",
                                    font=_font(11, True), wraplength=880, justify="left")
        self.status_lbl.pack(anchor="w")
        self.bar = ttk.Progressbar(c3, style="Bar.Horizontal.TProgressbar",
                                   maximum=1000, value=0)
        self.bar.pack(fill=X, pady=(10, 4))
        self.sub = StringVar(value="")
        ttk.Label(c3, textvariable=self.sub, style="Hint.TLabel").pack(anchor="w")

        ttk.Label(outer, text="진행 내용", style="Sub.TLabel").pack(anchor="w")
        logbox = ttk.Frame(outer)
        logbox.pack(fill=BOTH, expand=True, pady=(4, 0))
        self.logv = tk.Text(logbox, height=10, font=("Consolas" if os.name == "nt"
                                                     else "DejaVu Sans Mono", 9),
                            relief="solid", bd=1, bg="#ffffff", fg="#374151",
                            state="disabled", wrap="word")
        lsb = ttk.Scrollbar(logbox, orient="vertical", command=self.logv.yview)
        self.logv.configure(yscrollcommand=lsb.set)
        self.logv.pack(side=LEFT, fill=BOTH, expand=True)
        lsb.pack(side=RIGHT, fill=Y)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ helpers
    def h(self, fn):
        """Wrap a handler: a bug shows a dialog, it never kills the window."""
        def inner(*a, **k):
            try:
                return fn(*a, **k)
            except Exception as exc:                         # noqa: BLE001
                bridge.send_crash(exc, "\n".join(self.session_log[-400:]))
                messagebox.showerror("오류", korean_error(exc))
        return inner

    def log(self, line: str) -> None:
        self.q.put(("log", line))

    def _append(self, line: str) -> None:
        self.session_log.append(line)
        if len(self.session_log) > 6000:
            del self.session_log[:2000]
        self.logv.configure(state="normal")
        self.logv.insert(END, line + "\n")
        self.logv.see(END)
        self.logv.configure(state="disabled")

    def pick_dir(self) -> None:
        d = filedialog.askdirectory(title="PDF를 저장할 폴더를 선택하세요",
                                    initialdir=self.out_var.get() or str(Path.home()))
        if d:
            self.out_var.set(d)
            self.settings.data["outdir"] = d
            self.settings.save()

    def open_dir(self) -> None:
        d = self.out_var.get()
        if not d or not Path(d).is_dir():
            messagebox.showinfo("알림", "아직 저장된 폴더가 없습니다.")
            return
        if os.name == "nt":
            os.startfile(d)                                  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        else:
            webbrowser.open(f"file://{d}")

    def request_stop(self) -> None:
        self.cancel.set()
        self.status.set("중지하는 중입니다. 진행 중인 단계가 끝나면 멈춥니다...")

    def on_close(self) -> None:
        self.cancel.set()
        self.root.destroy()

    def parse_links(self) -> list[str]:
        raw = self.links.get("1.0", END)
        out: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            for part in re.split(r"[\s,]+", line):
                if part.startswith("http") or YT_RE.search(part):
                    if part not in out:
                        out.append(part)
        return out

    # ------------------------------------------------------------------ running
    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        urls = self.parse_links()
        if not urls:
            messagebox.showwarning("링크가 없습니다",
                                   "유튜브 링크를 한 줄에 하나씩 붙여넣어 주세요.")
            return
        outdir = Path(self.out_var.get() or paths.default_output_dir())
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except Exception:
            messagebox.showerror("저장 폴더 오류",
                                 "저장 폴더를 만들 수 없습니다. 다른 폴더를 선택해 주세요.")
            return
        self.settings.data["outdir"] = str(outdir)
        self.settings.save()

        self.cancel.clear()
        self.done_paths = []
        self.go.configure(state="disabled")
        self.stop.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.bar.configure(value=0)
        self.logv.configure(state="normal")
        self.logv.delete("1.0", END)
        self.logv.configure(state="disabled")
        title = self.title_var.get().strip() or None
        self.worker = threading.Thread(target=self._run_all, args=(urls, outdir, title),
                                       daemon=True, name="convert")
        self.worker.start()

    def _run_all(self, urls: list[str], outdir: Path, title: str | None) -> None:
        from ytscore import pipeline

        pipeline.set_log_sink(self.log)
        pipeline.set_cancel_event(self.cancel)
        total = len(urls)
        ok, failed = [], []
        for n, url in enumerate(urls, 1):
            if self.cancel.is_set():
                break
            self.q.put(("phase", (n, total, url)))
            started = time.time()
            run_log: list[str] = []
            sink = self.log

            def collect(line: str, _rl=run_log, _s=sink):
                _rl.append(line)
                _s(line)

            pipeline.set_log_sink(collect)
            # Keyed by the URL, not by the position in the queue. "job1" was shared
            # by the first link of every session, so a leftover download from an
            # earlier, interrupted conversion sat exactly where the next one looked.
            work = (paths.app_data_dir() / "work"
                    / hashlib.sha1(url.encode("utf-8")).hexdigest()[:16])
            name = safe_name(title, "") if title and total == 1 else ""
            result = None
            try:
                result = pipeline.run(
                    url=url, workdir=work, outdir=outdir, fps=4.0, dedup_thresh=0.30,
                    proxy=os.environ.get("YTSCORE_PROXY") or None,
                    name=name or f"__tmp_{int(time.time())}",
                    title_override=title,
                    progress=lambda f, i=n, t=total: self.q.put(("prog", (i, t, f))),
                    keep_video=False)
                final = self._finalise(Path(result["pdf"]), result["title"], outdir, title)
                result["pdf"] = str(final)
                ok.append(final)
                self.q.put(("one_done", (n, total, final, result)))
            except BaseException as exc:                      # noqa: BLE001
                msg = korean_error(exc)
                failed.append((url, msg))
                self.q.put(("one_fail", (n, total, url, msg)))
                if not isinstance(exc, Cancelled):
                    bridge.send_crash(exc, "\n".join(run_log[-400:]))
            finally:
                self._report(url, run_log, result, time.time() - started,
                             failed[-1][1] if failed and failed[-1][0] == url else None)
            pipeline.set_log_sink(self.log)
        self.q.put(("all_done", (ok, failed)))

    def _finalise(self, pdf: Path, video_title: str, outdir: Path,
                  title_override: str | None) -> Path:
        """Give the PDF the customer-visible name, without ever overwriting one."""
        base = safe_name(title_override or video_title, "악보")
        target = outdir / f"{base}.pdf"
        k = 2
        # `not samefile` is load-bearing. The pipeline has already written its own
        # PDF into outdir under a name derived from the SAME title, so on a first
        # conversion `target` IS `pdf`, `target.exists()` is true, and the loop
        # used to rename the customer's very first output to "제목 (2).pdf".
        while target.exists() and not (pdf.exists() and target.samefile(pdf)):
            target = outdir / f"{base} ({k}).pdf"
            k += 1
        try:
            pdf.replace(target)
        except Exception:
            return pdf
        for junk in (pdf.parent / f"{pdf.stem}.run.json",):
            try:
                junk.unlink()
            except Exception:
                pass
        strips = pdf.parent / f"{pdf.stem}_systems"
        if strips.is_dir():
            for f in strips.glob("*.png"):
                try:
                    f.unlink()
                except Exception:
                    pass
            try:
                strips.rmdir()
            except Exception:
                pass
        return target

    def _report(self, url: str, run_log: list[str], result: dict | None,
                elapsed: float, error: str | None) -> None:
        """One Artifacts report per conversion. Never surfaced in the UI."""
        try:
            summary = (f"url={url}\nelapsed={elapsed:.1f}s\n"
                       f"result={'OK' if result else 'FAILED'}\n")
            if result:
                summary += (f"systems={result.get('systems')} pages={result.get('pages')} "
                            f"polarity={result.get('polarity')} "
                            f"slot_mode={result.get('slot_mode')} "
                            f"fade_dropped={result.get('fade_copies_dropped')}\n"
                            f"pdf={result.get('pdf')}\n")
            if error:
                summary += f"error={error}\n"
            parts = {
                "run.log": "\n".join(run_log),
                "diagnostics.json": json.dumps(bridge.diagnostics(
                    {"url": url, "elapsed_sec": round(elapsed, 1),
                     "outdir": self.out_var.get(),
                     "title_field": self.title_var.get(),
                     "queue_size": len(self.parse_links())}),
                    ensure_ascii=False, indent=2),
            }
            if result:
                parts["run.json"] = json.dumps(result, ensure_ascii=False, indent=2,
                                               default=str)
                parts["video-meta.json"] = json.dumps(result.get("video_meta") or {},
                                                      ensure_ascii=False, indent=2)
            bridge.send(text=summary, parts=parts,
                        tag="ok" if result else "fail")
        except Exception:
            pass

    # ------------------------------------------------------------------ ui pump
    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "phase":
                    n, total, url = payload
                    self.status.set(f"({n}/{total}) 변환 중입니다...")
                    self.sub.set(url)
                elif kind == "prog":
                    n, total, f = payload
                    self.bar.configure(value=int(1000 * ((n - 1) + f) / total))
                    self.sub.set(self._phase_text(f))
                elif kind == "one_done":
                    n, total, path, result = payload
                    self.done_paths.append(path)
                    self.status.set(f"({n}/{total}) 완료: {path.name}")
                    self._append(f"=> PDF 저장: {path}  "
                                 f"(악보 {result.get('systems')}줄, {result.get('pages')}쪽)")
                elif kind == "one_fail":
                    n, total, url, msg = payload
                    self.status.set(f"({n}/{total}) 실패: {msg}")
                    self._append(f"!! {url} -> {msg}")
                elif kind == "all_done":
                    self._finish(*payload)
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    @staticmethod
    def _phase_text(f: float) -> str:
        if f < 0.24:
            return "영상을 내려받는 중..."
        if f < 0.33:
            return "악보 위치와 색을 분석하는 중..."
        if f < 0.78:
            return "화면에서 악보를 모으는 중..."
        if f < 0.9:
            return "같은 줄을 겹쳐 지우고 정리하는 중..."
        return "PDF를 만드는 중..."

    def _finish(self, ok: list[Path], failed: list) -> None:
        self.go.configure(state="normal")
        self.stop.configure(state="disabled")
        self.bar.configure(value=1000 if ok and not failed else self.bar["value"])
        if ok:
            self.open_btn.configure(state="normal")
        if ok and not failed:
            self.status.set(f"완료되었습니다. PDF {len(ok)}개를 저장했습니다: "
                            f"{', '.join(p.name for p in ok[:3])}"
                            f"{' 외' if len(ok) > 3 else ''}")
            self.status_lbl.configure(foreground=OK)
            # CI screenshot mode: a modal dialog would own the foreground window
            # and the capture would be of the dialog, not of the app.
            if not self.demo:
                messagebox.showinfo("변환 완료",
                                    f"PDF {len(ok)}개를 저장했습니다.\n\n"
                                    f"저장 위치: {self.out_var.get()}\n\n"
                                    "[폴더 열기] 버튼으로 바로 열 수 있습니다.")
        elif ok and failed:
            self.status.set(f"{len(ok)}개 완료, {len(failed)}개 실패했습니다. "
                            "실패한 링크는 아래 진행 내용을 확인해 주세요.")
            self.status_lbl.configure(foreground=ACCENT)
        elif failed:
            self.status.set(failed[0][1])
            self.status_lbl.configure(foreground=BAD)
        else:
            self.status.set("중지했습니다.")
            self.status_lbl.configure(foreground=MUTED)


def run_gui(demo: dict | None = None) -> int:
    root = Tk()
    app = App(root, demo=demo)
    if demo:
        _wire_demo(root, app, demo)
    root.mainloop()
    return 0


def _wire_demo(root: Tk, app: App, demo: dict) -> None:
    """
    CI only: fill the window in, run a real conversion, hold it on screen for the
    screenshot. Never reachable from the customer's build path.
    """
    if demo.get("url"):
        app.links.insert("1.0", demo["url"])
    if demo.get("title"):
        app.title_var.set(demo["title"])
    if demo.get("outdir"):
        app.out_var.set(demo["outdir"])
    hold = int(demo.get("hold", 240000))
    if demo.get("autostart", True):
        root.after(1500, app.start)
    root.after(hold, root.destroy)
