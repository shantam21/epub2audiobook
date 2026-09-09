"""Deterministic text cleanup and sentence-aware chunking.

Kokoro degrades past roughly 500 phoneme tokens in one pass, so text is cut
into chunks of a few hundred characters. The cuts always land on sentence
boundaries -- splitting mid-sentence is the single most audible defect in a
generated audiobook, because the model re-derives prosody per chunk.

Paragraph breaks survive as newlines inside a chunk; the TTS layer splits on
them so the pause lands where a reader would take one.
"""

from __future__ import annotations

import re
import unicodedata

# Roughly how many characters of prose Kokoro speaks per second at speed 1.0.
CHARS_PER_SECOND = 14.5

# Wall-clock cost of rendering, as a multiple of the finished audio's length.
# Measured at ~1.15x on a mid-range CPU with no GPU: a five-hour audiobook takes
# closer to six hours to generate. Kokoro is small, but it is not fast, and
# there is no GPU path here. Scale expectations, not just estimates.
RENDER_TIME_FACTOR = 1.15

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "rev", "hon", "gen",
    "col", "capt", "lt", "sgt", "fig", "vol", "no", "vs", "etc", "al", "ca",
    "cf", "ed", "eds", "esp", "inc", "ltd", "co", "approx", "dept", "univ",
    "i.e", "e.g", "a.m", "p.m", "u.s", "u.k",
}

# A boundary is the punctuation plus any closing quote or bracket. Matching the
# punctuation rather than the whitespace after it keeps the closing quote
# attached to its own sentence -- splitting on whitespace silently eats it.
_SENT_BOUNDARY = re.compile(r'[.!?…]["\'\)\]]*(?=\s)')
_WORD_BEFORE_END = re.compile(r'([A-Za-z\.]+)[\."\'\)\]]*\s*$')
_CLAUSE_SPLIT = re.compile(r'(?<=[,;:—–])\s+')
_ROMAN = re.compile(r"M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})", re.IGNORECASE)


def clean(text: str) -> str:
    """Normalise raw EPUB text into something worth speaking."""
    text = unicodedata.normalize("NFKC", text)

    text = text.replace("­", "")                    # soft hyphens
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("…", "...")
    text = re.sub(r"[—–]", " - ", text)        # em/en dash -> spoken pause
    text = text.replace(" ", " ")

    # Words hyphenated across a line break in the source.
    text = re.sub(r"(\w)-[ \t]*\n[ \t]*(\w)", r"\1\2", text)

    # Line-level pass first. Page numbers and scene-break rules are properties
    # of a whole line, and clearing them before the inline rules below stops a
    # scene break like "* * *" being chewed on as a footnote marker.
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if _is_running_head(line):
            continue
        lines.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))

    # A newline inside a paragraph is print layout, not a pause. The TTS layer
    # treats newlines as breath points, so these have to go or the narrator
    # stops in the middle of sentences.
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)

    # Bare footnote markers and bracketed reference numbers, anchored to their
    # own line so they cannot reach across a paragraph break.
    text = re.sub(r"\[\s*\d+\s*\]", "", text)
    text = re.sub(r"(?<=[a-z\.\,\"'\)])[ \t]*[\*†‡]+(?=\s|$)", "", text)

    text = re.sub(r"\.{4,}", "...", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def _is_running_head(line: str) -> bool:
    """Page numbers and standalone separators left over from print layout."""
    if not line:
        return False
    if re.fullmatch(r"\d{1,4}", line):
        return True
    if len(line) <= 7 and _ROMAN.fullmatch(line):
        return True
    if re.fullmatch(r"[^\w]{1,12}", line):  # "* * *", "---", "~"
        return True
    return False


def split_sentences(paragraph: str) -> list[str]:
    """Split on sentence enders, holding back on known abbreviations."""
    if not paragraph.strip():
        return []
    parts: list[str] = []
    start = 0
    for m in _SENT_BOUNDARY.finditer(paragraph):
        candidate = paragraph[start : m.end()].strip()
        if not candidate:
            continue
        if _ends_on_abbreviation(candidate) or _ends_on_initial(candidate):
            continue
        parts.append(candidate)
        start = m.end()
    tail = paragraph[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _ends_on_abbreviation(text: str) -> bool:
    if not text.endswith("."):
        return False
    m = _WORD_BEFORE_END.search(text)
    if not m:
        return False
    return m.group(1).rstrip(".").lower() in _ABBREVIATIONS


def _ends_on_initial(text: str) -> bool:
    """'J. R. R. Tolkien' -- a single capital plus a period is not a sentence."""
    return bool(re.search(r"(?:^|\s)[A-Z]\.$", text))


def chunk(text: str, max_chars: int = 380) -> list[str]:
    """Break cleaned text into TTS-sized chunks on sentence boundaries.

    Paragraph boundaries are preserved as newlines so the synthesiser can put a
    real pause there.
    """
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    for p_i, paragraph in enumerate(paragraphs):
        first_in_paragraph = True
        for sentence in split_sentences(paragraph):
            for unit in _fit(sentence, max_chars):
                sep = "\n" if (first_in_paragraph and p_i > 0 and current) else " "
                added = len(unit) + (len(sep) if current else 0)
                if current and current_len + added > max_chars:
                    chunks.append("".join(current).strip())
                    current, current_len = [unit], len(unit)
                else:
                    if current:
                        current.append(sep)
                        current_len += len(sep)
                    current.append(unit)
                    current_len += len(unit)
                first_in_paragraph = False

    if current:
        chunks.append("".join(current).strip())
    return [c for c in chunks if c.strip()]


def _fit(sentence: str, max_chars: int) -> list[str]:
    """Break a sentence that is too long on its own, preferring clause breaks."""
    if len(sentence) <= max_chars:
        return [sentence]

    out: list[str] = []
    buf = ""
    for clause in _CLAUSE_SPLIT.split(sentence):
        candidate = f"{buf} {clause}".strip() if buf else clause
        if len(candidate) > max_chars and buf:
            out.append(buf)
            buf = clause
        else:
            buf = candidate
    if buf:
        out.append(buf)

    # Still oversized (no punctuation at all) -- fall back to word boundaries.
    final: list[str] = []
    for part in out:
        while len(part) > max_chars:
            cut = part.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            final.append(part[:cut].strip())
            part = part[cut:].strip()
        if part:
            final.append(part)
    return final


def estimate_seconds(chars: int, speed: float = 1.0) -> float:
    return chars / (CHARS_PER_SECOND * max(speed, 0.1))


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"
