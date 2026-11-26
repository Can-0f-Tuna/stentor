"""Automatic picks the fast English model on the local engine when English is the only language, this package
transcribes with it, and nothing else is ever asked to run it."""
import threading
import types

import numpy as np
import pytest

from stentor import config
from stentor.app import Dictation, transcribe
from stentor.config import Config
from stentor.providers import TranscribeError
from stentor.settings_ui import SettingsServer
from stentor_pro import english_model, hebrew_model
from stentor_pro.english_model import REPO

from helpers import FakeFeedback

GB = 2**30


@pytest.fixture
def machine(monkeypatch, pro):
    """16 GB, the library installed and both models on disk; each test changes what it needs. The model itself is a
    stand-in that says what it was given."""
    state = types.SimpleNamespace(memory=16 * GB, installed=True, on_disk=True, fetched=[], gate=None, heard=[], broken=False)

    def download():
        if state.gate:
            state.gate.wait(5)
        state.fetched.append(REPO)
        state.on_disk = True

    class Model:
        def recognize(self, audio, sample_rate):
            if state.broken:
                raise RuntimeError("no such file")
            state.heard.append(len(audio) / sample_rate)
            return " from parakeet "
    monkeypatch.setattr(hebrew_model, "memory", lambda: state.memory)
    monkeypatch.setattr(hebrew_model, "system_hebrew", lambda: False)
    monkeypatch.setattr(hebrew_model, "downloaded", lambda: True)
    monkeypatch.setattr(english_model, "installed", lambda: state.installed)
    monkeypatch.setattr(english_model, "downloaded", lambda: state.on_disk)
    monkeypatch.setattr(english_model, "download", download)
    monkeypatch.setattr(pro.english.engine, "load", lambda: Model())
    return state


@pytest.fixture
def start(pro):
    """The app as run() builds it: one Config shared by the settings server and the dictation."""
    servers = []

    def run(**settings):
        cfg = Config(**{"provider": "local", **settings})
        server = SettingsServer(cfg, port=0).start()
        servers.append(server)
        server.open = lambda: None
        fb = FakeFeedback()
        pro.started(Dictation(cfg, fb), server, types.SimpleNamespace(wait_released=lambda timeout: True))
        return cfg, server, fb
    yield run
    for server in servers:
        server.stop()


def wait_until(check):
    done = threading.Event()
    for _ in range(200):
        if check():
            return True
        done.wait(0.01)
    return False


def speech(seconds: float = 1.0):
    return np.ones(int(16000 * seconds), np.float32) * 0.1


class NoWhisper:
    """The app's own local engine, which must never be asked for this model."""

    def transcribe(self, audio):
        raise AssertionError("the app's local engine was asked")


def test_english_alone_on_the_local_engine_gets_the_english_model(machine, start, pro):
    cfg, server, _ = start(languages="en")
    assert cfg.local_model == REPO and config.load().local_model == REPO
    models = server.state()["local_models"]
    assert models[0][0] == REPO and "CC-BY-4.0" in models[0][1]  # first in the list: what Automatic shows
    assert pro.transcribes(cfg)  # so the app doesn't load Whisper for nothing
    assert transcribe(cfg, NoWhisper(), speech()) == ("from parakeet", "extension")


@pytest.mark.parametrize("settings, memory, installed", [
    ({"languages": "en,he"}, 16 * GB, True),  # another language beside it, and the model knows only English
    ({"languages": ""}, 16 * GB, True),  # none picked: any language may come
    ({"languages": "en", "prompt": "Kubernetes"}, 16 * GB, True),  # a spelling hint, which this model can't take
    ({"languages": "en", "extra": {"premium_learn": True, "learned_terms": "Vercel"}}, 16 * GB, True),  # learned words, the same
    ({"languages": "en"}, 6 * GB, True),  # not enough memory
    ({"languages": "en"}, 16 * GB, False),  # the library isn't installed
    ({"languages": "en", "provider": "openai", "api_key": "k"}, 16 * GB, True),  # the cloud does the work
])
def test_automatic_stays_as_it_was_where_the_english_model_doesnt_fit(machine, start, pro, settings, memory, installed):
    machine.memory, machine.installed = memory, installed
    cfg, server, _ = start(**settings)
    assert cfg.local_model != REPO and server.state()["local_models"][0][0] != REPO
    assert not pro.english.takes(cfg)


