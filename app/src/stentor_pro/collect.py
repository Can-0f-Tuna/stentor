"""Sharing recordings to train our models. Off unless the server's switch is on and the user said Yes.

The answer belongs to the account, so it lives in account.json with the token and goes when you sign out; the
server keeps every answer as proof. A clip passes `eligible` when the dictation ends and `sharing` again before
each step of the upload, so turning this off stops it at once. Nothing here delays or fails a dictation: clips
wait in a small queue in memory, a failure drops the clip, and nothing is written to disk or logged.
"""
import json
import queue
import subprocess
import sys
import threading
import time
import urllib.request

from stentor import __version__, audio, paste

CONSENT_VERSION = 1  # the card as worded in this release; must match the server's, or nothing is asked or sent
MAX_QUEUED = 4
MAX_BYTES = 20 * 1024 * 1024  # the server's limit
# Not local: that audio stays on the computer. Not custom: it may be your own server (configs saved with the old
# "groq" preset load as custom now, so they are not shared either).
SHARED_PROVIDERS = ("openai",)
NEVER_RULES = ("off", "clipboard")
NOTIFY_EVERY = 24 * 3600  # where there is no overlay, a system notification at most once a day
CARD_TEXT = "Help improve accuracy? Open the Stentor settings to answer."


class Collector:
    def __init__(self, account):
        self.account = account
        self.clips = queue.Queue(MAX_QUEUED)
        self.worker = None
        self.sending = False

    # ---------- the answer ----------

    def switched_on(self) -> bool:
        data = self.account.data()
        return bool(data.get("collection")) and data.get("consent_version") == CONSENT_VERSION

    def answer(self) -> str:
        """'yes', 'no', or '' when this version of the card hasn't been answered."""
        consent = self.account.data().get("consent") or {}
        return consent.get("choice", "") if consent.get("version") == CONSENT_VERSION else ""

    def should_ask(self) -> bool:
        return self.switched_on() and not self.answer()

    def sharing(self) -> bool:
        """The server must have the Yes before anything goes up; it would refuse the clip otherwise."""
        consent = self.account.data().get("consent") or {}
        return self.switched_on() and self.answer() == "yes" and bool(consent.get("sent"))

    def choose(self, choice: str):
        """Saved here first: a No holds even if the server can't be reached. A later tick sends it."""
        if choice not in ("yes", "no"):
            raise ValueError("choose yes or no")
        self.account.merge(consent={"version": CONSENT_VERSION, "choice": choice, "sent": False})
        if choice == "no":
            self.forget()
        self.send_choice()

    def send_choice(self, timeout: float = 5.0):
        data = self.account.data()
        consent = data.get("consent") or {}
        if not data or consent.get("sent") or consent.get("version") != CONSENT_VERSION or not self.switched_on():
            return
        try:
            status, _ = self.account._request("/api/contribute/choice", {"version": CONSENT_VERSION, "choice": consent["choice"]},
                                              token=data["token"], timeout=timeout)
        except Exception:
            return
        if status == 200:
            with self.account.lock:
                now = self.account.data()
                if now.get("consent") == consent:  # not changed again meanwhile
                    self.account._write(dict(now, consent=dict(consent, sent=True)))

    def tick(self):
        if self.sending or (self.account.data().get("consent") or {}).get("sent", True):
            return
        self.sending = True

        def run():
            try:
                self.send_choice()
            finally:
                self.sending = False
        threading.Thread(target=run, daemon=True).start()

    # ---------- a finished dictation ----------

    def eligible(self, cfg, app: str) -> bool:
        if not self.sharing() or cfg.provider not in SHARED_PROVIDERS:
            return False
        if paste.rule_for(cfg.app_rules, app) in NEVER_RULES:
            return False
        # focus unknown: it could be the app you ruled out
        return bool(app) or not any(rule in NEVER_RULES for rule in cfg.app_rules.values())

    def take(self, cfg, recording, raw: str, final: str, app: str, language: str = "", confidence=None):
        """Only hands the clip to the worker: the work happens away from the dictation."""
        if recording is None or not raw.strip() or not final.strip() or not self.eligible(cfg, app):
            return
        try:
            self.clips.put_nowait({"recording": recording, "raw": raw, "final": final, "language": language, "confidence": confidence})
        except queue.Full:
            return
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._run, daemon=True)
            self.worker.start()

    def forget(self):
        """Everything still waiting is dropped."""
        try:
            while True:
                self.clips.get_nowait()
        except queue.Empty:
            pass

    def _run(self):
        while True:
            try:
                clip = self.clips.get(timeout=30)
            except queue.Empty:
                return
            try:
                self._upload(clip)
            except Exception:  # dropped, with nothing said: a message could hold dictated text
                pass

    def _upload(self, clip: dict):
        if not self.sharing():
            return
        speech = audio.speech_only(clip["recording"])  # the part that went to the transcriber
        if speech is None:
            return
        wav = audio.wav_bytes(speech)
        if len(wav) > MAX_BYTES:
            return
        body = {"raw": clip["raw"], "final": clip["final"], "language": clip["language"], "version": __version__, "size": len(wav)}
        if clip["confidence"] is not None:
            body["confidence"] = clip["confidence"]
        token = self.account.data().get("token", "")
        status, reply = self.account._request("/api/contribute/start", body, token=token, timeout=15)
        if status != 200 or not self.sharing():
            return
        request = urllib.request.Request(reply["upload_url"], data=wav, method="PUT", headers={"Content-Type": "audio/wav"})
        with urllib.request.urlopen(request, timeout=60):
            pass
        if self.sharing():  # no finish, no record: the server drops the file
            self.account._request("/api/contribute/finish", {"id": reply["id"]}, token=token, timeout=15)

    # ---------- deleting ----------

    def delete_all(self) -> dict:
        """Asks the server to delete everything this account shared. That also stops sharing."""
        data = self.account.data()
        if not data:
            raise ValueError("sign in first")
        self.forget()
        stopping = self.answer() == "yes"
        if stopping:
            self.account.merge(consent={"version": CONSENT_VERSION, "choice": "no", "sent": False})
        try:
            status, reply = self.account._request("/api/contribute/delete", {}, token=data["token"], timeout=20)
        except Exception:
            status, reply = 0, {}
        if status != 200:
            raise ValueError("Couldn't reach the server, so nothing was deleted. Try again.")
        if stopping:  # the server recorded this No itself
            self.account.merge(consent={"version": CONSENT_VERSION, "choice": "no", "sent": True})
        return reply

    # ---------- asking, where there is no overlay ----------

    def notify(self, feedback):
        """The settings page holds the card. This only says so, once a day at most."""
        if time.time() - float(self.account.data().get("consent_notified") or 0) < NOTIFY_EVERY:
            return
        self.account.merge(consent_notified=time.time())
        if sys.platform == "win32":
            _windows_balloon()
        else:
            feedback.notify(CARD_TEXT)


def _windows_balloon():
    """A balloon whose click opens the card. It runs on its own: the dictation doesn't wait for it."""
    command = json.dumps(sys.executable + " -m stentor consent open")
    script = f"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.BalloonTipTitle = 'Stentor'
$n.BalloonTipText = 'Help improve accuracy? Click to answer.'
Register-ObjectEvent $n BalloonTipClicked -Action {{ cmd /c {command} }} | Out-Null
$n.ShowBalloonTip(15000)
Start-Sleep 20
$n.Dispose()
"""
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script], creationflags=0x08000000)
    except OSError:
        pass
