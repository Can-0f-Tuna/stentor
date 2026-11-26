"""What edit by voice and the notice need beyond pasting: reading the selection and the clipboard without
losing what was on it, and bringing a window to the front."""
import json
import os
import shutil
import subprocess
import sys
import time

from stentor.paste import _has, _run, _wayland, _x11_copier, is_terminal


def _read_clipboard_bytes(primary: bool = False) -> bytes | None:
    """The clipboard (or on Linux the PRIMARY selection: the text selected right now) as UTF-8, or None."""
    try:
        if sys.platform == "darwin":
            r = _run(["pbpaste"])
            return r.stdout if r.returncode == 0 else None
        if sys.platform == "win32":
            import pyperclip
            return (pyperclip.paste() or "").encode("utf-8")
        if _wayland():
            if not _has("wl-paste"):
                return None
            r = _run(["wl-paste", "-n", "-t", "text"] + (["--primary"] if primary else []))
            return r.stdout if r.returncode == 0 else b""  # an empty clipboard exits 1
        if _has("xclip"):
            cmd = ["xclip", "-selection", "primary" if primary else "clipboard", "-o"]
        elif _has("xsel"):
            cmd = ["xsel", "-p" if primary else "-b", "-o"]
        else:
            return None
        r = _run(cmd)
        return r.stdout if r.returncode == 0 else b""
    except Exception:
        return None


def clipboard_text() -> str | None:
    """What is on the clipboard now, as text; None when it can't be read here."""
    raw = _read_clipboard_bytes()
    return None if raw is None else raw.decode("utf-8", "replace")


def _write_clipboard_bytes(data: bytes | None):
    """Puts bytes back on the clipboard; None or empty clears it."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=data or b"", timeout=2)
        elif sys.platform == "win32":
            import pyperclip
            pyperclip.copy((data or b"").decode("utf-8", "replace"))
        elif _wayland():
            if data:
                subprocess.run(["wl-copy"], input=data, timeout=2)
            else:
                subprocess.run(["wl-copy", "--clear"], timeout=2)
        else:
            tool, _ = _x11_copier()
            if tool:
                subprocess.run(tool, input=data or b"", timeout=2)
    except Exception:
        pass


def _send_copy(terminal: bool) -> bool:
    """Presses the copy shortcut in the focused app: Cmd+C, Ctrl+C, or Ctrl+Shift+C in a Linux terminal."""
    try:
        if sys.platform in ("darwin", "win32"):
            from pynput.keyboard import Controller, Key
            kb = Controller()
            with kb.pressed(Key.cmd if sys.platform == "darwin" else Key.ctrl):
                kb.tap("c")
            return True
        if _wayland():
            if _has("wtype"):
                keys = ["-M", "ctrl"] + (["-M", "shift"] if terminal else []) + ["c"] + (["-m", "shift"] if terminal else []) + ["-m", "ctrl"]
                if _run(["wtype", *keys]).returncode == 0:
                    return True
            if _has("ydotool"):
                seq = ["29:1"] + (["42:1"] if terminal else []) + ["46:1", "46:0"] + (["42:0"] if terminal else []) + ["29:0"]
                return _run(["ydotool", "key", *seq]).returncode == 0
            return False
        if _has("xdotool"):
            return _run(["xdotool", "key", "--clearmodifiers", "ctrl+shift+c" if terminal else "ctrl+c"]).returncode == 0
        from pynput.keyboard import Controller, Key
        kb = Controller()
        with kb.pressed(Key.ctrl):
            if terminal:
                with kb.pressed(Key.shift):
                    kb.tap("c")
            else:
                kb.tap("c")
        return True
    except Exception:
        return False


def grab_selection(app: str) -> tuple[str, object]:
    """The text selected in the focused app, and a function that puts the clipboard back as it was.

    Linux, when the focused app is unknown (GNOME or KDE on Wayland) or no key tool is installed: the PRIMARY
    selection, which needs no key press (a Ctrl+C sent to a terminal by mistake would interrupt what runs
    in it). Everywhere else: the clipboard is saved and cleared, the copy shortcut is pressed, and what
    lands on the clipboard is the selection; nothing there means nothing was selected."""
    linux = sys.platform.startswith("linux")
    can_press = not linux or not _wayland() or _has("wtype") or _has("ydotool")  # X11 falls back to pynput
    if linux and (not app or not can_press):
        raw = _read_clipboard_bytes(primary=True)
        return (raw or b"").decode("utf-8", "replace"), lambda: None
    saved = _read_clipboard_bytes()
    _write_clipboard_bytes(None)
    restore = lambda: _write_clipboard_bytes(saved)
    if not _send_copy(linux and is_terminal(app)):
        restore()
        return "", lambda: None
    deadline = time.monotonic() + 0.6
    while True:  # the app copies on its own time
        time.sleep(0.05)
        raw = _read_clipboard_bytes()
        if raw or time.monotonic() >= deadline:
            break
    return (raw or b"").decode("utf-8", "replace"), restore


def _raise_window(title: str) -> bool:
    """Focuses the window with this text in its title, switching to its workspace. False if it isn't there."""
    title = title.lower()
    if _wayland():
        if os.environ.get("NIRI_SOCKET"):
            for w in json.loads(_run(["niri", "msg", "-j", "windows"], text=True).stdout):
                if title in (w.get("title") or "").lower():
                    return _run(["niri", "msg", "action", "focus-window", "--id", str(w["id"])]).returncode == 0
        elif os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            for w in json.loads(_run(["hyprctl", "clients", "-j"], text=True).stdout):
                if title in (w.get("title") or "").lower():
                    return _run(["hyprctl", "dispatch", "focuswindow", f"address:{w['address']}"]).returncode == 0
        elif os.environ.get("SWAYSOCK"):
            stack = [json.loads(_run(["swaymsg", "-t", "get_tree"], text=True).stdout)]
            while stack:
                node = stack.pop()
                if node.get("pid") and title in (node.get("name") or "").lower():
                    return _run(["swaymsg", f"[con_id={node['id']}]", "focus"]).returncode == 0
                stack += node.get("nodes", []) + node.get("floating_nodes", [])
    elif shutil.which("xdotool"):
        return _run(["xdotool", "search", "--name", title, "windowactivate"]).returncode == 0
    return False


def raise_window(title: str, wait: float = 6.0) -> bool:
    """Brings a window that is just opening to the front, on the desktops that let us. A browser asked to open
    a page in the background stays where it is on Wayland. Windows and macOS bring it forward by themselves."""
    if not sys.platform.startswith("linux"):
        return False
    deadline = time.monotonic() + wait
    while True:
        try:
            if _raise_window(title):
                return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.15)
