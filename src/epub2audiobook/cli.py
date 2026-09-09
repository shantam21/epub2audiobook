"""Command line interface."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from . import assemble, epubsrc, manualprep, textprep
from .config import VOICES, RenderSettings, lang_for_voice
from .store import DONE, JobStore, file_hash, render_key

app = typer.Typer(
    add_completion=False,
    help="Convert EPUB books into chaptered M4B audiobooks with local Kokoro TTS.",
)
console = Console()

DB_NAME = "job.db"


# -- commands --------------------------------------------------------------


@app.command()
def voices() -> None:
    """List the Kokoro voices worth narrating a book with."""
    table = Table(title="Kokoro voices", header_style="bold")
    table.add_column("Voice", style="cyan")
    table.add_column("Character")
    for name, blurb in VOICES.items():
        table.add_row(name, blurb)
    console.print(table)
    console.print("\nPreview one before committing to a whole book:")
    console.print("  [dim]epub2ab sample book.epub --voice am_michael[/dim]")


@app.command()
def inspect(
    epub_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    min_chars: int = typer.Option(250, help="Skip sections shorter than this."),
    keep_front_matter: bool = typer.Option(False, help="Keep title pages, TOC, copyright."),
) -> None:
    """Show the chapters found in an EPUB and how long the audiobook will run."""
    book = epubsrc.load(epub_path, min_chars=min_chars, skip_front_matter=not keep_front_matter)

    table = Table(title=f"{book.title} - {book.author}", header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Chapter")
    table.add_column("Chars", justify="right")
    table.add_column("Est. audio", justify="right")

    total_chars = 0
    for chapter in book.chapters:
        cleaned = textprep.clean(chapter.text)
        total_chars += len(cleaned)
        table.add_row(
            str(chapter.idx),
            chapter.title[:60],
            f"{len(cleaned):,}",
            textprep.format_duration(textprep.estimate_seconds(len(cleaned))),
        )

    console.print(table)
    audio_seconds = textprep.estimate_seconds(total_chars)
    console.print(
        f"\n[bold]{len(book.chapters)}[/bold] chapters, "
        f"[bold]{total_chars:,}[/bold] characters, "
        f"about [bold]{textprep.format_duration(audio_seconds)}[/bold] of audio."
    )
    console.print(
        "Rendering takes roughly "
        f"[bold]{textprep.format_duration(audio_seconds * textprep.RENDER_TIME_FACTOR)}[/bold] "
        "on CPU - a little longer than the audiobook itself. "
        "[dim]It resumes, so you can stop and continue.[/dim]"
    )
    console.print(f"Cover art: {'found' if book.cover else 'none in this EPUB'}")

    from .llmprep import DEFAULT_MODEL, estimate_cost

    cost = estimate_cost(total_chars, DEFAULT_MODEL)
    console.print(f"LLM pre-pass with {DEFAULT_MODEL} would cost roughly [bold]${cost:.2f}[/bold].")


@app.command()
def sample(
    epub_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    voice: str = typer.Option("af_heart", help="Kokoro voice."),
    speed: float = typer.Option(1.0, help="Playback speed multiplier."),
    chapter: int = typer.Option(0, help="Chapter index to sample."),
    seconds: int = typer.Option(45, help="Roughly how much audio to render."),
    out: Path = typer.Option(Path("sample.wav"), help="Where to write the sample."),
) -> None:
    """Render a short sample so you can pick a voice before committing hours."""
    from .tts import KokoroEngine

    book = epubsrc.load(epub_path)
    if not book.chapters:
        console.print("[red]No readable chapters found in that EPUB.[/red]")
        raise typer.Exit(1)
    idx = max(0, min(chapter, len(book.chapters) - 1))

    text = textprep.clean(book.chapters[idx].text)
    budget = int(seconds * textprep.CHARS_PER_SECOND * speed)
    # Size chunks to the budget, or a short sample would still cost one whole
    # full-size chunk -- always at least one is taken.
    chunks = textprep.chunk(text, min(RenderSettings().max_chunk_chars, max(120, budget)))

    picked: list[str] = []
    used = 0
    for c in chunks:
        if used + len(c) > budget and picked:
            break
        picked.append(c)
        used += len(c)

    engine = KokoroEngine(lang=lang_for_voice(voice), voice=voice, speed=speed)
    with console.status(f"Loading Kokoro and rendering {used:,} characters..."):
        duration = engine.synthesize_to_file("\n".join(picked), out)
    console.print(
        f"[green]Wrote[/green] {out} - {textprep.format_duration(duration)} "
        f"of {voice} at {speed}x."
    )


@app.command()
def convert(
    epub_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    out: Path = typer.Option(None, "--out", "-o", help="Working directory (default: ./<book>)."),
    voice: str = typer.Option("af_heart", help="Kokoro voice. See `epub2ab voices`."),
    speed: float = typer.Option(1.0, help="Playback speed multiplier."),
    llm: bool = typer.Option(False, "--llm", help="Run the Claude text pre-pass."),
    llm_model: str = typer.Option("claude-opus-5", help="Model for the pre-pass."),
    llm_effort: str = typer.Option("low", help="Effort level for the pre-pass."),
    only: str = typer.Option(None, help="Chapters to convert, e.g. '0-5,9,12'."),
    bitrate: str = typer.Option("64k", help="AAC bitrate for the M4B."),
    min_chars: int = typer.Option(250, help="Skip sections shorter than this."),
    keep_front_matter: bool = typer.Option(False, help="Keep title pages, TOC, copyright."),
    max_chunk_chars: int = typer.Option(380, help="Characters per TTS chunk."),
    retry_failed: bool = typer.Option(False, help="Re-queue chunks that failed previously."),
    audio_only: bool = typer.Option(False, help="Render audio but skip the final M4B mux."),
    workers: int = typer.Option(
        0, help="Parallel synthesis processes. 0 picks a count from your CPU and free RAM."
    ),
) -> None:
    """Convert an EPUB to an M4B audiobook. Re-run to resume where you stopped."""
    work = _work_dir(epub_path, out)
    settings = RenderSettings(
        voice=voice,
        speed=speed,
        lang=lang_for_voice(voice),
        max_chunk_chars=max_chunk_chars,
    )
    _run(
        epub_path=epub_path,
        work=work,
        settings=settings,
        llm=llm,
        llm_model=llm_model,
        llm_effort=llm_effort,
        only=only,
        bitrate=bitrate,
        min_chars=min_chars,
        keep_front_matter=keep_front_matter,
        retry_failed=retry_failed,
        audio_only=audio_only,
        workers=workers,
    )


@app.command()
def resume(
    work: Path = typer.Argument(..., exists=True, file_okay=False, help="The job directory."),
    retry_failed: bool = typer.Option(True, help="Re-queue chunks that failed previously."),
    audio_only: bool = typer.Option(False, help="Render audio but skip the final M4B mux."),
    workers: int = typer.Option(
        0, help="Parallel synthesis processes. 0 picks a count from your CPU and free RAM."
    ),
) -> None:
    """Continue an interrupted conversion using its saved settings."""
    import json

    store = JobStore(work / DB_NAME)
    job = store.job()
    if job is None:
        console.print(f"[red]No saved job in {work}.[/red]")
        raise typer.Exit(1)

    saved = json.loads(job["settings"])
    settings = RenderSettings(
        **{k: v for k, v in saved.items() if k in RenderSettings.__annotations__}
    )
    # Chapter extraction options have to come back exactly as they were, or a
    # resume would rebuild a different chapter list and discard finished work.
    options = json.loads(job["options"] or "{}")
    epub_path = Path(job["epub_path"])
    llm_model = job["llm_model"]
    store.close()

    if not epub_path.exists():
        console.print(f"[red]The source EPUB has moved: {epub_path}[/red]")
        raise typer.Exit(1)

    _run(
        epub_path=epub_path,
        work=work,
        settings=settings,
        llm=bool(llm_model),
        llm_model=llm_model or "claude-opus-5",
        llm_effort=options.get("llm_effort", "low"),
        only=None,
        bitrate=options.get("bitrate", "64k"),
        min_chars=options.get("min_chars", 250),
        keep_front_matter=options.get("keep_front_matter", False),
        retry_failed=retry_failed,
        audio_only=audio_only,
        workers=workers,
        reuse_selection=True,
    )


@app.command("export-text")
def export_text(
    epub_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    out: Path = typer.Option(None, "--out", "-o", help="Working directory (default: ./<book>)."),
    only: str = typer.Option(None, help="Chapters to export, e.g. '0-5,9,12'."),
    part_chars: int = typer.Option(12000, help="Characters per paste."),
    min_chars: int = typer.Option(250, help="Skip sections shorter than this."),
    keep_front_matter: bool = typer.Option(False, help="Keep title pages, TOC, copyright."),
) -> None:
    """Write chapters out as text files to paste into the Claude app by hand.

    A no-API-key version of --llm: you paste, Claude replies, you save the reply,
    and `import-text` folds it back in.
    """
    import json

    work = _work_dir(epub_path, out)
    work.mkdir(parents=True, exist_ok=True)
    store = JobStore(work / DB_NAME)

    with console.status("Reading EPUB..."):
        book = epubsrc.load(
            epub_path, min_chars=min_chars, skip_front_matter=not keep_front_matter
        )
    if not book.chapters:
        console.print("[red]No readable chapters found. Try --keep-front-matter.[/red]")
        raise typer.Exit(1)

    # Keep whatever voice/speed an earlier convert already chose.
    existing = store.job()
    settings = (
        RenderSettings(
            **{
                k: v
                for k, v in json.loads(existing["settings"]).items()
                if k in RenderSettings.__annotations__
            }
        )
        if existing
        else RenderSettings()
    )

    store.init_job(
        epub_path=epub_path.resolve(),
        epub_hash=file_hash(epub_path),
        title=book.title,
        author=book.author,
        settings=settings.to_dict(),
        options={
            "min_chars": min_chars,
            "keep_front_matter": keep_front_matter,
            "bitrate": "64k",
            "llm_effort": "low",
        },
        llm_model=existing["llm_model"] if existing else None,
    )
    store.replace_chapters([(c.idx, c.title, c.href, c.text) for c in book.chapters])
    if only:
        store.set_selection(_parse_range(only))

    for chapter in store.chapters(selected_only=True):
        if chapter.prep_status not in ("clean", "llm"):
            store.set_prep(chapter.idx, textprep.clean(chapter.raw_text), "clean")

    prep_dir = work / "prep"
    parts = manualprep.export(prep_dir, store.chapters(selected_only=True), part_chars)
    pending = [p for p in parts if not p.done]
    store.close()

    console.print(
        Panel.fit(
            f"[bold]{len(parts)}[/bold] files to paste "
            f"({len(parts) - len(pending)} already answered)\n"
            f"[cyan]{prep_dir.resolve()}[/cyan]\n\n"
            "1. Open a new Claude conversation.\n"
            "2. Paste [bold]PROMPT.txt[/bold] as your first message.\n"
            "3. Paste each [bold]ch####-##.in.txt[/bold], saving each reply\n"
            "   alongside it as [bold]ch####-##.out.txt[/bold].\n"
            "4. Run [bold]epub2ab import-text \"" + str(work) + "\"[/bold]\n\n"
            "README.txt in that folder repeats all of this.",
            title="Ready to paste",
        )
    )


@app.command("import-text")
def import_text(
    work: Path = typer.Argument(..., exists=True, file_okay=False, help="The job directory."),
    force: bool = typer.Option(False, help="Accept replies that fail the length check."),
) -> None:
    """Fold hand-pasted Claude replies back into the job."""
    store = JobStore(work / DB_NAME)
    if store.job() is None:
        console.print(f"[red]No saved job in {work}. Run export-text first.[/red]")
        raise typer.Exit(1)

    by_chapter = manualprep.scan(work / "prep")
    if not by_chapter:
        console.print(f"[red]Nothing exported in {work / 'prep'}. Run export-text first.[/red]")
        raise typer.Exit(1)

    table = Table(title="Import", header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Chapter")
    table.add_column("Result")

    imported = waiting = refused = 0
    for chapter in store.chapters():
        parts = by_chapter.get(chapter.idx)
        if not parts:
            continue
        text, problem = manualprep.read_chapter(parts, force=force)
        if text:
            changed = text != chapter.prep_text
            store.set_prep(chapter.idx, text, "llm")
            imported += 1
            table.add_row(
                str(chapter.idx),
                chapter.title[:44],
                "[green]imported[/green]" + ("" if changed else " (unchanged)"),
            )
        elif problem and problem.startswith("waiting"):
            waiting += 1
            table.add_row(str(chapter.idx), chapter.title[:44], f"[dim]{problem}[/dim]")
        else:
            refused += 1
            table.add_row(str(chapter.idx), chapter.title[:44], f"[red]{problem}[/red]")

    console.print(table)
    console.print(
        f"\n[green]{imported}[/green] imported   "
        f"[dim]{waiting} awaiting a reply[/dim]"
        + (f"   [red]{refused} refused[/red]" if refused else "")
    )
    if refused and not force:
        console.print(
            "Refused chapters kept their automatic cleanup. Re-paste them, or "
            "pass [bold]--force[/bold] if you are sure the reply is complete."
        )
    if imported:
        console.print(
            f"\nNow run: [bold]epub2ab convert <book.epub> -o \"{work}\"[/bold]\n"
            "[dim]Chapters whose text changed will re-render; the rest are kept.[/dim]"
        )
    store.close()


@app.command()
def serve(
    data_dir: Path = typer.Option(
        None, "--data-dir", "-d", help="Where jobs live (default: ~/epub2audiobook)."
    ),
    host: str = typer.Option("127.0.0.1", help="Bind address. Localhost by default."),
    port: int = typer.Option(8000, help="Port to listen on."),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (development)."),
) -> None:
    """Run the web UI: upload a book, pick a voice, watch progress in a browser."""
    try:
        import uvicorn
    except ImportError:
        console.print(
            "[red]The web UI needs extra packages.[/red]\n"
            "Install them with: pip install 'epub2audiobook[web]'"
        )
        raise typer.Exit(1)

    from .server.app import create_app

    root = data_dir or (Path.home() / "epub2audiobook")
    root.mkdir(parents=True, exist_ok=True)

    console.print(
        Panel.fit(
            f"[bold]epub2audiobook[/bold]\n\n"
            f"Open [cyan]http://{host}:{port}[/cyan]\n"
            f"Jobs directory: {root}",
            title="Web UI",
        )
    )
    uvicorn.run(create_app(root), host=host, port=port, reload=reload, log_level="warning")


@app.command()
def prompt() -> None:
    """Print the Claude-app prompt used by the manual pre-pass."""
    console.print(manualprep.CHAT_PROMPT)


@app.command()
def status(
    work: Path = typer.Argument(..., exists=True, file_okay=False, help="The job directory.")
) -> None:
    """Show how far along a conversion is."""
    store = JobStore(work / DB_NAME)
    job = store.job()
    if job is None:
        console.print(f"[red]No saved job in {work}.[/red]")
        raise typer.Exit(1)

    table = Table(title=f"{job['title']} - {job['author']}", header_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Chapter")
    table.add_column("Prep")
    table.add_column("Chunks", justify="right")
    table.add_column("Audio", justify="right")

    for chapter in store.chapters():
        chunks = store.chunks(chapter.idx)
        done = sum(1 for c in chunks if c.status == DONE)
        mark = {"done": "[green]done[/green]", "pending": "[yellow]pending[/yellow]"}
        table.add_row(
            str(chapter.idx),
            ("" if chapter.selected else "[dim]") + chapter.title[:50],
            chapter.prep_status,
            f"{done}/{len(chunks)}" if chunks else "-",
            mark.get(chapter.status, chapter.status),
        )
    console.print(table)

    p = store.progress()
    console.print(
        f"\nChapters {p['chapters_done']}/{p['chapters']}  "
        f"Chunks {p['chunks_done']}/{p['chunks']}  "
        f"Rendered {textprep.format_duration(p['audio_seconds'])}"
        + (f"  [red]{p['chunks_failed']} failed[/red]" if p["chunks_failed"] else "")
    )
    store.close()


# -- the actual pipeline ---------------------------------------------------


def _run(
    *,
    epub_path: Path,
    work: Path,
    settings: RenderSettings,
    llm: bool,
    llm_model: str,
    llm_effort: str,
    only: str | None,
    bitrate: str,
    min_chars: int,
    keep_front_matter: bool,
    retry_failed: bool,
    audio_only: bool,
    workers: int = 0,
    reuse_selection: bool = False,
) -> None:
    from .tts import TTSError

    work.mkdir(parents=True, exist_ok=True)
    chunk_dir = work / "work" / "chunks"
    chapter_dir = work / "work" / "chapters"
    store = JobStore(work / DB_NAME)

    try:
        # 1. Parse the EPUB and reconcile it against any saved job.
        with console.status("Reading EPUB..."):
            book = epubsrc.load(
                epub_path, min_chars=min_chars, skip_front_matter=not keep_front_matter
            )
        if not book.chapters:
            console.print("[red]No readable chapters found. Try --keep-front-matter.[/red]")
            raise typer.Exit(1)

        digest = file_hash(epub_path)
        job = store.job()
        if job and job["epub_hash"] != digest:
            console.print(
                "[yellow]The EPUB changed since this job started; "
                "affected chapters will be re-rendered.[/yellow]"
            )

        store.init_job(
            epub_path=epub_path.resolve(),
            epub_hash=digest,
            title=book.title,
            author=book.author,
            settings=settings.to_dict(),
            options={
                "min_chars": min_chars,
                "keep_front_matter": keep_front_matter,
                "bitrate": bitrate,
                "llm_effort": llm_effort,
            },
            llm_model=llm_model if llm else None,
        )
        store.replace_chapters(
            [(c.idx, c.title, c.href, c.text) for c in book.chapters]
        )
        if not reuse_selection:
            store.set_selection(_parse_range(only) if only else None)
        if retry_failed:
            requeued = store.reset_failures()
            if requeued:
                console.print(f"Re-queued {requeued} previously failed chunks.")

        console.print(
            Panel.fit(
                f"[bold]{book.title}[/bold]\n{book.author}\n\n"
                f"Voice [cyan]{settings.voice}[/cyan] at {settings.speed}x   "
                f"Pre-pass: {llm_model if llm else 'deterministic cleanup only'}\n"
                f"Working directory: {work.resolve()}",
                title="epub2audiobook",
            )
        )

        chapters = store.chapters(selected_only=True)

        # 2. Text preparation (cheap, and worth finishing before any audio).
        _prepare_text(store, chapters, llm=llm, llm_model=llm_model, llm_effort=llm_effort)

        # 3. Plan chunks from the prepared text.
        chapters = store.chapters(selected_only=True)
        settings_key = settings.key()
        for chapter in chapters:
            pieces = textprep.chunk(chapter.text, settings.max_chunk_chars)
            store.sync_chunks(
                chapter.idx, [(p, render_key(p, settings_key)) for p in pieces]
            )

        # 4. Render every outstanding chunk.
        _render(store, chapters, chunk_dir, chapter_dir, settings, workers)

        # 5. Mux.
        if audio_only:
            console.print("[green]Audio rendered.[/green] Skipping the M4B mux as asked.")
            return
        _mux(store, work, book, bitrate)

    except KeyboardInterrupt:
        console.print(
            f"\n[yellow]Stopped.[/yellow] Progress is saved. Continue with:\n"
            f"  [bold]epub2ab resume \"{work}\"[/bold]"
        )
        raise typer.Exit(130)
    except (TTSError, assemble.AssemblyError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    finally:
        store.close()


def _prepare_text(store: JobStore, chapters, *, llm: bool, llm_model: str, llm_effort: str) -> None:
    """Deterministic cleanup for every chapter, then the optional Claude pass."""
    for chapter in chapters:
        if chapter.prep_status in ("clean", "llm"):
            continue
        store.set_prep(chapter.idx, textprep.clean(chapter.raw_text), "clean")

    if not llm:
        return

    from .llmprep import LLMPrepError, LLMPrepper, estimate_cost

    chapters = store.chapters(selected_only=True)
    todo = [c for c in chapters if c.prep_status != "llm"]
    if not todo:
        console.print("Text pre-pass already complete.")
        return

    pending_chars = sum(len(c.text) for c in todo)
    console.print(
        f"Running the {llm_model} pre-pass over {len(todo)} chapters "
        f"({pending_chars:,} chars, roughly ${estimate_cost(pending_chars, llm_model):.2f})."
    )

    try:
        prepper = LLMPrepper(model=llm_model, effort=llm_effort)
    except LLMPrepError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    with _progress() as progress:
        task = progress.add_task("Preparing text", total=len(todo))
        for chapter in todo:
            try:
                prepared = prepper.prepare(chapter.title, chapter.text)
                store.set_prep(chapter.idx, prepared, "llm")
            except LLMPrepError as exc:
                # Keep the deterministic cleanup and carry on; the book still
                # converts, just without the polish on this chapter.
                store.set_prep(chapter.idx, chapter.text, "clean", str(exc))
                progress.console.print(
                    f"[yellow]Chapter {chapter.idx} pre-pass failed, "
                    f"using cleaned text: {exc}[/yellow]"
                )
            progress.advance(task)

    console.print(
        f"Pre-pass tokens: {prepper.input_tokens:,} in / {prepper.output_tokens:,} out"
        + (f" ({prepper.cache_read_tokens:,} cached)" if prepper.cache_read_tokens else "")
    )


def _render(
    store: JobStore,
    chapters,
    chunk_dir: Path,
    chapter_dir: Path,
    settings,
    workers: int,
) -> None:
    from . import render as rnd

    outstanding = {c.idx: store.chunks(c.idx) for c in chapters}
    total = sum(len(v) for v in outstanding.values())
    already = sum(1 for v in outstanding.values() for c in v if c.status == DONE)

    if already >= total and all(store.chapter(c.idx).status == DONE for c in chapters):
        console.print("All chapters already rendered.")
        return

    # Build the work list up front, in chapter order, so chapters finish and
    # get assembled roughly in order and an interrupted run leaves a
    # contiguous prefix of the book done.
    jobs: list[rnd.ChunkJob] = []
    remaining: dict[int, int] = {}
    for chapter in chapters:
        pending = [
            c
            for c in outstanding[chapter.idx]
            if not (c.status == DONE and c.wav_path and Path(c.wav_path).exists())
        ]
        remaining[chapter.idx] = len(pending)
        for chunk in pending:
            jobs.append(
                rnd.ChunkJob(
                    chapter_idx=chapter.idx,
                    chunk_idx=chunk.idx,
                    text=chunk.text,
                    path=chunk_dir / f"ch{chapter.idx:04d}" / f"{chunk.idx:05d}.wav",
                )
            )

    if workers <= 0:
        workers = rnd.default_workers()
    workers = max(1, min(workers, len(jobs) or 1))
    threads = rnd.threads_per_worker(workers)

    console.print(
        f"Rendering {len(jobs)} of {total} chunks "
        f"with [cyan]{workers}[/cyan] worker{'s' if workers > 1 else ''} "
        f"x {threads} thread{'s' if threads > 1 else ''}."
    )

    def finish(result) -> None:
        """Record one chunk. Runs only in the parent, so SQLite stays single-writer."""
        if result.error:
            store.fail_chunk(result.chapter_idx, result.chunk_idx, result.error)
        else:
            store.finish_chunk(
                result.chapter_idx, result.chunk_idx, result.path, result.duration
            )

    with _progress() as progress:
        task = progress.add_task("Synthesising", total=total, completed=already)
        failures = 0

        if workers == 1:
            from .tts import KokoroEngine

            engine = KokoroEngine(
                lang=settings.lang, voice=settings.voice, speed=settings.speed
            )
            # Load before the loop so a broken install fails once, loudly,
            # rather than once per chunk.
            engine.load()
            for job in jobs:
                try:
                    duration = engine.synthesize_to_file(job.text, job.path)
                    result = rnd.ChunkResult(
                        job.chapter_idx, job.chunk_idx, str(job.path), duration
                    )
                except Exception as exc:  # one bad chunk must not lose the book
                    result = rnd.ChunkResult(
                        job.chapter_idx, job.chunk_idx, None, 0.0, repr(exc)
                    )
                failures += _record(store, result, progress, finish)
                progress.advance(task)
                _maybe_close_chapter(store, result.chapter_idx, remaining,
                                     chapter_dir, settings)
        else:
            import concurrent.futures as cf

            with cf.ProcessPoolExecutor(
                max_workers=workers,
                initializer=rnd.init_worker,
                initargs=(settings.lang, settings.voice, settings.speed, threads),
                # Bounds the resident-set drift a long-running worker
                # accumulates; without it a multi-hour book can end up swapping.
                max_tasks_per_child=rnd.MAX_TASKS_PER_WORKER,
            ) as pool:
                futures = {pool.submit(rnd.render_chunk, j): j for j in jobs}
                try:
                    for future in cf.as_completed(futures):
                        result = future.result()
                        failures += _record(store, result, progress, finish)
                        progress.advance(task)
                        _maybe_close_chapter(store, result.chapter_idx, remaining,
                                             chapter_dir, settings)
                except KeyboardInterrupt:
                    # Drop queued work immediately; finished chunks are already
                    # committed, so the next run resumes from here.
                    for f in futures:
                        f.cancel()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise

    # Chapters with no chunks at all would otherwise stall the mux forever.
    for chapter in chapters:
        if not store.chunks(chapter.idx):
            store.set_chapter_status(chapter.idx, DONE)
    store.touch()

    if failures:
        console.print(
            f"[red]{failures} chunks failed.[/red] Re-run with --retry-failed "
            "to queue them again."
        )


def _record(store: JobStore, result, progress, finish) -> int:
    finish(result)
    # Stamp the job so anything watching the database -- the web UI, another
    # terminal -- can tell a live run from an abandoned one.
    store.touch()
    if result.error:
        progress.console.print(
            f"[red]Chunk {result.chapter_idx}/{result.chunk_idx} failed:[/red] "
            f"{result.error[:160]}"
        )
        return 1
    return 0


def _maybe_close_chapter(
    store: JobStore, chapter_idx: int, remaining: dict[int, int], chapter_dir: Path, settings
) -> None:
    """Assemble a chapter's WAV as soon as its last chunk lands."""
    remaining[chapter_idx] -= 1
    if remaining[chapter_idx] > 0:
        return

    chunks = store.chunks(chapter_idx)
    if not chunks or not all(c.status == DONE for c in chunks):
        store.set_chapter_status(chapter_idx, "pending")
        return

    chapter_wav = chapter_dir / f"ch{chapter_idx:04d}.wav"
    if chapter_wav.exists() and store.chapter(chapter_idx).status == DONE:
        return
    duration = assemble.build_chapter_wav(
        [Path(c.wav_path) for c in chunks],
        chapter_wav,
        gap_ms=settings.gap_ms,
        tail_ms=settings.chapter_gap_ms,
    )
    store.set_chapter_audio(chapter_idx, str(chapter_wav), duration)


