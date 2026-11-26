"""What Stentor gets from this package, through its `stentor.extensions` entry point: the account and the sign-in,
Stentor Cloud (the "plus" plan inside: it transcribes in the cloud, is bought here, and brings the clean-up, edit by
voice and the dictionary that learns) with its settings, the notices, the local Hebrew and English models, the second
language check, and the question about sharing recordings with what follows from it.

Nothing here asks for an account before you dictate: "This computer" and a key of your own work signed out. The
account is only for Stentor Cloud."""
import argparse
import sys
import threading
import time
import urllib.parse
import weakref
import webbrowser
from html import escape
from pathlib import Path

from stentor import audio, cli, config as cfgmod, paste, textproc
from stentor.config import LOCAL_MODELS
from stentor.hotkey import parse_hotkey
from stentor.settings_ui import SETTINGS_TITLE, STALE_PAGE

from . import english_model, hebrew_model, premium, selection, silence
from .account import Account
from .channel import Channel
from .cloud import Cloud
from .collect import Collector
from .premium import FIELDS, MAX_FIXES, MAX_TERMS, MAX_WORD, PremiumError, option, terms_of

HERE = Path(__file__).parent
# Below this avg_logprob a clip heard in your other language is checked in your first one: on Whisper large-v3,
# short English heard right tends to score above it and Hebrew misheard as English below it.
UNSURE_LOGPROB = -0.43


def auth_page(problem: str) -> bytes:
    """Shown when a sign-in can't finish. Same look as the expired-link page."""
    return STALE_PAGE.replace(b"This settings link has expired.", b"The sign-in didn't finish.").replace(
        STALE_PAGE[STALE_PAGE.index(b"<p>"):STALE_PAGE.index(b"</p>") + 4], f"<p>{escape(problem)}</p>".encode("utf-8"))


class Session:
    """The Stentor Cloud features of one Dictation."""

    def __init__(self, dictation, account):
        self.d = dictation
        self.client = premium.Client(dictation.cfg, account)
        self.learner = premium.Learner(dictation.cfg, self.client, self._save_learned)
        self.grab = selection.grab_selection  # app -> (selected text, put the clipboard back)
        self.clipboard = selection.clipboard_text
        self.save_settings = self._save_in_memory  # the app saves through the settings server
        self.keys_up = lambda: True  # the app waits here until the shortcut's keys are let go
        self.watch = ""  # the last pasted dictation, compared once with the clipboard to spot your corrections
        self.last_clean = None  # what the last clean-up did, for the settings page: never the text

    def cleaned(self, text: str, app: str) -> str:
        """The text as you meant to type it; on any failure the text as dictated: a dictation is never lost."""
        started = time.monotonic()
        try:
            title = paste.focused_title() if hasattr(paste, "focused_title") else ""  # still the app it goes to
            cleaned, model = self.client.clean(text, app, title)
            cleaned = cleaned.strip() or text
        except Exception as e:
            reason = str(e) if isinstance(e, PremiumError) else type(e).__name__
            print(f"stentor: pasting the text as dictated ({reason})", file=sys.stderr, flush=True)
            self.last_clean = {"ok": False, "why": reason, "at": time.time()}
            return text
        seconds = time.monotonic() - started
        changed = cleaned != text.strip()
        print(f"cleaned up by {model or 'the server'} in {seconds:.1f}s" + ("" if changed else ", nothing to change"), flush=True)
        self.last_clean = {"ok": True, "model": model, "seconds": round(seconds, 2), "changed": changed, "at": time.time()}
        return cleaned

    def process_edit(self, recording):
        """Edit by voice: the selected text, changed the way you said, pasted over the selection."""
        d = self.d
        started = time.monotonic()
        app = d.focus()
        self.keys_up()  # a copy shortcut pressed while Alt and Shift are still down would be another shortcut
        with d.paste_lock:
            selected, restore = self.grab(app)
        if not selected.strip():
            restore()
            d.fb.error("Select some text first.")
            return
        try:
            speech = audio.speech_only(recording)
            raw, source = ("", "-") if speech is None else d.transcribe(speech)
            instruction = textproc.clean(raw, d.cfg)
            if not instruction:
                restore()
                d.fb.done()
                return
            result = self.client.edit(selected, instruction, app)
            if not result.strip():
                raise PremiumError("The server sent back no text, so the selection is left as it was.")
        except Exception as e:
            restore()
            d._fail(e)
            return
        d.last_used = time.monotonic()
        print(f"edited by voice ({source}) in {d.last_used - started:.1f}s", flush=True)  # never the text
        d.fb.done()
        rule = paste.rule_for(d.cfg.app_rules, app)
        d.deliver(result, app, submit_allowed=False)
        if rule != "clipboard":  # copy-only leaves the result on the clipboard for you to paste
            restore()

    # ---------- a dictionary that learns ----------

    def check_correction(self):
        """At the next press after a dictation, looks once at the clipboard: a copied, corrected version
        of what was pasted tells which words were wrong."""
        last, self.watch = self.watch, ""
        if not last or not self.learner.enabled():
            return

        def run():
            copied = self.clipboard()
            fixed = premium.corrections(last, copied) if copied else []
            if fixed:
                self.learner.add(fixed=fixed)
        threading.Thread(target=run, daemon=True).start()

    def _save_learned(self, terms: list, fixes: dict):
        self.save_settings({"learned_terms": ", ".join(terms), "learned_fixes": fixes})

    def _save_in_memory(self, changes: dict):
        with self.d.lock:
            for name, value in changes.items():
                self.d.cfg.extra[name] = cfgmod.coerce(name, value, FIELDS[name])


