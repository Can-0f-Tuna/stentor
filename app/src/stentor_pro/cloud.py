"""Stentor Cloud (the "plus" plan inside): dictation transcribed on the Stentor server, with no API key of your own.

Stentor asks this first for every clip (its `transcribe` extension hook). Whenever the cloud can't take one
(the subscription ended, this month's time used up, the server busy or out of reach) the answer is None and the service in
your settings takes the clip, so a dictation is never lost. A short notice says so, once, until the cloud works again.
"""
import base64
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

from stentor import __version__, providers
from stentor.audio import SAMPLE_RATE, wav_bytes

from .premium import option

MAX_SECONDS = 120  # the server's limit for one request
PIECE_SECONDS = 110  # a longer clip goes in pieces of at most this, cut at the quietest moment of their last
SEARCH_SECONDS = 20  # ... this many seconds
PROMPT_BYTES = 600  # the server takes at most this much prompt; Whisper reads the end of a prompt, so that stays
LIVE_TIMEOUT = 4.0  # a live pass that takes longer than this is skipped; the last pass has the words
PAUSE = {"net": 60.0, "busy": 20.0}  # seconds the service in the settings takes over after such a failure

# What takes over is your other choice in the settings: this computer, or a key of your own.
NOTICES = {
    "plan": "Stentor Cloud is off for this account, so your other transcription choice in the settings is used.",
    "month": "This month's Stentor Cloud time is used up, so your other transcription choice is used until {resets}.",
    "busy": "Stentor Cloud is busy, so your other transcription choice took this dictation.",
    "net": "Can't reach Stentor Cloud, so your other transcription choice is used for now.",
}


class CloudError(Exception):
    def __init__(self, kind: str, resets: str = ""):
        super().__init__(kind)
        self.kind, self.resets = kind, resets
        self.wait = PAUSE.get(kind, 0.0)  # seconds the service in the settings takes over


def timeout_for(seconds: float, live: bool) -> float:
    """Push to talk: a short clip answers in a second or two, so waiting long only delays the fallback."""
    return LIVE_TIMEOUT if live else min(20.0, 6.0 + seconds / 10)


def prompt_header(prompt: str) -> str:
    """The end of the prompt within PROMPT_BYTES, as unpadded base64url of its UTF-8."""
    data = prompt.encode("utf-8")
    if len(data) > PROMPT_BYTES:
        data = data[-PROMPT_BYTES:].decode("utf-8", "ignore").lstrip().encode("utf-8")  # never half a letter
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _quietest(audio: np.ndarray) -> int:
    """The sample in `audio` where a cut harms least: the middle of its quietest 300 ms."""
    frame = SAMPLE_RATE * 30 // 1000
    n = len(audio) // frame
    if n < 10:
        return len(audio)
    energy = np.sqrt(np.mean(audio[: n * frame].astype(np.float64).reshape(n, frame) ** 2, axis=1))
    smooth = np.convolve(energy, np.ones(10) / 10, mode="valid")  # 10 frames = 300 ms
    return (int(np.argmin(smooth)) + 5) * frame


def pieces(audio: np.ndarray, most: float = PIECE_SECONDS, search: float = SEARCH_SECONDS) -> list:
    """The clip in parts the server takes, each cut in a pause near its end."""
    limit, window = int(most * SAMPLE_RATE), int(search * SAMPLE_RATE)
    out, start = [], 0
    while len(audio) - start > limit:
        lo = start + limit - window
        cut = lo + _quietest(audio[lo:start + limit])
        out.append(audio[start:cut])
        start = cut
    out.append(audio[start:])
    return out


