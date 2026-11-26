"""Stentor Cloud (the "plus" plan): the cloud transcribes, the service in the settings takes over on any failure, and the plan shows and is bought
from the settings page. The site is a fake that answers what each test sets."""
import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from stentor import audio, paste
from stentor.app import Dictation
from stentor.config import Config
from stentor.settings_ui import SettingsServer
from stentor_pro import cloud as cloudmod, premium, silence
from stentor_pro.account import Account
from stentor_pro.cloud import pieces, prompt_header
from stentor_pro.premium import Client

from helpers import FakeFeedback, FakeRecorder, Pasted, settle

PLUS_ME = {"email": "you@example.com", "name": "You", "premium": True, "plan": "plus",
           "cloud": {"used": 1200, "limit": 18000, "resets": "2026-12-01"},
           "billing": {"status": "active", "renews_at": "2026-12-03T10:00:00Z", "cancel_at_period_end": False}}


class FakeSite:
    """/api/me, /api/cloud/transcribe, /api/billing/* and /api/premium/*, recording every request."""

    def __init__(self):
        self.requests = []
        self.me = dict(PLUS_ME)
        self.cloud = [(200, {"text": "from the cloud", "seconds": 1.0, "used": 1201, "limit": 18000, "resets": "2026-12-01"})]
        self.billing = {"checkout": (200, {"url": "https://pay.example/checkout/abc"}), "portal": (200, {"url": "https://pay.example/portal/xyz"})}
        self.headers = {}
        # a reply, or a function of the request body; the clean-up writes this one English word in English letters
        self.premium = {"clean": lambda body: (200, {"text": json.loads(body)["text"].replace("לגיטהאב", "ל-GitHub"), "cleaned": True,
                                                    "model": json.loads(body)["model"]})}
        self.delay = 0.0  # before a Premium answer
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
                self._reply(200, outer.me) if self.path == "/api/me" else self._reply(404, {})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                path, _, query = self.path.partition("?")
                outer.requests.append({"method": "POST", "path": path, "query": urllib.parse.parse_qs(query),
                                       "auth": self.headers.get("Authorization"), "type": self.headers.get("Content-Type"),
                                       "prompt": self.headers.get("X-Prompt"), "groq": self.headers.get("X-Groq-Key"),
                                       "live": self.headers.get("X-Live"), "body": body})
                if path == "/api/cloud/transcribe":
                    status, payload = outer.cloud.pop(0) if len(outer.cloud) > 1 else outer.cloud[0]
                    return self._reply(status, payload)
                if path.startswith("/api/billing/"):
                    return self._reply(*outer.billing[path.rsplit("/", 1)[-1]])
                if path.startswith("/api/premium/"):
                    time.sleep(outer.delay)
                    reply = outer.premium.get(path.rsplit("/", 1)[-1], (200, {"text": "styled"}))
                    return self._reply(*(reply(body) if callable(reply) else reply))
                self._reply(404, {})

            def _reply(self, status, payload):
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    for k, v in outer.headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def clips(self):
        return [r for r in self.requests if r["path"] == "/api/cloud/transcribe"]

    def cleans(self):
        return [json.loads(r["body"]) for r in self.requests if r["path"] == "/api/premium/clean"]


@pytest.fixture
def site():
    fake = FakeSite()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def plus(signed_in, site):
    """A Plus account, as the server reported it at the last check."""
    Account(site=site.url).check(max_age=0)
    assert Account().plus()
    return signed_in


@pytest.fixture
def fb():
    return FakeFeedback()


@pytest.fixture
def pro(pro, site, fb):
    pro.account = pro.cloud.account = pro.collector.account = Account(site=site.url)
    pro.cloud.notify = fb.notify
    return pro


def wait_for(cond, seconds=3.0):
    deadline = time.monotonic() + seconds
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)
    return cond()


def dictate(cfg, fb):
    out = Pasted()
    d = Dictation(cfg, fb, recorder_factory=FakeRecorder, paster=out.paste, copier=out.copy, focus=lambda: "slack")
    d.on_action("start")
    time.sleep(0.3)
    d.on_action("stop")
    settle(d)
    return [c["text"] for c in out.calls]


def own(api, **kw):
    """The user's own service: the fake OpenAI-compatible server."""
    return Config(**{"provider": "custom", "base_url": api.url, "api_key": "k", "min_hold": 0.1, "beeps": False, **kw})


def notices(fb):
    return [m for kind, m in (e for e in fb.log if isinstance(e, tuple)) if kind == "notify"]


