"""Sharing recordings: what the app sends, and above all when it sends nothing."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

from stentor import extensions
from stentor.app import Dictation
from stentor.config import Config
from stentor_pro import collect
from stentor_pro.account import Account
from stentor_pro.collect import Collector

from helpers import FakeFeedback, FakeRecorder, Pasted, settle

SECRET = "PINEAPPLE-42 the private words"


class FakeSite:
    """The site's endpoints, recording every request."""

    def __init__(self):
        self.requests, self.uploads = [], []
        self.status = {}
        self.me = {"collection": True, "consent_version": 1, "consent": None}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.requests.append((self.path, None, self.headers.get("Authorization")))
                self.reply(outer.status.get(self.path, 200), dict(outer.me, email="you@example.com", name="You", premium=False))

            def do_PUT(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.uploads.append((self.headers.get("Authorization"), self.headers.get("Content-Type"), body))
                self.reply(outer.status.get("PUT", 200), {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                outer.requests.append((self.path, body, self.headers.get("Authorization")))
                port = self.server.server_port
                self.reply(outer.status.get(self.path, 200), {"id": "clip-1", "upload_url": f"http://127.0.0.1:{port}/put?token=x"} if self.path.endswith("start") else {})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self):
        return [path for path, _, _ in self.requests]

    def contribute(self):
        return [path for path in self.paths() if path.startswith("/api/contribute")]


@pytest.fixture
def site():
    fake = FakeSite()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def pro(pro, site):
    pro.account.site = site.url
    return pro


def said_yes(account):
    account.merge(collection=True, consent_version=1, consent={"version": 1, "choice": "yes", "sent": True})


def speech():
    return FakeRecorder(None).stop()


def dictate(pro, provider="openai", rules=None, app="Editor", live=False, history_days=7.0):
    """A real dictation through the app, with the mic and the transcriber replaced."""
    pasted = Pasted()
    cfg = Config(api_key="k", provider=provider, app_rules=rules or {}, min_hold=0, beeps=False, history_days=history_days)
    if live:
        cfg.live_text = True
    d = Dictation(cfg, FakeFeedback(), paster=pasted.paste, copier=pasted.copy, recorder_factory=FakeRecorder,
                  focus=lambda: app)
    d.transcribe = lambda clip: (SECRET, "test")
    d.on_action("start")
    d.on_action("stop")
    settle(d)
    return d, pasted


def wait_for(check, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    return check()


def drained(pro):
    worker = pro.collector.worker
    assert wait_for(lambda: pro.collector.clips.empty())
    time.sleep(0.15)  # a clip in the worker's hands finishes
    return worker


def test_yes_sends_audio_texts_language_and_version_and_never_the_app(pro, site):
    said_yes(pro.account)
    d, pasted = dictate(pro, app="Secret Project Slack")
    assert wait_for(lambda: "/api/contribute/finish" in site.paths())
    assert pasted.calls[0]["text"]
    _, start, auth = next(r for r in site.requests if r[0].endswith("start"))
    assert set(start) == {"raw", "final", "language", "version", "size"}
    assert start["raw"] == SECRET and auth == "Bearer test-token"
    assert "Secret Project" not in json.dumps(site.requests)
    token, kind, wav = site.uploads[0]
    assert token is None and kind == "audio/wav" and wav[:4] == b"RIFF" and len(wav) == start["size"]


def test_the_confidence_and_language_go_along_when_the_transcript_gave_them(pro, site):
    said_yes(pro.account)
    pro.review_transcript("text", Config(languages="he,en"), "en", -0.2, lambda l: ("", "", None))
    pro.collector.take(Config(provider="openai", api_key="k"), speech(), "a", "b", "Editor", pro.heard, pro.sure)
    assert wait_for(lambda: any(p.endswith("start") for p in site.paths()))
    body = next(r[1] for r in site.requests if r[0].endswith("start"))
    assert body["confidence"] == -0.2 and body["language"] == "en"


@pytest.mark.parametrize("name, setup, run", [
    ("the switch is off", lambda a: a.merge(collection=False), {}),
    ("the switch is on for another version", lambda a: a.merge(consent_version=2), {}),
    ("no answer yet", lambda a: a.merge(consent=None), {}),
    ("No", lambda a: a.merge(consent={"version": 1, "choice": "no", "sent": True}), {}),
    ("a Yes to an older version", lambda a: a.merge(consent={"version": 0, "choice": "yes", "sent": True}), {}),
    ("a Yes the server hasn't got", lambda a: a.merge(consent={"version": 1, "choice": "yes", "sent": False}), {}),
    ("an app set to do nothing", None, {"rules": {"Editor": "off"}}),
    ("an app set to copy only", None, {"rules": {"editor": "clipboard"}}),
    ("focus unknown with an app ruled out", None, {"rules": {"Bank": "off"}, "app": ""}),
    ("the offline provider", None, {"provider": "local"}),
    ("your own server", None, {"provider": "custom"}),
])
def test_nothing_leaves(pro, site, name, setup, run):
    said_yes(pro.account)
    if setup:
        setup(pro.account)
    dictate(pro, **run)
    drained(pro)
    assert site.contribute() == [] and site.uploads == [], name


def test_nothing_leaves_when_signed_out(pro, site, signed_in):
    said_yes(pro.account)
    signed_in.unlink()
    pro.collector.take(Config(provider="openai", api_key="k"), speech(), "a", "b", "Editor")
    drained(pro)
    assert site.contribute() == []


def test_edit_by_voice_sends_nothing(pro, site, monkeypatch):
    said_yes(pro.account)
    d, _ = dictate(pro)
    assert wait_for(lambda: "/api/contribute/finish" in site.paths())
    site.requests.clear(); site.uploads.clear()
    session = pro.session(d)
    session.grab = lambda app: ("selected words", lambda: None)
    session.client.edit = lambda selected, instruction, app: "changed words"
    session.process_edit(FakeRecorder(None).stop())
    drained(pro)
    assert site.contribute() == [] and site.uploads == []


def test_a_good_rule_on_another_app_does_not_stop_a_known_app(pro, site):
    said_yes(pro.account)
    dictate(pro, rules={"Bank": "off"})
    assert wait_for(lambda: "/api/contribute/finish" in site.paths())


def test_turning_it_off_mid_way_stops_the_rest(pro, site):
    said_yes(pro.account)
    real, done = pro.account._request, []

    def request(path, *a, **k):
        reply = real(path, *a, **k)
        if path.endswith("start"):
            pro.account.merge(consent={"version": 1, "choice": "no", "sent": True})
        done.append(path)
        return reply
    pro.account._request = request
    dictate(pro)
    assert wait_for(lambda: done)
    drained(pro)
    assert site.uploads == [] and "/api/contribute/finish" not in site.paths()


def test_a_no_drops_what_is_waiting(pro, site):
    said_yes(pro.account)
    pro.collector.clips.put_nowait({"recording": speech(), "raw": "a", "final": "b", "language": "", "confidence": None})
    pro.collector.choose("no")
    assert pro.collector.clips.empty()
    assert wait_for(lambda: site.contribute() == ["/api/contribute/choice"])
    assert site.requests[-1][1] == {"version": 1, "choice": "no"}


def test_the_queue_is_bounded(pro, site):
    said_yes(pro.account)
    pro.collector.worker = threading.Thread(target=lambda: time.sleep(0.5))
    pro.collector.worker.start()
    for _ in range(collect.MAX_QUEUED + 5):
        pro.collector.take(Config(provider="openai", api_key="k"), speech(), "a", "b", "Editor")
    assert pro.collector.clips.qsize() == collect.MAX_QUEUED
    pro.collector.forget()


@pytest.mark.parametrize("broken", ["/api/contribute/start", "/api/contribute/finish", "PUT"])
def test_a_failure_drops_the_clip_quietly_and_the_dictation_is_fine(pro, site, broken, capsys):
    said_yes(pro.account)
    site.status[broken] = 500
    d, pasted = dictate(pro)
    assert wait_for(lambda: site.contribute())
    drained(pro)
    captured = capsys.readouterr()
    assert pasted.calls and d.fb.log[-1] == "done"
    assert "contribute" not in captured.out + captured.err and "Error" not in captured.err


def test_the_site_being_down_never_delays_the_dictation(pro, monkeypatch):
    said_yes(pro.account)
    pro.account.site = "http://127.0.0.1:9"

    def slow(*a, **k):
        time.sleep(5)
    monkeypatch.setattr(collect.audio, "speech_only", slow)
    started = time.monotonic()
    pro.collector.take(Config(provider="openai", api_key="k"), speech(), "a", "b", "Editor")
    assert time.monotonic() - started < 0.2


def test_nothing_is_written_to_disk(pro, site, tmp_path):
    said_yes(pro.account)
    before = {p for p in tmp_path.rglob("*") if p.is_file()}
    dictate(pro, history_days=0)  # the app's own local history is the owner's, and off here
    assert wait_for(lambda: "/api/contribute/finish" in site.paths())
    assert {p for p in tmp_path.rglob("*") if p.is_file()} == before
    assert SECRET not in "".join(p.read_text(errors="ignore") for p in before)


def test_the_answer_goes_with_the_account(pro, site, signed_in):
    said_yes(pro.account)
    pro.account.sign_out()
    assert not signed_in.exists() and not pro.collector.sharing()


def test_a_new_version_asks_again(pro, monkeypatch):
    said_yes(pro.account)
    assert not pro.collector.should_ask()
    monkeypatch.setattr(collect, "CONSENT_VERSION", 2)
    pro.account.merge(consent_version=2)
    assert pro.collector.should_ask() and not pro.collector.sharing()


def test_the_check_brings_the_switch_and_the_answer(pro, site):
    pro.account.merge(checked=0)
    pro.account.check()
    assert pro.collector.should_ask()
    site.me["consent"] = "yes"
    pro.account.check(max_age=0)
    assert pro.collector.sharing()
    site.me.update(collection=False)
    site.me.pop("consent"); site.me.pop("consent_version")
    pro.account.check(max_age=0)
    assert not pro.collector.sharing() and not pro.collector.should_ask()


def test_an_old_server_means_off(pro, site):
    site.me = {}
    pro.account.check(max_age=0)
    assert not pro.collector.switched_on()


def test_an_answer_on_its_way_is_not_overwritten_by_the_check(pro, site):
    pro.account.merge(collection=True, consent_version=1, consent={"version": 1, "choice": "no", "sent": False})
    site.me["consent"] = "yes"
    pro.account.check(max_age=0)
    assert pro.collector.answer() == "no"


def test_a_click_during_the_check_is_kept(pro, site):
    real = pro.account._request

    def clicked_meanwhile(*a, **k):
        reply = real(*a, **k)
        pro.account.merge(consent={"version": 1, "choice": "yes", "sent": False})
        return reply
    pro.account._request = clicked_meanwhile
    pro.account.check(max_age=0)
    assert pro.collector.answer() == "yes"


def test_an_unsent_answer_is_retried_by_tick(pro, site):
    pro.account.merge(collection=True, consent_version=1)
    site.status["/api/contribute/choice"] = 500
    pro.collector.choose("yes")
    assert pro.collector.answer() == "yes" and not pro.collector.sharing()
    site.status.clear()
    pro.collector.tick()
    assert wait_for(pro.collector.sharing)


def test_the_card_comes_with_each_dictation_until_answered_and_never_with_the_switch_off(pro):
    pro.feedback = FakeFeedback()
    pro.feedback.send = pro.feedback.log.append
    assert pro.pill_lines() == []
    pro.account.merge(collection=True, consent_version=1)
    assert pro.pill_lines() == ["K on"] and pro.pill_lines() == ["K on"]
    pro.account.merge(consent={"version": 1, "choice": "no", "sent": True})
    pro.tick()
    assert "K -" in pro.feedback.log and pro.pill_lines() == []


def test_the_stentor_cloud_notice_goes_first(pro):
    pro.account.merge(collection=True, consent_version=1, plan="plus", premium_notice=True)
    assert pro.pill_lines() == ["P on"]


def test_without_an_overlay_a_notification_says_so_once_a_day(pro, site):
    pro.account.merge(collection=True, consent_version=1)
    d, _ = dictate(pro)
    dictate(pro)
    notes = [e for e in d.fb.log if isinstance(e, tuple) and e[0] == "notify"]
    assert notes == [("notify", collect.CARD_TEXT)]


def test_service_never_depends_on_the_answer(pro, site):
    for answer in (None, "no", "yes"):
        pro.account.merge(collection=True, consent_version=1, consent={"version": 1, "choice": answer, "sent": True} if answer else None)
        _, pasted = dictate(pro)
        assert pasted.calls


def test_delete_stops_sharing_first_and_reports_failure(pro, site):
    said_yes(pro.account)
    site.status["/api/contribute/delete"] = 503
    with pytest.raises(ValueError, match="nothing was deleted"):
        pro.collector.delete_all()
    assert pro.collector.answer() == "no" and not pro.account.data()["consent"]["sent"]
    site.status.clear()
    assert pro.collector.delete_all() == {}
