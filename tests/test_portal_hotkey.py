import sys
import threading
from types import SimpleNamespace

import pytest

from hold_dictate import hotkey, portal_hotkey
from hold_dictate.hotkey import HoldDetector, parse_hotkey
from hold_dictate.portal_hotkey import PortalBackend, PortalRefused


class FakePortal:
    """The desktop's side. Tests name shortcuts "dictation" and "second"; the ids asked for are kept in `asked`."""

    def __init__(self, granted, events):
        self.granted = granted
        self.events = iter(events)
        self.closed = []
        self.asked = []
        self.ids = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def register(self):
        pass

    def create_session(self, keep_waiting, on_tick):
        return "/session"

    def bind(self, session, requested, keep_waiting, on_tick):
        self.asked.append([ident for ident, _, _ in requested])
        self.ids = {ident.split("-")[0]: ident for ident, _, _ in requested}
        if isinstance(self.granted, Exception):
            raise self.granted
        return {self.ids.get(name, name): trigger for name, trigger in self.granted.items()}

    def event(self, session, timeout):
        event = next(self.events)
        if callable(event):
            return event()
        kind, value = event
        if kind == "changed":
            return kind, {self.ids.get(name, name): trigger for name, trigger in value.items()}
        return kind, self.ids.get(value, value)

    def close_session(self, session):
        self.closed.append(session)


@pytest.fixture
def listener(monkeypatch):
    stop = threading.Event()
    backend = PortalBackend()
    actions, notices = [], []

    def run(detector, granted, events, mode="hold", stop_on_notice=None):
        fake = FakePortal(granted, [*events, stop.set])
        monkeypatch.setattr(portal_hotkey, "GlobalShortcuts", lambda: fake)

        def report(text):
            notices.append(text)
            if stop_on_notice and stop_on_notice in text:
                stop.set()

        backend.on_problem = report
        backend.run(detector, actions.append, lambda: None, stop=stop, mode=lambda: mode)
        run.portal = fake
        return actions, notices, fake.closed

    return run


def test_granted_shortcuts_follow_the_desktop_press_and_release(listener):
    detector = HoldDetector(parse_hotkey("alt+space"), parse_hotkey("f9"))

    def while_held():
        assert detector.is_down("space")
        assert detector.modifiers_held()
        assert not detector.wait_released(0)

    def after_release():
        assert not detector.is_down("space")
        assert not detector.modifiers_held()
        assert not detector.wait_released(0)  # the physical Alt may still be down

    actions, notices, closed = listener(detector, {"dictation": "Press <Alt>space", "second": "F9"}, [
        ("Activated", "dictation"), while_held, ("Activated", "dictation"),  # duplicate press
        ("Activated", "second"), ("Deactivated", "second"),  # another chord cannot stop this one
        ("Deactivated", "dictation"), after_release, ("Activated", "second"), ("Deactivated", "second"),
    ])
    assert actions == ["start", "stop", "second:start", "second:stop", "cancel"]
    assert notices[-1] == ""
    assert closed == ["/session"]