# ---------- routing ----------

def test_plus_dictates_through_the_cloud(api, site, pro, plus, fb):
    assert dictate(own(api, languages="he", prompt="Kubernetes"), fb) == ["from the cloud"]
    assert api.requests == []  # the user's own service wasn't asked
    clip = site.clips()[0]
    assert clip["auth"] == "Bearer test-token" and clip["type"] == "audio/wav" and clip["query"] == {"language": ["he"]}
    assert clip["body"][:4] == b"RIFF" and clip["groq"] is None
    prompt = base64.urlsafe_b64decode(clip["prompt"] + "=" * (-len(clip["prompt"]) % 4)).decode("utf-8")
    assert "Kubernetes" in prompt
    assert Account().public()["cloud"]["used"] == 1201  # this month's numbers, fresh from the answer


def test_free_or_turned_off_uses_your_own_service(api, site, pro, signed_in, fb):
    assert dictate(own(api), fb) == ["hello world"]  # free: never asks the cloud
    Account(site=site.url).check(max_age=0)
    assert dictate(own(api, extra={"plus_cloud": False}), fb) == ["hello world"]
    assert site.clips() == [] and len(api.requests) == 2


# ---------- a clip with no voice in it ----------

def test_a_clip_with_no_voice_is_not_sent_anywhere(api, site, pro, plus, fb):
    heard = []
    pro.voice.hears = lambda clip, gain: heard.append(float(np.abs(clip).max()) * gain) and False
    assert dictate(own(api, languages="he,en"), fb) == []
    assert site.clips() == [] and api.requests == [] and fb.log[-1] == "done"
    assert heard == [pytest.approx(silence.LEVEL)]  # every clip at one level, whatever the microphone's
    pro.voice.hears = lambda clip, gain: True
    assert dictate(own(api, languages="he,en"), fb) == ["from the cloud"]


def test_without_plus_the_check_guards_your_own_service_too(api, site, pro, fb):
    pro.voice.hears = lambda clip, gain: False
    assert dictate(own(api), fb) == [] and api.requests == []


def test_a_clip_goes_on_when_the_check_fails(api, site, pro, plus, fb, capsys):
    def broken(clip, gain):
        raise RuntimeError("secret words")
    pro.voice.hears = broken
    assert dictate(own(api), fb) == ["from the cloud"]
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "secret words" not in err
    pro.voice.hears = lambda clip, gain: False  # nothing to bring to a level: left to the transcriber
    assert not pro.voice.silent(np.zeros(0, np.float32)) and not pro.voice.silent(np.zeros(16000, np.float32))


def test_a_live_phrase_with_no_voice_is_not_sent_either(pro, plus, site):
    """Typing live, room noise went to the transcriber phrase by phrase and came back as words."""
    pro.voice.hears = lambda clip, gain: False
    speech = np.ones(16000, np.float32) * 0.1
    cfg = own(type("Api", (), {"url": "http://127.0.0.1:9"}), languages="en")
    assert pro.transcribe(cfg, speech, "en", "", True) == "" and site.clips() == []
    pro.voice.hears = lambda clip, gain: True
    assert pro.transcribe(cfg, speech, "en", "", True) == "from the cloud"


# ---------- the language check ----------

def test_several_languages_get_the_language_check_in_the_cloud_too(api, site, pro, plus, fb):
    """Left to auto-detect, the cloud wrote short Hebrew in Russian letters: it is asked again in the first language."""
    reply = {"seconds": 1.0, "used": 1, "limit": 18000, "resets": "2026-12-01"}
    site.cloud = [(200, {**reply, "text": "Бдика", "language": "russian", "logprob": -0.6}),
                  (200, {**reply, "text": "בדיקה", "language": "hebrew", "logprob": -0.2})]
    assert dictate(own(api, languages="he,en"), fb) == ["בדיקה"]
    assert [c["query"] for c in site.clips()] == [{}, {"language": ["he"]}] and api.requests == []


def test_an_unsure_language_on_the_list_is_asked_again_and_the_surer_text_wins(api, site, pro, plus, fb):
    reply = {"seconds": 1.0, "used": 1, "limit": 18000, "resets": "2026-12-01"}
    site.cloud = [(200, {**reply, "text": "Bidika", "language": "english", "logprob": -0.9}),
                  (200, {**reply, "text": "בדיקה", "language": "hebrew", "logprob": -0.2})]
    assert dictate(own(api, languages="he,en"), fb) == ["בדיקה"]
    assert len(site.clips()) == 2


