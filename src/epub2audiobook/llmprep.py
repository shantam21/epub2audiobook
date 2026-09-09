"""Optional Claude pre-pass that rewrites chapter text for the ear.

A TTS model reads what you give it literally: "Ch. 4", "1885", "Dr. Ashworth-
Vane", "p. 231n" and a stray page header all come out wrong. This pass converts
the text into something meant to be spoken, without changing the prose itself.

It is deliberately conservative. The output is length-checked against the
input, and any chapter that comes back suspiciously short falls back to the
deterministic cleanup -- a summarised chapter is far worse than an unpolished
one, and it would be invisible until you were listening to it.
"""

from __future__ import annotations

import re

DEFAULT_MODEL = "claude-opus-5"

# Stable across every request, so it caches. Nothing volatile belongs in here.
SYSTEM_PROMPT = """\
You prepare book text for a text-to-speech narrator. You rewrite the text so \
that a speech synthesiser reads it correctly aloud. You are not an editor and \
not a summariser.

Rules, in priority order:

1. Preserve every sentence and every idea. Never summarise, condense, omit, \
reorder, or add content. The output must carry the same prose as the input, \
sentence for sentence.
2. Expand what a synthesiser mispronounces:
   - Numerals and dates into spoken words ("1885" -> "eighteen eighty-five", \
"Chapter 12" -> "Chapter Twelve", "$4.50" -> "four dollars and fifty cents", \
"3/4" -> "three quarters").
   - Abbreviations into full words ("Dr." -> "Doctor", "St." -> "Saint" or \
"Street" as context requires, "e.g." -> "for example", "vs." -> "versus").
   - Symbols into words ("&" -> "and", "%" -> "percent", "#3" -> "number three").
   - Roman numerals used as ordinals ("Henry VIII" -> "Henry the Eighth").
   - Initialisms that are spoken as letters get spaced ("FBI" -> "F B I"); ones \
spoken as words are left alone ("NASA", "NATO").
3. Delete artefacts of the printed page: running heads, page numbers, footnote \
and endnote markers, figure and table captions stranded mid-paragraph, \
"[illustration]" placeholders, and cross-references like "see p. 231" when they \
are parenthetical noise.
4. Repair layout damage: rejoin words split across line breaks, rejoin \
paragraphs broken mid-sentence, and drop duplicated chapter titles.
5. Keep the author's words, spelling, dialect, and dialogue exactly as written. \
Do not modernise, correct grammar, or smooth style.
6. Keep paragraph breaks as blank lines. Use plain text only -- no markdown, no \
headings, no bullet characters, no commentary.

Return only the prepared text, wrapped in <normalized> and </normalized> tags. \
Write nothing before or after those tags.
"""

_TAG_RE = re.compile(r"<normalized>\s*(.*?)\s*</normalized>", re.DOTALL | re.IGNORECASE)

# Windows are cut at paragraph boundaries; each is normalised independently.
WINDOW_CHARS = 8000

# Length band the output has to land in. The floor is the one that matters --
# it catches summarisation, which is the failure that would otherwise go
# unnoticed until you were hours into listening. The ceiling only catches
# runaway repetition, and it has to be generous on short passages, where
# expanding a couple of numbers ("$4.50" -> "four dollars and fifty cents")
# genuinely doubles the character count.
MIN_RATIO, MAX_RATIO = 0.55, 2.2
SHORT_SOURCE_CHARS, SHORT_MAX_RATIO = 400, 4.0


class LLMPrepError(RuntimeError):
    pass


class LLMPrepper:
    """Wraps the Messages API for chapter normalisation."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        effort: str = "low",
    ):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise LLMPrepError(
                "The LLM pre-pass needs the anthropic package. "
                "Install it with: pip install 'epub2audiobook[llm]'"
            ) from exc

        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth
        # login` profile when api_key is None.
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0

    def prepare(self, title: str, text: str) -> str:
        """Normalise one chapter. Returns the original text on any soft failure."""
        out_parts: list[str] = []
        windows = _windows(text, WINDOW_CHARS)
        for i, window in enumerate(windows):
            header = f"Book chapter: {title}"
            if len(windows) > 1:
                header += f" (part {i + 1} of {len(windows)}; continue mid-chapter)"
            prepared = self._one(header, window)
            out_parts.append(prepared if prepared is not None else window)
        return "\n\n".join(out_parts).strip()

    def _one(self, header: str, window: str) -> str | None:
        prompt = f"{header}\n\nPrepare this text for narration:\n\n{window}"
        # ~3.5 chars per token, doubled for expansion headroom.
        max_tokens = min(32_000, int(len(window) / 3.5 * 2) + 2_000)

        try:
            with self.client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                response = stream.get_final_message()
        except self._anthropic.BadRequestError as exc:
            raise LLMPrepError(f"Bad request: {exc}") from exc
        except self._anthropic.AuthenticationError as exc:
            raise LLMPrepError(
                "Authentication failed. Set ANTHROPIC_API_KEY or run `ant auth login`."
            ) from exc
        except self._anthropic.RateLimitError as exc:
            raise LLMPrepError(f"Rate limited: {exc}") from exc
        except self._anthropic.APIStatusError as exc:
            raise LLMPrepError(f"API error {exc.status_code}: {exc}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise LLMPrepError(f"Network error: {exc}") from exc

        usage = response.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

        if response.stop_reason == "refusal":
            return None
        if response.stop_reason == "max_tokens":
            # A truncated window would silently lose the tail of the chapter.
            return None

        text = "".join(b.text for b in response.content if b.type == "text")
        match = _TAG_RE.search(text)
        normalized = (match.group(1) if match else text).strip()
        if not _plausible(window, normalized):
            return None
        return normalized


def _plausible(source: str, result: str) -> bool:
    """Guard against summarisation, refusals, and empty responses."""
    if not result:
        return False
    ratio = len(result) / max(len(source), 1)
    ceiling = SHORT_MAX_RATIO if len(source) < SHORT_SOURCE_CHARS else MAX_RATIO
    return MIN_RATIO <= ratio <= ceiling


def _windows(text: str, size: int) -> list[str]:
    """Cut text into windows of about `size` chars, always at a paragraph break."""
    paragraphs = text.split("\n\n")
    windows: list[str] = []
    buf: list[str] = []
    length = 0
    for paragraph in paragraphs:
        if buf and length + len(paragraph) > size:
            windows.append("\n\n".join(buf))
            buf, length = [], 0
        buf.append(paragraph)
        length += len(paragraph) + 2
    if buf:
        windows.append("\n\n".join(buf))
    return windows or [text]


def estimate_cost(total_chars: int, model: str = DEFAULT_MODEL) -> float:
    """Rough USD estimate. Output is assumed to match input in length."""
    prices = {  # (input $/MTok, output $/MTok)
        "claude-opus-5": (5.0, 25.0),
        "claude-sonnet-5": (2.0, 10.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }
    in_price, out_price = prices.get(model, (5.0, 25.0))
    tokens = total_chars / 3.5 / 1_000_000
    return tokens * in_price + tokens * out_price
