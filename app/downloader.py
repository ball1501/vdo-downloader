"""Download queue: worker threads running yt-dlp with progress hooks."""

from __future__ import annotations

import os
import queue
import shutil
import tempfile
import threading
import time
import uuid

import yt_dlp

from . import extractor, ytstrip

# transparently unwrap CDNs that disguise video segments as PNG/.webp files
ytstrip.install()

MAX_HISTORY = 100


class Job:
    def __init__(
        self,
        url: str,
        format_id: str,
        outdir: str,
        display_title: str | None,
        media_url: str | None = None,
        media_headers: dict | None = None,
        cookies: list | None = None,
    ):
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.format_id = format_id
        self.outdir = outdir
        self.display_title = display_title or url
        self.media_url = media_url or url
        self.media_headers = media_headers
        self.cookies = cookies
        self.title: str | None = None
        self.status = "queued"  # queued|extracting|downloading|processing|done|error|cancelled
        self.progress: float | None = None
        self.speed: float | None = None
        self.eta: int | None = None
        self.filename: str | None = None
        self.error: str | None = None
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.cancel_event = threading.Event()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title or self.display_title,
            "format_id": self.format_id,
            "outdir": self.outdir,
            "status": self.status,
            "progress": self.progress,
            "speed": self.speed,
            "eta": self.eta,
            "filename": self.filename,
            "error": self.error,
            "created_at": self.created_at,
        }


class JobManager:
    def __init__(self, max_workers: int = 2, default_outdir: str = "downloads"):
        self.default_outdir = default_outdir
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.Lock()
        self._version = 0
        self._workers = [
            threading.Thread(target=self._worker, daemon=True, name=f"dl-worker-{i}")
            for i in range(max_workers)
        ]
        for t in self._workers:
            t.start()

    # ---- state -----------------------------------------------------------
    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [self._jobs[jid].to_dict() for jid in self._order]

    def _bump(self) -> None:
        with self._lock:
            self._version += 1

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    # ---- queue operations --------------------------------------------------
    def submit(
        self,
        url: str,
        format_id: str,
        outdir: str | None = None,
        title: str | None = None,
        media_url: str | None = None,
        media_headers: dict | None = None,
        cookies: list | None = None,
    ) -> Job:
        outdir = os.path.abspath(os.path.expanduser(outdir or self.default_outdir))
        job = Job(url, format_id, outdir, title, media_url, media_headers, cookies)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune_locked()
        self._queue.put(job.id)
        self._bump()
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        if job.status in ("queued", "extracting", "downloading", "processing"):
            job.cancel_event.set()
            if job.status == "queued":  # worker will skip it
                job.status = "cancelled"
                job.finished_at = time.time()
                self._bump()
            return True
        return False

    def _prune_locked(self) -> None:
        if len(self._order) <= MAX_HISTORY:
            return
        terminal = ("done", "error", "cancelled")
        kept: list[str] = []
        dropped = 0
        for jid in self._order:
            job = self._jobs[jid]
            if job.status in terminal and len(self._order) - dropped > MAX_HISTORY:
                del self._jobs[jid]
                dropped += 1
            else:
                kept.append(jid)
        self._order = kept

    # ---- workers -----------------------------------------------------------
    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is None or job.cancel_event.is_set():
                continue
            try:
                self._run_job(job)
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                job.status = "error"
                job.error = str(exc) or exc.__class__.__name__
                job.finished_at = time.time()
                self._bump()
            finally:
                self._queue.task_done()

    def _run_job(self, job: Job) -> None:
        job.status = "extracting"
        self._bump()

        media_url = job.media_url
        headers = dict(job.media_headers or {})
        cookies = job.cookies
        if job.media_url == job.url:  # client gave no resolved media URL -> resolve now
            try:
                result = extractor.extract_formats(job.url)
            except extractor.ExtractError as exc:
                if job.cancel_event.is_set():
                    job.status = "cancelled"
                else:
                    job.status = "error"
                    job.error = str(exc)
                job.finished_at = time.time()
                self._bump()
                return
            job.title = result["title"]
            media_url = result["media_url"] or job.url
            headers = dict(result.get("media_headers") or {})
            cookies = result.get("cookies")
        else:
            job.title = job.display_title

        if job.cancel_event.is_set():
            job.status = "cancelled"
            job.finished_at = time.time()
            self._bump()
            return

        job.status = "downloading"
        self._bump()

        os.makedirs(job.outdir, exist_ok=True)

        def hook(d: dict) -> None:
            if job.cancel_event.is_set():
                raise yt_dlp.utils.DownloadCancelled()
            status = d.get("status")
            if status == "downloading":
                job.status = "downloading"
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                if total:
                    job.progress = round(done / total * 100, 1)
                job.speed = d.get("speed")
                job.eta = d.get("eta")
                self._bump()
            elif status == "finished":
                job.progress = 100.0
                job.speed = None
                job.eta = None
                self._bump()

        def pp_hook(d: dict) -> None:
            if job.cancel_event.is_set():
                raise yt_dlp.utils.DownloadCancelled()
            if d.get("status") == "started":
                job.status = "processing"
                self._bump()

        opts = {
            "outtmpl": os.path.join(job.outdir, "%(title)s.%(ext)s"),
            "format": _format_selector(job.format_id),
            "progress_hooks": [hook],
            "postprocessor_hooks": [pp_hook],
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "retries": 5,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 5,
            "http_headers": {
                "User-Agent": extractor.USER_AGENT,
                "Referer": job.url,
                "Accept-Language": "th,en;q=0.8",
                **headers,
            },
        }
        cookie_file = _write_cookie_file(cookies) if cookies else None
        if cookie_file:
            opts["cookiefile"] = cookie_file
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        if os.path.exists(ffmpeg):
            opts["ffmpeg_location"] = ffmpeg

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(media_url, download=True)
                info = ydl.sanitize_info(info)
                requested = (info or {}).get("requested_downloads") or []
                if requested and requested[0].get("filepath"):
                    job.filename = requested[0]["filepath"]
            if job.cancel_event.is_set():
                job.status = "cancelled"
            else:
                job.status = "done"
                job.progress = 100.0
        except yt_dlp.utils.DownloadCancelled:
            job.status = "cancelled"
        except yt_dlp.utils.DownloadError as exc:
            job.status = "error"
            job.error = str(exc)
        finally:
            if cookie_file:
                try:
                    os.unlink(cookie_file)
                except OSError:
                    pass
        job.finished_at = time.time()
        self._bump()


def _write_cookie_file(cookies: list) -> str | None:
    """Convert sniffed cookie dicts into a Netscape cookie file for yt-dlp."""
    if not cookies:
        return None
    fd, path = tempfile.mkstemp(prefix="vdo-cookies-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            domain = c.get("domain") or ""
            include_sub = "TRUE" if domain.startswith(".") else "FALSE"
            expires = int(c.get("expires") or 2147483647) or 2147483647
            fh.write(
                f"{domain}\t{include_sub}\t{c.get('path') or '/'}\t"
                f"{'TRUE' if c.get('secure') else 'FALSE'}\t{expires}\t"
                f"{c.get('name')}\t{c.get('value')}\n"
            )
    return path


def _format_selector(format_id: str) -> str:
    """Turn a format_id from the extract step into a yt-dlp format string."""
    if not format_id or format_id == "best":
        return "bestvideo*+bestaudio/best"
    return f"{format_id}+bestaudio/{format_id}/best"
