import io
import json

import pytest

from stentor import updater
from stentor.config import Config
from stentor_pro import channel
from stentor_pro.channel import Channel

SITE = "https://site.example"
GOOD = {"version": "99.0.0", "core_tag": "v99.0.0", "wheel": f"{SITE}/dl/stentor_pro-99.0.0-py3-none-any.whl"}


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_the_channel_reads_its_own_file_not_the_one_hold_dictate_copies_read(monkeypatch):
    asked = []
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: asked.append(request.full_url) or Reply(json.dumps(GOOD).encode()))
    assert Channel(SITE).latest() == "v99.0.0" and asked == [f"{SITE}/stentor-release.json"]


def serve(monkeypatch, body):
    def answer(request, timeout):
        if isinstance(body, Exception):
            raise body
        return Reply(body)
    monkeypatch.setattr("urllib.request.urlopen", answer)


def installs(monkeypatch):
    ran = []
    monkeypatch.setattr(updater, "installed_by_uv", lambda: True)
    monkeypatch.setattr(updater, "find_uv", lambda: "uv")
    u = updater.Updater(Config())
    u.source = Channel(SITE)
    monkeypatch.setattr(u, "_run_install", lambda cmd: ran.append(cmd))
    return u, ran


def test_an_update_keeps_this_package_and_the_extras(monkeypatch):
    real = updater.importlib.util.find_spec
    monkeypatch.setattr(updater.importlib.util, "find_spec", lambda name: name == "faster_whisper" or real(name))
    serve(monkeypatch, json.dumps(GOOD).encode())
    u, ran = installs(monkeypatch)
    assert u.install() is True
    # on this copy's Python: left to itself uv picks the newest one, which the offline engine has no wheels for
    assert ran[0][:8] == ["uv", "tool", "install", "--force", "--python", updater.install_python(["local"]), "--with", GOOD["wheel"]]
    assert ran[0][8:10] == ["--with", channel.ENGLISH_ENGINE]  # the offline engine is there, so the English model's library stays too
    assert ran[0][10].startswith("stentor[local") and ran[0][10].endswith(" @ https://github.com/Can-0f-Tuna/stentor/archive/refs/tags/v99.0.0.tar.gz")


def test_the_offline_engine_comes_with_this_package(monkeypatch):
    real = updater.importlib.util.find_spec
    monkeypatch.setattr(updater.importlib.util, "find_spec", lambda name: None if name in ("faster_whisper", "pynput") else real(name))
    serve(monkeypatch, json.dumps(GOOD).encode())
    u, ran = installs(monkeypatch)
    assert u.install_offline() is True
    assert ran[0][6:] == ["--with", GOOD["wheel"], "--with", channel.ENGLISH_ENGINE, updater.install_spec("v99.0.0", ["local"])]


def test_without_the_offline_engine_an_update_brings_no_speech_library(monkeypatch):
    real = updater.importlib.util.find_spec
    monkeypatch.setattr(updater.importlib.util, "find_spec", lambda name: None if name in ("faster_whisper", "pynput") else real(name))
    serve(monkeypatch, json.dumps(GOOD).encode())
    u, ran = installs(monkeypatch)
    assert u.install() is True
    assert ran[0][6:] == ["--with", GOOD["wheel"], updater.install_spec("v99.0.0", [])]


def test_the_installed_copy_uses_the_channel(pro):
    assert isinstance(updater.Updater(Config()).source, Channel)


@pytest.mark.parametrize("body", [
    OSError("no answer"), b"<html>not json</html>", b"[]", b"{}",
    json.dumps({**GOOD, "version": 99}).encode(),
    json.dumps({**GOOD, "version": "99.0"}).encode(),
    json.dumps({**GOOD, "core_tag": "v98.0.0"}).encode(),
    json.dumps({**GOOD, "wheel": "https://elsewhere.example/dl/x.whl"}).encode(),
    json.dumps({**GOOD, "wheel": f"{SITE}/dl/x.whl --index-url http://bad"}).encode(),
    json.dumps({**GOOD, "wheel": f"{SITE}/dl/../x.tar.gz"}).encode(),
])
def test_a_failed_or_malformed_check_leaves_the_copy_alone(monkeypatch, body):
    serve(monkeypatch, body)
    u, ran = installs(monkeypatch)
    assert u.install() is False and ran == [] and u.latest == "" and "could not check" in u.status
