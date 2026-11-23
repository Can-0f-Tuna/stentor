"""Start Stentor at login: systemd (Linux), a LaunchAgent (macOS), the Run key (Windows)."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import config_dir

LABEL = "io.github.stentor"


def _command() -> list[str]:
    """How to launch the daemon: this Python, so it works whichever way the tool was installed."""
    python = sys.executable
    if sys.platform == "win32":  # pythonw has no console window
        pythonw = Path(python).with_name("pythonw.exe")
        python = str(pythonw if pythonw.exists() else python)
    return [python, "-m", "stentor", "run"]


def _linux_unit() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / "stentor.service"


def _linux_desktop() -> Path:
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data / "applications" / "stentor-settings.desktop"


def _write_linux_desktop():
    """An app-menu entry that opens the settings page (Linux has no tray icon here)."""
    entry = _linux_desktop()
    entry.parent.mkdir(parents=True, exist_ok=True)
    python = sys.executable
    entry.write_text(
        "[Desktop Entry]\nType=Application\nName=Stentor settings\n"
        "Comment=API key, language, hotkey and look of Stentor\n"
        f"Exec={python} -m stentor settings --wait 5\nIcon=audio-input-microphone\n"
        "Terminal=false\nCategories=Utility;Settings;\n"
    )


def ensure_portal_desktop_entry():
    """Host-portal registration needs a desktop entry even for a run from the terminal."""
    entry = _linux_desktop().with_name(f"{LABEL}.desktop")
    system_dirs = (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    if any((Path(data) / "applications" / entry.name).is_file() for data in system_dirs if data):
        return
    entry.parent.mkdir(parents=True, exist_ok=True)
    python = sys.executable.replace("\\", "\\\\\\\\").replace("%", "%%")
    for char in ('"', '`', '$'):
        python = python.replace(char, "\\\\" + char)
    try:
        with entry.open("x", encoding="utf-8") as file:
            file.write("[Desktop Entry]\nType=Application\nName=Stentor\nNoDisplay=true\n"
                       f'Exec="{python}" -m stentor settings\nIcon=audio-input-microphone\n')
    except FileExistsError:
        pass


def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _sh(*cmd) -> int:
    return subprocess.run(list(cmd), capture_output=True).returncode


def install() -> str:
    cmd = _command()
    if sys.platform == "win32":
        import winreg
        line = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.SetValueEx(key, "stentor", 0, winreg.REG_SZ, line)
        flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
        subprocess.Popen(cmd, creationflags=flags, close_fds=True)
        return "Will start at every login, and is running now."
    if sys.platform == "darwin":
        log = config_dir() / "stentor.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"<string>{c}</string>" for c in cmd)
        plist = _mac_plist()
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            f'<plist version="1.0"><dict><key>Label</key><string>{LABEL}</string>'
            f"<key>ProgramArguments</key><array>{args}</array>"
            "<key>RunAtLoad</key><true/>"
            # Restart after a crash, but not after Quit from the menu bar.
            "<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>"
            f"<key>StandardOutPath</key><string>{log}</string><key>StandardErrorPath</key><string>{log}</string>"
            "</dict></plist>\n"
        )
        domain = f"gui/{os.getuid()}"
        _sh("launchctl", "bootout", domain, str(plist))
        if _sh("launchctl", "bootstrap", domain, str(plist)) != 0:
            return f"Wrote {plist}. Log out and back in to start it."
        return "Will start at every login, and is running now."
    _write_linux_desktop()
    if not shutil.which("systemctl"):
        return "systemd was not found. Start `stentor run` from your session's autostart instead."
    unit = _linux_unit()
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\nDescription=Stentor: hold a hotkey to dictate\n"
        "After=graphical-session.target\nPartOf=graphical-session.target\n\n"
        f"[Service]\nExecStart={' '.join(cmd)}\nRestart=on-failure\nRestartSec=5\n\n"
        "[Install]\nWantedBy=graphical-session.target\n"
    )
    _sh("systemctl", "--user", "daemon-reload")
    # restart, not just start: after a reinstall the running copy must pick up the new version
    if _sh("systemctl", "--user", "enable", "stentor.service") != 0 or _sh("systemctl", "--user", "restart", "stentor.service") != 0:
        return f"Wrote {unit}, but could not start it. Try: systemctl --user enable --now stentor"
    return "Will start at every login, and is running now."


def uninstall() -> str:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "stentor")
        except FileNotFoundError:
            pass
        subprocess.run(["taskkill", "/F", "/IM", "pythonw.exe", "/FI", "WINDOWTITLE eq stentor*"], capture_output=True)
        return "Autostart removed. A running copy stops at the next logout."
    if sys.platform == "darwin":
        plist = _mac_plist()
        _sh("launchctl", "bootout", f"gui/{os.getuid()}", str(plist))
        plist.unlink(missing_ok=True)
        return "Autostart removed and stopped."
    _sh("systemctl", "--user", "disable", "--now", "stentor.service")
    _linux_unit().unlink(missing_ok=True)
    _linux_desktop().unlink(missing_ok=True)
    _sh("systemctl", "--user", "daemon-reload")
    return "Autostart removed and stopped."


def status() -> str:
    if sys.platform == "win32":
        return "Windows: see Task Manager for pythonw.exe; autostart is in HKCU\\...\\Run."
    if sys.platform == "darwin":
        return "running" if _sh("launchctl", "print", f"gui/{os.getuid()}/{LABEL}") == 0 else "not running"
    return "running" if _sh("systemctl", "--user", "is-active", "--quiet", "stentor.service") == 0 else "not running"
