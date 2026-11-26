"""The voice check itself: the bundled model on noise and on a voice, the rule that turns its numbers into a yes or
a no, and the model against the ONNX file it was taken from. What a dictation does with the answer is in test_cloud."""
import importlib.util
from pathlib import Path

import numpy as np
import pytest

from stentor import audio
from stentor_pro import silence
from stentor_pro.silence import SpeechCheck, has_speech

from helpers import vowel

REAL = SpeechCheck.hears  # conftest swaps it for the other tests
QUIETER = 10 ** -1.5  # a microphone set 30 dB too low


@pytest.fixture
def check(monkeypatch):
    monkeypatch.setattr(SpeechCheck, "hears", REAL)
    return SpeechCheck()


def knocks(seconds=2.0) -> np.ndarray:
    """Room noise the app's own gate lets through: a hum with two knocks on top."""
    rng = np.random.default_rng(1)
    hum = rng.normal(0, 0.002, int(seconds * audio.SAMPLE_RATE)).astype(np.float32)
    hum[12000:14000] += rng.normal(0, 0.05, 2000).astype(np.float32)
    hum[24000:25000] += rng.normal(0, 0.08, 1000).astype(np.float32)
    return hum


def spoken() -> np.ndarray:
    return np.concatenate([np.zeros(4000, np.float32), vowel(), np.zeros(4000, np.float32)])


def test_noise_is_stopped_and_a_voice_is_heard_whatever_the_microphone_level(check):
    for level in (1.0, QUIETER):
        noise, voice = audio.speech_only(knocks() * level), audio.speech_only(spoken() * level)
        assert noise is not None and voice is not None  # both pass the app's own gate
        assert check.silent(noise) and not check.silent(voice)
    assert check.silent(audio.speech_only(np.tile(knocks(), 5)))  # ten seconds of it
    inside = np.concatenate([knocks(5), vowel() * 0.1, knocks(5)])  # a quiet voice in the middle of the noise
    assert not check.silent(audio.speech_only(inside))


def test_speech_is_a_stretch_longer_than_160_ms():
    def windows(*runs):  # (how many, probability) ... in blocks of seven
        flat = [p for count, p in runs for _ in range(count)]
        return [flat[i:i + 7] for i in range(0, len(flat), 7)]
    w = silence.WINDOW
    assert not has_speech(windows((40, 0.1)), 40 * w)
    assert not has_speech(windows((10, 0.1), (5, 0.9), (70, 0.1)), 85 * w)  # 160 ms exactly, then a long quiet
    assert has_speech(windows((10, 0.1), (6, 0.9), (70, 0.1)), 86 * w)
    assert has_speech(windows((30, 0.1), (1, 0.4)), 37 * w)  # still going when the clip ends
    assert not has_speech(windows((30, 0.1), (1, 0.4)), 35 * w)
    assert has_speech(windows((3, 0.9), (20, 0.3), (3, 0.9), (70, 0.1)), 96 * w)  # never quiet enough in between to end
    assert not has_speech(windows((3, 0.9), (70, 0.1), (3, 0.9), (70, 0.1)), 146 * w)  # two short ones, 2 s apart
    assert not has_speech(windows((6, 0.39)), 6 * w)


@pytest.mark.skipif(importlib.util.find_spec("faster_whisper") is None, reason="needs faster-whisper, for its ONNX model")
def test_the_numpy_model_is_the_onnx_model():
    from faster_whisper import vad
    if not (Path(vad.get_assets_path()) / "silero_vad_v6.onnx").is_file():
        pytest.skip("this faster-whisper ships another model")
    options = vad.VadOptions(threshold=silence.THRESHOLD, min_speech_duration_ms=160)
    model, reference = silence.Silero(), vad.get_vad_model()
    rng = np.random.default_rng(2)
    long = np.concatenate([knocks(9), vowel(0.4), knocks(9), vowel(0.15), knocks()])  # more than one block
    clips = [spoken(), knocks(), long, rng.normal(0, 0.1, 16000 * 3).astype(np.float32), vowel(2.0)[:512 * 40], vowel(0.5)[:700]]
    for clip in clips:
        theirs = reference(np.pad(clip, (0, silence.WINDOW - len(clip) % silence.WINDOW)))
        for block in (silence.BLOCK, 7):  # whatever the block, what one hears carries into the next
            ours = np.concatenate(list(model.probabilities(clip, block)))
            assert np.abs(ours - theirs).max() < 1e-4
        assert has_speech(model.probabilities(clip), len(clip)) == bool(vad.get_speech_timestamps(clip.copy(), options))


def test_a_long_recording_is_heard_at_its_first_voice():
    """Ten minutes with a voice at the start: the model reads one block, not all of them."""
    model, read = silence.Silero(), []
    clip = np.concatenate([spoken(), np.zeros(600 * audio.SAMPLE_RATE, np.float32)])

    def blocks():
        for block in model.probabilities(clip):
            read.append(len(block))
            yield block
    assert has_speech(blocks(), len(clip)) and read == [silence.BLOCK]
