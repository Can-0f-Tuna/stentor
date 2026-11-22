"""The portal hotkey against the real xdg-desktop-portal, on a bus of its own, with tests/portal_stand_in.py as the
desktop behind it. Runs where xdg-desktop-portal, dbus-daemon and python3-gi are installed, and is skipped elsewhere.
It checks the wire: requests, sessions, the app's identity and the signals. It is not a check on GNOME or KDE."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from hold_dictate.hotkey import HoldDetector, parse_hotkey
from hold_dictate.portal_hotkey import PortalBackend, PortalUnavailable

PORTAL = "/usr/libexec/xdg-desktop-portal"
SYSTEM_PYTHON = "/usr/bin/python3"
STAND_IN = Path(__file__).with_name("portal_stand_in.py")


def available() -> bool:
    if not sys.platform.startswith("linux") or not os.path.exists(PORTAL) or not shutil.which("dbus-daemon"):
        return False
    try:
        import jeepney  # noqa: F401
    except ImportError:
        return False
    return subprocess.run([SYSTEM_PYTHON, "-c", "from gi.repository import Gio"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not available(), reason="needs xdg-desktop-portal, dbus-daemon and python3-gi")


class Desktop:
    """A session bus, the real portal on it, and the stand-in desktop behind the portal."""

    def __init__(self, policy: str, monkeypatch):
        self.root = Path(tempfile.mkdtemp(prefix="hd-portal-"))  # short: a socket path has a length limit
        self.processes = []
        for name in ("config/xdg-desktop-portal", "data", "run", "portals"):
            (self.root / name).mkdir(parents=True)
        (self.root / "run").chmod(0o700)
        (self.root / "portals" / "standin.portal").write_text(
            "[portal]\nDBusName=org.freedesktop.impl.portal.desktop.standin\n"
            "Interfaces=org.freedesktop.impl.portal.GlobalShortcuts;\nUseIn=standin\n")
        (self.root / "config" / "xdg-desktop-portal" / "portals.conf").write_text("[preferred]\ndefault=standin\n")
        env = {"XDG_CONFIG_HOME": str(self.root / "config"), "XDG_DATA_HOME": str(self.root / "data"),
               "XDG_RUNTIME_DIR": str(self.root / "run"), "XDG_DESKTOP_PORTAL_DIR": str(self.root / "portals"),
               "XDG_CURRENT_DESKTOP": "standin", "XDG_SESSION_TYPE": "wayland", "HOME": str(self.root)}
        for name in ("DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"):
            monkeypatch.delenv(name, raising=False)
        self.log = (self.root / "portal.log").open("w")  # the bus starts the portal at the first call, as a session does
        bus = self.start(["dbus-daemon", "--session", "--nofork", "--print-address", f"--address=unix:dir={self.root / 'run'}"],
                         env, stderr=self.log)
        env["DBUS_SESSION_BUS_ADDRESS"] = bus.stdout.readline().strip()
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        self.env = {**os.environ, **env}
        self.stand_in = self.start([SYSTEM_PYTHON, str(STAND_IN), policy], self.env, stdin=subprocess.PIPE)
        assert self.heard() == "ready"

    def start(self, command, env, **pipes):
        pipes.setdefault("stdout", subprocess.PIPE)
        process = subprocess.Popen(command, env={**os.environ, **env}, text=True, **pipes)
        self.processes.append(process)
        return process

    def heard(self) -> str:
        """The stand-in's next line: what the portal just asked it."""
        return self.stand_in.stdout.readline().strip()

    def tell(self, line: str):
        self.stand_in.stdin.write(line + "\n")
        self.stand_in.stdin.flush()

    def close(self):
        for process in reversed(self.processes):
            process.terminate()
        for process in self.processes:
            process.wait(5)
        self.log.close()
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture
def desktop(monkeypatch):
    made = []

    def make(policy="grant"):
        made.append(Desktop(policy, monkeypatch))
        deadline = time.monotonic() + 15
        while PortalBackend().probe():  # until the portal answers on the bus
            assert time.monotonic() < deadline, (made[-1].root / "portal.log").read_text()[-2000:]
            time.sleep(0.2)
        return made[-1]

    yield make
    for one in made:
        one.close()


