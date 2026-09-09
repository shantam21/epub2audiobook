"""Kokoro-82M speech synthesis.

Kokoro is loaded once per process and reused for the whole book -- model load
is the expensive part, generation is comparatively cheap. Everything is 24 kHz
mono float32 until the final encode.
"""

from __future__ import annotations

import os
import threading
import warnings
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import SAMPLE_RATE

# Below this amplitude counts as silence when trimming chunk edges, so the gaps
# between chunks are the ones we chose rather than whatever the model padded.
_SILENCE_FLOOR = 0.0025
_EDGE_KEEP_MS = 30

KOKORO_REPO = "hexgrad/Kokoro-82M"


class TTSError(RuntimeError):
    pass


class KokoroEngine:
    """Thread-safe wrapper around a Kokoro pipeline."""

    def __init__(self, lang: str = "a", voice: str = "af_heart", speed: float = 1.0):
        self.lang = lang
        self.voice = voice
        self.speed = speed
        self._pipeline = None
        self._lock = threading.Lock()

    def load(self) -> None:
        if self._pipeline is not None:
            return
        # Kokoro's own model definition trips several torch deprecation and
        # config warnings on construction. They are about Kokoro's internals,
        # not anything the caller can act on, and they bury real errors.
        warnings.filterwarnings("ignore", category=FutureWarning, module="torch")
        warnings.filterwarnings("ignore", category=UserWarning, module="torch")
        warnings.filterwarnings("ignore", category=UserWarning, module="huggingface_hub.*")
        os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
        # Windows without Developer Mode cannot symlink into the HF cache. It
        # falls back to copying, which is fine and not worth a wall of text.
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        try:
            from kokoro import KPipeline
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise TTSError(
                "Kokoro is not installed. Run `uv sync` in the project, "
                "or: pip install kokoro soundfile"
            ) from exc
        except OSError as exc:  # pragma: no cover - platform guard
            # Almost always PyTorch failing to load its own DLLs on Windows.
            raise TTSError(
                f"PyTorch could not load its native libraries: {exc}\n\n"
                "On Windows this is nearly always an out-of-date Visual C++ "
                "runtime -- PyTorch needs 14.40 or newer. Install it with:\n"
                "  winget install Microsoft.VCRedist.2015+.x64\n"
                "then open a new terminal and re-run."
            ) from exc
        try:
            try:
                # Naming the repo explicitly avoids Kokoro's "defaulting
                # repo_id" warning on every run.
                self._pipeline = KPipeline(lang_code=self.lang, repo_id=KOKORO_REPO)
            except TypeError:
                self._pipeline = KPipeline(lang_code=self.lang)
        except Exception as exc:
            raise TTSError(
                f"Could not load Kokoro (lang_code={self.lang!r}): {exc}\n"
                "On Windows this is usually a missing espeak-ng backend used for "
                "out-of-vocabulary words. Install it from "
                "https://github.com/espeak-ng/espeak-ng/releases and re-run."
            ) from exc

    def synthesize(self, text: str) -> np.ndarray:
        """Render one chunk to a float32 mono waveform."""
        self.load()
        segments: list[np.ndarray] = []
        with self._lock:
            # split_pattern splits on the paragraph newlines the chunker left in
            # place, which is where a narrator would naturally pause.
            generator = self._pipeline(
                text, voice=self.voice, speed=self.speed, split_pattern=r"\n+"
            )
            for result in generator:
                audio = _as_array(result)
                if audio is not None and audio.size:
                    segments.append(_trim(audio))

        if not segments:
            raise TTSError(f"Kokoro produced no audio for: {text[:80]!r}")
        if len(segments) == 1:
            return segments[0]
        pause = silence(220)
        joined: list[np.ndarray] = []
        for i, seg in enumerate(segments):
            if i:
                joined.append(pause)
            joined.append(seg)
        return np.concatenate(joined)

    def synthesize_to_file(self, text: str, path: Path) -> float:
        audio = self.synthesize(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        # format is explicit: soundfile infers it from the extension, and the
        # ".part" suffix defeats that.
        sf.write(tmp, audio, SAMPLE_RATE, subtype="PCM_16", format="WAV")
        tmp.replace(path)  # atomic: a half-written wav is never marked done
        return len(audio) / SAMPLE_RATE


def silence(ms: int) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * ms / 1000), dtype=np.float32)


def _as_array(result) -> np.ndarray | None:
    """Kokoro has returned both 3-tuples and result objects across versions."""
    audio = None
    if isinstance(result, tuple):
        audio = result[-1]
    else:
        audio = getattr(result, "audio", None)
        if audio is None:
            audio = getattr(result, "output", None)
    if audio is None:
        return None
    if hasattr(audio, "detach"):  # torch tensor
        audio = audio.detach().cpu().numpy()
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    return audio


def _trim(audio: np.ndarray) -> np.ndarray:
    """Strip model-added silence from both ends, keeping a short cushion."""
    loud = np.flatnonzero(np.abs(audio) > _SILENCE_FLOOR)
    if loud.size == 0:
        return audio
    keep = int(SAMPLE_RATE * _EDGE_KEEP_MS / 1000)
    start = max(0, int(loud[0]) - keep)
    end = min(len(audio), int(loud[-1]) + keep)
    return audio[start:end]