def test_paste_wait_includes_the_settle_time(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(hotkey, "time", SimpleNamespace(monotonic=lambda: now[0]))
    detector = HoldDetector(parse_hotkey("alt+space"))
    detector.set_portal_shortcuts({"dictation": None})  # description could not be parsed
    detector.feed_shortcut("dictation", True)
    assert detector.modifiers_held()
    assert not detector.wait_released(0)
    detector.feed_shortcut("dictation", False)
    now[0] += 0.24
    assert not detector.wait_released(0)
    now[0] += 0.02
    assert detector.wait_released(0)


def test_a_different_desktop_key_is_reported_and_triggers_the_same_action(listener):
    detector = HoldDetector(parse_hotkey("alt+space"))

    def while_held():
        assert detector.is_down("ctrl") and detector.is_down("d")
        assert not detector.is_down("alt") and not detector.is_down("space")

    actions, notices, _ = listener(detector, {"dictation": "Ctrl+D"}, [
        ("Activated", "dictation"), while_held, ("Deactivated", "dictation"),
    ])
    assert actions[:2] == ["start", "stop"]
    assert "assigned Ctrl+D" in notices[-1]
    assert str(detector.hotkey) == "alt+space"


def test_a_shortcut_the_desktop_did_not_assign_is_reported_and_ignored(listener):
    detector = HoldDetector(parse_hotkey("alt+space"), parse_hotkey("f9"))
    actions, notices, _ = listener(detector, {"second": "F9"}, [
        ("Activated", "dictation"), ("Deactivated", "dictation"),
        ("Activated", "second"), ("Deactivated", "second"),
    ])
    assert actions == ["second:start", "second:stop", "cancel"]
    assert "did not assign a key" in notices[-1] and "alt+space" in notices[-1]


def test_declining_the_desktop_request_leaves_the_shortcut_off(listener):
    detector = HoldDetector(parse_hotkey("f10"))
    actions, notices, closed = listener(detector, PortalRefused("Request declined."), [],
                                       stop_on_notice="declined")
    assert actions == []
    assert notices[-1] == "Request declined."
    assert closed == ["/session"]


def test_a_closed_session_cancels_a_held_recording(listener):
    detector = HoldDetector(parse_hotkey("ctrl+d"))
    actions, notices, closed = listener(detector, {"dictation": "Ctrl+D"}, [
        ("Activated", "dictation"), ("closed", None),
    ], stop_on_notice="closed")
    assert actions == ["start", "cancel"]
    assert "desktop closed the shortcut session" in notices[-1]
    assert not detector.is_down("ctrl")
    assert closed == ["/session"]


def test_a_lost_release_keeps_recording_until_the_next_press_and_release(listener):
    detector = HoldDetector(parse_hotkey("f9"))
    actions, notices, _ = listener(detector, {"dictation": "F9"}, [
        ("Activated", "dictation"),  # its release never comes
        ("Activated", "dictation"), ("Deactivated", "dictation"),
    ])
    assert actions == ["start", "stop", "cancel"]  # the last one is the session closing
    assert notices[-1] == ""


def test_toggle_mode_needs_no_release(listener):
    detector = HoldDetector(parse_hotkey("f9"))
    actions, _, _ = listener(detector, {"dictation": "F9"}, [("Activated", "dictation"), ("Deactivated", "dictation")], mode="toggle")
    assert actions == ["start", "stop", "cancel"]  # both at the press: the app latches the recording, as after a tap


def test_a_changed_key_is_asked_for_under_a_new_id(listener, monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    detector = HoldDetector(parse_hotkey("alt+space"), parse_hotkey("f9"))
    listener(detector, {"dictation": "Alt+Space", "second": "F9"}, [lambda: detector.set_hotkey(parse_hotkey("ctrl+d")) and None,
                                                                    ("Activated", "dictation")])
    # one request each time, with every shortcut in it: a desktop replaces the app's whole set at a bind
    assert listener.portal.asked == [["dictation-ALT-space", "second-F9"], ["dictation-CTRL-d", "second-F9"]]
    assert listener.portal.closed == ["/session", "/session"]


GNOME_LISTED = """org.gnome.desktop.wm.keybindings activate-window-menu ['<Alt>space']
org.gnome.desktop.wm.keybindings close ['<Alt>F4']
org.gnome.shell.keybindings toggle-message-tray ['<Super>v', '<Super>m']
org.gnome.settings-daemon.plugins.media-keys screensaver ['<Super>l']
org.gnome.desktop.wm.keybindings switch-input-source @as []
org.gnome.settings-daemon.plugins.media-keys logout ['<Control><Alt>Delete']"""


def test_a_key_gnome_uses_itself_is_reported(listener, monkeypatch):
    """GNOME grants Alt+Space to an app and goes on opening its window menu with it."""
    assert portal_hotkey.kept_by_gnome(parse_hotkey("alt+space"), GNOME_LISTED) == "activate-window-menu"
    assert portal_hotkey.kept_by_gnome(parse_hotkey("super+m"), GNOME_LISTED) == "toggle-message-tray"
    assert portal_hotkey.kept_by_gnome(parse_hotkey("ctrl+alt+space"), GNOME_LISTED) == ""
    assert portal_hotkey.kept_by_gnome(parse_hotkey("f9"), GNOME_LISTED) == ""
    monkeypatch.setattr(portal_hotkey, "gnome_bindings", lambda: GNOME_LISTED)
    _, notices, _ = listener(HoldDetector(parse_hotkey("alt+space")), {"dictation": "Alt+Space"}, [])
    assert "GNOME uses alt+space itself (activate-window-menu)" in notices[-1]


def test_gnome_is_not_asked_on_another_desktop(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert portal_hotkey.gnome_bindings() == ""


def test_hyprland_binds_by_name_so_the_id_never_changes(listener, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "Hyprland")
    detector = HoldDetector(parse_hotkey("alt+space"))
    actions, notices, _ = listener(detector, {}, [("Activated", "dictation"), ("Deactivated", "dictation")])
    assert listener.portal.asked == [["dictation"]]
    assert actions[:2] == ["start", "stop"]
    assert "io.github.hold-dictate:dictation" in notices[-1]


@pytest.mark.parametrize("wayland,portal,evdev,display,forced,expected", [
    (True, True, True, True, None, "evdev"),  # the keyboard itself comes first where it can be read
    (True, True, False, True, None, "portal"),
    (True, False, True, True, None, "evdev"),
    (True, False, False, True, None, None),
    (False, True, True, True, None, "evdev"),
    (False, True, False, True, None, "pynput"),
    (True, True, True, True, "portal", "portal"),
    (True, True, True, True, "pynput", "pynput"),
    (True, False, True, True, "portal", None),
    (True, True, False, True, "evdev", None),
])
def test_linux_backend_order_and_override(monkeypatch, wayland, portal, evdev, display, forced, expected):
    monkeypatch.setattr(sys, "platform", "linux")
    for name, value in {
        "XDG_SESSION_TYPE": "wayland" if wayland else "x11",
        "WAYLAND_DISPLAY": "wayland-1" if wayland else None,
        "DISPLAY": ":1" if display else None,
        "HOLD_DICTATE_HOTKEY_BACKEND": forced,
    }.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(PortalBackend, "probe", lambda self: "" if portal else "Portal unavailable.")
    monkeypatch.setattr(hotkey, "EvdevBackend", lambda: SimpleNamespace(name="evdev", usable=lambda: evdev))
    monkeypatch.setattr(hotkey.PynputBackend, "usable", lambda self, quick: True)
    backend, problem = hotkey.choose_backend()
    assert getattr(backend, "name", None) == expected
    assert bool(problem) == (backend is None)
