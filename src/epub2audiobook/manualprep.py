"""Manual text pre-pass: export chapters, paste them into the Claude app, import
the replies back.

Same quality benefit as the API pre-pass with no key and no per-token cost. The
tradeoff is your time: one paste per chapter part. The import side applies the
same paranoid length check the API path uses, so a chapter that came back
summarised is refused rather than silently narrated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .llmprep import SYSTEM_PROMPT, _plausible

# Chapters are split at this size so a single reply never runs into the app's
# per-message output limit. Well within what Claude handles in one go.
PART_CHARS = 12_000

_TAG_RE = re.compile(r"<normalized>\s*(.*?)\s*</normalized>", re.DOTALL | re.IGNORECASE)
_PART_RE = re.compile(r"^ch(\d{4})-(\d{2})\.in\.txt$")

CHAT_PROMPT = f"""\
{SYSTEM_PROMPT}
I will paste book text in my following messages, one chunk per message. Apply \
the rules above to each one and reply with only the prepared text inside \
<normalized> tags. Do not comment on the text, ask questions, or summarise what \
you changed -- just return the prepared text, every time, for every message I \
send. If a chunk starts or ends mid-sentence, leave it that way; it will be \
rejoined with its neighbours.
"""

INSTRUCTIONS = """\
HOW TO USE THIS FOLDER
======================

1. Open a new conversation in the Claude app.

2. Paste the entire contents of PROMPT.txt as your first message. Claude will
   acknowledge it. You only do this once per conversation.

3. For each ch####-##.in.txt file, in order:
     - paste the whole file as your next message
     - copy Claude's reply
     - save it next to the input as the same name with .out.txt
       (ch0000-01.in.txt  ->  ch0000-01.out.txt)

   Keeping it all in one conversation is better than starting fresh each time:
   Claude stays consistent about how it handles recurring names and numbers.

4. When you have done as many as you want, run:
     epub2ab import-text "<this folder's parent>"

   You do not have to finish every chapter. Anything without an .out.txt file
   keeps the automatic cleanup instead, and you can import again later.

5. Then convert as usual:
     epub2ab convert <book.epub> -o "<this folder's parent>"

NOTES
-----
- Keeping the <normalized> tags in what you save is fine; they are stripped.
- If a reply looks truncated, ask Claude to continue and append the rest to the
  same .out.txt file before importing.
- Import refuses any chapter that came back far shorter than it went in, which
  is what a summarised chapter looks like. Use --force to override.
"""


@dataclass
class Part:
    chapter_idx: int
    part_idx: int
    in_path: Path
    out_path: Path

    @property
    def done(self) -> bool:
        return self.out_path.exists() and self.out_path.stat().st_size > 0


def split_parts(text: str, size: int = PART_CHARS) -> list[str]:
    """Cut a chapter into paste-sized parts, always at a paragraph break."""
    paragraphs = text.split("\n\n")
    parts: list[str] = []
    buf: list[str] = []
    length = 0
    for paragraph in paragraphs:
        if buf and length + len(paragraph) > size:
            parts.append("\n\n".join(buf))
            buf, length = [], 0
        buf.append(paragraph)
        length += len(paragraph) + 2
    if buf:
        parts.append("\n\n".join(buf))
    return parts or [text]


def export(prep_dir: Path, chapters, size: int = PART_CHARS) -> list[Part]:
    """Write one .in.txt per chapter part, plus the prompt and instructions."""
    prep_dir.mkdir(parents=True, exist_ok=True)
    (prep_dir / "PROMPT.txt").write_text(CHAT_PROMPT, encoding="utf-8")
    (prep_dir / "README.txt").write_text(INSTRUCTIONS, encoding="utf-8")

    written: list[Part] = []
    for chapter in chapters:
        for i, body in enumerate(split_parts(chapter.text, size), start=1):
            part = _part(prep_dir, chapter.idx, i)
            # Never clobber an input the user may already have pasted.
            if not part.in_path.exists() or part.in_path.read_text(
                encoding="utf-8"
            ) != body:
                part.in_path.write_text(body, encoding="utf-8")
            written.append(part)
    return written


def scan(prep_dir: Path) -> dict[int, list[Part]]:
    """Group the exported parts on disk by chapter index."""
    by_chapter: dict[int, list[Part]] = {}
    if not prep_dir.exists():
        return by_chapter
    for path in sorted(prep_dir.glob("ch*.in.txt")):
        m = _PART_RE.match(path.name)
        if not m:
            continue
        ch, pt = int(m.group(1)), int(m.group(2))
        by_chapter.setdefault(ch, []).append(_part(prep_dir, ch, pt))
    for parts in by_chapter.values():
        parts.sort(key=lambda p: p.part_idx)
    return by_chapter


def read_chapter(parts: list[Part], force: bool = False) -> tuple[str | None, str | None]:
    """Join a chapter's replies. Returns (text, problem)."""
    if not parts:
        return None, "no exported parts"
    missing = [p.part_idx for p in parts if not p.done]
    if missing:
        return None, f"waiting on part{'s' if len(missing) > 1 else ''} " + ", ".join(
            str(m) for m in missing
        )

    bodies: list[str] = []
    for part in parts:
        raw = part.out_path.read_text(encoding="utf-8").strip()
        match = _TAG_RE.search(raw)
        body = (match.group(1) if match else raw).strip()
        if not body:
            return None, f"part {part.part_idx} is empty"
        source = part.in_path.read_text(encoding="utf-8")
        if not force and not _plausible(source, body):
            return None, (
                f"part {part.part_idx} came back {len(body):,} chars "
                f"from {len(source):,} - looks summarised or truncated"
            )
        bodies.append(body)
    return "\n\n".join(bodies), None


def _part(prep_dir: Path, chapter_idx: int, part_idx: int) -> Part:
    stem = f"ch{chapter_idx:04d}-{part_idx:02d}"
    return Part(
        chapter_idx=chapter_idx,
        part_idx=part_idx,
        in_path=prep_dir / f"{stem}.in.txt",
        out_path=prep_dir / f"{stem}.out.txt",
    )
