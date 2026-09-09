"""The paste-by-hand pre-pass: export, save replies, import."""

import pytest

from epub2audiobook import manualprep


class FakeChapter:
    def __init__(self, idx, text):
        self.idx = idx
        self.text = text


@pytest.fixture
def prep(tmp_path):
    return tmp_path / "prep"


class TestSplitParts:
    def test_short_chapter_is_one_part(self):
        assert manualprep.split_parts("a short chapter", 12000) == ["a short chapter"]

    def test_splits_on_paragraph_breaks_only(self):
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 100 for i in range(20))
        parts = manualprep.split_parts(text, 1000)
        assert len(parts) > 1
        assert "\n\n".join(parts) == text

    def test_loses_nothing(self):
        text = "\n\n".join(f"Para {i}." for i in range(50))
        assert "\n\n".join(manualprep.split_parts(text, 100)) == text


class TestExport:
    def test_writes_prompt_and_instructions(self, prep):
        manualprep.export(prep, [FakeChapter(0, "Some text.")])
        assert (prep / "PROMPT.txt").exists()
        assert (prep / "README.txt").exists()

    def test_names_parts_predictably(self, prep):
        long_text = "\n\n".join("word " * 200 for _ in range(10))
        parts = manualprep.export(prep, [FakeChapter(3, long_text)], size=1000)
        assert parts[0].in_path.name == "ch0003-01.in.txt"
        assert parts[1].in_path.name == "ch0003-02.in.txt"

    def test_re_export_does_not_clobber_saved_replies(self, prep):
        chapter = FakeChapter(0, "Some text here.")
        parts = manualprep.export(prep, [chapter])
        parts[0].out_path.write_text("<normalized>Some text here.</normalized>", "utf-8")

        manualprep.export(prep, [chapter])  # user runs export-text again
        assert parts[0].out_path.exists()
        assert manualprep.read_chapter(manualprep.scan(prep)[0])[0] == "Some text here."


class TestImport:
    def _one(self, prep, source, reply):
        parts = manualprep.export(prep, [FakeChapter(0, source)])
        parts[0].out_path.write_text(reply, encoding="utf-8")
        return manualprep.read_chapter(manualprep.scan(prep)[0])

    def test_strips_normalized_tags(self, prep):
        text, problem = self._one(prep, "In 1885 he paid.", "<normalized>In eighteen eighty-five he paid.</normalized>")
        assert problem is None
        assert text == "In eighteen eighty-five he paid."

    def test_accepts_a_reply_without_tags(self, prep):
        text, problem = self._one(prep, "In 1885 he paid.", "In eighteen eighty-five he paid.")
        assert problem is None and text.startswith("In eighteen")

    def test_reports_a_missing_reply(self, prep):
        manualprep.export(prep, [FakeChapter(0, "Some text.")])
        text, problem = manualprep.read_chapter(manualprep.scan(prep)[0])
        assert text is None and "waiting" in problem

    def test_refuses_a_summarised_reply(self, prep):
        text, problem = self._one(prep, "word " * 500, "He went to the shop.")
        assert text is None and "summarised" in problem

    def test_force_accepts_it_anyway(self, prep):
        parts = manualprep.export(prep, [FakeChapter(0, "word " * 500)])
        parts[0].out_path.write_text("He went to the shop.", encoding="utf-8")
        text, problem = manualprep.read_chapter(manualprep.scan(prep)[0], force=True)
        assert problem is None and text == "He went to the shop."

    def test_rejoins_multi_part_chapters(self, prep):
        source = "\n\n".join("word " * 150 for _ in range(8))
        parts = manualprep.export(prep, [FakeChapter(0, source)], size=800)
        assert len(parts) > 1
        for i, part in enumerate(parts):
            body = part.in_path.read_text(encoding="utf-8")
            part.out_path.write_text(f"<normalized>{body}</normalized>", encoding="utf-8")
        text, problem = manualprep.read_chapter(manualprep.scan(prep)[0])
        assert problem is None
        assert text.split() == source.split()

    def test_waits_when_only_some_parts_are_answered(self, prep):
        source = "\n\n".join("word " * 150 for _ in range(8))
        parts = manualprep.export(prep, [FakeChapter(0, source)], size=800)
        parts[0].out_path.write_text(parts[0].in_path.read_text("utf-8"), encoding="utf-8")
        text, problem = manualprep.read_chapter(manualprep.scan(prep)[0])
        assert text is None and "waiting on part" in problem

    def test_rejects_an_empty_reply(self, prep):
        text, problem = self._one(prep, "Some real text here.", "   ")
        assert text is None and "empty" in problem


class TestChatPrompt:
    def test_carries_the_rules_and_the_paste_protocol(self):
        p = manualprep.CHAT_PROMPT
        assert "<normalized>" in p
        assert "summarise" in p.lower()
        assert "one chunk per message" in p
