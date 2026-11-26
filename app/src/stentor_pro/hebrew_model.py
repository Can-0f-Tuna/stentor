"""Automatic local model for Hebrew: ivrit.ai's Whisper turbo instead of `small`, on a computer that can run it.

A model trained on Hebrew, it makes far fewer errors in Hebrew than `small`. It needs about 2 GB of memory while it
runs and a 1.6 GB download, so it is picked only when:

- the provider is local,
- the languages include Hebrew, or none are picked and the computer's own language is Hebrew,
- the computer has 8 GB of memory or more,
- the model setting is Automatic (`small`, the app's first choice) or this model, and the user didn't pick `small`
  over it (`local_model_auto` turns off then).

The download runs in the background with a notice before and after, and the model in use stays until it is done:
a dictation never waits minutes for it."""
import locale
import os
import subprocess
import sys
import threading

REPO = "ivrit-ai/whisper-large-v3-turbo-ct2"
NOTE = "Hebrew, most accurate: ivrit.ai's Whisper large-v3 turbo, Apache-2.0 licence (about 1.6 GB)"
AUTOMATIC = "small"  # what the app's Automatic stores
MIN_MEMORY = 7 * 2**30  # an "8 GB" computer reports a little under 8 GiB
FIELD = "local_model_auto"  # False once you pick small while this model is the recommendation
FIELDS = {FIELD: True}


def memory() -> int:
    """Bytes of memory in the computer, 0 when it can't tell."""
    try:
        if sys.platform == "win32":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("free", ctypes.c_ulonglong), ("pagefile", ctypes.c_ulonglong), ("pagefree", ctypes.c_ulonglong),
                            ("virtual", ctypes.c_ulonglong), ("virtualfree", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]
            status = Status()
            status.length = ctypes.sizeof(Status)
            return status.total if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else 0
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def system_hebrew() -> bool:
    """True when the computer's language is Hebrew ('he' or the old code 'iw')."""
    names = [os.environ.get(name, "") for name in ("LC_ALL", "LC_MESSAGES", "LANG")]
    try:
        names.append(locale.getlocale()[0] or "")
    except ValueError:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            names.append("he" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x0D else "")
        except (AttributeError, OSError):
            pass
    elif sys.platform == "darwin":
        try:
            names.append(subprocess.run(["defaults", "read", "-g", "AppleLocale"], capture_output=True, text=True, timeout=2).stdout)
        except (OSError, subprocess.SubprocessError):
            pass
    return any(name.strip().lower()[:2] in ("he", "iw") for name in names)


def speaks_hebrew(cfg) -> bool:
    codes = cfg.language_codes()
    return "he" in codes if codes else system_hebrew()


def wanted(cfg) -> bool:
    """This model would be the better Automatic choice here."""
    return cfg.provider == "local" and speaks_hebrew(cfg) and memory() >= MIN_MEMORY


def download():
    from faster_whisper.utils import download_model
    download_model(REPO)


def downloaded() -> bool:
    """All of it is on disk: a download cut short leaves the weights out, and loading would fetch them then.
    Asks the download cache alone: faster-whisper brings PyAV's libraries with it, which macOS checks on their
    first load after an install or an update, and the app started only after that, a minute or more later."""
    try:
        from huggingface_hub import try_to_load_from_cache
        return isinstance(path := try_to_load_from_cache(REPO, "model.bin"), str) and os.path.isfile(path)
    except Exception:
        return False


class HebrewModel:
    def __init__(self):
        self.ready = None  # downloaded: None until checked, then True once it is
        self.fetching = None  # the download thread
        self.failed = ""
        self.picked = False  # you chose this model before it was here: it goes in when the download is done
        self.settled = False  # the settings have had the model they should have since the app started
        self.save = None  # the settings server's save, once the app has started
        self.notify = lambda message: None
        self.lock = threading.Lock()

    def is_ready(self) -> bool:
        if not self.ready:
            self.ready = downloaded()
        return self.ready

    def recommended(self, cfg) -> str:
        """What Automatic means for these settings right now."""
        return REPO if wanted(cfg) and self.is_ready() else AUTOMATIC

    def models(self, base: list, cfg) -> list:
        """The app's list of local models, with this one first while it is the recommendation."""
        rest = [row for row in base if row[0] != REPO]
        if not wanted(cfg):
            return rest + [[REPO, NOTE]]
        if self.recommended(cfg) == REPO:
            return [[REPO, NOTE]] + rest
        status = "downloading" if self.fetching and self.fetching.is_alive() else "downloads when you pick it"
        return rest + [[REPO, f"{NOTE}; {status}"]]

    def check(self, new, changes: dict):
        """At each save: the model the settings should have. Never changes a model you chose yourself."""
        if "local_model" in changes:  # you chose; Automatic is whatever was recommended a moment ago
            new.extra[FIELD] = new.local_model == self.recommended(new) or (new.local_model == REPO and wanted(new))
            self.picked = new.local_model == REPO
        if new.local_model == REPO and not self.is_ready():
            self.fetch()
            new.local_model = AUTOMATIC  # until it is here; the download saves it when it is done
            return
        if not new.extra.get(FIELD, True) or new.local_model not in (AUTOMATIC, REPO):
            return
        if wanted(new) and not self.is_ready():
            self.fetch()
        new.local_model = self.recommended(new)

    def started(self, cfg, save, notify):
        self.cfg, self.save, self.notify = cfg, save, notify
        self.tick()

    def tick(self):
        """Until it has worked once: saves the recommended model if the settings should have another one. A save
        the settings refuse (a value they can't take) is tried again at the next tick."""
        cfg = getattr(self, "cfg", None)
        if self.settled or cfg is None:
            return
        if not cfg.extra.get(FIELD, True) or cfg.local_model not in (AUTOMATIC, REPO):
            self.settled = True
        elif wanted(cfg) and not self.is_ready():
            self.fetch()
            self.settled = True  # the download saves it
        elif cfg.local_model == self.recommended(cfg):
            self.settled = True
        else:
            try:
                self.save({})  # check() picks the model
                self.settled = True
            except ValueError:
                pass

    def fetch(self):
        with self.lock:
            if self.ready or (self.fetching and self.fetching.is_alive()):
                return
            self.fetching = threading.Thread(target=self._download, daemon=True)
            self.fetching.start()

    def _download(self):
        self.notify("Downloading the Hebrew model (1.6 GB). Dictation works as before until it is ready.")
        try:
            download()
        except Exception as e:  # offline, or the disk is full: the next start tries again
            self.failed = type(e).__name__
            print(f"stentor: the Hebrew model didn't download ({self.failed}); staying on {AUTOMATIC}", file=sys.stderr, flush=True)
            return
        self.ready, self.failed = True, ""
        if self.save is None:
            return
        try:
            self.save({"local_model": REPO} if self.picked else {})
        except ValueError:  # refused by the settings: tick() tries again
            self.settled = False
            return
        self.notify("The Hebrew model is ready. Your next dictation uses it.")
