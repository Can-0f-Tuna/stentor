import json
import threading
import time
import weakref
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from stentor import paste, textproc
from stentor.app import Dictation
from stentor.config import Config
from stentor.providers import language_hints
from stentor_pro import premium
from stentor_pro.account import Account
from stentor_pro.premium import PROMPT_BYTES, Client, Learner, PremiumError, corrections, option

from helpers import FakeFeedback, FakeRecorder, Pasted, settle


class FakePremiumSite:
    """The site's premium API: records every request, answers what the test sets per route."""

    def __init__(self):
        self.requests = []
        self.replies = {"edit": (200, {"text": "edited"}), "clean": (200, {"text": "cleaned"}),
                        "learn": (200, {"terms": [], "fixes": {}})}
        self.me_status, self.delay = 200, 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization")})
                self._reply(outer.me_status, {"email": "you@example.com", "name": "You", "premium": False})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "groq": self.headers.get("X-Groq-Key"), "body": body})
                time.sleep(outer.delay)
                status, payload = outer.replies.get(self.path.rsplit("/", 1)[-1], (404, {}))
                self._reply(status, payload)

            def _reply(self, status, payload):
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:  # the client gave up waiting
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def posts(self, kind):
        return [r for r in self.requests if r["path"] == f"/api/premium/{kind}"]


@pytest.fixture
def site():
    fake = FakePremiumSite()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def cloud_account(signed_in):
    """A Stentor Cloud subscriber (the plan the server calls "plus")."""
    data = json.loads(signed_in.read_text())
    signed_in.write_text(json.dumps({**data, "plan": "plus"}))
    return signed_in


def key_cfg(**kw):
    """Your own key, which never runs these features."""
    return Config(**{"provider": "openai", "api_key": "sk-mykey1234", "api_key_service": "openai", "languages": "he,en", **kw})


def wait_for(cond, seconds=3.0):
    deadline = time.monotonic() + seconds
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)
    return cond()


# ---------- the client ----------

