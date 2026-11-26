"""Your Stentor account, needed only for Stentor Cloud: dictation through "This computer" or a key of your own
works without one. You sign in with Google once on the settings page; after that the app asks the server about
every 10 minutes whether the account is still there and its plan: "free" or "plus" (the internal name of a Stentor
Cloud subscription), with this month's cloud time and the subscription. The old "premium" flag is still read and
shown, but it turns nothing on: only the plan does.

Signing in: the settings page opens <site>/api/auth/start with this server's port and a random state. After
Google, the site sends the browser back to http://127.0.0.1:<port>/auth with a one-time code, which is
traded for the account token here. The token is kept in account.json next to the settings, readable only by you.
"""
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from stentor import __version__, config as cfgmod

SITE = (cfgmod.env("SITE") or "https://stentor-app.vercel.app").rstrip("/")
CHECK_EVERY = 10 * 60  # seconds between checks with the server while the app runs
STATE_SECONDS = 10 * 60  # how long a started sign-in may take
NOTICE_ROUND = 2  # raise it to show the "Stentor Cloud is on" notice once more to every subscriber


def account_path() -> Path:
    return cfgmod.config_dir() / "account.json"


class Account:
    def __init__(self, path: Path | None = None, site: str = SITE):
        self.path = path or account_path()
        self.site = site
        self.pending: dict[str, float] = {}  # sign-in state -> when it expires
        self.lock = threading.Lock()
        self.checking = False

    # ---------- the saved account ----------

    def data(self) -> dict:
        """Read fresh each time: the stand-alone settings page and the running app share the file."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and isinstance(data.get("token"), str) and data["token"] else {}
        except Exception:
            return {}

    def signed_in(self) -> bool:
        return bool(self.data())

    def public(self) -> dict | None:
        """What the settings page may show. Never the token."""
        data = self.data()
        if not data:
            return None
        plus = data.get("plan") == "plus"
        return {"email": str(data.get("email", "")), "name": str(data.get("name") or ""), "premium": bool(data.get("premium")),
                "plan": "plus" if plus else "free", "cloud": _numbers(data.get("cloud")) if plus else None,
                "billing": _billing(data.get("billing")), "sale": data.get("sale") is True}

    def premium(self) -> bool:
        return bool(self.data().get("premium"))

    def plus(self) -> bool:
        return self.data().get("plan") == "plus"

    def _write(self, data: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")  # the app and a `stentor consent` may write at once
        tmp.write_text(json.dumps(data), encoding="utf-8")
        if sys.platform != "win32":
            tmp.chmod(0o600)  # holds the account token
        os.replace(tmp, self.path)

    def _clear(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    # ---------- talking to the site ----------

    def _request(self, path: str, body: dict | None = None, token: str = "", timeout: float = 8.0) -> tuple[int, dict]:
        headers = {"User-Agent": f"stentor/{__version__}", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.site + path, data=data, headers=headers, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}

    def begin(self, port: int) -> str:
        """The address that starts a Google sign-in which comes back to this computer's settings page."""
        state = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            self.pending = {s: t for s, t in self.pending.items() if t > now}
            self.pending[state] = now + STATE_SECONDS
        return f"{self.site}/api/auth/start?" + urllib.parse.urlencode({"to": "app", "port": port, "state": state})

    def finish(self, code: str, state: str) -> str:
        """Trades the one-time code for the account token. Returns '' when signed in, else what went wrong."""
        with self.lock:
            expires = self.pending.pop(state, 0) if state else 0
        if expires < time.time():
            return "This sign-in link is old or wasn't started here. Go back to the settings and sign in again."
        if not code:
            return "Google didn't finish the sign-in. Go back to the settings and try again."
        try:
            status, reply = self._request("/api/auth/exchange", {"code": code})
        except Exception:
            return "Couldn't reach the Stentor server. Check the internet connection and try again."
        if status == 403:
            return "This account is blocked."
        if status != 200 or not isinstance(reply.get("token"), str):
            return "The sign-in didn't go through. Go back to the settings and try again."
        user = reply.get("user") or {}
        self._write({"token": reply["token"], "email": user.get("email", ""), "name": user.get("name", ""),
                     "premium": bool(user.get("premium")), "plan": "plus" if user.get("plan") == "plus" else "free",
                     "checked": time.time(), "notice_round": NOTICE_ROUND})
        return ""

    def check(self, max_age: float = CHECK_EVERY, timeout: float = 8.0) -> bool:
        """Asks the server about the account when the last answer is older than max_age. Signed out by
        the server (or blocked): forget the account. Server unreachable: keep what we know."""
        data = self.data()
        if not data or time.time() - float(data.get("checked") or 0) < max_age:
            return bool(data)
        try:
            status, reply = self._request("/api/me", token=data["token"], timeout=timeout)
        except Exception:
            return True
        if status == 401:
            if self.data().get("token") == data["token"]:  # not replaced by a new sign-in meanwhile
                self._clear()
            return False
        if status == 200:
            with self.lock:
                current = self.data()  # read again: a notice or the consent card may have been clicked while we waited
                if current.get("token") == data["token"]:
                    plus = reply.get("plan") == "plus"  # older servers send no plan: free
                    was_plus = current.get("plan") == "plus"  # before this answer: the notice comes when Stentor Cloud starts
                    current.update(email=reply.get("email", data.get("email", "")), name=reply.get("name", data.get("name", "")),
                                   premium=bool(reply.get("premium")), checked=time.time(),
                                   premium_notice=plus and bool(current.get("premium_notice") or not was_plus),
                                   collection=reply.get("collection") is True, consent_version=reply.get("consent_version", 0))
                    current.update(plan="plus" if plus else "free", cloud=_numbers(reply.get("cloud")) if plus else None,
                                   billing=_billing(reply.get("billing")),  # also after the subscription ended
                                   sale=reply.get("sale") is True)  # Stentor Cloud on sale; older servers say nothing: not
                    consent = current.get("consent")
                    if current["collection"] and (not consent or consent.get("sent")):  # an answer still on its way wins
                        current.pop("consent", None)
                        if reply.get("consent") in ("yes", "no"):
                            current["consent"] = {"version": reply["consent_version"], "choice": reply["consent"], "sent": True}
                    self._write(current)
        return True

    def billing_url(self, kind: str) -> str:
        """The page where Stentor Cloud is bought ("checkout") or the subscription is managed ("portal"). ValueError with
        a sentence for the settings page when there is none."""
        token = self.data().get("token", "")
        if not token:
            raise ValueError("Sign in to use Stentor Cloud.")
        try:
            status, reply = self._request(f"/api/billing/{kind}", {}, token=token, timeout=10)
        except Exception:
            raise ValueError("Couldn't reach the Stentor server. Check the internet connection and try again.") from None
        if status == 401:
            threading.Thread(target=lambda: self.check(max_age=0, timeout=5), daemon=True).start()
            raise ValueError("You're signed out. Sign in again.")
        url = reply.get("url") if status == 200 else None
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("The Stentor server couldn't open that page. Try again in a moment.")
        return url

    def merge(self, **fields):
        """Changes a few saved fields. Does nothing while signed out."""
        with self.lock:
            data = self.data()
            if data:
                self._write(dict(data, **fields))

    def premium_notice(self) -> bool:
        """True from the moment Stentor Cloud starts for this account until the user has clicked the notice that shows
        its features. (The field names are from when they were called Premium.)"""
        data = self.data()
        return bool(data.get("plan") == "plus" and (data.get("premium_notice") or data.get("notice_round", 1) < NOTICE_ROUND))

    def premium_notice_seen(self):
        if self.premium_notice():
            self.merge(premium_notice=False, notice_round=NOTICE_ROUND)

    def tick(self):
        """Called often by the app; checks in the background about every 10 minutes."""
        if self.checking:
            return
        data = self.data()
        if not data or time.time() - float(data.get("checked") or 0) < CHECK_EVERY:
            return
        self.checking = True

        def run():
            try:
                self.check()
            finally:
                self.checking = False
        threading.Thread(target=run, daemon=True).start()

    def sign_out(self):
        token = self.data().get("token", "")
        self._clear()
        if token:
            try:
                self._request("/api/auth/logout", {}, token=token, timeout=5)
            except Exception:
                pass  # the token is gone from this computer; the server forgets it within a year anyway


def _numbers(cloud) -> dict | None:
    """This month's cloud time: {used, limit} in seconds and the day it resets, as the server sent them."""
    if not isinstance(cloud, dict):
        return None
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    return {"used": num(cloud.get("used")), "limit": num(cloud.get("limit")), "resets": str(cloud.get("resets") or "")}


def _billing(billing) -> dict | None:
    if not isinstance(billing, dict):
        return None
    return {"status": str(billing.get("status") or ""), "renews_at": str(billing.get("renews_at") or ""),
            "cancel_at_period_end": billing.get("cancel_at_period_end") is True, "pending": billing.get("pending") is True}
