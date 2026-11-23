"""The hotkey through the desktop's GlobalShortcuts portal, for a Wayland session where the keyboard cannot be read.

The desktop decides: it may grant the key asked for, assign another, ask you first, or refuse. It reports the whole
shortcut going down and coming up, never single keys.

What desktops do with a request was learned from anomalyco/hex (src/linux/portal.rs and src/linux/input.rs, MIT
License, Copyright (c) 2026 Christopher Langton), and the shortcut id rule in `shortcut_id` is theirs:

- a desktop keeps the key it stored for an id and ignores a new one asked for under the same id (GNOME, KDE);
- a desktop keeps one set of shortcuts per app and replaces it at every bind (KDE), so all go in one request;
- giving up on a request while its dialog is open leaves the shortcuts with no key (KDE), so the wait is long;
- Hyprland takes every shortcut without a key: you bind the id by name in its config.

Checked against xdg-desktop-portal 1.21 with a stand-in for the desktop's side (tests/portal_stand_in.py), and by
hand on GNOME Shell 51 in a headless session (2026-11-23): its dialog showed the request, and once added, holding
and tapping F9 and Ctrl+Alt+D gave a start at the press and a stop at the release; a restart bound with no dialog;
Alt+Space was granted and never arrived, which `kept_by_gnome` now reports. Not yet run on KDE or Hyprland."""
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from uuid import uuid4

from .hotkey import parse_hotkey
from .service import LABEL, ensure_portal_desktop_entry

DESKTOP = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
SHORTCUTS = "org.freedesktop.portal.GlobalShortcuts"
SESSION = "org.freedesktop.portal.Session"
REQUEST = "org.freedesktop.portal.Request"
REGISTRY = "org.freedesktop.host.portal.Registry"
CALL_TIMEOUT = 5
APPROVAL_TIMEOUT = 30 * 60


class PortalUnavailable(Exception):
    """The desktop could not provide a working GlobalShortcuts session."""


class PortalRefused(Exception):
    """The desktop shortcut request was declined."""


class PortalCallError(PortalUnavailable):
    """The session bus or portal rejected a method call."""


def _trigger(hotkey):
    parts = str(hotkey).split("+")
    key = parts.pop()
    return "+".join([("LOGO" if mod == "super" else mod.upper()) for mod in parts]
                    + [key if len(key) == 1 or key == "space" else key.upper()])


def _described_hotkey(label):
    """Best effort: trigger_description is human-readable and can be localized."""
    label = re.sub(r"<([^>]+)>", r"\1+", label.strip().lower().removeprefix("press "))
    label = re.sub(r"\b(logo|primary)\b(?=\s*\+)", lambda m: {"logo": "super", "primary": "ctrl"}[m[0]], label)
    try:
        return parse_hotkey(label)
    except ValueError:
        return None


def desktop_assigns() -> bool:
    """Hyprland: the key is bound in its config, by the shortcut's id."""
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) or "hyprland" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()


def shortcut_id(name: str, hotkey, assigns: bool) -> str:
    """The id a shortcut is asked for under. A changed key gets a new id, or the desktop would keep the old key;
    an unchanged one keeps its id, and with it any key you gave it in the desktop's own settings. Where the desktop
    assigns the keys the id is the bare name, so a bind written in its config survives a change here."""
    return name if assigns else f"{name}-{_trigger(hotkey).replace('+', '-')}"


GNOME_SCHEMAS = ("org.gnome.desktop.wm.keybindings", "org.gnome.shell.keybindings", "org.gnome.mutter.keybindings",
                 "org.gnome.mutter.wayland.keybindings", "org.gnome.settings-daemon.plugins.media-keys")