def test_a_second_request_that_fails_is_a_failed_cloud_dictation(api, site, pro, plus, fb):
    """Not the first, unsure text handed over as if it had been checked."""
    reply = {"seconds": 1.0, "used": 1, "limit": 18000, "resets": "2026-12-01"}
    site.cloud = [(200, {**reply, "text": "Bidika", "language": "english", "logprob": -0.9}), (429, {"error": "busy"})]
    assert dictate(own(api, languages="he,en"), fb) == ["hello world"]  # your own service took it
    assert len(site.clips()) == 2 and len(api.requests) == 1 and "busy" in notices(fb)[0]


def test_sure_english_and_an_older_server_cost_one_request(api, site, pro, plus, fb):
    reply = {"seconds": 1.0, "used": 1, "limit": 18000, "resets": "2026-12-01"}
    site.cloud = [(200, {**reply, "text": "Run it.", "language": "english", "logprob": -0.1})]
    assert dictate(own(api, languages="he,en"), fb) == ["Run it."]
    site.cloud = [(200, {**reply, "text": "no language told"})]  # a server from before the check
    assert dictate(own(api, languages="he,en"), fb) == ["no language told"]
    assert len(site.clips()) == 2


def test_a_live_pass_gets_the_language_check_too(site, pro, plus):
    """A phrase typed at a pause stays as typed: the last pass does not write it again."""
    reply = {"seconds": 0, "used": 1, "limit": 18000, "resets": "2026-12-01"}
    site.cloud = [(200, {**reply, "text": "Бдика", "language": "russian", "logprob": -0.6}),
                  (200, {**reply, "text": "בדיקה", "language": "hebrew", "logprob": -0.2})]
    assert pro.transcribe(Config(languages="he,en"), np.ones(16000, np.float32) * 0.1, "", "", True) == "בדיקה"
    assert [(c["query"], c["live"]) for c in site.clips()] == [({}, "1"), ({"language": ["he"]}, "1")]


def test_plus_needs_no_api_key(site, pro, plus, fb):
    assert dictate(Config(provider="openai", min_hold=0.1, beeps=False), fb) == ["from the cloud"]


@pytest.mark.parametrize("status, payload, kind", [
    (401, {"error": "signed out"}, None),
    (403, {"error": "plan"}, "plan"),
    (429, {"error": "month", "used": 18000, "limit": 18000, "resets": "2026-12-01"}, "month"),
    (429, {"error": "busy"}, "busy"),
    (502, {"error": "upstream"}, "net"),
    (503, {"error": "unavailable"}, "net"),
    (400, {"error": "empty"}, None),
])
def test_a_failing_cloud_hands_the_dictation_to_your_own_service(api, site, pro, plus, fb, status, payload, kind):
    site.cloud = [(status, payload)]
    assert dictate(own(api), fb) == ["hello world"]
    assert len(site.clips()) == 1 and len(api.requests) == 1
    if kind:
        assert wait_for(lambda: notices(fb)) and notices(fb) == [cloudmod.NOTICES[kind].format(resets="1 December")]
    else:
        time.sleep(0.1)
        assert notices(fb) == []


def test_unreachable_cloud_falls_back_and_waits_before_trying_again(api, site, pro, plus, fb):
    site.server.shutdown()
    site.server.server_close()
    assert dictate(own(api), fb) == ["hello world"]
    assert wait_for(lambda: notices(fb) == [cloudmod.NOTICES["net"]])
    assert dictate(own(api), fb) == ["hello world"]  # paused: no second timeout to wait through
    assert pro.cloud.paused_until > time.monotonic()


def test_a_used_up_month_stops_asking_until_the_server_says_otherwise(api, site, pro, plus, fb):
    site.cloud = [(429, {"error": "month", "used": 18000, "limit": 18000, "resets": "2026-12-01"})]
    dictate(own(api), fb)
    dictate(own(api), fb)
    assert len(site.clips()) == 1 and len(api.requests) == 2
    assert not pro.transcribes(own(api))
    Account(site=site.url).check(max_age=0)  # a new month: the server's numbers start again
    assert pro.transcribes(own(api))


