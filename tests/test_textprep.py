"""The text pipeline is where audible defects come from, so it gets the tests."""

from epub2audiobook import textprep


class TestClean:
    def test_collapses_newlines_inside_a_paragraph(self):
        # Print layout wraps lines; the synthesiser would pause at each one.
        out = textprep.clean("He walked north\nalong the river\nfor an hour.")
        assert out == "He walked north along the river for an hour."

    def test_keeps_paragraph_breaks(self):
        out = textprep.clean("First para.\n\nSecond para.")
        assert out == "First para.\n\nSecond para."

    def test_rejoins_hyphenated_line_breaks(self):
        assert "lamplighter" in textprep.clean("the lamp-\nlighter went by")

    def test_drops_page_numbers_and_roman_folios(self):
        out = textprep.clean("End of thought.\n\n231\n\nvii\n\nNext thought.")
        assert "231" not in out and "vii" not in out
        assert "Next thought." in out

    def test_keeps_real_words_that_look_like_roman_numerals(self):
        assert "civil" in textprep.clean("civil")

    def test_drops_scene_break_rules(self):
        out = textprep.clean("Before.\n\n* * *\n\nAfter.")
        assert "*" not in out
        assert "Before." in out and "After." in out

    def test_strips_bracketed_footnote_refs(self):
        assert textprep.clean("A claim[12] was made.") == "A claim was made."

    def test_normalises_quotes_and_dashes(self):
        out = textprep.clean("“Well—yes,” she said.")
        assert "“" not in out and "—" not in out
        assert '"Well - yes," she said.' == out


class TestSplitSentences:
    def test_keeps_closing_quote_with_its_sentence(self):
        parts = textprep.split_sentences('"A record of light." The FBI disagreed.')
        assert parts == ['"A record of light."', "The FBI disagreed."]

    def test_does_not_split_on_abbreviations(self):
        parts = textprep.split_sentences("He lived on Wick St. He kept a ledger.")
        assert parts == ["He lived on Wick St. He kept a ledger."]

    def test_does_not_split_on_initials(self):
        parts = textprep.split_sentences("A man named J. R. R. Halloway arrived.")
        assert len(parts) == 1

    def test_splits_on_question_and_exclamation(self):
        parts = textprep.split_sentences("Who goes there? Nobody! Silence.")
        assert parts == ["Who goes there?", "Nobody!", "Silence."]


class TestChunk:
    def test_never_exceeds_the_limit(self):
        text = " ".join(f"Sentence number {i} runs on for a while." for i in range(200))
        for piece in textprep.chunk(text, 380):
            assert len(piece) <= 380

    def test_cuts_only_on_sentence_boundaries(self):
        text = " ".join(f"Sentence number {i} runs on for a while." for i in range(60))
        for piece in textprep.chunk(text, 380):
            assert piece.rstrip().endswith((".", "!", "?", '"'))

    def test_splits_an_oversized_sentence_on_clauses(self):
        long_sentence = ", ".join(f"clause number {i} here" for i in range(60)) + "."
        pieces = textprep.chunk(long_sentence, 200)
        assert len(pieces) > 1
        assert all(len(p) <= 200 for p in pieces)

    def test_preserves_paragraph_breaks_as_newlines(self):
        pieces = textprep.chunk("First para.\n\nSecond para.", 380)
        assert pieces == ["First para.\nSecond para."]

    def test_loses_no_words(self):
        text = textprep.clean(
            "In 1885, Dr. Vane kept a ledger.\n\n"
            'He wrote, "It is a record of light." Nothing more.\n\n'
            "Wick St. ran north for 1,204 yards."
        )
        joined = " ".join(textprep.chunk(text, 120)).replace("\n", " ")
        assert joined.split() == text.replace("\n", " ").split()


class TestEstimates:
    def test_duration_scales_with_speed(self):
        assert textprep.estimate_seconds(1000, 2.0) < textprep.estimate_seconds(1000, 1.0)

    def test_format_duration(self):
        assert textprep.format_duration(45) == "45s"
        assert textprep.format_duration(125) == "2m 05s"
        assert textprep.format_duration(3725) == "1h 02m 05s"
