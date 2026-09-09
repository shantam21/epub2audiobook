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
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DB_NAME = "job.db"
UPLOAD_NAME = "source.epub"

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

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._procs: dict[str, JobProcess] = {}
        self._lock = threading.Lock()

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
            if entry.is_dir() and (entry / DB_NAME).exists():
                try:
                    jobs.append(self.summary(entry.name))
                except Exception:
                    continue  # a half-created job should not break the list
        return sorted(jobs, key=lambda j: j.updated_at, reverse=True)

    def summary(self, job_id: str, with_chapters: bool = False) -> JobSummary:
        path = self.job_dir(job_id)
        db = path / DB_NAME
        if not db.exists():
            raise FileNotFoundError(job_id)

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

    def create(self, epub_bytes: bytes, filename: str) -> str:
        job_id = _unique_dir_name(self.data_dir, Path(filename).stem)
        path = self.data_dir / job_id
        path.mkdir(parents=True)
        (path / UPLOAD_NAME).write_bytes(epub_bytes)
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


def _tail(path: Path, lines: int = 40) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


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
