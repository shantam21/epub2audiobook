"""Audio assembly: chunks -> chapter WAVs -> a single chaptered M4B.

The M4B is what Apple Books wants: an MPEG-4 audio container with embedded
chapter markers, cover art, and audiobook metadata. Apple Books keeps your
listening position per file and uses the chapter markers for its scrubber, so
getting the markers right is what makes the result usable rather than just
playable.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import SAMPLE_RATE
from .tts import silence


class AssemblyError(RuntimeError):
    pass


@dataclass
class ChapterAudio:
    title: str
    path: Path
    duration: float


def require_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise AssemblyError(
            "ffmpeg was not found on PATH, and it is needed to write the .m4b.\n"
            "  Windows:  winget install Gyan.FFmpeg\n"
            "  macOS:    brew install ffmpeg\n"
            "  Linux:    sudo apt install ffmpeg\n"
            "Then open a new terminal so PATH is refreshed."
        )
    return exe


def build_chapter_wav(
    chunk_paths: list[Path],
    out_path: Path,
    gap_ms: int = 320,
    tail_ms: int = 900,
) -> float:
    """Concatenate chunk WAVs into one chapter WAV, written incrementally."""
    if not chunk_paths:
        raise AssemblyError(f"No audio chunks to assemble for {out_path.name}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".part.wav")
    gap = silence(gap_ms)
    tail = silence(tail_ms)
    frames = 0

    with sf.SoundFile(
        tmp, "w", samplerate=SAMPLE_RATE, channels=1, subtype="PCM_16"
    ) as out:
        for i, path in enumerate(chunk_paths):
            if i:
                out.write(gap)
                frames += len(gap)
            audio, sr = sf.read(path, dtype="float32", always_2d=False)
            if sr != SAMPLE_RATE:
                raise AssemblyError(f"{path} is {sr} Hz, expected {SAMPLE_RATE} Hz")
            audio = np.asarray(audio, dtype=np.float32).reshape(-1)
            out.write(audio)
            frames += len(audio)
        out.write(tail)
        frames += len(tail)

    tmp.replace(out_path)
    return frames / SAMPLE_RATE


def build_m4b(
    chapters: list[ChapterAudio],
    out_path: Path,
    *,
    work_dir: Path,
    title: str,
    author: str,
    description: str = "",
    year: str = "",
    cover: bytes | None = None,
    cover_media_type: str = "image/jpeg",
    bitrate: str = "64k",
    notify: Callable[[str], None] | None = None,
) -> Path:
    """Encode chapter WAVs into one chaptered, cover-art M4B."""
    if not chapters:
        raise AssemblyError("Nothing to assemble - no completed chapters.")
    ffmpeg = require_ffmpeg()
    work_dir.mkdir(parents=True, exist_ok=True)

    concat_file = work_dir / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{_escape_concat(c.path)}'\n" for c in chapters), encoding="utf-8"
    )

    meta_file = work_dir / "chapters.ffmeta"
    meta_file.write_text(
        _ffmetadata(chapters, title, author, description, year), encoding="utf-8"
    )

    cover_file = None
    if cover:
        ext = ".png" if "png" in cover_media_type else ".jpg"
        cover_file = work_dir / f"cover{ext}"
        cover_file.write_bytes(cover)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out_path.with_suffix(".part.m4b")

    def command(with_cover: bool) -> list[str]:
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-i", str(meta_file),
        ]
        if with_cover:
            cmd += ["-i", str(cover_file)]
        cmd += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
        if with_cover:
            # Re-encode rather than copy: the MP4 muxer rejects a PNG stream
            # ("could not find tag for codec png"). MJPEG needs full-range
            # YUV explicitly, or the encoder refuses to open at all.
            cmd += [
                "-map", "2:v",
                "-c:v", "mjpeg",
                "-pix_fmt", "yuvj420p",
                "-disposition:v:0", "attached_pic",
            ]
        cmd += [
            "-c:a", "aac", "-b:a", bitrate, "-ar", str(SAMPLE_RATE), "-ac", "1",
            "-movflags", "+faststart",
            "-f", "mp4",
            str(tmp_out),
        ]
        return cmd

    proc = subprocess.run(command(bool(cover_file)), capture_output=True, text=True)
    if proc.returncode != 0 and cover_file:
        # A malformed or exotic cover image is not worth losing the audiobook
        # over -- drop it and keep the audio.
        retry = subprocess.run(command(False), capture_output=True, text=True)
        if retry.returncode == 0:
            if notify:
                notify(
                    "Cover art could not be encoded, so the audiobook was "
                    "written without it."
                )
            tmp_out.replace(out_path)
            return out_path
    if proc.returncode != 0:
        raise AssemblyError(
            "ffmpeg failed while writing the .m4b:\n" + (proc.stderr or proc.stdout)[-4000:]
        )

    tmp_out.replace(out_path)
    return out_path


def _escape_concat(path: Path) -> str:
    """The concat demuxer takes POSIX-style paths in single quotes."""
    return path.resolve().as_posix().replace("'", r"'\''")


def _esc(value: str) -> str:
    """ffmetadata reserves =, ;, #, \\ and newline."""
    out = []
    for ch in value:
        if ch in "=;#\\":
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\\n")
        else:
            out.append(ch)
    return "".join(out)


def _ffmetadata(
    chapters: list[ChapterAudio], title: str, author: str, description: str, year: str
) -> str:
    lines = [";FFMETADATA1"]
    lines.append(f"title={_esc(title)}")
    lines.append(f"artist={_esc(author)}")
    lines.append(f"album_artist={_esc(author)}")
    lines.append(f"album={_esc(title)}")
    lines.append("genre=Audiobook")
    lines.append("media_type=2")  # 2 = Audiobook; makes Apple Books shelve it correctly
    if description:
        lines.append(f"description={_esc(description[:2000])}")
        lines.append(f"comment={_esc(description[:2000])}")
    if year:
        lines.append(f"date={_esc(year)}")

    start_ms = 0
    for chapter in chapters:
        end_ms = start_ms + int(round(chapter.duration * 1000))
        lines += [
            "",
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={max(end_ms - 1, start_ms)}",
            f"title={_esc(chapter.title)}",
        ]
        start_ms = end_ms
    return "\n".join(lines) + "\n"
