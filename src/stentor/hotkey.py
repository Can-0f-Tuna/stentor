"""Hold-to-talk hotkey detection.

HoldDetector accepts raw key events or whole-shortcut events. Backends supply the events:
  - PortalBackend uses the desktop's GlobalShortcuts portal on Wayland.
  - EvdevBackend reads /dev/input directly: works on Wayland and X11, needs the `input` group.
  - PynputBackend uses the OS keyboard hooks on Windows and macOS (and Linux X11).
"""
import os
import queue
import select
import struct
import sys
import threading
import time
from pathlib import Path

from .config import env

MODIFIERS = ("alt", "ctrl", "shift", "super")
PORTAL_RELEASE_SETTLE = 0.25  # the desktop may report the chord up before its physical modifiers
ALIASES = {
    "option": "alt", "opt": "alt", "control": "ctrl", "cmd": "super", "command": "super",
    "win": "super", "windows": "super", "meta": "super",
}


class Hotkey:
    def __init__(self, mods: frozenset, key: str):
        self.mods, self.key = mods, key

    def __str__(self):
        return "+".join([m for m in MODIFIERS if m in self.mods] + [self.key])


def parse_hotkey(text: str) -> Hotkey:
    parts = [ALIASES.get(p.strip().lower(), p.strip().lower()) for p in text.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    *mods, key = parts
    for m in mods:
        if m not in MODIFIERS:
            raise ValueError(f"'{m}' is not a modifier (use alt, ctrl, shift, super)")
    if key in MODIFIERS:
        raise ValueError("the hotkey needs a normal key last, for example alt+space")
    valid = key == "space" or (len(key) == 1 and key.isalnum()) or (key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12)
    if not valid:
        raise ValueError(f"unsupported key '{key}' (use space, a-z, 0-9 or f1-f12)")
    if not mods and not key.startswith("f"):
        raise ValueError("a plain key would trigger while typing; add a modifier such as alt+")
    return Hotkey(frozenset(mods), key)


def parse_paste_last_hotkey(text: str, hotkey: Hotkey, second: Hotkey | None) -> Hotkey | None:
    if not isinstance(text, str):
        raise ValueError("The paste-last shortcut must be text, for example ctrl+alt+v.")
    if not text.strip():
        return None
    try:
        key = parse_hotkey(text)
    except ValueError as e:
        raise ValueError(f"Invalid paste-last shortcut: {e}") from e
    for other, label in ((hotkey, "dictation"), (second, "second dictation")):
        if str(key) == str(other):
            raise ValueError(f"The paste-last shortcut must differ from the {label} shortcut ({other}).")
    return key


def desktop_keeps(hotkey: Hotkey) -> str:
    """What the Linux desktop does with this key itself, or "": GNOME, Xfce, Cinnamon and MATE open the window menu
    on Alt+Space and KDE opens KRunner, and Stentor can't keep the key from them as it does on Windows and macOS."""
    if not sys.platform.startswith("linux"):
        return ""
    import re
    import shutil
    import subprocess
    wanted = (frozenset(hotkey.mods), hotkey.key)
    names = {"control": "ctrl", "primary": "ctrl", "ctrl": "ctrl", "alt": "alt", "shift": "shift", "super": "super",
             "meta": "super", "mod4": "super"}

    def same(mods, key) -> bool:
        return (frozenset(names.get(m.lower(), m.lower()) for m in mods), key.lower()) == wanted
    try:
        from .portal_hotkey import gnome_bindings, kept_by_gnome
        listed = gnome_bindings()
        if listed and (taken := kept_by_gnome(hotkey, listed)):
            return f"GNOME's {taken}"
    except Exception:
        pass
    if shutil.which("xfconf-query"):  # Xfce: "/xfwm4/custom/<Alt>space   popup_menu_key"
        try:
            out = subprocess.run(["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-l", "-v"], capture_output=True,
                                 text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and "/custom/" in parts[0] and parts[1].strip() not in ("", "empty"):
                accel = parts[0].rsplit("/", 1)[-1]
                if same(re.findall(r"<([^>]+)>", accel), re.sub(r"<[^>]+>", "", accel)):
                    return f"Xfce's {parts[1].strip()}"
    kde = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kglobalshortcutsrc"
    try:  # KDE: "_launch=Alt+Space\\tAlt+F2\\tSearch,Alt+Space\\tAlt+F2\\tSearch,KRunner"
        for line in kde.read_text(encoding="utf-8", errors="replace").splitlines():
            name, _, value = line.partition("=")
            fields = value.split(",")
            for accel in fields[0].split("\\t") if fields else []:
                *mods, key = accel.split("+") if accel and accel != "none" else ["", ""]
                if key and same(mods, key):
                    return f"KDE's {fields[-1] or name}"
    except OSError:
        pass
    return ""


class HoldDetector:
    """Turns raw key events into actions. Letting go of any part of the hotkey stops.

    The dictation hotkey gives 'start' and 'stop'; the optional second hotkey gives 'second:start' and
    'second:stop'. The paste-last hotkey gives 'paste-last' once per press. When shortcuts match the
    same keys held (alt+space inside alt+shift+space), the one with more modifiers wins."""

    def __init__(self, hotkey: Hotkey, second: Hotkey | None = None, *, paste_last: Hotkey | None = None):
        self.hotkey = hotkey
        self.second = second
        self.paste_last = paste_last
        self.down: dict[str, set] = {}  # name -> physical key ids currently held
        self.active = False
        self.which = ""  # "" for the dictation hotkey, "second" for the second hotkey, while active
        self._portal_keys: dict[str, Hotkey | None] = {}
        self._portal_down: set[str] = set()
        self._settled_at = 0.0
        self._portal_state = threading.Condition()

    def hotkeys(self) -> list:
        return [h for h in (self.hotkey, self.second, self.paste_last) if h is not None]

    def _stop_if(self, which: str) -> str | None:
        if self.active and self.which == which:
            self.active = False
            return "second:stop" if which else "stop"
        return None

    def set_hotkey(self, hotkey: Hotkey) -> str | None:
        """Switches the hotkey while running. Returns 'stop' if a recording with it was in progress."""
        action, self.hotkey = self._stop_if(""), hotkey
        return action

    def set_second_hotkey(self, hotkey: Hotkey | None) -> str | None:
        """Switches (or with None, turns off) the second hotkey. Returns 'second:stop' if it was held."""
        action, self.second = self._stop_if("second"), hotkey
        return action

    def is_down(self, name: str) -> bool:
        with self._portal_state:
            return bool(self.down.get(name)) or any(
                hotkey is not None and (name == hotkey.key or name in hotkey.mods)
                for shortcut in self._portal_down
                for hotkey in (self._portal_keys[shortcut],)
            )

    def modifiers_held(self) -> bool:
        with self._portal_state:
            if self._portal_down:
                return self._portal_modifiers_held()
        return any(self.is_down(m) for m in self.hotkey.mods)

    def any_modifier_held(self) -> bool:
        with self._portal_state:
            if self._portal_modifiers_held():
                return True
        return any(self.is_down(m) for m in MODIFIERS)

    def _portal_modifiers_held(self) -> bool:
        # An opaque desktop description gives us no way to rule out modifiers.
        return any(hotkey is None or hotkey.mods for shortcut in self._portal_down
                   for hotkey in (self._portal_keys[shortcut],))

    def set_portal_shortcuts(self, shortcuts: dict[str, Hotkey | None]):
        with self._portal_state:
            if self._portal_down:
                self._settled_at = time.monotonic() + PORTAL_RELEASE_SETTLE
            self._portal_keys = shortcuts
            self._portal_down.clear()
            self.active = False
            self._portal_state.notify_all()

    def feed_shortcut(self, shortcut: str, pressed: bool) -> str | None:
        """Use the desktop's binding decision. Two overlapping shortcuts still start only one recording."""
        with self._portal_state:
            if shortcut not in self._portal_keys:
                return None
            which = "" if shortcut == "dictation" else "second"
            if pressed:
                if shortcut in self._portal_down:
                    return None
                self._portal_down.add(shortcut)
                if self.active:
                    return None
                if shortcut == "paste":
                    return "paste-last"
                self.active, self.which = True, which
                return "second:start" if which else "start"
            if shortcut not in self._portal_down:
                return None
            self._portal_down.remove(shortcut)
            # A portal release can precede the release of the physical modifiers.
            self._settled_at = time.monotonic() + PORTAL_RELEASE_SETTLE
            self._portal_state.notify_all()
            return None if shortcut == "paste" else self._stop_if(which)

    def portal_paste_ready(self) -> bool:
        with self._portal_state:
            return not self._portal_down and time.monotonic() >= self._settled_at

    def wait_released(self, seconds: float = 1.0) -> bool:
        """Waits for raw modifiers or for the desktop shortcut to release and settle."""
        deadline = time.monotonic() + seconds
        with self._portal_state:
            while not self.portal_paste_ready():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                delay = left if self._portal_down else self._settled_at - time.monotonic()
                self._portal_state.wait(min(left, delay))
        while self.any_modifier_held():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True

    def feed(self, name: str, ident, pressed: bool):
        """Returns a recording action, 'paste-last' or None."""
        ids = self.down.setdefault(name, set())
        if pressed:
            repeated = ident in ids
            ids.add(ident)
            if self.active:
                return None
            shortcuts = (("", self.hotkey), ("second", self.second), ("paste-last", self.paste_last))
            matches = [(len(h.mods), which) for which, h in shortcuts
                       if h is not None and name == h.key and all(self.is_down(m) for m in h.mods)]
            if matches:
                which = max(matches, key=lambda m: m[0])[1]  # a tie: dictation first
                if which == "paste-last":
                    return None if repeated else which
                self.active, self.which = True, which
                return "second:start" if self.which else "start"
        else:
            ids.discard(ident)
            current = self.second if self.which else self.hotkey
            if self.active and current is not None and (name == current.key or name in current.mods):
                self.active = False
                return "second:stop" if self.which else "stop"
        return None


# ---------- Linux evdev ----------

EV_FORMAT = "llHHi"
EV_SIZE = struct.calcsize(EV_FORMAT)
EV_KEY = 1
_LETTER_ROWS = {"q": 16, "a": 30, "z": 44}
EVDEV_CODES = {
    "alt": (56, 100), "ctrl": (29, 97), "shift": (42, 54), "super": (125, 126), "space": (57,),
}
for _row, _start in _LETTER_ROWS.items():
    _letters = {"q": "qwertyuiop", "a": "asdfghjkl", "z": "zxcvbnm"}[_row]
    for _i, _ch in enumerate(_letters):
        EVDEV_CODES[_ch] = (_start + _i,)
for _i, _ch in enumerate("1234567890"):
    EVDEV_CODES[_ch] = (2 + _i,)
for _i in range(1, 11):
    EVDEV_CODES[f"f{_i}"] = (58 + _i,)
EVDEV_CODES["f11"], EVDEV_CODES["f12"] = (87,), (88,)
CODE_TO_NAME = {code: name for name, codes in EVDEV_CODES.items() for code in codes}


def keyboard_devices() -> list[str]:
    paths = []
    try:
        blocks = open("/proc/bus/input/devices").read().split("\n\n")
    except OSError:
        return paths
    for block in blocks:
        for line in block.splitlines():
            if line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1].split()
                if "kbd" in handlers:
                    paths += ["/dev/input/" + h for h in handlers if h.startswith("event")]
    return paths


class EvdevBackend:
    name = "evdev"

    def __init__(self, devices=None):
        self.devices, self.unreadable = [], []
        self.paths = {}  # open device -> its path, for the keyboards found on this computer
        self.watch = devices is None  # keyboards come and go (Bluetooth, USB): look again while running
        if devices is not None:  # tests pass file objects
            self.devices = devices
            return
        self.rescan()

    def rescan(self):
        """Opens the keyboards that aren't open yet: at the start, and one that was plugged in or came back."""
        self.unreadable = []
        for path in keyboard_devices():
            if path in self.paths.values():
                continue
            try:
                dev = open(path, "rb", buffering=0)
            except PermissionError:
                self.unreadable.append(path)
            except OSError:
                continue  # it left again between the listing and the open
            else:
                self.devices.append(dev)
                self.paths[dev] = path

    def _drop(self, dev):
        self.devices.remove(dev)
        self.paths.pop(dev, None)
        try:
            dev.close()
        except OSError:
            pass

    def usable(self) -> bool:
        return bool(self.devices)

    def run(self, detector: HoldDetector, on_action, on_tick=None, tick_seconds: float = 5.0):
        scanned = time.monotonic()
        while self.devices or self.watch:
            ready, _, _ = select.select(self.devices, [], [], tick_seconds if on_tick or self.watch else None)
            if self.watch and time.monotonic() - scanned >= tick_seconds:  # not at every key: it reads a file
                scanned = time.monotonic()
                self.rescan()
            if not ready:
                if on_tick:
                    on_tick()
                continue
            for dev in ready:
                try:
                    raw = dev.read(EV_SIZE)
                except OSError:  # ENODEV: a headset or keyboard turned off; the others go on, and it is reopened when it returns
                    raw = b""
                if len(raw) < EV_SIZE:  # device went away (or a test pipe closed)
                    self._drop(dev) if dev in self.paths else self.devices.remove(dev)
                    continue
                _, _, ev_type, code, value = struct.unpack(EV_FORMAT, raw)
                name = CODE_TO_NAME.get(code)
                if ev_type != EV_KEY or value == 2 or name is None:
                    continue
                action = detector.feed(name, code, value == 1)
                if action:
                    on_action(action)
            if on_tick:
                on_tick()


# ---------- Windows / macOS / X11 via pynput ----------

_WIN_VK = {"alt": (0x12, 0xA4, 0xA5), "ctrl": (0x11, 0xA2, 0xA3), "shift": (0x10, 0xA0, 0xA1), "super": (0x5B, 0x5C)}
_MAC_FLAGS = {"alt": 0x80000, "ctrl": 0x40000, "shift": 0x20000, "super": 0x100000}


def _pynput_name(key) -> str | None:
    from pynput import keyboard
    special = {
        "alt": "alt", "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
        "ctrl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
        "shift": "shift", "shift_l": "shift", "shift_r": "shift",
        "cmd": "super", "cmd_l": "super", "cmd_r": "super", "space": "space",
    }
    if isinstance(key, keyboard.Key):
        return special.get(key.name) or (key.name if key.name.startswith("f") and key.name[1:].isdigit() else None)
    return _key_name(getattr(key, "vk", None), getattr(key, "char", None))


# macOS's virtual key codes of the letters and digits (kVK_ANSI_*), by place on the keyboard
_MAC_KEYS = dict(zip([0, 11, 8, 2, 14, 3, 5, 4, 34, 38, 40, 37, 46, 45, 31, 35, 12, 15, 1, 17, 32, 9, 13, 7, 16, 6],
                     "abcdefghijklmnopqrstuvwxyz")) | dict(zip([29, 18, 19, 20, 21, 23, 22, 26, 28, 25], "0123456789"))


def _key_name(vk, char, platform: str = sys.platform) -> str | None:
    """A letter or digit key by the key itself, not what it types: with Option held V types "√" on macOS, with
    Ctrl held it types a control character, and in the Hebrew layout it types "ה", so ctrl+alt+v never matched."""
    if isinstance(vk, int):
        if platform == "darwin" and vk in _MAC_KEYS:
            return _MAC_KEYS[vk]
        if platform == "win32" and (0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39):
            return chr(vk).lower()
    return char.lower() if char else None


class PynputBackend:
    name = "pynput"
    on_problem = None  # macOS: called with what to turn on while the keyboard can't be read, and '' once it can

    def usable(self, quick: bool = False) -> bool:
        """quick: only look for pynput. Importing it loads Quartz on macOS, a second warm and far more cold."""
        if quick:
            import importlib.util
            return importlib.util.find_spec("pynput") is not None
        try:
            import pynput.keyboard  # noqa: F401
            return True
        except Exception:
            return False

    def run(self, detector: HoldDetector, on_action, on_tick=None, tick_seconds: float = 5.0):
        from pynput import keyboard

        win_state: set = set()
        actions: queue.Queue = queue.Queue()

        def deliver():
            # The OS calls the hooks below with the keyboard waiting on them: Windows drops a slow low-level hook,
            # and macOS turns off a tap that takes too long (a first recording waits for the Microphone prompt).
            # So they only note the key; what it starts runs here.
            while True:
                action = actions.get()
                try:
                    on_action(action)
                except Exception as e:
                    print(f"stentor: {action} failed: {e!r}", file=sys.stderr, flush=True)

        threading.Thread(target=deliver, daemon=True, name="hotkey actions").start()

        def handle(key, pressed):
            name = _pynput_name(key)
            if name is None:
                return
            action = detector.feed(name, name, pressed)
            if action:
                actions.put(action)

        def win_filter(msg, data):
            # Alt+Space opens the window menu on Windows; hide the Space press while dictating.
            vk, down = data.vkCode, msg in (0x100, 0x104)
            for mod, codes in _WIN_VK.items():
                if vk in codes:
                    (win_state.add if down else win_state.discard)(mod)
            # read each time: the settings page can change the hotkeys while running
            # and a shortcut's letter (paste-last's V) would reach the app too
            name = "space" if vk == 0x20 else _key_name(vk, None, "win32")
            if name and any(h.key == name and h.mods and h.mods <= win_state for h in detector.hotkeys()):
                listener.suppress_event()
            return True

        def mac_intercept(event_type, event):
            # Option+Space types a non-breaking space on macOS; swallow Space while dictating, and a shortcut's
            # letter (paste-last's V), which the app in front would get too.
            try:
                import Quartz
                if event_type in _MAC_TAP_OFF:
                    _mac_enable_tap(listener, "it was turned off" if event_type == _MAC_TAP_OFF[1] else "it was too slow")
                    return event
                code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                flags = Quartz.CGEventGetFlags(event)
                name = "space" if code == 49 else _MAC_KEYS.get(code)
                if name and any(h.key == name and h.mods and all(flags & _MAC_FLAGS[m] for m in h.mods) for h in detector.hotkeys()):
                    return None
            except Exception:
                pass
            return event

        extra, listener_class = {}, keyboard.Listener
        if sys.platform == "win32":
            extra["win32_event_filter"] = win_filter
        elif sys.platform == "darwin":
            extra["darwin_intercept"] = mac_intercept
            listener_class = _mac_listener_class(keyboard.Listener)
        while True:
            if sys.platform == "darwin":
                self._mac_wait_for_access(on_tick)
            listener = listener_class(
                on_press=lambda k: handle(k, True), on_release=lambda k: handle(k, False), **extra
            )
            listener.start()
            while listener.is_alive():
                time.sleep(min(tick_seconds, 1.0))
                if sys.platform == "darwin":
                    _mac_check_tap(listener)
                if on_tick:
                    on_tick()
            if sys.platform != "darwin" or _mac_trusted():
                return  # gone for good: the caller exits with an error, and a login service starts it again
            # macOS: Accessibility was switched off while running; wait for it to come back

    def _mac_wait_for_access(self, on_tick):
        """Without Accessibility pynput's tap can't start. Asks once, says what to turn on, and goes on as soon
        as it is on: a running app used to ignore the switch until a restart."""
        from . import macos
        if _mac_trusted():
            return
        macos.ask_accessibility()
        print(f"stentor: {macos.accessibility_problem().replace(chr(10), ' ')}", file=sys.stderr, flush=True)
        if self.on_problem:
            self.on_problem(macos.accessibility_problem())
        while not _mac_trusted():
            time.sleep(1)
            if on_tick:
                on_tick()
        print("stentor: Accessibility is on; the shortcut works now", flush=True)
        if self.on_problem:
            self.on_problem("")


# kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput: macOS tells the tap it was switched off this way
_MAC_TAP_OFF = (0xFFFFFFFE, 0xFFFFFFFF)


def _mac_trusted() -> bool:
    from . import macos
    try:
        return macos.accessibility_trusted()
    except Exception:
        return True  # can't tell: let pynput try


def _mac_listener_class(base):
    """pynput's listener, keeping its event tap where we can switch it back on: pynput never does."""
    class Listener(base):
        tap = None

        def _create_event_tap(self):
            self.tap = super()._create_event_tap()
            return self.tap
    return Listener


def _mac_enable_tap(listener, why: str):
    import Quartz
    if listener.tap is not None:
        Quartz.CGEventTapEnable(listener.tap, True)
        print(f"stentor: macOS turned the keyboard tap off ({why}); it is back on", file=sys.stderr, flush=True)


def _mac_check_tap(listener):
    """The tap's own notice can be missed; once a second, a tap that is off is switched back on."""
    try:
        import Quartz
        if listener.tap is not None and not Quartz.CGEventTapIsEnabled(listener.tap):
            _mac_enable_tap(listener, "found off")
    except Exception:
        pass


def choose_backend(quick: bool = False, *, skip_portal: bool = False):
    """Returns (backend or None, problem text). The doctor probe doesn't request a shortcut.

    On Linux the keyboard itself comes first where it can be read: it works on every desktop and it sees the real
    press and release. The desktop's shortcut portal is for a Wayland session without that access, such as before
    the first log-out after the install. STENTOR_HOTKEY_BACKEND (auto, evdev, portal, pynput) picks one."""
    if sys.platform.startswith("linux"):
        forced = (env("HOTKEY_BACKEND") or "auto").strip().lower() or "auto"
        if forced not in ("auto", "portal", "evdev", "pynput"):
            return None, "Set STENTOR_HOTKEY_BACKEND to auto, portal, evdev or pynput."
        if forced in ("auto", "evdev"):
            backend = EvdevBackend()
            if backend.usable():
                return backend, ""
        wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE") == "wayland"
        portal_problem = ""
        if not skip_portal and (forced == "portal" or (forced == "auto" and wayland)):
            from .portal_hotkey import PortalBackend
            backend = PortalBackend()
            portal_problem = backend.probe()
            if not portal_problem:
                return backend, ""
        if forced == "portal":
            return None, portal_problem
        if forced == "pynput" or (forced == "auto" and os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")):
            backend = PynputBackend()
            if backend.usable(quick):
                return backend, ""
            if forced == "pynput":
                return None, "The pynput keyboard backend needs a working X11 display and stentor[x11]."
        return None, (
            "cannot read the keyboard. Add your user to the 'input' group, then log out and back in:\n"
            "    sudo usermod -aG input $USER\n"
            "Or bind a key in your desktop's keyboard settings to `stentor dictation toggle`."
        )
    backend = PynputBackend()
    if backend.usable(quick):
        return backend, ""
    return None, "the 'pynput' package is not available"