def test_a_notice_once_until_the_cloud_works_again(site, pro, plus, fb):
    site.cloud = [(429, {"error": "busy"})]
    cfg, speech = Config(), np.ones(16000, np.float32) * 0.1
    for _ in range(2):
        assert pro.transcribe(cfg, speech, "", "", False) is None
        pro.cloud.paused_until = 0
    assert wait_for(lambda: notices(fb)) and len(notices(fb)) == 1
    site.cloud = [(200, {"text": "back"})]
    assert pro.transcribe(cfg, speech, "", "", False) == "back"
    site.cloud = [(429, {"error": "busy"})]
    pro.transcribe(cfg, speech, "", "", False)
    assert wait_for(lambda: len(notices(fb)) == 2)


def test_busy_waits_as_long_as_the_server_asks(site, pro, plus):
    site.cloud, site.headers = [(429, {"error": "busy"})], {"Retry-After": "7"}
    pro.transcribe(Config(), np.ones(16000, np.float32) * 0.1, "", "", False)
    assert 6 < pro.cloud.paused_until - time.monotonic() <= 7


def test_a_failed_live_pass_neither_pauses_nor_notifies(site, pro, plus, fb):
    site.cloud = [(503, {"error": "unavailable"})]
    assert pro.transcribe(Config(), np.ones(16000, np.float32) * 0.1, "", "", True) is None
    time.sleep(0.1)
    assert pro.cloud.paused_until == 0 and notices(fb) == []


# ---------- long clips and the prompt ----------

def test_long_clips_go_in_pieces_cut_in_a_pause():
    loud = lambda s: (np.sin(np.arange(int(16000 * s)) / 3) * 0.3).astype(np.float32)
    quiet = lambda s: np.zeros(int(16000 * s), np.float32)
    clip = np.concatenate([loud(100), quiet(0.6), loud(150)])
    parts = pieces(clip)
    assert [len(p) for p in parts[:1]] and all(len(p) <= 110 * 16000 for p in parts)
    assert np.array_equal(np.concatenate(parts), clip)
    cut = len(parts[0]) / 16000
    assert 100 <= cut <= 100.6  # in the pause, not in a word
    assert pieces(loud(30))[0] is not None and len(pieces(loud(30))) == 1


def test_a_long_clip_is_sent_in_pieces_and_joined(site, pro, plus):
    site.cloud = [(200, {"text": "one"}), (200, {"text": "two"}), (200, {"text": "three"})]
    speech = (np.sin(np.arange(16000 * 250) / 3) * 0.3).astype(np.float32)
    assert pro.transcribe(Config(), speech, "", "", False) == "one two three"
    assert all(len(c["body"]) <= 44 + 120 * 16000 * 2 for c in site.clips())


def test_the_prompt_keeps_its_end_within_600_bytes():
    long = "שלום " * 200 + "Kubernetes"
    sent = base64.urlsafe_b64decode(prompt_header(long) + "==").decode("utf-8")  # whole letters only
    assert sent.endswith("Kubernetes") and len(sent.encode("utf-8")) <= 600
    assert "=" not in prompt_header("ab")


def test_timeouts_suit_push_to_talk():
    assert cloudmod.timeout_for(3, False) < 7 and cloudmod.timeout_for(120, False) <= 20 and cloudmod.timeout_for(20, True) == 4


# ---------- the plan on the settings page ----------

def test_the_account_carries_the_plan_and_this_months_time(site, plus):
    shown = Account().public()
    assert shown["plan"] == "plus" and shown["cloud"] == {"used": 1200, "limit": 18000, "resets": "2026-12-01"}
    assert shown["billing"] == {"status": "active", "renews_at": "2026-12-03T10:00:00Z", "cancel_at_period_end": False, "pending": False}
    assert "token" not in json.dumps(shown)


def test_an_older_server_without_a_plan_means_free(site, signed_in):
    site.me = {"email": "you@example.com", "name": "You", "premium": True}
    Account(site=site.url).check(max_age=0)
    shown = Account().public()
    assert shown["plan"] == "free" and shown["cloud"] is None and not Account().plus()


@pytest.fixture
def server(pro, site):
    s = SettingsServer(Config(api_key="gsk_secret1234"), port=0).start()
    yield s
    s.stop()