def _mux(store: JobStore, work: Path, book, bitrate: str) -> None:
    chapters = store.chapters(selected_only=True)
    ready = [c for c in chapters if c.status == DONE and c.audio_path]
    missing = [c for c in chapters if c.status != DONE]

    if missing:
        console.print(
            f"[yellow]{len(missing)} chapters are not finished "
            f"({', '.join(str(c.idx) for c in missing[:8])}"
            f"{'...' if len(missing) > 8 else ''}).[/yellow]\n"
            "Fix the failures and re-run, or pass --audio-only to skip the mux."
        )
        raise typer.Exit(1)

    out_file = work / f"{_safe_name(book.title)}.m4b"
    with console.status("Encoding M4B..."):
        assemble.build_m4b(
            [
                assemble.ChapterAudio(c.title, Path(c.audio_path), c.duration or 0.0)
                for c in ready
            ],
            out_file,
            work_dir=work / "work",
            title=book.title,
            author=book.author,
            description=book.description,
            cover=book.cover,
            cover_media_type=book.cover_media_type,
            bitrate=bitrate,
            notify=lambda msg: console.print(f"[yellow]{msg}[/yellow]"),
        )

    total = sum(c.duration or 0.0 for c in ready)
    size_mb = out_file.stat().st_size / (1024 * 1024)
    console.print(
        Panel.fit(
            f"[green]{out_file.resolve()}[/green]\n"
            f"{len(ready)} chapters   {textprep.format_duration(total)}   {size_mb:.1f} MB\n\n"
            "Import into Apple Books: open the Books app and drag the .m4b in,\n"
            "or File > Add to Library. It lands under Audiobooks and syncs via iCloud.",
            title="Done",
        )
    )


# -- helpers ---------------------------------------------------------------


def _progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )


def _work_dir(epub_path: Path, out: Path | None) -> Path:
    if out:
        return out
    return Path.cwd() / _safe_name(epub_path.stem)


def _safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    return (name or "audiobook")[:120]


def _parse_range(spec: str) -> set[int]:
    """Parse '0-5,9,12' into a set of chapter indices."""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    return out


def main() -> None:  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
