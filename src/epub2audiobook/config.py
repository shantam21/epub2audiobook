"""Tunable defaults, shared in one place so the CLI and the store agree."""

from __future__ import annotations

from dataclasses import dataclass, asdict, fields

SAMPLE_RATE = 24_000  # Kokoro always outputs 24 kHz mono

# Kokoro voice packs worth surfacing. The full set ships with the model; these
# are the ones that hold up over a whole book without going flat.
VOICES = {
    "af_heart": "American female - warm, the best all-round narrator",
    "af_bella": "American female - brighter, a little faster",
    "af_nicole": "American female - soft, close-mic ASMR feel",
    "af_sarah": "American female - neutral, newsreader-ish",
    "am_michael": "American male - steady, good for non-fiction",
    "am_adam": "American male - deeper, slower",
    "bf_emma": "British female - measured, good for classics",
    "bf_isabella": "British female - crisper",
    "bm_george": "British male - older, storytelling",
    "bm_lewis": "British male - low and even",
}

# Kokoro language codes. First letter of the voice name matches the code.
LANG_FOR_VOICE_PREFIX = {"a": "a", "b": "b"}


@dataclass
class RenderSettings:
    """Everything that changes the produced audio.

    Any change here invalidates cached chunk renders, so it is hashed into
    each chunk's render key.
    """

    voice: str = "af_heart"
    speed: float = 1.0
    lang: str = "a"
    engine: str = "kokoro"
    max_chunk_chars: int = 380
    gap_ms: int = 320          # silence between chunks inside a chapter
    chapter_gap_ms: int = 900  # silence at the end of each chapter

    def key(self) -> str:
        return "|".join(
            f"{f.name}={getattr(self, f.name)}" for f in sorted(fields(self), key=lambda f: f.name)
        )

    def to_dict(self) -> dict:
        return asdict(self)


def lang_for_voice(voice: str) -> str:
    return LANG_FOR_VOICE_PREFIX.get(voice[:1], "a")