def post(server, path):
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=b"{}",
                                     headers={"X-Token": server.token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


@pytest.mark.parametrize("route, kind", [("/api/plan/upgrade", "checkout"), ("/api/plan/manage", "portal")])
def test_upgrade_and_manage_open_the_page_in_the_browser(server, site, plus, monkeypatch, route, kind):
    opened = []
    monkeypatch.setattr("stentor_pro.extension.webbrowser.open", opened.append)
    status, state = post(server, route)
    assert status == 200 and state["opened"] and opened == [site.billing[kind][1]["url"]]
    assert state["account"]["plan"] == "plus" and state["config"]["plus_cloud"] is True
    billing = [r for r in site.requests if r["path"] == f"/api/billing/{kind}"]
    assert billing[0]["auth"] == "Bearer test-token"


@pytest.mark.parametrize("reply", [(200, {"url": "http://pay.example/x"}), (200, {}), (503, {"error": "unavailable"})])
def test_no_page_opens_without_a_safe_address(server, site, signed_in, monkeypatch, reply):
    opened = []
    monkeypatch.setattr("stentor_pro.extension.webbrowser.open", opened.append)
    site.billing["checkout"] = reply
    status, answer = post(server, "/api/plan/upgrade")
    assert status == 400 and "couldn't open" in answer["error"] and opened == []


def test_the_page_offers_the_plan():
    from stentor_pro.extension import HERE
    html, script = (HERE / "settings_part.html").read_text(), (HERE / "settings_part.js").read_text()
    for id in ("planGroup", "planBtn", "usageBar", "usageText", "cloudPanel", "cloudBackup", "plus_clean"):
        assert f'id="{id}"' in html
    # the cloud is one of the answers to "Where", not a switch of its own
    assert 'id="plus_cloud"' not in html and "pageHooks.places.push" in script and "plus_cloud: false" in script
    assert "Clean up what you said" in html and "Payment processing" in script
    assert "/api/plan/upgrade" in script and "/api/plan/manage" in script and "/api/account/check" in script
    # Stentor Cloud is always offered first under "Where", with the way in for each state, and nothing is locked
    assert "shown: () => true" in script and "Recommended" in script and "Sign in with Google" in script
    assert "Stentor Cloud is opening soon" in script and "Subscribe" in script and "a.sale" in script
    assert "Sign in to start" not in script and ".locked" not in html + script and "inert" not in script


# ---------- the Stentor Cloud features ----------

def test_the_features_on_stentor_cloud_need_no_key(site, plus, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env1234")  # never sent
    client = Client(Config(provider="local"), Account(site=site.url))
    assert client.problem() == "" and client.clean("שלחתי לגיטהאב") == ("שלחתי ל-GitHub", "qwen/qwen3.8-27b")
    assert [r["groq"] for r in site.requests if r["path"] == "/api/premium/clean"] == [None]


def test_the_old_premium_flag_without_stentor_cloud_turns_nothing_on(site, signed_in):
    data = json.loads(signed_in.read_text())
    signed_in.write_text(json.dumps({**data, "premium": True}))
    assert Client(Config(provider="openai", api_key="sk-mine1234"), Account(site=site.url)).problem() == premium.CLOUD_ONLY


# ---------- live passes ----------

def test_live_passes_say_so_and_a_used_up_live_month_keeps_only_final_passes(site, pro, plus, fb):
    speech = np.ones(16000, np.float32) * 0.1
    assert pro.transcribe(Config(), speech, "", "", True) == "from the cloud"
    assert [c["live"] for c in site.clips()] == ["1"]
    site.cloud = [(429, {"error": "live", "resets": "2999-01-01"})]
    assert pro.transcribe(Config(), speech, "", "", True) is None
    assert pro.transcribe(Config(), speech, "", "", True) is None  # not asked again this month
    assert len(site.clips()) == 2 and Account().data()["live_off_until"] == "2999-01-01"
    site.cloud = [(200, {"text": "final"})]
    assert pro.transcribe(Config(), speech, "", "", False) == "final"
    assert site.clips()[-1]["live"] is None
    assert wait_for(lambda: any(r["path"] == "/api/me" for r in site.requests[1:]))  # the numbers, fresh
    time.sleep(0.1)
    assert notices(fb) == [] and pro.cloud.paused_until == 0


# ---------- the clean-up ----------

HEBREW_TEXT = "שלחתי לגיטהאב"


def he(api, **kw):
    return own(api, languages="he,en", **kw)


@pytest.fixture
def title(monkeypatch):
    monkeypatch.setattr(paste, "focused_title", lambda: "#general - Acme - Slack")


def test_plus_cleans_up_the_dictation_before_pasting(api, site, pro, plus, fb, title):
    site.cloud = [(200, {"text": HEBREW_TEXT})]
    cfg = he(api, extra={"learned_terms": "Kubernetes, pnpm"})
    assert dictate(cfg, fb) == ["שלחתי ל-GitHub"]
    assert site.cleans() == [{"text": HEBREW_TEXT, "app": "slack", "title": "#general - Acme - Slack", "languages": ["he", "en"],
                              "model": "qwen/qwen3.8-27b", "terms": ["Kubernetes", "pnpm"]}]
    assert [r["groq"] for r in site.requests if r["path"] == "/api/premium/clean"] == [None]


def test_every_language_is_cleaned_up(api, site, pro, plus, fb, title):
    site.cloud = [(200, {"text": "um so check the logs"})]
    site.premium["clean"] = (200, {"text": "So check the logs.", "cleaned": True})
    assert dictate(own(api, languages="en"), fb) == ["So check the logs."]


@pytest.mark.parametrize("change", ["setting off", "free"])
def test_no_clean_up_when_off_or_without_the_plan(api, site, pro, plus, fb, title, change):
    site.cloud = [(200, {"text": HEBREW_TEXT})] * 3
    cfg = {"setting off": he(api, extra={"plus_clean": False})}.get(change, he(api))
    if change == "free":
        site.me = {"email": "you@example.com", "name": "You", "premium": False, "plan": "free"}
        Account(site=site.url).check(max_age=0)
    dictate(cfg, fb)
    assert site.cleans() == []


def test_typing_live_leaves_the_words_as_heard(api, pro, plus):
    d = Dictation(he(api), FakeFeedback(), recorder_factory=FakeRecorder, paster=lambda *a, **k: "")
    assert pro.rewriter(d) is not None
    d.cfg.live_text = True
    assert pro.rewriter(d) is None


@pytest.mark.parametrize("failure", ["slow", "error", "nonsense", "blank"])
def test_a_slow_or_failing_clean_up_pastes_the_text_as_heard(api, site, pro, plus, fb, title, monkeypatch, failure):
    site.cloud = [(200, {"text": HEBREW_TEXT})]
    if failure == "slow":
        site.delay = 8.0  # far past the bound below, which a busy computer running the tests must still meet
        monkeypatch.setattr(premium, "clean_wait", lambda text: 0.3)
    site.premium["clean"] = {"slow": site.premium["clean"], "error": (503, {"error": "unavailable"}), "nonsense": (200, {"cleaned": 1}),
                             "blank": (200, {"text": "  "})}[failure]
    started = time.monotonic()
    assert dictate(he(api), fb) == [HEBREW_TEXT]
    assert time.monotonic() - started < 6.0  # it didn't wait for the slow answer
    assert len(site.cleans()) == 1


def test_the_wait_grows_with_the_text():
    assert premium.clean_wait("x" * 100) == 4.0
    assert premium.clean_wait("x" * 1300) == 6.0
    assert premium.clean_wait("x" * 8000) == 12.5


def test_your_own_key_dictates_as_heard_without_the_clean_up(api, site, pro, signed_in, fb, monkeypatch, title):
    """The old Premium-on-your-own-key path is gone: a key of your own transcribes, the text comes as heard."""
    data = json.loads(signed_in.read_text())
    signed_in.write_text(json.dumps({**data, "premium": True}))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env1234")
    api.text = HEBREW_TEXT
    assert dictate(he(api), fb) == [HEBREW_TEXT]
    assert len(api.requests) == 1 and site.clips() == [] and site.cleans() == []


def test_your_own_service_failing_is_reported_once_not_tried_twice(api, site, pro, signed_in, fb, monkeypatch):
    api.status = 500
    assert dictate(he(api, local_fallback=False), fb) == []
    assert len(api.requests) == 1 and any(isinstance(e, tuple) and e[0] == "error" for e in fb.log)


def test_the_daily_premium_limit_shows_a_notice_once(site, pro, plus, fb, monkeypatch):
    monkeypatch.setattr(premium, "notify", fb.notify)
    monkeypatch.setattr(premium, "_day_noticed", "")
    site.premium["clean"] = (429, {"error": "day"})
    for _ in range(2):
        with pytest.raises(premium.PremiumError, match="Today's limit"):
            Client(Config(), Account(site=site.url)).clean("hello")
    assert wait_for(lambda: notices(fb)) and notices(fb) == [premium.DAY_LIMIT]
    time.sleep(0.1)
    assert len(notices(fb)) == 1


def test_a_payment_on_its_way_shows(site, signed_in):
    site.me = {**PLUS_ME, "plan": "free", "billing": {"status": "pending", "pending": True}}
    Account(site=site.url).check(max_age=0)
    assert Account().public()["billing"]["pending"] is True