class Listening:
    """The backend on a thread, as the app runs it, with what it did and said."""

    def __init__(self, detector, mode="hold"):
        self.actions, self.notices, self.stop, self.failed = [], [], threading.Event(), []
        backend = PortalBackend()
        backend.on_problem = self.notices.append

        def run():
            try:
                backend.run(detector, self.actions.append, lambda: None, stop=self.stop, mode=lambda: mode)
            except Exception as e:
                self.failed.append(e)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def wait(self, done, what=""):
        deadline = time.monotonic() + 10
        while not done():
            assert not self.failed, self.failed
            assert time.monotonic() < deadline, (what, self.actions, self.notices)
            time.sleep(0.02)

    def end(self):
        self.stop.set()
        self.thread.join(10)
        assert not self.thread.is_alive() and not self.failed, self.failed


def test_the_portal_grants_the_keys_and_reports_press_and_release(desktop):
    made = desktop("grant")
    detector = HoldDetector(parse_hotkey("alt+space"), parse_hotkey("f9"))
    listening = Listening(detector)
    assert made.heard() == "session io.github.hold-dictate"  # the portal knows which app asks
    assert made.heard() == "bound dictation-ALT-space=ALT+space second-F9=F9"
    listening.wait(lambda: listening.notices and listening.notices[-1] == "", "granted as asked: nothing to report")
    made.tell("press dictation-ALT-space")
    listening.wait(lambda: listening.actions == ["start"], "press")
    assert detector.is_down("space") and not detector.wait_released(0)
    made.tell("release dictation-ALT-space")
    listening.wait(lambda: listening.actions == ["start", "stop"], "release")
    made.tell("press second-F9")
    made.tell("release second-F9")
    listening.wait(lambda: listening.actions == ["start", "stop", "second:start", "second:stop"], "second shortcut")
    listening.end()
    assert made.heard() == "closed"  # the session is closed on the desktop's side too


def test_another_key_from_the_desktop_is_reported_and_still_dictates(desktop):
    made = desktop("other")
    listening = Listening(HoldDetector(parse_hotkey("alt+space")))
    listening.wait(lambda: any("assigned Ctrl+D" in notice for notice in listening.notices), "the notice")
    made.tell("press dictation-ALT-space")
    made.tell("release dictation-ALT-space")
    listening.wait(lambda: listening.actions == ["start", "stop"], "press and release")
    listening.end()


def test_a_key_changed_in_the_desktop_is_followed(desktop):
    made = desktop("grant")
    detector = HoldDetector(parse_hotkey("f9"))
    listening = Listening(detector)
    listening.wait(lambda: listening.notices and listening.notices[-1] == "", "bound")
    made.tell("change dictation-F9 Ctrl+D")
    listening.wait(lambda: any("assigned Ctrl+D" in notice for notice in listening.notices), "the changed key")
    made.tell("press dictation-F9")
    listening.wait(lambda: listening.actions[-1:] == ["start"], "press after the change")
    assert detector.is_down("ctrl")
    listening.end()


def test_a_declined_request_leaves_the_app_running_with_a_notice(desktop):
    made = desktop("refuse")
    listening = Listening(HoldDetector(parse_hotkey("f9")))
    listening.wait(lambda: any("declined" in notice for notice in listening.notices), "the refusal")
    assert listening.actions == [] and listening.thread.is_alive()
    listening.end()


def test_a_shortcut_with_no_key_is_reported(desktop):
    made = desktop("unassigned")
    listening = Listening(HoldDetector(parse_hotkey("f9")))
    listening.wait(lambda: any("did not assign a key" in notice for notice in listening.notices), "the notice")
    made.tell("press dictation-F9")
    time.sleep(0.5)
    assert listening.actions == []
    listening.end()


def test_a_session_the_desktop_closes_is_reported(desktop):
    made = desktop("grant")
    listening = Listening(HoldDetector(parse_hotkey("f9")))
    listening.wait(lambda: listening.notices and listening.notices[-1] == "", "bound")
    made.tell("press dictation-F9")
    listening.wait(lambda: listening.actions == ["start"], "press")
    made.tell("close")
    listening.wait(lambda: any("closed the shortcut session" in notice for notice in listening.notices), "the notice")
    assert listening.actions == ["start", "cancel"]
    listening.end()


def test_a_portal_that_cannot_bind_hands_over_to_the_other_backends(desktop):
    desktop("broken")  # it answers and lists the interface, as GNOME's portal does on a desktop that is not GNOME
    backend = PortalBackend()
    with pytest.raises(PortalUnavailable, match="could not assign"):
        backend.run(HoldDetector(parse_hotkey("f9")), lambda action: None, lambda: None, stop=threading.Event(), mode=lambda: "hold")


def test_without_a_session_bus_the_portal_is_not_chosen(monkeypatch, tmp_path):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={tmp_path}/none")
    assert PortalBackend().probe()
