"""Job supervision for the web UI.

Conversions run as subprocesses of the CLI rather than inside the web server.
That keeps torch, Kokoro and their several hundred megabytes of model weights
out of the server process entirely: the server only ever supervises processes
and reads the job's SQLite database, so it stays responsive and a crashing
render can never take the UI down with it.

Progress is read straight from each job's `job.db`, which is already the
single source of truth for the CLI. The web UI therefore shows exactly what
`epub2ab status` shows, and a conversion started from the terminal appears in
the browser (and vice versa) with no extra bookkeeping.
"""

from __future__ import annotations

import json
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

DB_NAME = "job.db"
UPLOAD_NAME = "source.epub"
META_NAME = "job.json"

# A job whose database was stamped this recently is treated as live even when
# this process did not start it -- that is how a terminal-launched conversion
# shows up in the browser.
EXTERNAL_RUN_TIMEOUT = 90.0


@dataclass
class JobProcess:
    popen: subprocess.Popen
    started_at: float
    log_path: Path
    stopping: bool = False


@dataclass
class JobSummary:
    id: str
    title: str
    author: str
    state: str                     # idle | running | done | failed
    chapters: int = 0
    chapters_done: int = 0
    chunks: int = 0
    chunks_done: int = 0
    chunks_failed: int = 0
    audio_seconds: float = 0.0
    voice: str = ""
    speed: float = 1.0
    output: str | None = None
    updated_at: float = 0.0
    error: str | None = None
    chapter_rows: list[dict] = field(default_factory=list)

    @property
    def percent(self) -> float:
        return 100.0 * self.chunks_done / self.chunks if self.chunks else 0.0