class Cloud:
    def __init__(self, account):
        self.account = account
        self.notify = lambda message: None  # the running app's notice, once it has started
        self.paused_until = 0.0  # after a failure, the service in the settings takes over until then
        self.noticed = set()  # the notices shown since the cloud last worked
        self.lock = threading.Lock()

    def usage(self) -> dict:
        cloud = self.account.data().get("cloud")
        return cloud if isinstance(cloud, dict) else {}

    def month_used(self) -> bool:
        cloud = self.usage()
        try:
            return float(cloud.get("limit") or 0) > 0 and float(cloud.get("used") or 0) >= float(cloud["limit"])
        except (TypeError, ValueError):
            return False

    def live_off(self) -> bool:
        """This month's live passes are used up: until the day the month starts again, only final passes go."""
        until = self.account.data().get("live_off_until")
        return isinstance(until, str) and time.strftime("%Y-%m-%d") < until[:10]

    def wanted(self, cfg) -> bool:
        """A Stentor Cloud subscription, the cloud not turned off in the settings, and time left this month."""
        return self.account.plus() and bool(option(cfg, "plus_cloud")) and not self.month_used()

    def transcribe(self, cfg, speech, force: str, prompt: str, live: bool):
        """The text, or None for the service in the settings."""
        if not self.wanted(cfg) or time.monotonic() < self.paused_until or (live and self.live_off()):
            return None
        try:
            parts = pieces(speech)
            text = " ".join(t for t in (self._piece(cfg, p, force, prompt, live) for p in parts) if t)
        except CloudError as e:
            self._failed(e, live)
            return None
        with self.lock:
            self.noticed.clear()
        return text.strip()

    def _piece(self, cfg, audio, force: str, prompt: str, live: bool) -> str:
        """One piece's text. With several languages picked it gets the app's own language check, as a key of your
        own does: left to auto-detect, short Hebrew came back in Russian or Arabic letters. While typing live too:
        a phrase typed at a pause is not typed again by the last pass, so it has to be right the first time."""
        if force or len(cfg.language_codes()) < 2:
            return self._request(audio, force, prompt, live)[0]
        failed = []

        def ask(language: str) -> tuple:
            try:
                return self._request(audio, language, prompt, live)
            except CloudError as e:  # the one failure the check lets through, so a failed second request isn't hidden
                failed.append(e)
                raise providers.TranscribeError(e.kind) from e
        try:
            return providers.checked(cfg, ask)
        except providers.TranscribeError:
            if failed:
                raise failed[-1] from None
            raise

    def _request(self, audio, force: str, prompt: str, live: bool) -> tuple:
        """(text, the language the cloud heard, how sure it is or None)."""
        token = self.account.data().get("token", "")
        url = f"{self.account.site}/api/cloud/transcribe"
        if force:
            url += "?" + urllib.parse.urlencode({"language": force})
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "audio/wav", "Accept": "application/json",
                   "User-Agent": f"stentor/{__version__}"}
        if live:
            headers["X-Live"] = "1"  # not counted against the month's time, and limited on its own
        if prompt.strip():
            headers["X-Prompt"] = prompt_header(prompt.strip())
        request = urllib.request.Request(url, data=wav_bytes(audio), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout_for(len(audio) / SAMPLE_RATE, live)) as response:
                reply = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
                error = str(body.get("error", "")) if isinstance(body, dict) else ""
            except Exception:
                body, error = {}, ""
            failure = self._error(e.code, error, body if isinstance(body, dict) else {})
            if failure.kind == "busy":
                try:
                    failure.wait = min(PAUSE["net"], max(1.0, float(e.headers.get("Retry-After") or PAUSE["busy"])))
                except (TypeError, ValueError):
                    pass
            raise failure from None
        except (TimeoutError, OSError, ValueError):  # URLError is an OSError; ValueError: not JSON
            raise CloudError("net") from None
        if not isinstance(reply, dict) or not isinstance(reply.get("text"), str):
            raise CloudError("net")
        self._note_usage(reply)
        sure = reply.get("logprob")
        return (reply["text"].strip(), str(reply.get("language") or ""),
                float(sure) if isinstance(sure, (int, float)) and not isinstance(sure, bool) else None)

    def _error(self, status: int, error: str, body: dict) -> CloudError:
        if status == 401:
            self._refresh_account()  # signed out on the server: the next press asks for the sign-in
            return CloudError("signed out")
        if status == 403:
            self._refresh_account()
            return CloudError("plan")
        if status in (400, 413, 415):  # this clip only, such as one too short to hold a word
            return CloudError("clip")
        if status == 429 and error == "live":
            resets = str(body.get("resets") or self.usage().get("resets") or "")[:10]
            self.account.merge(live_off_until=resets or time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400)))
            self._refresh_account()
            return CloudError("live")
        if status == 429 and error == "month":
            self._note_usage(body, used_up=True)
            return CloudError("month", str(body.get("resets") or self.usage().get("resets") or ""))
        if status == 429:
            return CloudError("busy")
        return CloudError("net")  # 502, 503 and anything else

    def _note_usage(self, reply: dict, used_up: bool = False):
        """Keeps this month's numbers for the settings page, and the cloud off once they are used up."""
        cloud = dict(self.usage())
        for name in ("used", "limit", "resets"):
            if isinstance(reply.get(name), (int, float, str)) and not isinstance(reply.get(name), bool):
                cloud[name] = reply[name]
        if used_up and cloud.get("limit"):
            cloud["used"] = max(cloud.get("used") or 0, cloud["limit"])
        if cloud:
            self.account.merge(cloud=cloud)

    def _refresh_account(self):
        threading.Thread(target=lambda: self.account.check(max_age=0, timeout=5), daemon=True).start()

    def _failed(self, e: CloudError, live: bool):
        print(f"stentor: Stentor Cloud didn't take this clip ({e.kind}); using the service in the settings",
              file=sys.stderr, flush=True)
        if live:  # a slow live pass is no outage, and the last pass shows anything that is
            return
        if e.wait:
            self.paused_until = time.monotonic() + e.wait
        message = NOTICES.get(e.kind)
        with self.lock:
            if not message or e.kind in self.noticed:
                return
            self.noticed.add(e.kind)
        message = message.format(resets=pretty_date(e.resets) or "next month")
        threading.Thread(target=self.notify, args=(message,), daemon=True).start()  # a notification may take a moment


def pretty_date(iso: str) -> str:
    """'2026-12-01' -> '1 December'; '' when it isn't a date."""
    try:
        y, m, d = (int(part) for part in str(iso)[:10].split("-"))
        months = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                  "October", "November", "December"]
        return f"{d} {months[m - 1]}" if 1 <= m <= 12 and 1 <= d <= 31 and y else ""
    except ValueError:
        return ""