def test_client_sends_the_token_and_the_languages_and_never_a_key(site, cloud_account, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env")  # a key in the environment stays on this computer
    client = Client(key_cfg(), Account(site=site.url))
    assert client.edit("Hello there", "make it formal", "slack") == "edited"
    req = site.posts("edit")[0]
    assert req["auth"] == "Bearer test-token" and req["groq"] is None
    assert req["body"] == {"text": "Hello there", "instruction": "make it formal", "app": "slack", "languages": ["he", "en"]}


@pytest.mark.parametrize("account", [{"premium": False}, {"premium": True}, {"premium": True, "plan": "free"}])
def test_without_stentor_cloud_no_key_runs_the_features(site, signed_in, monkeypatch, account):
    """The old Premium flag and a key of your own (saved, or in the environment) turn nothing on any more."""
    signed_in.write_text(json.dumps({**json.loads(signed_in.read_text()), **account}))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env")
    client = Client(key_cfg(), Account(site=site.url))
    assert client.problem() == premium.CLOUD_ONLY and "Stentor Cloud" in premium.CLOUD_ONLY
    with pytest.raises(PremiumError, match="Stentor Cloud"):
        client.clean("hi")
    assert site.requests == []


def test_signed_out_never_calls_the_server(site, signed_in):
    signed_in.unlink()
    client = Client(key_cfg(), Account(site=site.url))
    with pytest.raises(PremiumError, match="Stentor Cloud"):
        client.edit("hi", "shorter")
    assert Client(key_cfg(), None).problem() == premium.CLOUD_ONLY
    assert site.requests == []


@pytest.mark.parametrize("status, error, message", [
    (400, "groq key", "couldn't use this text"),
    (400, "text", "couldn't use this text"),
    (429, "groq busy", "language model is busy"),
    (502, "groq", "language model didn't answer"),
    (503, "", "server is busy"),
])
def test_client_errors_have_short_messages(site, cloud_account, status, error, message):
    site.replies["clean"] = (status, {"error": error})
    with pytest.raises(PremiumError, match=message) as e:
        Client(key_cfg(), Account(site=site.url)).clean("hello")
    assert "groq" not in str(e.value).lower()  # the server's word for its provider never reaches the user


def test_signed_out_on_the_server_signs_out_here(site, cloud_account):
    site.replies["edit"], site.me_status = (401, {"error": "signed out"}), 401
    with pytest.raises(PremiumError, match="signed out"):
        Client(key_cfg(), Account(site=site.url)).edit("text", "shorter")
    assert wait_for(lambda: not cloud_account.exists())  # account.check(max_age=0) saw the 401


def test_a_subscription_ended_on_the_server_refreshes_the_account(site, cloud_account):
    site.replies["clean"] = (403, {"error": "plan"})
    with pytest.raises(PremiumError, match="Stentor Cloud is off"):
        Client(key_cfg(), Account(site=site.url)).clean("hello")
    assert wait_for(lambda: json.loads(cloud_account.read_text())["plan"] == "free")


def test_unreachable_and_slow_servers(site, cloud_account, monkeypatch):
    with pytest.raises(PremiumError, match="Couldn't reach"):
        Client(key_cfg(), Account(site="http://127.0.0.1:9")).clean("hello")
    monkeypatch.setattr(premium, "clean_wait", lambda text: 0.2)
    site.delay = 1.0
    with pytest.raises(PremiumError, match="too long"):
        Client(key_cfg(), Account(site=site.url)).clean("hello")


def test_edit_checks_the_selection_before_sending(site, cloud_account):
    client = Client(key_cfg(), Account(site=site.url))
    with pytest.raises(PremiumError, match="Select some text"):
        client.edit("  ", "shorter")
    with pytest.raises(PremiumError, match="too much text"):
        client.edit("x" * 8001, "shorter")
    assert site.requests == []


# ---------- clean up what you said ----------

def clean_dictation(api, site, pro, monkeypatch, **kw):
    monkeypatch.setattr(paste, "focused_title", lambda: "#builds - Slack")
    cfg = Config(provider="custom", base_url=api.url, api_key="k", min_hold=0.1, beeps=False, **kw)
    out, fb = Pasted(), FakeFeedback()
    pro.account = Account(site=site.url)
    d = Dictation(cfg, fb, recorder_factory=FakeRecorder, paster=out.paste, copier=out.copy, focus=lambda: "slack")
    return d, out, fb


def say(d, seconds=0.3):
    d.on_action("start")
    time.sleep(seconds)
    d.on_action("stop")
    settle(d)


def test_clean_up_rewrites_before_pasting(api, site, pro, cloud_account, monkeypatch):
    api.text = "um hey can you check the build"
    site.replies["clean"] = (200, {"text": "Hey, can you check the build?", "cleaned": True})
    d, out, fb = clean_dictation(api, site, pro, monkeypatch, remove_fillers=True)  # the model gets the "um": it does fillers
    say(d)
    assert [c["text"] for c in out.calls] == ["Hey, can you check the build?"]
    assert site.posts("clean")[0]["body"] == {"text": "um hey can you check the build", "app": "slack", "title": "#builds - Slack",
                                              "languages": [], "model": "qwen/qwen3.8-27b", "terms": []}
    assert fb.log == ["recording", "working", "done"]
    last = pro.session(d).last_clean
    assert {k: last[k] for k in ("ok", "changed")} == {"ok": True, "changed": True}


def test_the_settings_page_shows_the_model_and_the_last_clean_up(api, site, pro, cloud_account, monkeypatch):
    api.text = "hello world"
    site.replies["clean"] = (200, {"text": "Hello, world.", "cleaned": True, "model": "openai/gpt-oss-20b"})
    d, out, _ = clean_dictation(api, site, pro, monkeypatch, extra={"clean_model": "openai/gpt-oss-20b"})
    pro.dictation = weakref.ref(d)  # as started() sets it in the running app
    say(d)
    assert site.posts("clean")[0]["body"]["model"] == "openai/gpt-oss-20b"
    state = pro.settings_state(d.cfg)["clean"]
    assert state["models"]["qwen/qwen3.8-27b"] == "Qwen3.8 27B"
    assert state["last"]["model"] == "openai/gpt-oss-20b" and state["last"]["changed"] is True
    site.replies["clean"] = (502, {"error": "groq"})
    say(d)
    assert pro.settings_state(d.cfg)["clean"]["last"]["ok"] is False
    # the server gave the text back untouched (the free model busy): a skip, never "nothing to change"
    site.replies["clean"] = (200, {"text": "hello world", "cleaned": False, "skipped": "groq"})
    say(d)
    last = pro.settings_state(d.cfg)["clean"]["last"]
    assert last["ok"] is False and "busy" in last["why"] and out.calls[-1]["text"].strip() == "hello world"


@pytest.mark.parametrize("reply", [(502, {"error": "groq"}), (200, {"nope": 1}), (200, {"text": "  "})])
def test_clean_up_failure_pastes_the_original(api, site, pro, cloud_account, monkeypatch, capsys, reply):
    api.text = "keep this exactly"
    site.replies["clean"] = reply
    d, out, _ = clean_dictation(api, site, pro, monkeypatch)
    say(d)
    assert [c["text"] for c in out.calls] == ["keep this exactly"]
    if reply[0] != 200 or "nope" in reply[1]:
        assert "pasting the text as dictated" in capsys.readouterr().err


def test_clean_up_timeout_pastes_the_original(api, site, pro, cloud_account, monkeypatch):
    monkeypatch.setattr(premium, "clean_wait", lambda text: 0.2)
    site.delay = 1.0
    d, out, _ = clean_dictation(api, site, pro, monkeypatch)
    say(d)
    assert [c["text"] for c in out.calls] == ["hello world"]


def test_clean_up_is_skipped_without_stentor_cloud_or_when_off(api, site, pro, signed_in, monkeypatch):
    d, out, _ = clean_dictation(api, site, pro, monkeypatch)  # signed in, not subscribed
    say(d)
    assert [c["text"] for c in out.calls] == ["hello world"] and site.requests == []
    signed_in.write_text(json.dumps({**json.loads(signed_in.read_text()), "premium": True}))  # the old flag alone
    say(d)
    assert len(out.calls) == 2 and site.requests == []
    signed_in.write_text(json.dumps({**json.loads(signed_in.read_text()), "plan": "plus"}))
    d.cfg.extra["plus_clean"] = False
    say(d)
    assert len(out.calls) == 3 and site.requests == []
    signed_in.unlink()  # signed out, the setting on: dictation goes on, as heard
    d.cfg.extra["plus_clean"] = True
    say(d)
    assert out.calls[-1]["text"].strip() == "hello world" and site.requests == []


# ---------- edit by voice ----------

class Grab:
    def __init__(self, selected):
        self.selected, self.apps, self.restored = selected, [], 0

    def __call__(self, app):
        self.apps.append(app)
        return self.selected, self.restore

    def restore(self):
        self.restored += 1


class FakePremium:
    """Stands in for premium.Client in the Dictation tests."""

    def __init__(self, problem="", edited="edited", fail=None):
        self._problem, self.edited, self.fail = problem, edited, fail
        self.edits, self.learns = [], []

    def problem(self):
        return self._problem

    def edit(self, text, instruction, app=""):
        self.edits.append((text, instruction, app))
        if self.fail:
            raise PremiumError(self.fail)
        return self.edited

    def clean(self, text, app="", title=""):
        return text, "qwen/qwen3.8-27b"

    def learn(self, transcript, corrections, terms, fixes):
        self.learns.append({"transcript": transcript, "corrections": corrections, "terms": terms, "fixes": fixes})
        return terms + ["Learned"], fixes


def attach(pro, d, client, **parts):
    session = pro.session(d)
    session.client = session.learner.client = client
    for name, value in parts.items():
        setattr(session, name, value)
    return session


def edit_dictation(api, pro, grab, client, app="code", **kw):
    api.text = "make it shorter"
    cfg = Config(**{"provider": "custom", "base_url": api.url, "api_key": "k", "min_hold": 0.1, "beeps": False, **kw})
    out, fb = Pasted(), FakeFeedback()
    d = Dictation(cfg, fb, recorder_factory=FakeRecorder, paster=out.paste, copier=out.copy, focus=lambda: app)
    attach(pro, d, client, grab=grab)
    return d, out, fb


def edit_say(d, seconds=0.3):
    d.on_action("second:start")
    time.sleep(seconds)
    d.on_action("second:stop")
    settle(d)


def test_edit_by_voice_replaces_the_selection(api, pro):
    grab, client = Grab("This is a very long sentence that goes on."), FakePremium(edited="A long sentence.")
    d, out, fb = edit_dictation(api, pro, grab, client, auto_submit=True)
    edit_say(d)
    assert client.edits == [("This is a very long sentence that goes on.", "make it shorter", "code")]
    assert out.calls == [{"text": "A long sentence.", "app": "code", "shift": None, "submit": False}]  # never Enter
    assert grab.restored == 1 and grab.apps == ["code"]
    assert fb.log == ["recording", "working", "done"]
    assert d.last_text == ""  # an edit is not a dictation


def test_edit_without_a_selection_says_so(api, pro):
    grab, client = Grab(""), FakePremium()
    d, out, fb = edit_dictation(api, pro, grab, client)
    edit_say(d)
    assert ("error", "Select some text first.") in fb.log
    assert client.edits == [] and out.calls == [] and grab.restored == 1


def test_edit_server_failure_leaves_the_selection(api, pro):
    grab, client = Grab("some text"), FakePremium(fail="The language model is busy. Try again in a moment.")
    d, out, fb = edit_dictation(api, pro, grab, client)
    edit_say(d)
    assert out.calls == [] and grab.restored == 1
    assert ("error", "The language model is busy. Try again in a moment.") in fb.log


def test_edit_copy_only_app_keeps_the_result_on_the_clipboard(api, pro):
    grab, client = Grab("some text"), FakePremium(edited="new text")
    d, out, _ = edit_dictation(api, pro, grab, client, app="keepassxc", app_rules={"keepass": "clipboard"})
    edit_say(d)
    assert out.copied == ["new text"] and grab.restored == 0


def test_without_stentor_cloud_the_edit_shortcut_dictates_as_before(api, pro):
    grab, client = Grab("some text"), FakePremium(problem=premium.CLOUD_ONLY)
    started = []
    d, out, fb = edit_dictation(api, pro, grab, client)
    d.recorder_factory = lambda level: started.append(1) or FakeRecorderStub()
    d.on_action("second:start")
    assert started == [1] and d.second_work is None and not any(kind == "error" for kind, *_ in fb.log)
    assert wait_for(lambda: ("notify", premium.EDIT_OFFER) in fb.log)  # once, saying why, and offering Stentor Cloud
    d.recorder = None
    d.on_action("second:start")
    time.sleep(0.1)
    assert started == [1, 1] and fb.log.count(("notify", premium.EDIT_OFFER)) == 1


# ---------- a dictionary that learns ----------

def test_corrections_from_an_edited_copy():
    assert corrections("push it to get hub today.", "Push it to GitHub today.") == [{"heard": "get hub", "written": "GitHub"}]
    assert corrections("deploy to versal now", "deploy to Vercel now") == [{"heard": "versal", "written": "Vercel"}]
    assert corrections("hello world", "something else entirely, a new text") == []  # not a corrected version
    assert corrections("hello world", "hello world") == []
    assert corrections("hello there, world", "hello there world") == []  # punctuation only
    long = "one two three four five six seven eight nine ten"
    assert corrections(long, long.replace("two three four five", "2 3 4 5")) == []  # more than 3 words at once


def test_learner_batches_and_waits_between_calls():
    cfg = Config(extra={"premium_learn": True})
    client, saved = FakePremium(), []
    learner = Learner(cfg, client, lambda terms, fixes: saved.append((terms, fixes)), every=0.5)
    learner.add("first")
    assert wait_for(lambda: len(client.learns) == 1)
    learner.add("second")
    learner.add("", fixed=[{"heard": "get hub", "written": "GitHub"}])
    learner.add("third")
    time.sleep(0.2)
    assert len(client.learns) == 1  # waits out the minute (here half a second)
    assert wait_for(lambda: len(client.learns) == 2)
    second = client.learns[1]
    assert second["transcript"] == "second\nthird" and second["corrections"] == [{"heard": "get hub", "written": "GitHub"}]
    assert client.learns[0]["transcript"] == "first"
    assert saved[0] == (["Learned"], {})


def test_learner_is_off_without_the_setting_or_premium():
    client = FakePremium()
    Learner(Config(extra={"premium_learn": False}), client, lambda *a: None, every=0).add("text")
    Learner(Config(extra={"premium_learn": True}), FakePremium(problem=premium.CLOUD_ONLY), lambda *a: None, every=0).add("text")
    time.sleep(0.1)
    assert client.learns == []


def test_learner_keeps_what_you_removed_meanwhile():
    cfg = Config(extra={"premium_learn": True, "learned_terms": "Kubernetes, Vercel", "learned_fixes": {"get hub": "GitHub", "versal": "Vercel"}})
    learner = Learner(cfg, FakePremium(), lambda *a: None)
    sent_terms, sent_fixes = ["Kubernetes", "Vercel"], dict(cfg.extra["learned_fixes"])
    cfg.extra.update(learned_terms="Kubernetes", learned_fixes={"get hub": "GitHub"})  # you removed two in the settings
    terms, fixes = learner._apply(sent_terms, sent_fixes, ["Kubernetes", "Vercel", "pnpm"], {**sent_fixes, "nucs": "Next"})
    assert terms == ["Kubernetes", "pnpm"] and fixes == {"get hub": "GitHub", "nucs": "Next"}


def test_learning_after_dictation_and_from_a_corrected_copy(api, pro):
    api.text = "push it to get hub"
    client, clipboard = FakePremium(), []
    cfg = Config(provider="custom", base_url=api.url, api_key="k", min_hold=0.1, beeps=False, extra={"premium_learn": True})
    out, fb = Pasted(), FakeFeedback()

    def read_clipboard():
        clipboard.append(1)
        return "push it to GitHub"
    d = Dictation(cfg, fb, recorder_factory=FakeRecorder, paster=out.paste, copier=out.copy, focus=lambda: "")
    attach(pro, d, client, clipboard=read_clipboard).learner.every = 0.3
    say(d)
    assert out.calls[0]["text"] == "push it to get hub"
    assert wait_for(lambda: len(client.learns) == 1)
    assert client.learns[0]["transcript"] == "push it to get hub" and client.learns[0]["corrections"] == []
    assert wait_for(lambda: option(cfg, "learned_terms") == "Learned")  # saved
    d.on_action("second:start")  # the next press of either hotkey looks at the clipboard, once
    d.on_action("second:stop")
    d.finish()
    assert wait_for(lambda: len(client.learns) == 2, 3)
    assert client.learns[1]["corrections"] == [{"heard": "get hub", "written": "GitHub"}]
    d.on_action("start")
    d.on_action("stop")
    settle(d)
    assert clipboard == [1]


def test_learned_fixes_apply_and_your_own_win(cloud_account):
    cfg = Config(word_overrides={"Versal": "Vercel"}, extra={"premium_learn": True, "learned_fixes": {"get hub": "GitHub", "versal": "Versel"}})
    assert textproc.clean("push get hub to versal", cfg) == "push GitHub to Vercel"
    cfg.extra["premium_learn"] = False
    assert textproc.clean("push get hub to versal", cfg) == "push get hub to Vercel"


def test_what_was_learned_applies_only_with_stentor_cloud(signed_in):
    cfg = Config(provider="openai", prompt="Hint", extra={"premium_learn": True, "learned_terms": "Vercel", "learned_fixes": {"get hub": "GitHub"}})
    for account in ({"plan": "free", "premium": True}, None):  # not subscribed, then signed out
        if account is None:
            signed_in.unlink()
        else:
            signed_in.write_text(json.dumps({**json.loads(signed_in.read_text()), **account}))
        assert textproc.clean("push get hub", cfg) == "push get hub" and language_hints(cfg)[1] == "Hint"


def test_learned_terms_join_the_spelling_hint_within_the_limit(cloud_account):
    cfg = Config(provider="openai", prompt="Kubernetes", extra={"premium_learn": True, "learned_terms": "kubernetes, Vercel, pnpm, Vercel"})
    assert language_hints(cfg)[1] == "Kubernetes Vercel, pnpm."
    cfg.extra["learned_terms"] = ", ".join(f"term{i:03d}" for i in range(100))
    prompt = language_hints(cfg)[1]
    assert len(prompt.encode()) <= PROMPT_BYTES and "term000" in prompt and "term099" not in prompt
    cfg.extra["premium_learn"] = False
    assert language_hints(cfg)[1] == "Kubernetes"


class FakeRecorderStub:
    def stop(self):
        return None
