"""SQLite-backed job store.

This is what makes a conversion resumable. Every unit of work -- a chapter's
text prep, and each individual TTS chunk -- is a row with a status. Killing the
process at any point loses at most one chunk (a few seconds of audio), and the
next run picks up exactly where it stopped.

Rows are keyed by a *render key* (a hash of the text plus every setting that
affects the audio), so changing the voice or speed correctly invalidates old
renders instead of silently mixing two narrators into one book.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1

PENDING, RUNNING, DONE, FAILED = "pending", "running", "done", "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    epub_path   TEXT NOT NULL,
    epub_hash   TEXT NOT NULL,
    title       TEXT,
    author      TEXT,
    settings    TEXT NOT NULL,
    options     TEXT NOT NULL DEFAULT '{}',
    llm_model   TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chapter (
    idx          INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    href         TEXT,
    raw_text     TEXT NOT NULL,
    prep_text    TEXT,
    prep_status  TEXT NOT NULL DEFAULT 'pending',
    prep_error   TEXT,
    audio_path   TEXT,
    duration     REAL,
    status       TEXT NOT NULL DEFAULT 'pending',
    selected     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS chunk (
    chapter_idx INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    render_key  TEXT NOT NULL,
    wav_path    TEXT,
    duration    REAL,
    status      TEXT NOT NULL DEFAULT 'pending',
    error       TEXT,
    PRIMARY KEY (chapter_idx, idx)
);

CREATE INDEX IF NOT EXISTS chunk_status ON chunk(status);
"""


def render_key(text: str, settings_key: str) -> str:
    h = hashlib.sha256()
    h.update(settings_key.encode("utf-8"))
    h.update(b"\x00")
    h.update(text.encode("utf-8"))
    return h.hexdigest()[:32]


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:32]


@dataclass
class Chapter:
    idx: int
    title: str
    href: str | None
    raw_text: str
    prep_text: str | None
    prep_status: str
    audio_path: str | None
    duration: float | None
    status: str
    selected: bool

    @property
    def text(self) -> str:
        """The text that should actually be spoken."""
        return self.prep_text or self.raw_text


@dataclass
class Chunk:
    chapter_idx: int
    idx: int
    text: str
    render_key: str
    wav_path: str | None
    duration: float | None
    status: str


