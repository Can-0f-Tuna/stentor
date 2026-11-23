"""Menu-bar / system-tray icon on Windows and macOS: Settings, Copy last dictation, Check for updates, Quit.

Linux has no tray here (Wayland trays need extra system libraries); it gets an app-menu entry instead.
"""
import sys

from . import mark


def available() -> bool:
    if sys.platform not in ("win32", "darwin"):
        return False
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:
        return False


class Tray:
    def __init__(self, on_settings, on_check_updates, on_quit, on_copy_last=None):
        import pystray
        self.on_quit = on_quit
        menu = pystray.Menu(
            pystray.MenuItem("Settings", lambda: on_settings(), default=True),
            pystray.MenuItem("Copy last dictation", lambda: on_copy_last and on_copy_last()),
            pystray.MenuItem("Check for updates", lambda: on_check_updates()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Stentor", self._quit),
        )
        self.icon = pystray.Icon("stentor", mark.image(), "Stentor", menu)

    def _quit(self):
        self.icon.stop()
        self.on_quit()

    def run(self):
        """Blocks; must be called on the main thread (macOS requires it)."""
        self.icon.run()

    def stop(self):
        self.icon.stop()