class JobManager:
    """Owns the jobs directory and any running conversion processes."""

    def __init__(self, data_dir: Path, on_event: Callable[[str], None] | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._procs: dict[str, JobProcess] = {}
        self._lock = threading.Lock()
        # Conversions run in subprocesses whose output goes to the job's log
        # file. Without this callback a failure is invisible from the terminal
        # that started the server, which is the worst way to debug anything.
        self._on_event = on_event or (lambda _msg: None)

    # -- discovery ---------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        # job ids are directory names; refuse anything that could escape.
        if not job_id or "/" in job_id or "\\" in job_id or job_id.startswith("."):
            raise ValueError(f"invalid job id: {job_id!r}")
        path = (self.data_dir / job_id).resolve()
        if not str(path).startswith(str(self.data_dir.resolve())):
            raise ValueError(f"invalid job id: {job_id!r}")
        return path

    def list_jobs(self) -> list[JobSummary]:
        jobs = []
        for entry in sorted(self.data_dir.iterdir()):
            if not entry.is_dir():
                continue
            # A job exists from the moment it is uploaded. job.db only appears
            # once a conversion starts, so it cannot be what makes a job real.
            if not (entry / DB_NAME).exists() and not (entry / META_NAME).exists():
                continue
            try:
                jobs.append(self.summary(entry.name))
            except Exception:
                continue  # a half-created job should not break the list
        return sorted(jobs, key=lambda j: j.updated_at, reverse=True)

    def summary(self, job_id: str, with_chapters: bool = False) -> JobSummary:
        path = self.job_dir(job_id)
        db = path / DB_NAME
        if not db.exists():
            # Uploaded but never converted: report what we know from the
            # metadata written at upload time, rather than 404.
            return self._pending_summary(job_id, path)

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            job = con.execute("SELECT * FROM job WHERE id = 1").fetchone()
            stats = con.execute(_STATS_SQL).fetchone()
            rows = []
            if with_chapters:
                rows = [
                    {
                        "idx": r["idx"],
                        "title": r["title"],
                        "selected": bool(r["selected"]),
                        "prep": r["prep_status"],
                        "status": r["status"],
                        "duration": r["duration"],
                        "chunks": r["chunks"],
                        "chunks_done": r["done"],
                    }
                    for r in con.execute(_CHAPTERS_SQL).fetchall()
                ]
        finally:
            con.close()

        settings = json.loads(job["settings"]) if job else {}
        output = next((p for p in path.glob("*.m4b") if "PREVIEW" not in p.name), None)

        updated_at = (job["updated_at"] if job else 0.0) or 0.0
        state, error = self._state(job_id, stats, output, updated_at)
        return JobSummary(
            id=job_id,
            title=(job["title"] if job else job_id) or job_id,
            author=(job["author"] if job else "") or "",
            state=state,
            chapters=stats["chapters"],
            chapters_done=stats["chapters_done"],
            chunks=stats["chunks"],
            chunks_done=stats["chunks_done"],
            chunks_failed=stats["chunks_failed"],
            audio_seconds=stats["audio_seconds"] or 0.0,
            voice=settings.get("voice", ""),
            speed=settings.get("speed", 1.0),
            output=output.name if output else None,
            updated_at=(job["updated_at"] if job else 0.0) or 0.0,
            error=error,
            chapter_rows=rows,
        )

    def _pending_summary(self, job_id: str, path: Path) -> JobSummary:
        """A job that has been uploaded but not yet converted."""
        if not path.is_dir():
            raise FileNotFoundError(job_id)
        meta = {}
        try:
            meta = json.loads((path / META_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

        with self._lock:
            proc = self._procs.get(job_id)
        # A conversion can be running while it is still preparing text, before
        # it has written the database.
        state = "running" if proc and proc.popen.poll() is None else "idle"

        return JobSummary(
            id=job_id,
            title=meta.get("title") or job_id,
            author=meta.get("author") or "",
            state=state,
            updated_at=meta.get("created_at", 0.0),
        )

    def _state(self, job_id: str, stats, output, updated_at: float) -> tuple[str, str | None]:
        with self._lock:
            proc = self._procs.get(job_id)
        if proc and proc.popen.poll() is None:
            return "running", None
        if output:
            return "done", None
        # A conversion started from the terminal has no process here, but it
        # stamps the database as it works. Recent activity means it is live.
        if time.time() - updated_at < EXTERNAL_RUN_TIMEOUT:
            return "running", None
        if proc and proc.popen.returncode not in (0, None):
            tail = _tail(proc.log_path)
            if proc.stopping:
                return "idle", None
            return "failed", tail
        if stats["chunks_failed"]:
            return "failed", f"{stats['chunks_failed']} chunks failed"
        return "idle", None

    # -- lifecycle ---------------------------------------------------------

    def create(
        self,
        epub_bytes: bytes,
        filename: str,
        title: str = "",
        author: str = "",
    ) -> str:
        """Create a job. Named after the book, falling back to the filename.

        EPUB filenames from the wild are long and full of punctuation -- the
        book's own title makes a far better directory name and job id.
        """
        job_id = _unique_dir_name(self.data_dir, title or Path(filename).stem)
        path = self.data_dir / job_id
        path.mkdir(parents=True)
        (path / UPLOAD_NAME).write_bytes(epub_bytes)
        (path / META_NAME).write_text(
            json.dumps(
                {
                    "title": title,
                    "author": author,
                    "filename": filename,
                    "created_at": time.time(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return job_id

    def start(
        self,
        job_id: str,
        *,
        voice: str,
        speed: float,
        only: str | None,
        workers: int,
        llm: bool,
        keep_front_matter: bool,
    ) -> None:
        path = self.job_dir(job_id)
        with self._lock:
            existing = self._procs.get(job_id)
            if existing and existing.popen.poll() is None:
                raise RuntimeError("This job is already running.")

        epub = path / UPLOAD_NAME
        if not epub.exists():
            raise FileNotFoundError("The uploaded EPUB is missing from this job.")

        cmd = [
            sys.executable, "-m", "epub2audiobook", "convert", str(epub),
            "-o", str(path),
            "--voice", voice,
            "--speed", str(speed),
            "--workers", str(workers),
            "--retry-failed",
        ]
        if only:
            cmd += ["--only", only]
        if llm:
            cmd += ["--llm"]
        if keep_front_matter:
            cmd += ["--keep-front-matter"]

        log_path = path / "convert.log"
        log = log_path.open("ab")
        popen = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(path),
            creationflags=_CREATE_NEW_PROCESS_GROUP,
        )
        with self._lock:
            self._procs[job_id] = JobProcess(popen, time.time(), log_path)

        self._on_event(f"[{job_id}] started (pid {popen.pid}, voice {voice}, {workers or 'auto'} workers)")
        threading.Thread(
            target=self._reap, args=(job_id, popen, log_path, log), daemon=True
        ).start()

    def _reap(self, job_id: str, popen: subprocess.Popen, log_path: Path, handle) -> None:
        """Wait for a conversion and report how it ended, to the server console."""
        code = popen.wait()
        try:
            handle.close()
        except OSError:
            pass

        with self._lock:
            proc = self._procs.get(job_id)
        if proc is not None and proc.stopping:
            self._on_event(f"[{job_id}] stopped by request")
            return
        if code == 0:
            self._on_event(f"[{job_id}] finished")
            return

        tail = _tail(log_path, 15)
        self._on_event(
            f"[{job_id}] FAILED with exit code {code}. Last lines of "
            f"{log_path}:\n{tail}"
        )

    def stop(self, job_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(job_id)
        if not proc or proc.popen.poll() is not None:
            return False
        proc.stopping = True
        # Kill the tree: the render pool's workers are children of the CLI.
        _terminate_tree(proc.popen)
        return True

    def delete(self, job_id: str) -> None:
        self.stop(job_id)
        path = self.job_dir(job_id)
        time.sleep(0.2)  # let file handles close on Windows
        shutil.rmtree(path, ignore_errors=True)
        with self._lock:
            self._procs.pop(job_id, None)

    def log(self, job_id: str, lines: int = 40) -> str:
        return _tail(self.job_dir(job_id) / "convert.log", lines)

    def output_path(self, job_id: str) -> Path | None:
        path = self.job_dir(job_id)
        return next((p for p in path.glob("*.m4b") if "PREVIEW" not in p.name), None)

    def shutdown(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            if proc.popen.poll() is None:
                proc.stopping = True
                _terminate_tree(proc.popen)


# -- helpers ---------------------------------------------------------------

_CREATE_NEW_PROCESS_GROUP = (
    subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
)

_STATS_SQL = """
SELECT
  (SELECT COUNT(*) FROM chapter WHERE selected=1) AS chapters,
  (SELECT COUNT(*) FROM chapter WHERE selected=1 AND status='done') AS chapters_done,
  (SELECT COUNT(*) FROM chunk c JOIN chapter h ON h.idx=c.chapter_idx
     WHERE h.selected=1) AS chunks,
  (SELECT COUNT(*) FROM chunk c JOIN chapter h ON h.idx=c.chapter_idx
     WHERE h.selected=1 AND c.status='done') AS chunks_done,
  (SELECT COUNT(*) FROM chunk c JOIN chapter h ON h.idx=c.chapter_idx
     WHERE h.selected=1 AND c.status='failed') AS chunks_failed,
  (SELECT COALESCE(SUM(c.duration),0) FROM chunk c JOIN chapter h ON h.idx=c.chapter_idx
     WHERE h.selected=1 AND c.status='done') AS audio_seconds
"""

_CHAPTERS_SQL = """
SELECT ch.idx, ch.title, ch.selected, ch.prep_status, ch.status, ch.duration,
       COUNT(c.idx) AS chunks,
       COALESCE(SUM(CASE WHEN c.status='done' THEN 1 ELSE 0 END), 0) AS done
FROM chapter ch
LEFT JOIN chunk c ON c.chapter_idx = ch.idx
GROUP BY ch.idx
ORDER BY ch.idx
"""


def _unique_dir_name(root: Path, stem: str) -> str:
    import re

    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", stem).strip().rstrip(".")[:80] or "book"
    candidate = base
    n = 2
    while (root / candidate).exists():
        candidate = f"{base} ({n})"
        n += 1
    return candidate


# The CLI writes rich-formatted output: box drawing, ANSI colour, and progress
# bars redrawn with carriage returns. Rendered in a browser that is a wall of
# line-art, so strip it back to the words before showing it.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BOX_CHARS = (
    "─━│┃┌┏┐┓└┗┘┛"
    "═║╔╗╚╝╴╵╶╷"
    "█░▒▓▰▱"
)


def _readable(line: str) -> str:
    """One log line, without the terminal decoration."""
    line = _ANSI_RE.sub("", line.split("\r")[-1])
    return line.strip(_BOX_CHARS + " ")


def _tail(path: Path, lines: int = 40) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    cleaned = (_readable(line) for line in text.splitlines()[-lines:])
    return "\n".join(line for line in cleaned if line)


def _terminate_tree(popen: subprocess.Popen) -> None:
    """Stop the CLI and any render workers it spawned."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(popen.pid)],
                capture_output=True,
                check=False,
            )
        else:
            popen.send_signal(signal.SIGINT)
            try:
                popen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                popen.kill()
    except Exception:
        popen.kill()
