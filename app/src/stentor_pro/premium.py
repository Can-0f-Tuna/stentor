"""The Stentor Cloud features, client side: edit by voice, the clean-up of what you said, and a dictionary that learns.

The work happens on the Stentor server, for a Stentor Cloud subscription only (the plan the server calls "plus"):
this module only sends the text involved and hands back the answer. It never logs the token or the text.
"""
import difflib
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request

from stentor import __version__

TIMEOUTS = {"edit": 15.0, "learn": 15.0}
MAX_TEXT, MAX_INSTRUCTION, MAX_APP, MAX_TITLE = 8000, 1000, 200, 300
MAX_CORRECTIONS, MAX_TERMS, MAX_FIXES, MAX_WORD = 20, 100, 100, 60
# The clean-up's answer is the whole text again, so the server gives its model 2.5 s plus 1 s per 600 characters
# (cleanTimeoutMs in the site's premium.js) and then answers with the text as sent; this waits half a second more.
clean_wait = lambda text: min(12.0, 2.5 + -(-len(text) // 600) * 1.0) + 0.5
LEARN_EVERY = 60.0  # seconds between learn calls; transcripts in between are sent together
SIMILAR = 0.7  # a copied text this close to the last dictation is a corrected version of it
CLOUD_ONLY = "This comes with Stentor Cloud. Subscribe on the settings page to use it."
EDIT_OFFER = "Edit by voice comes with Stentor Cloud, so this shortcut dictates as usual. Subscribe on the settings page to use it."
DAY_LIMIT = "Today's limit for the Stentor Cloud features is used up. They're back tomorrow."
HEBREW = re.compile(r"[\u0590-\u05ff]")
# Whisper reads only the last 224 tokens of a prompt (hosted services and faster-whisper alike). Learned words
# are added only while the prompt stays under this many UTF-8 bytes, about 3 per token, which leaves room.
PROMPT_BYTES = 600
# The settings kept in config.toml for these features, and their defaults.
FIELDS = {
    "edit_hotkey": "alt+shift+space",  # hold it over selected text and say what to change; empty = off
    "premium_learn": False,  # a dictionary that learns from what you dictate and the words you fix
    "learned_terms": "",  # comma-separated words and names it learned; added to the spelling hint
    "learned_fixes": {},  # heard -> written, learned; your word_overrides win
    "plus_cloud": True,  # Stentor Cloud: transcribe on the Stentor server instead of the service in the settings
    "plus_clean": True,  # a language model cleans up each dictation before it is pasted (fillers, self-corrections, English words)
    "clean_model": "qwen/qwen3.8-27b",  # which one; CLEAN_MODELS below, the same list as the server's
}

# The clean-up's models, as the site's premium.js CLEAN_MODELS allows them: id -> the name in the settings.
# Why the server gave a dictation back without cleaning it (the "skipped" field of /api/premium/clean; "groq" is
# the server's word for its model provider being busy).
SKIPPED = {
    "groq": "the clean-up model is busy, skipped",
    "day": "today's clean-ups are used up, skipped",
    "db": "the server could not check your plan, skipped",
    "long": "the model wrote much more than you said, kept what you said",
    "short": "the model dropped too much, kept what you said",
    "empty": "the model answered nothing, kept what you said",
}
CLEAN_MODELS = {"qwen/qwen3.8-27b": "Qwen3.8 27B", "openai/gpt-oss-120b": "GPT-OSS 120B", "openai/gpt-oss-20b": "GPT-OSS 20B"}

notify = lambda message: None  # the running app's notice, once it has started
_day_noticed = ""  # the day the daily-limit notice was shown


def _notice_day():
    global _day_noticed
    today = time.strftime("%Y-%m-%d")
    if _day_noticed != today:
        _day_noticed = today
        threading.Thread(target=notify, args=(DAY_LIMIT,), daemon=True).start()


class PremiumError(RuntimeError):
    """A short message for the user."""


def option(cfg, name: str):
    return cfg.setting(name, FIELDS[name])


def terms_of(text: str) -> list[str]:
    """'Kubernetes, Vercel,, kubernetes' -> ['Kubernetes', 'Vercel']: trimmed, no repeats in any case."""
    out, seen = [], set()
    for part in str(text or "").split(","):
        term = part.strip()[:MAX_WORD]
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out


def merged_fixes(learned: dict, own: dict) -> dict:
    """The learned fixes under your own word replacements: on the same word (in any case), yours win."""
    if not isinstance(learned, dict) or not learned:
        return own
    mine = {str(k).strip().lower() for k in own}
    return {**{k: v for k, v in learned.items() if str(k).strip().lower() not in mine}, **own}


def with_terms(prompt: str, learned: str) -> str:
    """The prompt plus the learned words it doesn't have yet, as many as fit in PROMPT_BYTES."""
    have = prompt.lower()
    added = []
    for term in terms_of(learned):
        if re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", have):
            continue
        candidate = ", ".join(added + [term]) + "."
        if len((prompt + " " + candidate).strip().encode("utf-8")) > PROMPT_BYTES:
            break
        added.append(term)
        have += " " + term.lower()
    return (prompt + " " + ", ".join(added) + ".").strip() if added else prompt


class Client:
    """Talks to {site}/api/premium/*. Raises PremiumError with a sentence for the user."""

    def __init__(self, cfg, account):
        self.cfg, self.account = cfg, account

    def problem(self) -> str:
        """'' when the features can run now: with a Stentor Cloud subscription (signed in, plan "plus"). Else why not.
        A key of your own never runs them."""
        if self.account is None or not self.account.plus():
            return CLOUD_ONLY
        return ""

    def _refresh_account(self):
        """Signed out or the subscription ended on the server: let the account notice, without waiting for it."""
        threading.Thread(target=lambda: self.account.check(max_age=0, timeout=5), daemon=True).start()

    def _post(self, kind: str, body: dict, timeout: float = 0.0) -> dict:
        problem = self.problem()
        if problem:
            raise PremiumError(problem)
        headers = {
            "Authorization": f"Bearer {self.account.data().get('token', '')}",
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": f"stentor/{__version__}",
        }
        request = urllib.request.Request(f"{self.account.site}/api/premium/{kind}", data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout or TIMEOUTS[kind]) as response:
                reply = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                error = str((json.loads(e.read() or b"{}") or {}).get("error", ""))
            except Exception:
                error = ""
            raise PremiumError(self._explain(e.code, error)) from None
        except (TimeoutError, OSError) as e:  # URLError is an OSError
            slow = isinstance(e, TimeoutError) or "timed out" in str(e)
            raise PremiumError("The Stentor server took too long to answer." if slow
                               else "Couldn't reach the Stentor server. Check the internet connection.") from None
        except ValueError:
            raise PremiumError("The Stentor server sent an answer Stentor doesn't understand.") from None
        if not isinstance(reply, dict):
            raise PremiumError("The Stentor server sent an answer Stentor doesn't understand.")
        return reply

    def _explain(self, status: int, error: str) -> str:
        if status == 401:
            self._refresh_account()
            return "You're signed out. Sign in again on the settings page."
        if status == 403:
            self._refresh_account()
            return "Stentor Cloud is off for this account."
        if status == 400:
            return "The server couldn't use this text."
        if status == 429 and error == "day":
            _notice_day()
            return DAY_LIMIT
        if status == 429:
            return "The language model is busy. Try again in a moment."
        if status == 502:
            return "The language model didn't answer. Try again."
        return "The Stentor server is busy. Try again in a moment."

    def _text(self, reply: dict) -> str:
        text = reply.get("text")
        if not isinstance(text, str):
            raise PremiumError("The Stentor server sent an answer Stentor doesn't understand.")
        return text

    def edit(self, text: str, instruction: str, app: str = "") -> str:
        if not text.strip():
            raise PremiumError("Select some text first.")
        if len(text) > MAX_TEXT:
            raise PremiumError(f"That's too much text to edit at once ({MAX_TEXT} characters at most).")
        return self._text(self._post("edit", {"text": text, "instruction": instruction[:MAX_INSTRUCTION],
                                              "app": app[:MAX_APP], "languages": self.cfg.language_codes()}))

    def clean(self, text: str, app: str = "", title: str = "") -> tuple[str, str]:
        """The text you meant to type, and the model that wrote it: fillers and false starts out, English words in
        English letters, punctuation, a form that fits the app. The window title tells the model more about where
        it goes ("Inbox - Gmail")."""
        if len(text) > MAX_TEXT:
            raise PremiumError("too long to clean up")
        model = option(self.cfg, "clean_model")
        reply = self._post("clean", {"text": text, "app": app[:MAX_APP], "title": title[:MAX_TITLE],
                                     "languages": self.cfg.language_codes(), "model": model if model in CLEAN_MODELS else "",
                                     "terms": terms_of(option(self.cfg, "learned_terms"))[-MAX_TERMS:]},
                           timeout=clean_wait(text))
        if reply.get("skipped"):  # the server gave the text back untouched: say why instead of "nothing to change"
            raise PremiumError(SKIPPED.get(reply["skipped"], "the clean-up was skipped"))
        return self._text(reply), str(reply.get("model") or "")

    def learn(self, transcript: str, corrections: list, terms: list, fixes: dict) -> tuple[list, dict]:
        reply = self._post("learn", {
            "transcript": transcript[-MAX_TEXT:],
            "corrections": [c for c in corrections if len(c["heard"]) <= MAX_WORD and len(c["written"]) <= MAX_WORD][-MAX_CORRECTIONS:],
            "terms": terms[-MAX_TERMS:],
            "fixes": dict(list(fixes.items())[-MAX_FIXES:]),
        })
        new_terms, new_fixes = reply.get("terms"), reply.get("fixes")
        if not isinstance(new_terms, list) or not isinstance(new_fixes, dict):
            raise PremiumError("The Stentor server sent an answer Stentor doesn't understand.")
        return ([str(t) for t in new_terms if isinstance(t, str)],
                {str(k): str(v) for k, v in new_fixes.items() if isinstance(k, str) and isinstance(v, str)})


# ---------- a dictionary that learns ----------

_EDGE = re.compile(r"^[\"'“”‘’«»()\[\]{}.,!?;:…]+|[\"'“”‘’«»()\[\]{}.,!?;:…]+$")


def _words(text: str) -> list[str]:
    return [w for w in (_EDGE.sub("", w) for w in text.split()) if w]


def corrections(dictated: str, edited: str) -> list[dict]:
    """The words you changed in a dictation: [{heard, written}], short ones only. Empty when `edited` isn't
    a corrected version of `dictated` (the same text, or too different to be one)."""
    if not dictated or not edited or edited == dictated:
        return []
    if difflib.SequenceMatcher(None, dictated, edited, autojunk=False).ratio() < SIMILAR:
        return []
    a, b = _words(dictated), _words(edited)
    found = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, [w.lower() for w in a], [w.lower() for w in b], autojunk=False).get_opcodes():
        if op != "replace" or i2 - i1 > 3 or j2 - j1 > 3:
            continue
        heard, written = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        if heard.lower() != written.lower() and len(heard) <= MAX_WORD and len(written) <= MAX_WORD:
            found.append({"heard": heard, "written": written})
    return found


class Learner:
    """Collects dictations and corrections, and sends them to `learn` at most once a minute, in the
    background. `save(terms, fixes)` stores what came back."""

    def __init__(self, cfg, client: Client, save, every: float = LEARN_EVERY):
        self.cfg, self.client, self.save, self.every = cfg, client, save, every
        self.lock = threading.Lock()
        self.transcripts: list[str] = []
        self.fixed: list[dict] = []
        self.last_sent = -1e9
        self.timer = None
        self.busy = False

    def enabled(self) -> bool:
        return bool(option(self.cfg, "premium_learn")) and not self.client.problem()

    def add(self, transcript: str = "", fixed: list | None = None):
        if not self.enabled():
            return
        with self.lock:
            if transcript:
                self.transcripts.append(transcript)
                while len(self.transcripts) > 1 and sum(len(t) + 1 for t in self.transcripts) > MAX_TEXT:
                    self.transcripts.pop(0)
            self.fixed = (self.fixed + list(fixed or []))[-MAX_CORRECTIONS:]
            self._schedule()

    def _schedule(self):
        if self.timer is not None or self.busy or not (self.transcripts or self.fixed):
            return
        delay = max(0.0, self.last_sent + self.every - time.monotonic())
        self.timer = threading.Timer(delay, self.flush)
        self.timer.daemon = True
        self.timer.start()

    def flush(self):
        with self.lock:
            self.timer = None
            if self.busy or not (self.transcripts or self.fixed):
                return
            transcript, fixed = "\n".join(self.transcripts)[-MAX_TEXT:], self.fixed
            self.transcripts, self.fixed, self.busy = [], [], True
            self.last_sent = time.monotonic()
        terms, fixes = terms_of(option(self.cfg, "learned_terms")), dict(option(self.cfg, "learned_fixes") or {})
        try:
            if self.enabled():
                new_terms, new_fixes = self.client.learn(transcript, fixed, terms, fixes)
                self.save(*self._apply(terms, fixes, new_terms, new_fixes))
        except PremiumError as e:
            print(f"stentor: the dictionary didn't learn this time: {e}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"stentor: the dictionary didn't learn this time ({type(e).__name__})", file=sys.stderr, flush=True)
        finally:
            with self.lock:
                self.busy = False
                self._schedule()  # what came in meanwhile

    def _apply(self, sent_terms, sent_fixes, new_terms, new_fixes) -> tuple[list, dict]:
        """Applies only what the server changed to the lists as they are now, so a word you removed in the
        settings while the request was out stays removed."""
        now_terms, now_fixes = terms_of(option(self.cfg, "learned_terms")), dict(option(self.cfg, "learned_fixes") or {})
        sent_lower, new_lower = {t.lower() for t in sent_terms}, {t.lower() for t in new_terms}
        terms = [t for t in now_terms if t.lower() in new_lower or t.lower() not in sent_lower]
        have = {t.lower() for t in terms}
        terms += [t for t in terms_of(",".join(new_terms)) if t.lower() not in sent_lower and t.lower() not in have]
        fixes = {k: v for k, v in now_fixes.items() if k in new_fixes or k not in sent_fixes}
        fixes.update({k: v for k, v in new_fixes.items() if sent_fixes.get(k) != v and k.strip() and len(k) <= MAX_WORD and len(v) <= MAX_WORD})
        return terms[-MAX_TERMS:], dict(list(fixes.items())[-MAX_FIXES:])