class JobStore:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- job ---------------------------------------------------------------

    def job(self) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM job WHERE id = 1").fetchone()

    def init_job(
        self,
        *,
        epub_path: Path,
        epub_hash: str,
        title: str,
        author: str,
        settings: dict,
        options: dict,
        llm_model: str | None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                """INSERT INTO job (id, epub_path, epub_hash, title, author, settings,
                                    options, llm_model, created_at, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       epub_path  = excluded.epub_path,
                       settings   = excluded.settings,
                       options    = excluded.options,
                       llm_model  = excluded.llm_model,
                       updated_at = excluded.updated_at""",
                (
                    str(epub_path),
                    epub_hash,
                    title,
                    author,
                    json.dumps(settings, sort_keys=True),
                    json.dumps(options, sort_keys=True),
                    llm_model,
                    now,
                    now,
                ),
            )
            self._db.commit()

    def touch(self) -> None:
        with self._lock:
            self._db.execute("UPDATE job SET updated_at = ? WHERE id = 1", (time.time(),))
            self._db.commit()

    # -- chapters ----------------------------------------------------------

    def replace_chapters(self, chapters: list[tuple[int, str, str | None, str]]) -> None:
        """Seed chapter rows.

        A chapter whose raw text is unchanged keeps all of its progress; one
        whose text changed is reset along with its chunks.
        """
        with self._lock:
            existing = {
                r["idx"]: r
                for r in self._db.execute("SELECT idx, raw_text FROM chapter").fetchall()
            }
            for idx, title, href, raw_text in chapters:
                prev = existing.pop(idx, None)
                if prev is not None and prev["raw_text"] == raw_text:
                    self._db.execute(
                        "UPDATE chapter SET title = ?, href = ? WHERE idx = ?", (title, href, idx)
                    )
                    continue
                self._db.execute("DELETE FROM chunk WHERE chapter_idx = ?", (idx,))
                self._db.execute(
                    """INSERT INTO chapter (idx, title, href, raw_text, prep_status, status)
                       VALUES (?, ?, ?, ?, 'pending', 'pending')
                       ON CONFLICT(idx) DO UPDATE SET
                           title = excluded.title, href = excluded.href,
                           raw_text = excluded.raw_text, prep_text = NULL,
                           prep_status = 'pending', prep_error = NULL,
                           audio_path = NULL, duration = NULL, status = 'pending'""",
                    (idx, title, href, raw_text),
                )
            for stale_idx in existing:
                self._db.execute("DELETE FROM chunk WHERE chapter_idx = ?", (stale_idx,))
                self._db.execute("DELETE FROM chapter WHERE idx = ?", (stale_idx,))
            self._db.commit()

    def chapters(self, selected_only: bool = False) -> list[Chapter]:
        sql = "SELECT * FROM chapter"
        if selected_only:
            sql += " WHERE selected = 1"
        sql += " ORDER BY idx"
        return [_chapter(r) for r in self._db.execute(sql).fetchall()]

    def chapter(self, idx: int) -> Chapter | None:
        row = self._db.execute("SELECT * FROM chapter WHERE idx = ?", (idx,)).fetchone()
        return _chapter(row) if row else None

    def set_selection(self, selected: set[int] | None) -> None:
        with self._lock:
            if selected is None:
                self._db.execute("UPDATE chapter SET selected = 1")
            else:
                self._db.execute("UPDATE chapter SET selected = 0")
                for idx in selected:
                    self._db.execute("UPDATE chapter SET selected = 1 WHERE idx = ?", (idx,))
            self._db.commit()

    def set_prep(self, idx: int, text: str | None, status: str, error: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE chapter SET prep_text = ?, prep_status = ?, prep_error = ? WHERE idx = ?",
                (text, status, error, idx),
            )
            self._db.commit()

    def set_chapter_audio(self, idx: int, wav_path: str, duration: float) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE chapter SET audio_path = ?, duration = ?, status = 'done' WHERE idx = ?",
                (wav_path, duration, idx),
            )
            self._db.commit()

    def set_chapter_status(self, idx: int, status: str) -> None:
        with self._lock:
            self._db.execute("UPDATE chapter SET status = ? WHERE idx = ?", (status, idx))
            self._db.commit()

    # -- chunks ------------------------------------------------------------

    def sync_chunks(self, chapter_idx: int, chunks: list[tuple[str, str]]) -> None:
        """Write the chunk plan for a chapter.

        A chunk keeps its rendered audio only if its text, its render key, and
        the wav file on disk all still line up.
        """
        with self._lock:
            existing = {
                r["idx"]: r
                for r in self._db.execute(
                    "SELECT * FROM chunk WHERE chapter_idx = ?", (chapter_idx,)
                ).fetchall()
            }
            for i, (text, rkey) in enumerate(chunks):
                prev = existing.pop(i, None)
                if (
                    prev is not None
                    and prev["render_key"] == rkey
                    and prev["status"] == DONE
                    and prev["wav_path"]
                    and Path(prev["wav_path"]).exists()
                ):
                    continue
                self._db.execute(
                    """INSERT INTO chunk (chapter_idx, idx, text, render_key, status)
                       VALUES (?, ?, ?, ?, 'pending')
                       ON CONFLICT(chapter_idx, idx) DO UPDATE SET
                           text = excluded.text, render_key = excluded.render_key,
                           wav_path = NULL, duration = NULL,
                           status = 'pending', error = NULL""",
                    (chapter_idx, i, text, rkey),
                )
            for stale_idx in existing:
                self._db.execute(
                    "DELETE FROM chunk WHERE chapter_idx = ? AND idx = ?", (chapter_idx, stale_idx)
                )
            self._db.commit()

    def chunks(self, chapter_idx: int, status: str | None = None) -> list[Chunk]:
        sql = "SELECT * FROM chunk WHERE chapter_idx = ?"
        args: list = [chapter_idx]
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY idx"
        return [
            Chunk(
                chapter_idx=r["chapter_idx"],
                idx=r["idx"],
                text=r["text"],
                render_key=r["render_key"],
                wav_path=r["wav_path"],
                duration=r["duration"],
                status=r["status"],
            )
            for r in self._db.execute(sql, args).fetchall()
        ]

    def finish_chunk(self, chapter_idx: int, idx: int, wav_path: str, duration: float) -> None:
        with self._lock:
            self._db.execute(
                """UPDATE chunk SET wav_path = ?, duration = ?, status = 'done', error = NULL
                   WHERE chapter_idx = ? AND idx = ?""",
                (wav_path, duration, chapter_idx, idx),
            )
            self._db.commit()

    def fail_chunk(self, chapter_idx: int, idx: int, error: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE chunk SET status = 'failed', error = ? WHERE chapter_idx = ? AND idx = ?",
                (error[:1000], chapter_idx, idx),
            )
            self._db.commit()

    def reset_failures(self) -> int:
        with self._lock:
            cur = self._db.execute(
                "UPDATE chunk SET status = 'pending', error = NULL "
                "WHERE status IN ('failed', 'running')"
            )
            self._db.execute(
                "UPDATE chapter SET prep_status = 'pending' WHERE prep_status = 'failed'"
            )
            self._db.commit()
            return cur.rowcount

    # -- reporting ---------------------------------------------------------

    def progress(self) -> dict:
        row = self._db.execute(
            """SELECT
                 (SELECT COUNT(*) FROM chapter WHERE selected = 1) AS chapters,
                 (SELECT COUNT(*) FROM chapter WHERE selected = 1 AND status = 'done')
                     AS chapters_done,
                 (SELECT COUNT(*) FROM chunk c JOIN chapter ch ON ch.idx = c.chapter_idx
                    WHERE ch.selected = 1) AS chunks,
                 (SELECT COUNT(*) FROM chunk c JOIN chapter ch ON ch.idx = c.chapter_idx
                    WHERE ch.selected = 1 AND c.status = 'done') AS chunks_done,
                 (SELECT COUNT(*) FROM chunk c JOIN chapter ch ON ch.idx = c.chapter_idx
                    WHERE ch.selected = 1 AND c.status = 'failed') AS chunks_failed,
                 (SELECT COALESCE(SUM(c.duration), 0) FROM chunk c
                    JOIN chapter ch ON ch.idx = c.chapter_idx
                    WHERE ch.selected = 1 AND c.status = 'done') AS audio_seconds
            """
        ).fetchone()
        return dict(row)


def _chapter(r: sqlite3.Row) -> Chapter:
    return Chapter(
        idx=r["idx"],
        title=r["title"],
        href=r["href"],
        raw_text=r["raw_text"],
        prep_text=r["prep_text"],
        prep_status=r["prep_status"],
        audio_path=r["audio_path"],
        duration=r["duration"],
        status=r["status"],
        selected=bool(r["selected"]),
    )
