"""Parallel chunk rendering across worker processes.

Kokoro on CPU does not saturate a modern machine on its own -- torch's intra-op
threading tops out around two or three cores, so most of the box sits idle
through a multi-hour book. Running several worker processes, each with a small
thread budget, uses the rest.

Only synthesis is parallel. Every database write still happens in the parent
process, so SQLite keeps exactly one writer and the crash-safety guarantee is
unchanged: a chunk is marked done only after its .wav is fully on disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .tts import KokoroEngine

# Each worker holds its own Kokoro model; the pipeline is not shareable across
# processes. This is process-global by necessity -- it is what the pool's
# initializer sets up once per worker, instead of per chunk.
_ENGINE: KokoroEngine | None = None

# A worker's resident set with torch, Kokoro and spaCy loaded. Measured at
# ~1.0 GB on Windows, drifting up towards 1.5 GB as a worker runs.
WORKER_RAM_GB = 1.3

# Deliberately NOT recycling workers.
#
# ProcessPoolExecutor's `max_tasks_per_child` deadlocks on Python 3.12 /
# Windows / spawn: a 30-line reproduction with `time.sleep` as the payload
# hangs 3 runs out of 3, and removing the parameter completes in 1.6s. Because
# the pool spreads work evenly, every worker reaches the limit on the same
# chunk, so the pool tries to replace all of them at once and never recovers.
#
# It was added to bound a memory leak that measurement had already shown was
# not a leak: a worker's resident set climbs from ~1.0 GB to ~1.5 GB and then
# settles (+400 MB, then +34 MB over the same interval). Recycling guarded a
# problem that does not exist, at the cost of a guaranteed hang partway
# through every long book.

# Share of total RAM we will spend on workers when free memory reads low.
# Measured: two workers on a 9.8 GB machine settle near 3 GB, about a third.
BUDGET_FRACTION = 0.35


@dataclass
class ChunkJob:
    chapter_idx: int
    chunk_idx: int
    text: str
    path: Path


@dataclass
class ChunkResult:
    chapter_idx: int
    chunk_idx: int
    path: str | None
    duration: float
    error: str | None = None


def default_workers() -> int:
    """A worker count that speeds things up without thrashing the machine."""
    logical = os.cpu_count() or 2
    # Two logical cores per worker, so each still gets real intra-op threading.
    by_cpu = max(1, logical // 4)
    by_ram = max(1, int(memory_budget_gb() / WORKER_RAM_GB))
    return max(1, min(4, by_cpu, by_ram))


def memory_budget_gb() -> float:
    """How much RAM we are willing to spend on workers.

    Free memory alone is far too pessimistic: every OS reports only unused
    pages as free, while a large share of what looks "in use" is reclaimable
    cache. On a 10 GB machine showing 1.4 GB free, two workers settled at about
    3 GB combined and never swapped -- roughly a third of total RAM. So take
    whichever is larger: what is genuinely free, or a third of the total.
    """
    return max(_available_gb(), _total_gb() * BUDGET_FRACTION)


def threads_per_worker(workers: int) -> int:
    physical = max(1, (os.cpu_count() or 2) // 2)
    return max(1, physical // max(1, workers))


def init_worker(lang: str, voice: str, speed: float, threads: int) -> None:
    """Pool initializer: cap thread use, then load the model once."""
    # Set before torch is imported by the engine, or the limits are ignored.
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(threads))

    global _ENGINE
    _ENGINE = KokoroEngine(lang=lang, voice=voice, speed=speed)
    _ENGINE.load()

    try:
        import torch

        torch.set_num_threads(threads)
    except Exception:  # thread pinning is an optimisation, never a requirement
        pass


def render_chunk(job: ChunkJob) -> ChunkResult:
    """Render one chunk in a worker. Never raises -- errors come back as data."""
    try:
        if _ENGINE is None:  # pragma: no cover - initializer always runs first
            raise RuntimeError("worker was not initialised")
        duration = _ENGINE.synthesize_to_file(job.text, job.path)
        return ChunkResult(job.chapter_idx, job.chunk_idx, str(job.path), duration)
    except Exception as exc:
        return ChunkResult(job.chapter_idx, job.chunk_idx, None, 0.0, repr(exc))


def _total_gb() -> float:
    try:
        if os.name == "nt":
            return _win_memory()[0]
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024**3)
    except Exception:
        return 8.0


def _available_gb() -> float:
    """Free physical memory, so we don't start more workers than fit."""
    try:
        if os.name == "nt":
            return _win_memory()[1]

        pages = os.sysconf("SC_AVPHYS_PAGES")
        return pages * os.sysconf("SC_PAGE_SIZE") / (1024**3)
    except Exception:
        return 4.0  # unknown: assume enough for two workers


def _win_memory() -> tuple[float, float]:
    """(total GB, available GB) from GlobalMemoryStatusEx."""
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullTotalPhys / (1024**3), status.ullAvailPhys / (1024**3)