class Extension:
    def __init__(self):
        self.account = Account()  # only Stentor Cloud needs it
        self.sessions = weakref.WeakKeyDictionary()
        self.feedback = None  # the running app's, once it has started
        self.notice_sent = False
        self.collector = Collector(self.account)
        self.cloud = Cloud(self.account)
        self.dictation = None  # the running app's, once it has started
        self.card_sent = False
        self.edit_offered = False  # the edit shortcut pressed without Stentor Cloud said so, once this run
        self.heard = ""   # what the last transcript said about itself, when it did
        self.sure = None
        self.hebrew = hebrew_model.HebrewModel()  # the local model Automatic picks for Hebrew
        self.english = english_model.EnglishModel()  # and for English alone, which this package runs itself
        self.voice = silence.SpeechCheck()  # stops a clip of room noise before it turns into words

    def session(self, dictation) -> Session:
        if dictation not in self.sessions:
            self.sessions[dictation] = Session(dictation, self.account)
        return self.sessions[dictation]

    # ---------- dictating ----------

    def before_recording(self, dictation) -> str:
        self.session(dictation).check_correction()
        self.heard, self.sure = "", None
        return ""  # signed in or not: an account is only for Stentor Cloud

    def second_hotkey(self, cfg, hotkey):
        text = option(cfg, "edit_hotkey")
        if not text.strip():
            return None
        try:
            edit = parse_hotkey(text)
            if str(edit) == str(hotkey):
                raise ValueError("it is the same as the dictation shortcut")
            return edit
        except ValueError as e:  # turned off for now, so the settings page can still save
            print(f"stentor: the edit-by-voice shortcut {text!r} doesn't work ({e}); it is off for now", file=sys.stderr, flush=True)
            cfg.extra["edit_hotkey"] = ""
            return None

    def second_recording(self, dictation):
        """What the edit shortcut does now: edit, with Stentor Cloud; without it (signed out or not subscribed) a plain
        dictation, as the shortcut did before edit by voice, and the first time in a run a short notice says why."""
        session = self.session(dictation)
        if not session.client.problem():
            return session.process_edit
        if not self.edit_offered:
            self.edit_offered = True
            threading.Thread(target=dictation.fb.notify, args=(premium.EDIT_OFFER,), daemon=True).start()
        return None

    def rewriter(self, dictation):
        """The clean-up, unless you type live: then the words are in the app as you say them, and stay as heard."""
        session = self.session(dictation)
        cfg = dictation.cfg
        return session.cleaned if option(cfg, "plus_clean") and not cfg.live_text and not session.client.problem() else None

    def after_dictation(self, dictation, audio, raw, cleaned, final, app):
        session = self.session(dictation)
        if session.learner.enabled():
            session.watch = final
            session.learner.add(cleaned)  # sent in the background, at most once a minute
        codes = dictation.cfg.language_codes()
        self.collector.take(dictation.cfg, audio, raw, final, app, self.heard or (codes[0] if len(codes) == 1 else ""), self.sure)
        if self.collector.should_ask() and not getattr(dictation.fb, "events", None):  # no overlay here
            self.collector.notify(dictation.fb)

    # ---------- transcribing ----------

    def transcribe(self, cfg, speech, force: str, prompt: str, live: bool):
        """Stentor Cloud for a subscriber, or the local English model when the settings name it; None leaves the clip to the
        service in the settings. A clip with no voice in it is nobody's: its text is empty. So is a live phrase:
        typing moves past one only once it ends in a pause, and one still going is sent again longer, so a voice
        that starts late in it is heard then."""
        if self.voice.silent(speech):
            print(f"no voice in this {'phrase' if live else 'clip'}, so it is not transcribed", flush=True)
            return ""
        text = self.cloud.transcribe(cfg, speech, force, prompt, live)
        if text is None and self.english.takes(cfg):
            return self.english.transcribe(cfg, speech)
        return text

    def transcribes(self, cfg) -> bool:
        return self.cloud.wanted(cfg) or self.english.takes(cfg)

    # ---------- text ----------

    def _learned(self, cfg, name: str):
        """The dictionary that learns is part of Stentor Cloud: what it learned applies only with a subscription."""
        return option(cfg, name) if option(cfg, "premium_learn") and self.account.plus() else None

    def adjust_prompt(self, prompt: str, cfg) -> str:
        learned = self._learned(cfg, "learned_terms")
        return premium.with_terms(prompt, learned) if learned else prompt

    def adjust_words(self, overrides: dict, cfg) -> dict:
        learned = self._learned(cfg, "learned_fixes")
        return premium.merged_fixes(learned, overrides) if learned else overrides

    def review_transcript(self, text: str, cfg, heard: str, sure, again) -> str:
        """A language on your list that Whisper is unsure of (Hebrew misheard as English) is transcribed again
        in your first language, and the surer text wins."""
        first = cfg.language_codes()[0]
        self.heard, self.sure = heard, sure
        if heard not in ("", first) and sure is not None and sure < UNSURE_LOGPROB:
            second, _, sure_again = again(first)
            if sure_again is not None and sure_again > sure:
                self.heard, self.sure = first, sure_again
                return second
        return text

    # ---------- the running app ----------

    def started(self, dictation, server, detector):
        session = self.session(dictation)
        session.keys_up = lambda: detector.wait_released(1.0)
        session.save_settings = server.save  # under the server's lock, on top of the settings as they are then
        self.feedback = dictation.fb
        self.cloud.notify = premium.notify = dictation.fb.notify
        self.dictation = weakref.ref(dictation)
        self.hebrew.started(dictation.cfg, server.save, dictation.fb.notify)
        self.english.started(dictation.cfg, server.save, dictation.fb.notify)

    def tick(self):
        self.account.tick()
        self.hebrew.tick()
        self.english.tick()
        self.collector.tick()
        if self.card_sent and not self.collector.should_ask():  # answered, or the switch went off
            self.feedback.send("K -")
            self.card_sent = False
        wanted = self.account.premium_notice()
        if wanted and not self.feedback.events:  # no overlay on this system
            self.feedback.notify("Stentor Cloud is on! Open the settings to choose your features.")
            self.account.premium_notice_seen()
        elif not wanted and self.notice_sent:  # clicked, or the subscription ended first
            self.feedback.send("P -")
            self.notice_sent = False

    def pill_lines(self) -> list:
        """A notice comes with the next dictation, one at a time."""
        if self.account.premium_notice():
            self.notice_sent = True
            return ["P on"]
        if self.collector.should_ask():
            self.card_sent = True
            return ["K on"]
        return []

    def notices(self) -> list:
        return [str(HERE / "notices" / "PremiumHost.qml"), str(HERE / "notices" / "ConsentHost.qml")]

    # ---------- settings and commands ----------

    def settings_fields(self) -> dict:
        return {**FIELDS, **hebrew_model.FIELDS}

    def settings_state(self, cfg) -> dict:
        return {"account": self.account.public(),
                "premium": {"terms": terms_of(option(cfg, "learned_terms"))},
                "sharing": {"on": self.collector.switched_on(), "ask": self.collector.should_ask(), "answer": self.collector.answer(),
                            "page": f"{self.account.site}/data"},
                "local_models": self.english.models(self.hebrew.models(LOCAL_MODELS, cfg), cfg),
                "clean": {"models": premium.CLEAN_MODELS, "last": self._last_clean()}}

    def _last_clean(self):
        d = self.dictation() if self.dictation else None
        return self.sessions[d].last_clean if d is not None and d in self.sessions else None

    def settings_page(self) -> tuple:
        return (HERE / "settings_part.html").read_text(encoding="utf-8"), (HERE / "settings_part.js").read_text(encoding="utf-8")

    def check_settings(self, new, changes: dict):
        for name, default in FIELDS.items():
            new.extra.setdefault(name, default)
        for gone in ("premium_style", "plus_fix_english"):  # before 0.9.3; the clean-up does both
            new.extra.pop(gone, None)
        # typed live, the text can't be cleaned up at the end, so the last of the two chosen turns the other off
        if changes.get("live_text") is True:
            new.extra["plus_clean"] = False
        elif changes.get("plus_clean") is True:
            new.live_text = False
        if option(new, "clean_model") not in premium.CLEAN_MODELS:
            raise ValueError("pick a clean-up model from the list")
        main = parse_hotkey(new.hotkey)
        if option(new, "edit_hotkey"):
            try:
                edit = parse_hotkey(option(new, "edit_hotkey"))
            except ValueError as e:
                raise ValueError(f"edit shortcut: {e}") from None
            if str(edit) == str(main):
                raise ValueError("that's the Edit by voice shortcut. Pick another one" if "hotkey" in changes
                                 else "Edit by voice needs a different shortcut from dictation")
            new.extra["edit_hotkey"] = str(edit)
        new.extra["learned_terms"] = ", ".join(terms_of(option(new, "learned_terms"))[-MAX_TERMS:])
        fixes = {k: v for k, v in option(new, "learned_fixes").items() if len(k) <= MAX_WORD and len(v) <= MAX_WORD}
        new.extra["learned_fixes"] = dict(list(fixes.items())[-MAX_FIXES:])
        self.english.release(new)  # first, so the Hebrew model's check sees Automatic where English can't serve
        self.hebrew.check(new, changes)
        self.english.check(new, changes)

    def settings_routes(self, server) -> dict:
        def sign_in() -> dict:
            return {"url": self.account.begin(server.port)}

        def sign_out() -> dict:
            self.account.sign_out()
            return server.state()

        def check_account() -> dict:
            self.account.check(max_age=0, timeout=5)
            return server.state()

        def answer(choice: str):
            def route() -> dict:
                if not self.collector.switched_on():
                    raise ValueError("This isn't available.")
                self.collector.choose(choice)
                return server.state()
            return route

        def delete_shared() -> dict:
            deleted = self.collector.delete_all()
            return dict(server.state(), deleted=deleted)

        def billing(kind: str):
            def route() -> dict:
                webbrowser.open(self.account.billing_url(kind))  # the page comes back to the site; the settings check on focus
                return dict(server.state(), opened=True)
            return route

        return {"GET /auth": self._back_from_google, "POST /api/plan/upgrade": billing("checkout"), "POST /api/plan/manage": billing("portal"), "POST /api/account/signin": sign_in,
                "POST /api/account/signout": sign_out, "POST /api/account/check": check_account,
                "POST /api/sharing/yes": answer("yes"), "POST /api/sharing/no": answer("no"), "POST /api/sharing/delete": delete_shared}

    def _back_from_google(self, handler, query: str):
        """The browser, back from Google through the site."""
        if not handler._host_ok():
            return handler._json({"error": "forbidden"}, 403)
        params = urllib.parse.parse_qs(query)
        problem = self.account.finish(params.get("code", [""])[0], params.get("state", [""])[0])
        if problem:  # no settings link here: this request carries no proof it came from you
            return handler._send(400, auth_page(problem), "text/html; charset=utf-8")
        handler.send_response(303)
        handler.send_header("Location", f"/?t={handler.app.token}")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def update_source(self):
        return Channel()

    def doctor_lines(self) -> list:
        """Nothing to fix while signed out: the account is only for Stentor Cloud."""
        if not self.account.signed_in():
            return []
        return [(True, "Stentor Cloud on" if self.account.plus() else "signed in to your Stentor account", "")]

    def extend_cli(self, commands):
        settings = commands.choices["settings"]
        settings.add_argument("--premium", action="store_true", help="go straight to the Stentor Cloud features")
        settings.set_defaults(fn=self._cmd_settings)
        consent = commands.add_parser("consent", help=argparse.SUPPRESS)  # what the card's buttons run
        consent.add_argument("what", choices=["yes", "no", "info", "open"])
        consent.add_argument("--wait", type=float, default=0)
        consent.set_defaults(fn=self._cmd_consent)

    def _cmd_consent(self, args) -> int:
        if args.what == "info":
            webbrowser.open(f"{self.account.site}/data")
        elif args.what == "open":
            return cli.cmd_settings(args, "#sharingCard", lambda: selection.raise_window(SETTINGS_TITLE))
        elif self.collector.switched_on():
            self.collector.choose(args.what)
        return 0

    def _cmd_settings(self, args) -> int:
        if not args.premium:
            return cli.cmd_settings(args)
        self.account.premium_notice_seen()  # the click on the "Stentor Cloud is on" notice
        return cli.cmd_settings(args, "#premGroup", lambda: selection.raise_window(SETTINGS_TITLE))
