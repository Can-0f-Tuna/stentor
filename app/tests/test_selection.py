import shutil
import sys

import pytest

from stentor_pro import selection

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux paste helpers")


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A fake Wayland clipboard: wl-copy and wl-paste keep it in a file; wtype's Ctrl+C copies `selection`."""
    clip, primary, picked, log = (tmp_path / n for n in ("clip", "primary", "selection", "log"))
    scripts = {
        "wl-copy": f'echo "wl-copy $*" >> "{log}"\nif [ "$1" = --clear ]; then : > "{clip}"; elif [ "$1" = -- ]; then printf %s "$2" > "{clip}"; else cat > "{clip}"; fi',
        "wl-paste": f'if [ "$4" = --primary ]; then f="{primary}"; else f="{clip}"; fi\n[ -s "$f" ] || exit 1\ncat "$f"',
        "wtype": f'echo "wtype $*" >> "{log}"\ncase "$*" in *" c "*) cp "{picked}" "{clip}";; esac',
    }
    for name, body in scripts.items():
        (tmp_path / name).write_text("#!/bin/sh\n" + body + "\n")
        (tmp_path / name).chmod(0o755)
    for f in (clip, primary, picked):
        f.write_text("")
    for tool in ("cat", "cp"):
        (tmp_path / tool).symlink_to(shutil.which(tool))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    return clip, primary, picked, log


def test_grab_selection_copies_then_restores_the_clipboard(board):
    clip, primary, picked, log = board
    clip.write_text("what you had copied")
    picked.write_text("the selected words")
    text, restore = selection.grab_selection("firefox")
    assert text == "the selected words"
    assert "wtype -M ctrl c -m ctrl" in log.read_text()
    restore()
    assert clip.read_text() == "what you had copied"
    assert selection.clipboard_text() == "what you had copied"


def test_grab_selection_nothing_selected_and_terminals(board, monkeypatch):
    clip, primary, picked, log = board
    monkeypatch.setattr(selection.time, "sleep", lambda s: None)
    clip.write_text("kept")
    text, restore = selection.grab_selection("kitty")  # a terminal copies with Ctrl+Shift+C
    assert text == "" and "wtype -M ctrl -M shift c -m shift -m ctrl" in log.read_text()
    restore()
    assert clip.read_text() == "kept"


def test_grab_selection_unknown_app_reads_the_primary_selection(board):
    clip, primary, picked, log = board
    primary.write_text("selected in a terminal")
    clip.write_text("kept")
    text, restore = selection.grab_selection("")  # GNOME or KDE on Wayland: no Ctrl+C that could stop a program
    restore()
    assert text == "selected in a terminal" and not log.exists() and clip.read_text() == "kept"


@pytest.mark.parametrize("desktop, listing, expected", [
    ("NIRI_SOCKET", '[{"id": 7, "title": "Stentor settings - Firefox"}]', ["niri", "msg", "action", "focus-window", "--id", "7"]),
    ("HYPRLAND_INSTANCE_SIGNATURE", '[{"address": "0xab", "title": "Stentor Settings"}]', ["hyprctl", "dispatch", "focuswindow", "address:0xab"]),
    ("SWAYSOCK", '{"nodes": [{"id": 4, "pid": 9, "name": "Stentor settings", "nodes": []}]}', ["swaymsg", "[con_id=4]", "focus"]),
])
def test_raise_window_finds_the_browser_by_its_title(monkeypatch, desktop, listing, expected):
    calls = []
    for name in ("NIRI_SOCKET", "HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(desktop, "x")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setattr(selection.sys, "platform", "linux")
    monkeypatch.setattr(selection, "_run", lambda cmd, **kw: (calls.append(cmd), type("R", (), {"stdout": listing, "returncode": 0})())[1])
    assert selection.raise_window("Stentor settings")
    assert calls[-1] == expected


def test_raise_window_gives_up_when_the_window_never_comes(monkeypatch):
    monkeypatch.setenv("NIRI_SOCKET", "x")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setattr(selection.sys, "platform", "linux")
    monkeypatch.setattr(selection, "_run", lambda cmd, **kw: type("R", (), {"stdout": "[]", "returncode": 0})())
    assert not selection.raise_window("Stentor settings", wait=0)
