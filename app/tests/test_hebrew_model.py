"""Automatic picks ivrit.ai's Hebrew model on the local engine, when the computer can run it, and never over your choice."""
import threading
import types

import pytest

from stentor import config
from stentor.app import Dictation
from stentor.config import Config
from stentor.settings_ui import SettingsServer
from stentor_pro import hebrew_model
from stentor_pro.hebrew_model import REPO

from helpers import FakeFeedback

GB = 2**30


@pytest.fixture
def machine(monkeypatch):
    """16 GB, an English system, the model already on disk; each test changes what it needs."""
    state = types.SimpleNamespace(memory=16 * GB, hebrew=False, on_disk=True, fetched=[], gate=None)

    def download():
        if state.gate:
            state.gate.wait(5)
        state.fetched.append(REPO)
        state.on_disk = True
    monkeypatch.setattr(hebrew_model, "memory", lambda: state.memory)
    monkeypatch.setattr(hebrew_model, "system_hebrew", lambda: state.hebrew)
    monkeypatch.setattr(hebrew_model, "downloaded", lambda: state.on_disk)
    monkeypatch.setattr(hebrew_model, "download", download)
    return state


@pytest.fixture
def start(pro):
    """The app as run() builds it: one Config shared by the settings server and the dictation."""
    servers = []

    def run(**settings):
        cfg = Config(**{"provider": "local", **settings})
        server = SettingsServer(cfg, port=0).start()
        servers.append(server)
        server.open = lambda: None  # signed out, started() opens the page
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


def test_hebrew_on_the_local_engine_gets_the_hebrew_model(machine, start):
    cfg, server, _ = start(languages="he,en")
    assert cfg.local_model == REPO and config.load().local_model == REPO
    models = server.state()["local_models"]
    assert models[0][0] == REPO and "Apache-2.0" in models[0][1]  # first in the list: what Automatic shows


@pytest.mark.parametrize("settings, memory, hebrew_system", [
    ({"languages": "he"}, 6 * GB, False),  # not enough memory
    ({"languages": "en"}, 16 * GB, True),  # Hebrew isn't one of the languages picked
    ({"languages": ""}, 16 * GB, False),  # none picked, and the system isn't in Hebrew
    ({"languages": "he", "provider": "openai", "api_key": "k"}, 16 * GB, False),  # the cloud does the work
])
def test_small_stays_where_the_hebrew_model_doesnt_fit(machine, start, settings, memory, hebrew_system):
    machine.memory, machine.hebrew = memory, hebrew_system
    cfg, server, _ = start(**settings)
    assert cfg.local_model == "small" and server.state()["local_models"][0][0] == "small"


def test_no_language_picked_on_a_hebrew_system_counts_as_hebrew(machine, start):
    machine.hebrew = True
    cfg, _, _ = start(languages="")
    assert cfg.local_model == REPO


def test_a_model_you_chose_is_left_alone(machine, start):
    cfg, server, _ = start(languages="he", local_model="large-v3-turbo")
    assert cfg.local_model == "large-v3-turbo"
    server.save({"languages": ["he", "en"]})
    assert cfg.local_model == "large-v3-turbo"


def test_picking_small_over_it_sticks(machine, start):
    cfg, server, _ = start(languages="he")
    server.save({"local_model": "small"})
    assert cfg.local_model == "small"
    server.save({"languages": ["he", "en"]})
    assert cfg.local_model == "small" and config.load().local_model == "small"
    server.save({"local_model": REPO})  # back to Automatic
    server.save({"provider": "openai", "api_key": "k"})
    assert cfg.local_model == "small"  # the cloud: Automatic is small again, for the fallback
    server.save({"provider": "local"})
    assert cfg.local_model == REPO


def test_the_download_runs_in_the_background_and_dictation_keeps_small_until_it_is_done(machine, start):
    machine.on_disk, machine.gate = False, threading.Event()
    cfg, server, fb = start(languages="he")
    assert cfg.local_model == "small"  # a dictation now doesn't wait for 1.6 GB
    assert "downloading" in server.state()["local_models"][-1][1]
    assert wait_until(lambda: fb.log) and "Downloading the Hebrew model" in fb.log[0][1]
    machine.gate.set()
    assert wait_until(lambda: cfg.local_model == REPO)
    assert machine.fetched == [REPO] and wait_until(lambda: len(fb.log) == 2) and "ready" in fb.log[1][1]


def test_picking_it_before_it_is_here_waits_for_the_download(machine, start):
    machine.on_disk, machine.gate = False, threading.Event()
    cfg, server, _ = start(languages="en")
    server.save({"local_model": REPO})
    assert cfg.local_model == "small"
    machine.gate.set()
    assert wait_until(lambda: cfg.local_model == REPO)
    server.save({"languages": ["en", "fr"]})
    assert cfg.local_model == REPO  # your choice, though Automatic would be small here


def test_signed_out_it_is_picked_all_the_same(machine, start, signed_in):
    signed_in.unlink()  # an account is only for Stentor Cloud
    cfg, server, _ = start(languages="he")
    assert cfg.local_model == REPO and config.load().local_model == REPO


def test_on_disk_is_read_from_the_download_cache_alone(tmp_path, monkeypatch):
    """The check runs as the app starts: faster-whisper (and PyAV with it) stays unloaded, since macOS checks those
    libraries on their first load after an update, and the app waited a minute or more for it."""
    constants = pytest.importorskip("huggingface_hub.constants")
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path))
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", None)  # importing it would fail
    repo = tmp_path / ("models--" + REPO.replace("/", "--"))
    snapshot = repo / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("abc123")
    assert not hebrew_model.downloaded()  # cut short: no weights
    (snapshot / "model.bin").write_bytes(b"weights")
    assert hebrew_model.downloaded()