def gnome_bindings() -> str:
    """GNOME's own shortcuts, as `gsettings` lists them. Empty anywhere else."""
    import shutil
    import subprocess
    if "gnome" not in os.environ.get("XDG_CURRENT_DESKTOP", "").lower() or not shutil.which("gsettings"):
        return ""
    found = []
    for schema in GNOME_SCHEMAS:
        try:
            found.append(subprocess.run(["gsettings", "list-recursively", schema], capture_output=True, text=True, timeout=3).stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    return "\n".join(found)


def kept_by_gnome(hotkey, listed: str) -> str:
    """The name of GNOME's own shortcut on this key, or "". GNOME grants an app a key it uses itself and then never
    sends it: Alt+Space opens the window menu (seen on GNOME 51, and what anomalyco/hex warns of)."""
    wanted = (frozenset(hotkey.mods), str(hotkey.key).lower())
    names = {"control": "ctrl", "primary": "ctrl", "ctrl": "ctrl", "alt": "alt", "shift": "shift", "super": "super", "meta": "super"}
    for line in listed.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        for accelerator in re.findall(r"'([^']+)'", parts[2]):
            mods = frozenset(names.get(mod.lower(), mod.lower()) for mod in re.findall(r"<([^>]+)>", accelerator))
            if (mods, re.sub(r"<[^>]+>", "", accelerator).lower()) == wanted:
                return parts[1]
    return ""


class GlobalShortcuts:
    """One connection owns the requests, sessions, and signal subscriptions."""

    def __init__(self):
        # Keep jeepney optional at import time, including when the package was installed without dependencies.
        try:
            from jeepney import DBusAddress, HeaderFields, MatchRule, new_method_call
            from jeepney.io.blocking import open_dbus_connection
            from jeepney.wrappers import DBusErrorResponse, unwrap_msg
        except ImportError as e:
            raise PortalUnavailable("The desktop shortcut portal needs the jeepney package.") from e

        self.address, self.fields, self.rule = DBusAddress, HeaderFields, MatchRule
        self.method_call, self.unwrap = new_method_call, unwrap_msg
        self.dbus_error = DBusErrorResponse
        try:
            self.connection = open_dbus_connection()
        except (OSError, ValueError) as e:
            raise PortalUnavailable(f"Could not connect to the desktop session bus ({e}).") from e
        self.signals = deque()
        self.pending_events = deque()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.connection.close()
        if isinstance(exc[1], self.dbus_error):
            raise PortalCallError(f"The desktop shortcut portal rejected a request ({exc[1]}).") from exc[1]

    def _call(self, interface, method, signature=None, body=(), path=PATH):
        message = self.method_call(self.address(path, bus_name=DESKTOP, interface=interface),
                                   method, signature, body)
        return self.unwrap(self.connection.send_and_get_reply(message, timeout=CALL_TIMEOUT))

    def version(self):
        return self._call("org.freedesktop.DBus.Properties", "Get", "ss", (SHORTCUTS, "version"))[0][1]

    def register(self):
        ensure_portal_desktop_entry()
        try:
            self._call(REGISTRY, "Register", "sa{sv}", (LABEL, {}))
        except self.dbus_error as e:
            # Older portals infer host identities themselves.
            if not e.name.startswith("org.freedesktop.DBus.Error.Unknown"):
                raise PortalUnavailable(f"The desktop portal could not identify hold-dictate ({e}).") from e

        bus = self.connection.bus_proxy
        owner, = bus.GetNameOwner(DESKTOP, _timeout=CALL_TIMEOUT)
        owner_changed = self.rule(type="signal", sender="org.freedesktop.DBus", interface="org.freedesktop.DBus",
                                  member="NameOwnerChanged")
        owner_changed.add_arg_condition(0, DESKTOP)
        rules = (
            self.rule(type="signal", sender=owner, path_namespace=PATH),
            owner_changed,
        )
        for rule in rules:
            self.connection.filter(rule, queue=self.signals)
            bus.AddMatch(rule, _timeout=CALL_TIMEOUT)
        if bus.GetNameOwner(DESKTOP, _timeout=CALL_TIMEOUT) != (owner,):
            raise PortalUnavailable("The desktop shortcut portal restarted.")

    def _signal(self, timeout):
        message = self.connection.recv_until_filtered(self.signals, timeout=timeout)
        fields = message.header.fields
        return fields[self.fields.interface], fields[self.fields.member], fields[self.fields.path], message.body

    def _request(self, method, signature, args, keep_waiting, on_tick, timeout):
        token = "hd_" + uuid4().hex
        sender = self.connection.unique_name[1:].replace(".", "_")
        expected = f"{PATH}/request/{sender}/{token}"
        args[-1]["handle_token"] = ("s", token)
        path, = self._call(SHORTCUTS, method, signature, tuple(args))
        deadline = time.monotonic() + timeout
        while keep_waiting() and time.monotonic() < deadline:
            try:
                interface, member, emitted_at, body = self._signal(0.25)
            except TimeoutError:
                on_tick()
                continue
            if member == "NameOwnerChanged":
                raise PortalUnavailable("The desktop shortcut portal stopped.")
            if interface == REQUEST and member == "Response" and emitted_at in (path, expected):
                code, result = body
                if code == 0:
                    return result
                if code == 1:
                    raise PortalRefused("The desktop shortcut request was declined. Change the shortcut or restart hold-dictate to ask again.")
                raise PortalUnavailable(f"The desktop could not {'create a shortcut session' if method == 'CreateSession' else 'assign global shortcuts'}.")
            if method == "BindShortcuts":
                event = self._session_event(args[0], interface, member, emitted_at, body)
                if event == ("closed", None):
                    raise PortalRefused("The desktop closed the shortcut session. Change the shortcut or restart hold-dictate to reconnect.")
                if event:
                    self.pending_events.append(event)
        try:
            self._call(REQUEST, "Close", path=path)
        except (self.dbus_error, OSError):
            pass
        if keep_waiting():
            if method == "CreateSession":
                raise PortalUnavailable("The desktop did not respond to the shortcut session request.")
            raise PortalRefused("The desktop shortcut request timed out. Change the shortcut or restart hold-dictate to ask again.")
        return {}

    def create_session(self, keep_waiting, on_tick):
        result = self._request("CreateSession", "a{sv}", [{"session_handle_token": ("s", "hd_" + uuid4().hex)}],
                               keep_waiting, on_tick, CALL_TIMEOUT)
        session = result.get("session_handle", ("s", ""))[1]
        if not session and keep_waiting():
            raise PortalUnavailable("The desktop did not return a shortcut session.")
        return session

    def bind(self, session, requested, keep_waiting, on_tick):
        shortcuts = [(name, {"description": ("s", label), "preferred_trigger": ("s", _trigger(key))})
                     for name, label, key in requested]
        result = self._request("BindShortcuts", "oa(sa{sv})sa{sv}", [session, shortcuts, "", {}],
                               keep_waiting, on_tick, APPROVAL_TIMEOUT)
        return self._triggers(result.get("shortcuts", ("a(sa{sv})", []))[1])

    @staticmethod
    def _triggers(shortcuts):
        return {name: props.get("trigger_description", ("s", ""))[1].strip() for name, props in shortcuts}

    def event(self, session, timeout):
        if self.pending_events:
            return self.pending_events.popleft()
        try:
            interface, member, path, body = self._signal(timeout)
        except TimeoutError:
            return None
        return self._session_event(session, interface, member, path, body)

    def _session_event(self, session, interface, member, path, body):
        if (interface == SESSION and path == session and member == "Closed") or member == "NameOwnerChanged":
            return "closed", None
        if interface == SHORTCUTS and body[0] == session:
            if member in ("Activated", "Deactivated"):
                return member, body[1]
            if member == "ShortcutsChanged":
                return "changed", self._triggers(body[1])
        return None

    def close_session(self, session):
        self.pending_events.clear()
        try:
            self._call(SESSION, "Close", path=session)
        except (self.dbus_error, OSError):
            pass


class PortalBackend:
    name = "portal"
    on_problem = staticmethod(lambda text: None)

    def probe(self):
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return "No desktop session bus is available for global shortcuts."
        try:
            with GlobalShortcuts() as portal:
                if portal.version() >= 1:
                    return ""
        except PortalCallError:
            pass
        except PortalUnavailable as e:
            return str(e)
        except OSError:
            pass
        return "The desktop does not offer the GlobalShortcuts portal."

    def run(self, detector, on_action, on_tick, *, stop, mode):
        actions = queue.Queue()
        next_tick = 0.0

        def tick():
            nonlocal next_tick
            now = time.monotonic()
            if now >= next_tick:
                next_tick = now + 5.0
                on_tick()

        def deliver():
            for action in iter(actions.get, None):
                try:
                    on_action(action)
                except Exception as e:
                    print(f"hold-dictate: {action} failed: {e!r}", file=sys.stderr, flush=True)

        worker = threading.Thread(target=deliver, daemon=True, name="portal hotkey actions")
        worker.start()
        try:
            with GlobalShortcuts() as portal:
                portal.register()
                self._listen(portal, detector, actions.put, tick, stop, mode)
        except OSError as e:
            raise PortalUnavailable(f"The desktop shortcut portal could not be reached ({e}).") from e
        finally:
            actions.put(None)
            worker.join()

    def _listen(self, portal, detector, emit, on_tick, stop, mode):
        assigns = desktop_assigns()

        def requested():
            wanted = [("dictation", "Dictate", detector.hotkey)]
            if detector.second is not None:
                wanted.append(("second", "Second dictation shortcut", detector.second))
            return [(shortcut_id(name, key, assigns), name, label, key) for name, label, key in wanted]

        while not stop.is_set():
            shortcuts = requested()
            ids = [ident for ident, _, _, _ in shortcuts]
            session = None
            listening = False

            def unchanged():
                return not stop.is_set() and ids == [ident for ident, _, _, _ in requested()]

            try:
                self.on_problem("Waiting for the desktop to assign shortcuts. If it asks, approve the request.")
                session = portal.create_session(unchanged, on_tick)
                if session and unchanged():
                    granted = portal.bind(session, [(ident, label, key) for ident, _, label, key in shortcuts], unchanged, on_tick)
                    if unchanged():
                        listening = True
                        self._events(portal, session, shortcuts, granted, detector, emit, on_tick, unchanged, mode, assigns)
            except PortalRefused as e:
                self.on_problem(str(e))
            finally:
                if session:
                    if listening:
                        emit("cancel")
                        detector.set_portal_shortcuts({})
                    portal.close_session(session)
            # Keep Settings available after refusal or closure. Saving another shortcut can ask again.
            while unchanged():
                if stop.wait(0.25):
                    break
                on_tick()

    def _events(self, portal, session, shortcuts, granted, detector, emit, on_tick, unchanged, mode, assigns):
        """A press with no release (Hyprland 0.56 loses one when the modifier comes up first) is not guessed at: the
        recording goes on until the next press and release, as a long hold would."""
        names = {ident: name for ident, name, _, _ in shortcuts}
        report = None
        notices, effective = self._bindings(shortcuts, granted, assigns)
        detector.set_portal_shortcuts(effective)
        listed = gnome_bindings()
        while unchanged():
            notice = " ".join(notices + self._kept(shortcuts, effective, listed))
            if notice != report:
                self.on_problem(notice)
                report = notice
            event = portal.event(session, 0.25)
            if event is None:
                on_tick()
                continue
            if not unchanged():
                break
            kind, value = event
            if kind == "closed":
                self.on_problem("The desktop closed the shortcut session. Change the shortcut or restart hold-dictate to reconnect.")
                break
            if kind == "changed":  # you gave it another key in the desktop's settings
                emit("cancel")
                notices, effective = self._bindings(shortcuts, value, assigns)
                detector.set_portal_shortcuts(effective)
                continue
            name = names.get(value)
            if name not in effective:
                continue
            pressed = kind == "Activated"
            action = detector.feed_shortcut(name, pressed)
            if not action:
                continue
            toggle = mode() == "toggle"
            if pressed:
                emit(action)
                if toggle:  # a tap: the release may come late or never, and Toggle does not need it
                    emit("second:stop" if name == "second" else "stop")
            elif not toggle:
                emit(action)

    @staticmethod
    def _kept(shortcuts, effective, listed):
        """A notice for each shortcut whose key GNOME uses itself."""
        notices = []
        for _, name, label, requested in shortcuts:
            key = effective.get(name) or (requested if name in effective else None)
            taken = kept_by_gnome(key, listed) if key is not None and listed else ""
            if taken:
                notices.append(f"GNOME uses {key} itself ({taken}), so it will not reach hold-dictate. "
                               f"Choose another shortcut for {label.lower()}, such as ctrl+alt+space.")
        return notices

    @staticmethod
    def _bindings(shortcuts, granted, assigns=False):
        """What the desktop gave each shortcut: notices for the settings page, and the keys by name."""
        notices, effective = [], {}
        for ident, name, label, requested in shortcuts:
            trigger = granted.get(ident)
            if assigns:  # no key to report: it is whatever the desktop's config binds
                effective[name] = _described_hotkey(trigger) if trigger else None
                notices.append(f"Bind a key to {LABEL}:{ident} in the desktop's config for {label.lower()}.")
                continue
            if not trigger:
                notices.append(f"The desktop did not assign a key for {label.lower()} ({requested}). Choose another shortcut in desktop settings or here.")
                continue
            actual = _described_hotkey(trigger)
            effective[name] = actual
            if actual is None or str(actual) != str(requested):
                notices.append(f"The desktop assigned {trigger} for {label.lower()} (requested {requested}).")
        return notices, effective
