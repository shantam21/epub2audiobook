"""Resume semantics: the property that makes a multi-hour conversion survivable."""

import numpy as np
import pytest
import soundfile as sf

from epub2audiobook import assemble
from epub2audiobook.config import SAMPLE_RATE, RenderSettings
from epub2audiobook.store import DONE, JobStore, render_key


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "job.db")
    s.init_job(
        epub_path=tmp_path / "b.epub",
        epub_hash="abc123",
        title="A Book",
        author="An Author",
        settings=RenderSettings().to_dict(),
        options={"min_chars": 250},
        llm_model=None,
    )
    s.replace_chapters([(0, "One", "c0.xhtml", "Chapter one text."),
                        (1, "Two", "c1.xhtml", "Chapter two text.")])
    yield s
    s.close()


def plan(store, settings, texts_by_chapter):
    for idx, pieces in texts_by_chapter.items():
        store.sync_chunks(idx, [(p, render_key(p, settings.key())) for p in pieces])


def render(store, tmp_path, ch, k, seconds=0.5):
    path = tmp_path / f"ch{ch}_{k}.wav"
    t = np.linspace(0, seconds, int(SAMPLE_RATE * seconds), dtype=np.float32)
    sf.write(path, (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), SAMPLE_RATE,
             subtype="PCM_16")
    store.finish_chunk(ch, k, str(path), seconds)
    return path


class TestJob:
    def test_options_round_trip(self, store):
        import json
        assert json.loads(store.job()["options"])["min_chars"] == 250


class TestResume:
    def test_completed_chunks_survive_a_replan(self, store, tmp_path):
        s = RenderSettings()
        plan(store, s, {0: ["A.", "B."]})
        render(store, tmp_path, 0, 0)
        assert store.progress()["chunks_done"] == 1

        plan(store, s, {0: ["A.", "B."]})  # identical replan
        assert store.progress()["chunks_done"] == 1

    def test_changing_the_voice_invalidates_audio(self, store, tmp_path):
        plan(store, RenderSettings(), {0: ["A.", "B."]})
        render(store, tmp_path, 0, 0)

        plan(store, RenderSettings(voice="am_michael"), {0: ["A.", "B."]})
        assert store.progress()["chunks_done"] == 0

    def test_changed_text_invalidates_only_that_chunk(self, store, tmp_path):
        s = RenderSettings()
        plan(store, s, {0: ["A.", "B."]})
        render(store, tmp_path, 0, 0)
        render(store, tmp_path, 0, 1)

        plan(store, s, {0: ["A.", "B EDITED."]})
        chunks = store.chunks(0)
        assert chunks[0].status == DONE
        assert chunks[1].status == "pending"

    def test_a_missing_wav_file_is_re_queued(self, store, tmp_path):
        s = RenderSettings()
        plan(store, s, {0: ["A."]})
        path = render(store, tmp_path, 0, 0)
        path.unlink()  # simulate a wiped work directory

        plan(store, s, {0: ["A."]})
        assert store.chunks(0)[0].status == "pending"

    def test_editing_the_epub_keeps_untouched_chapters(self, store, tmp_path):
        s = RenderSettings()
        plan(store, s, {0: ["A."], 1: ["B."]})
        render(store, tmp_path, 0, 0)
        render(store, tmp_path, 1, 0)

        store.replace_chapters([(0, "One", "c0.xhtml", "Chapter one text."),
                                (1, "Two", "c1.xhtml", "Chapter two text, REWRITTEN.")])
        assert len(store.chunks(0)) == 1 and store.chunks(0)[0].status == DONE
        assert store.chunks(1) == []

    def test_removed_chapters_are_dropped(self, store):
        store.replace_chapters([(0, "One", "c0.xhtml", "Chapter one text.")])
        assert [c.idx for c in store.chapters()] == [0]

    def test_reset_failures_requeues(self, store):
        plan(store, RenderSettings(), {0: ["A."]})
        store.fail_chunk(0, 0, "boom")
        assert store.progress()["chunks_failed"] == 1
        assert store.reset_failures() == 1
        assert store.chunks(0)[0].status == "pending"


class TestSelection:
    def test_progress_counts_only_selected_chapters(self, store):
        plan(store, RenderSettings(), {0: ["A."], 1: ["B."]})
        store.set_selection({0})
        p = store.progress()
        assert p["chapters"] == 1 and p["chunks"] == 1

    def test_clearing_selection_restores_all(self, store):
        store.set_selection({0})
        store.set_selection(None)
        assert store.progress()["chapters"] == 2


class TestChapterAssembly:
    def test_duration_accounts_for_gaps(self, store, tmp_path):
        s = RenderSettings()
        plan(store, s, {0: ["A.", "B.", "C."]})
        paths = [render(store, tmp_path, 0, k) for k in range(3)]

        duration = assemble.build_chapter_wav(
            paths, tmp_path / "ch0.wav", gap_ms=s.gap_ms, tail_ms=s.chapter_gap_ms
        )
        expected = 3 * 0.5 + 2 * s.gap_ms / 1000 + s.chapter_gap_ms / 1000
        assert duration == pytest.approx(expected, abs=0.02)

    def test_refuses_an_empty_chapter(self, tmp_path):
        with pytest.raises(assemble.AssemblyError):
            assemble.build_chapter_wav([], tmp_path / "x.wav")


class TestFFMetadata:
    def test_chapters_are_contiguous_and_ordered(self):
        chapters = [
            assemble.ChapterAudio("One", None, 10.0),
            assemble.ChapterAudio("Two", None, 5.5),
        ]
        meta = assemble._ffmetadata(chapters, "T", "A", "", "")
        starts = [int(l.split("=")[1]) for l in meta.splitlines() if l.startswith("START=")]
        ends = [int(l.split("=")[1]) for l in meta.splitlines() if l.startswith("END=")]
        assert starts == [0, 10000]
        assert ends == [9999, 15499]

    def test_marks_the_file_as_an_audiobook(self):
        meta = assemble._ffmetadata([assemble.ChapterAudio("One", None, 1.0)], "T", "A", "", "")
        assert "media_type=2" in meta
        assert "genre=Audiobook" in meta

    def test_escapes_reserved_characters(self):
        chapters = [assemble.ChapterAudio("Cost = $5; see #3", None, 1.0)]
        meta = assemble._ffmetadata(chapters, "T", "A", "", "")
        assert r"title=Cost \= $5\; see \#3" in meta


class TestWorkCleanup:
    """A finished job should be the one .m4b, not a folder of scaffolding
    around it: the intermediates are roughly ten times the output's size."""

    def test_measures_a_directory(self, tmp_path):
        from epub2audiobook.cli import _dir_size_mb

        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.bin").write_bytes(b"x" * (2 * 1024 * 1024))
        (tmp_path / "b.bin").write_bytes(b"x" * (1024 * 1024))
        assert _dir_size_mb(tmp_path) == pytest.approx(3.0, abs=0.05)

    def test_a_missing_directory_is_zero(self, tmp_path):
        from epub2audiobook.cli import _dir_size_mb

        assert _dir_size_mb(tmp_path / "nope") == 0.0
