"""FastAPI application: REST for control, server-sent events for progress.

The server never imports torch. It supervises CLI subprocesses and reads each
job's SQLite database, so the UI stays responsive while several hundred
megabytes of model run elsewhere.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import VOICES
from ..textprep import RENDER_TIME_FACTOR, estimate_seconds
from .jobs import JobManager

STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


class StartRequest(BaseModel):
    voice: str = "af_heart"
    speed: float = 1.0
    only: str | None = None
    workers: int = 0
    llm: bool = False
    keep_front_matter: bool = False


def create_app(data_dir: Path, on_event: Callable[[str], None] | None = None) -> FastAPI:
    manager = JobManager(data_dir, on_event=on_event)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        # Never leave a render orphaned when the server goes down.
        manager.shutdown()

    app = FastAPI(title="epub2audiobook", version="0.1.0", lifespan=lifespan)

    # -- metadata ----------------------------------------------------------

    @app.get("/api/voices")
    def voices() -> dict:
        from .. import render

        workers = render.default_workers()
        return {
            "voices": [{"id": k, "description": v} for k, v in VOICES.items()],
            "suggested_workers": workers,
            "threads_per_worker": render.threads_per_worker(workers),
            "data_dir": str(manager.data_dir),
        }

    # -- jobs --------------------------------------------------------------

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        return {"jobs": [asdict(j) | {"percent": j.percent} for j in manager.list_jobs()]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        try:
            summary = manager.summary(job_id, with_chapters=True)
        except FileNotFoundError:
            raise HTTPException(404, "No such job")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return asdict(summary) | {"percent": summary.percent, "log": manager.log(job_id, 30)}

    @app.post("/api/jobs")
    async def create_job(file: UploadFile = File(...)) -> dict:
        if not file.filename or not file.filename.lower().endswith(".epub"):
            raise HTTPException(400, "Please upload a .epub file.")
        data = await file.read()
        if not data:
            raise HTTPException(400, "That file is empty.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "That EPUB is larger than 200 MB.")
        if not data.startswith(b"PK"):
            raise HTTPException(400, "That does not look like an EPUB (not a zip archive).")

        job_id = manager.create(data, file.filename)
        try:
            details = _inspect(manager.job_dir(job_id) / "source.epub")
        except Exception as exc:
            # A file that is a zip but not a readable EPUB got this far. Don't
            # leave a job directory behind that can never be converted.
            manager.delete(job_id)
            raise HTTPException(
                400, f"That file could not be read as an EPUB: {exc}"
            ) from exc
        if not details["chapters"]:
            manager.delete(job_id)
            raise HTTPException(
                400,
                "No readable chapters were found in that EPUB. It may be "
                "image-only (a scanned book), or DRM-protected.",
            )
        return {"id": job_id} | details

    @app.get("/api/jobs/{job_id}/inspect")
    def inspect_job(job_id: str) -> dict:
        try:
            path = manager.job_dir(job_id) / "source.epub"
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not path.exists():
            raise HTTPException(404, "No source EPUB for this job")
        return _inspect(path)

    @app.post("/api/jobs/{job_id}/start")
    def start_job(job_id: str, req: StartRequest) -> dict:
        try:
            manager.start(
                job_id,
                voice=req.voice,
                speed=req.speed,
                only=req.only,
                workers=req.workers,
                llm=req.llm,
                keep_front_matter=req.keep_front_matter,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/stop")
    def stop_job(job_id: str) -> dict:
        return {"stopped": manager.stop(job_id)}

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str) -> dict:
        try:
            manager.delete(job_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/download")
    def download(job_id: str):
        try:
            path = manager.output_path(job_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not path or not path.exists():
            raise HTTPException(404, "This job has no finished audiobook yet.")
        return FileResponse(path, media_type="audio/mp4", filename=path.name)

    # -- live progress -----------------------------------------------------

    @app.get("/api/jobs/{job_id}/events")
    async def events(job_id: str):
        async def stream():
            last = None
            while True:
                try:
                    summary = manager.summary(job_id, with_chapters=True)
                except Exception:
                    yield 'event: gone\ndata: {}\n\n'
                    return
                payload = asdict(summary) | {"percent": summary.percent}
                text = json.dumps(payload, default=str)
                if text != last:  # only push when something actually changed
                    yield f"data: {text}\n\n"
                    last = text
                else:
                    yield ": keepalive\n\n"
                if summary.state in ("done", "failed"):
                    return
                await asyncio.sleep(2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- static front end --------------------------------------------------

    if STATIC_DIR.exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            index = STATIC_DIR / "index.html"
            if index.exists():
                return HTMLResponse(index.read_text(encoding="utf-8"))
            return HTMLResponse("<h1>Front end not built</h1>", status_code=404)
    else:

        @app.get("/")
        def missing():
            return HTMLResponse(
                "<h1>epub2audiobook</h1><p>The front end is not built. Run "
                "<code>npm install &amp;&amp; npm run build</code> in <code>frontend/</code>.</p>"
            )

    return app


def _inspect(epub_path: Path) -> dict:
    """Chapter listing and time estimates, without starting anything."""
    from .. import epubsrc, textprep

    book = epubsrc.load(epub_path)
    chapters = []
    total = 0
    for chapter in book.chapters:
        cleaned = textprep.clean(chapter.text)
        total += len(cleaned)
        chapters.append(
            {
                "idx": chapter.idx,
                "title": chapter.title,
                "chars": len(cleaned),
                "estimated_seconds": estimate_seconds(len(cleaned)),
            }
        )
    return {
        "title": book.title,
        "author": book.author,
        "has_cover": book.cover is not None,
        "chapters": chapters,
        "total_chars": total,
        "estimated_audio_seconds": estimate_seconds(total),
        "estimated_render_seconds": estimate_seconds(total) * RENDER_TIME_FACTOR,
    }
