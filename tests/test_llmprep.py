"""Pure-logic tests for the Claude pre-pass. No network involved."""

import pytest

from epub2audiobook import llmprep


class TestWindows:
    def test_short_text_is_one_window(self):
        assert llmprep._windows("a short chapter", 8000) == ["a short chapter"]

    def test_splits_only_on_paragraph_breaks(self):
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 200 for i in range(10))
        windows = llmprep._windows(text, 2000)
        assert len(windows) > 1
        for w in windows:
            assert not w.startswith(" ") and not w.endswith("\n")

    def test_loses_no_content(self):
        text = "\n\n".join(f"Paragraph number {i}." for i in range(40))
        rejoined = "\n\n".join(llmprep._windows(text, 200))
        assert rejoined == text


class TestPlausible:
    def test_accepts_a_normal_expansion(self):
        # Number expansion legitimately makes the text longer.
        assert llmprep._plausible("In 1885 he paid $4.50.", "In eighteen eighty-five he paid four dollars and fifty cents.")

    def test_rejects_a_summary(self):
        source = "word " * 1000
        assert not llmprep._plausible(source, "He went to the shop.")

    def test_rejects_empty(self):
        assert not llmprep._plausible("some text here", "")

    def test_rejects_runaway_output(self):
        assert not llmprep._plausible("short", "padding " * 500)


class TestTagExtraction:
    def test_pulls_text_out_of_tags(self):
        m = llmprep._TAG_RE.search("<normalized>\nHello there.\n</normalized>")
        assert m.group(1) == "Hello there."

    def test_ignores_surrounding_chatter(self):
        raw = "Here you go:\n<normalized>The text.</normalized>\nLet me know!"
        assert llmprep._TAG_RE.search(raw).group(1) == "The text."

    def test_handles_multiline_bodies(self):
        raw = "<normalized>Line one.\n\nLine two.</normalized>"
        assert llmprep._TAG_RE.search(raw).group(1) == "Line one.\n\nLine two."


class TestCostEstimate:
    def test_scales_with_length(self):
        assert llmprep.estimate_cost(1_000_000) > llmprep.estimate_cost(100_000)

    def test_cheaper_models_cost_less(self):
        chars = 600_000
        assert llmprep.estimate_cost(chars, "claude-haiku-4-5") < llmprep.estimate_cost(
            chars, "claude-opus-5"
        )

    def test_novel_is_in_a_sane_range(self):
        # ~600k chars is a typical 400-page novel.
        cost = llmprep.estimate_cost(600_000, "claude-opus-5")
        assert 1.0 < cost < 20.0


class TestSystemPrompt:
    def test_is_stable(self):
        """Nothing volatile in the prompt, or prompt caching silently breaks."""
        assert llmprep.SYSTEM_PROMPT == llmprep.SYSTEM_PROMPT
        for marker in ("{", "}", "%s"):
            assert marker not in llmprep.SYSTEM_PROMPT

    def test_forbids_summarising(self):
        assert "summarise" in llmprep.SYSTEM_PROMPT.lower()


def test_missing_sdk_raises_a_useful_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("no module named anthropic")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(llmprep.LLMPrepError, match="pip install"):
        llmprep.LLMPrepper()


class TestPlausibleShortSources:
    def test_long_source_holds_a_tight_ceiling(self):
        source = "word " * 400  # 2000 chars
        assert not llmprep._plausible(source, "x" * 5000)

    def test_short_source_still_rejects_runaway(self):
        assert not llmprep._plausible("In 1885 he paid $4.50.", "x" * 500)
