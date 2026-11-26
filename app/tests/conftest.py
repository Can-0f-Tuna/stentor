import os

os.environ["STENTOR_SITE"] = "http://127.0.0.1:9"  # read at import; no test may reach the real site

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from stentor import extensions


class FakeApi:
    """An OpenAI-compatible transcription server that records what it receives."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.text = "hello world"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(("GET", self.path, self.headers.get("Authorization"), b""))
                self._reply(200 if outer.status == 200 else outer.status, {"data": []})

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append(("POST", self.path, self.headers.get("Authorization"), body))
                self._reply(outer.status, {"text": outer.text} if outer.status == 200 else {"error": {"message": "nope"}})

            def _reply(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def api():
    fake = FakeApi()
    yield fake
    fake.server.shutdown()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("STENTOR_CONFIG_DIR", str(tmp_path / "cfg"))
    for name in ("GROQ_API_KEY", "OPENAI_API_KEY", "STENTOR_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_real_ducking(monkeypatch):
    """Tests must never turn down the music on the machine running them."""
    monkeypatch.setattr("stentor.ducking.backend", lambda: None)


@pytest.fixture(autouse=True)
def fresh_extension(isolated_config, monkeypatch):
    """Each test finds the extension through its real entry point, with the account in that test's folder."""
    monkeypatch.setattr("stentor.extensions._loaded", None)


@pytest.fixture
def pro():
    return extensions.loaded()[0]


@pytest.fixture(autouse=True)
def a_tone_is_a_voice(monkeypatch):
    """The tests' recorder plays a tone, which is no voice. The tests of the voice check put the real model back."""
    monkeypatch.setattr("stentor_pro.silence.SpeechCheck.hears", lambda self, audio, gain: True)


@pytest.fixture(autouse=True)
def signed_in(isolated_config, tmp_path):
    """Most tests run signed in; the account tests remove this file to sign out."""
    path = tmp_path / "cfg" / "account.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": "test-token", "email": "you@example.com", "name": "You", "premium": False, "checked": time.time()}))
    return path
