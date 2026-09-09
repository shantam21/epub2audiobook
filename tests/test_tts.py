"""TTS wrapper behaviour that does not need the model loaded."""

import numpy as np
import pytest
import soundfile as sf

from epub2audiobook import tts
from epub2audiobook.config import SAMPLE_RATE


def tone(seconds=0.5, amplitude=0.2):
    t = np.linspace(0, seconds, int(SAMPLE_RATE * seconds), dtype=np.float32)
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


class TestSynthesizeToFile:
    def test_writes_a_readable_wav(self, tmp_path, monkeypatch):
        engine = tts.KokoroEngine()
        monkeypatch.setattr(engine, "synthesize", lambda text: tone(0.5))

        out = tmp_path / "chunk" / "00000.wav"
        duration = engine.synthesize_to_file("hello", out)

        assert out.exists()
        audio, sr = sf.read(out, dtype="float32")
        assert sr == SAMPLE_RATE
        assert duration == pytest.approx(0.5, abs=0.01)
        assert len(audio) == pytest.approx(SAMPLE_RATE * 0.5, abs=10)

    def test_leaves_no_partial_file_behind(self, tmp_path, monkeypatch):
        """The .part temp name must not survive, and must not defeat the
        WAV format detection on the way through."""
        engine = tts.KokoroEngine()
        monkeypatch.setattr(engine, "synthesize", lambda text: tone(0.2))

        out = tmp_path / "00000.wav"
        engine.synthesize_to_file("hello", out)
        assert list(tmp_path.iterdir()) == [out]

    def test_a_failed_render_leaves_no_output(self, tmp_path, monkeypatch):
        engine = tts.KokoroEngine()

        def boom(text):
            raise tts.TTSError("model exploded")

        monkeypatch.setattr(engine, "synthesize", boom)
        out = tmp_path / "00000.wav"
        with pytest.raises(tts.TTSError):
            engine.synthesize_to_file("hello", out)
        assert not out.exists()


class TestTrim:
    def test_removes_leading_and_trailing_silence(self):
        padded = np.concatenate([np.zeros(SAMPLE_RATE, np.float32), tone(0.5),
                                 np.zeros(SAMPLE_RATE, np.float32)])
        trimmed = tts._trim(padded)
        assert len(trimmed) < len(padded)
        # The cushion is kept, so the tone itself is never clipped.
        assert len(trimmed) >= len(tone(0.5))

    def test_leaves_pure_silence_alone(self):
        silent = np.zeros(1000, dtype=np.float32)
        assert len(tts._trim(silent)) == 1000


class TestAsArray:
    def test_accepts_a_tuple_result(self):
        audio = tts._as_array(("graphemes", "phonemes", tone(0.1)))
        assert audio.dtype == np.float32 and audio.ndim == 1

    def test_accepts_an_object_result(self):
        class Result:
            audio = tone(0.1)

        assert tts._as_array(Result()).ndim == 1

    def test_returns_none_for_an_unknown_shape(self):
        assert tts._as_array(object()) is None


class TestSilence:
    def test_length_matches_the_requested_duration(self):
        assert len(tts.silence(320)) == int(SAMPLE_RATE * 0.32)