def test_with_a_spelling_hint_you_can_still_pick_it_and_it_stays(machine, start):
    cfg, server, _ = start(languages="en", prompt="Kubernetes")
    assert cfg.local_model == "small" and server.state()["local_models"][-1][0] == REPO  # offered, not first
    server.save({"local_model": REPO})
    server.save({"beeps": False})
    assert cfg.local_model == REPO and config.load().local_model == REPO


def test_another_language_or_the_cloud_takes_the_setting_back_to_automatic(machine, start, pro):
    cfg, server, _ = start(languages="en")
    server.save({"languages": ["en", "he"]})
    assert cfg.local_model == hebrew_model.REPO  # the Hebrew model's turn, in the same save
    server.save({"languages": ["en"]})
    assert cfg.local_model == REPO  # and back
    server.save({"provider": "openai", "api_key": "k"})
    assert cfg.local_model == "small" and not pro.transcribes(cfg)  # the fallback there is Whisper
    server.save({"provider": "local"})
    assert cfg.local_model == REPO


def test_a_model_you_chose_is_left_alone(machine, start):
    cfg, server, _ = start(languages="en", local_model="large-v3-turbo")
    assert cfg.local_model == "large-v3-turbo"
    server.save({"local_model": "small"})  # small over the English model, on purpose
    server.save({"beeps": False})
    assert cfg.local_model == "small" and config.load().local_model == "small"
    server.save({"local_model": REPO})  # back to Automatic
    server.save({"languages": ["en", "fr"]})
    assert cfg.local_model == "small"
    server.save({"languages": ["en"]})
    assert cfg.local_model == REPO


def test_the_download_runs_in_the_background_and_dictation_keeps_small_until_it_is_done(machine, start):
    machine.on_disk, machine.gate = False, threading.Event()
    cfg, server, fb = start(languages="en")
    assert cfg.local_model == "small"  # a dictation now doesn't wait for 620 MB
    assert "downloading" in server.state()["local_models"][-1][1]
    assert wait_until(lambda: fb.log) and "Downloading the English model" in fb.log[0][1]
    machine.gate.set()
    assert wait_until(lambda: cfg.local_model == REPO)
    assert machine.fetched == [REPO] and wait_until(lambda: len(fb.log) == 2) and "ready" in fb.log[1][1]


def test_a_long_dictation_goes_in_parts(machine, start):
    cfg, _, _ = start(languages="en")
    text, _ = transcribe(cfg, NoWhisper(), speech(75))
    assert len(machine.heard) >= 3 and max(machine.heard) <= 30 and abs(sum(machine.heard) - 75) < 0.1  # all of it, in parts
    assert text == " ".join(["from parakeet"] * len(machine.heard))


def test_a_model_that_doesnt_run_says_so_and_whisper_is_not_asked_for_its_name(machine, start, capsys):
    cfg, _, _ = start(languages="en")
    machine.broken = True
    with pytest.raises(TranscribeError) as error:
        transcribe(cfg, NoWhisper(), speech())
    assert "Pick another model" in str(error.value)
    assert "no such file" not in capsys.readouterr().err  # the type only


def test_the_model_leaves_memory_after_a_stretch_unused(machine, start, pro, monkeypatch):
    cfg, _, _ = start(languages="en")
    engine = pro.english.engine
    engine.model, engine.used = object(), english_model.time.monotonic()
    pro.english.tick()
    assert engine.model is not None
    engine.used -= cfg.idle_unload_seconds + 1
    pro.english.tick()
    assert engine.model is None
