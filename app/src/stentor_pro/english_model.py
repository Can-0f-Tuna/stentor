"""Automatic local model for English: NVIDIA's Parakeet TDT 0.6B v2 instead of Whisper `small`.

For English it is about as accurate as `small` and much faster on a CPU: the text is there well under a second
after the key is let go, where `small` can take several seconds. The gain grows with the cores the computer has free.

It runs on ONNX Runtime through onnx-asr, not on faster-whisper, so this package transcribes those clips itself
(Extension.transcribe) and the app's own local engine never sees this model's name.

It is English only and takes no spelling hint, so Automatic picks it only when:

- the provider is local and English is the one language picked,
- there is no spelling hint and no learned word to lose,
- onnx-asr is installed (the offline install brings it) and the computer has 8 GB of memory or more,
- the model setting is Automatic or this model (`local_model_auto`, shared with the Hebrew model).

You can still pick it yourself with a spelling hint set; the hint is not used then. With another language picked,
or on a cloud service, the setting goes back to Automatic: nothing else could run this model. The download (620 MB)
runs in the background with a notice before and after, as the Hebrew model's does."""
import importlib.util
import sys
import threading
import time

from stentor.audio import SAMPLE_RATE
from stentor.providers import TranscribeError, physical_cores

from . import hebrew_model
from .cloud import pieces

REPO = "nemo-parakeet-tdt-0.6b-v2"  # onnx-asr's name for it, and the name in the settings
HUB = "istupakov/parakeet-tdt-0.6b-v2-onnx"  # the ONNX conversion of nvidia/parakeet-tdt-0.6b-v2, both CC-BY-4.0
FILES = ["config.json", "vocab.txt", "encoder-model.int8.onnx", "decoder_joint-model.int8.onnx"]
NOTE = "English, faster: NVIDIA Parakeet TDT 0.6B v2, CC-BY-4.0 licence (about 620 MB). No spelling hint"
AUTOMATIC = hebrew_model.AUTOMATIC
FIELD = hebrew_model.FIELD
PIECE_SECONDS, SEARCH_SECONDS = 30.0, 8.0  # a long dictation goes in parts cut in a pause: whole, it lost words and took 2 GB


def installed() -> bool:
    try:
        return all(importlib.util.find_spec(name) is not None for name in ("onnx_asr", "onnxruntime", "huggingface_hub"))
    except (ImportError, ValueError):
        return False


def usable(cfg) -> bool:
    """This model can do these settings' dictations at all."""
    return cfg.provider == "local" and cfg.language_codes() == ["en"] and installed()


def hinted(cfg) -> bool:
    """A spelling hint or learned words, which this model can't take."""
    return bool(cfg.prompt.strip() or (cfg.setting("premium_learn", False) and str(cfg.setting("learned_terms", "")).strip()))


def wanted(cfg) -> bool:
    """This model would be the better Automatic choice here."""
    return usable(cfg) and not hinted(cfg) and hebrew_model.memory() >= hebrew_model.MIN_MEMORY


def folder(local_only: bool = True) -> str:
    from huggingface_hub import snapshot_download
    return snapshot_download(HUB, allow_patterns=FILES, local_files_only=local_only)


def download():
    folder(local_only=False)


def downloaded() -> bool:
    """All of it is on disk: a download cut short leaves the weights out."""
    try:
        import os
        path = folder()
        return all(os.path.isfile(os.path.join(path, name)) for name in FILES)
    except Exception:
        return False


class Engine:
    """The model in memory, loaded on first use and freed after a stretch of inactivity."""

    def __init__(self):
        self.model = None
        self.used = 0.0
        self.lock = threading.Lock()  # the live words and the final text can ask at the same time

    def load(self):
        if self.model is None:
            import onnx_asr
            import onnxruntime
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = physical_cores()
            self.model = onnx_asr.load_model(REPO, folder(), quantization="int8", sess_options=options)
        return self.model

    def transcribe(self, audio) -> str:
        with self.lock:
            model = self.load()
            parts = pieces(audio, PIECE_SECONDS, SEARCH_SECONDS)
            text = " ".join(t for t in (str(model.recognize(part, sample_rate=SAMPLE_RATE)).strip() for part in parts) if t)
            self.used = time.monotonic()
            return text

    def unload_idle(self, seconds: float):
        if self.model is not None and time.monotonic() - self.used >= seconds and self.lock.acquire(blocking=False):
            try:
                self.model = None
            finally:
                self.lock.release()


class EnglishModel:
    def __init__(self):
        self.engine = Engine()
        self.ready = None  # downloaded: None until checked, then True once it is
        self.fetching = None  # the download thread
        self.picked = False  # you chose this model before it was here: it goes in when the download is done
        self.settled = False  # the settings have had the model they should have since the app started
        self.save = None  # the settings server's save, once the app has started
        self.notify = lambda message: None
        self.lock = threading.Lock()

    def is_ready(self) -> bool:
        if not self.ready:
            self.ready = downloaded()
        return self.ready

    # ---------- transcribing ----------

    def takes(self, cfg) -> bool:
        """The settings name this model, so its dictations come here and not to the app's own local engine."""
        return cfg.provider == "local" and cfg.local_model == REPO

    def transcribe(self, cfg, speech) -> str:
        try:
            return self.engine.transcribe(speech)
        except Exception as e:  # the type only: nothing of the audio or the text
            print(f"stentor: the English model failed ({type(e).__name__})", file=sys.stderr, flush=True)
            raise TranscribeError(f"the English model didn't run ({type(e).__name__}). Pick another model in the settings.",
                                  "The English model didn't run. Pick another model in the settings.") from None

    # ---------- the setting ----------

    def models(self, rows: list, cfg) -> list:
        """The list of local models with this one in it while it can run: first while Automatic means it."""
        rest = [row for row in rows if row[0] != REPO]
        if not usable(cfg):
            return rest
        if wanted(cfg) and self.is_ready():
            return [[REPO, NOTE]] + rest
        if self.is_ready():
            return rest + [[REPO, NOTE]]
        status = "downloading" if self.fetching and self.fetching.is_alive() else "downloads when you pick it"
        return rest + [[REPO, f"{NOTE}; {status}"]]

    def release(self, new):
        """Before the other checks: settings this model can't serve go back to Automatic, for them to decide."""
        if new.local_model == REPO and not usable(new):
            new.local_model = AUTOMATIC

    def check(self, new, changes: dict):
        """At each save, after release() and the Hebrew model's check. Never changes a model you chose yourself."""
        if not usable(new):
            return
        if "local_model" in changes:  # you chose; it stays Automatic only if you picked what Automatic means here
            new.extra[FIELD] = new.local_model == (REPO if wanted(new) else AUTOMATIC)
            self.picked = new.local_model == REPO
        if new.local_model == REPO and not self.is_ready():
            self.fetch()
            new.local_model = AUTOMATIC  # until it is here; the download saves it when it is done
            return
        if not new.extra.get(FIELD, True) or new.local_model not in (AUTOMATIC, REPO):
            return
        if wanted(new) and not self.is_ready():
            self.fetch()
        new.local_model = REPO if wanted(new) and self.is_ready() else AUTOMATIC

    def started(self, cfg, save, notify):
        self.cfg, self.save, self.notify = cfg, save, notify
        self.tick()

    def tick(self):
        """Frees the model when it has not been used for a while, and, until it has worked once, saves the model the
        settings should have. A save the settings refuse (a value they can't take) is tried again at the next tick."""
        cfg = getattr(self, "cfg", None)
        if cfg is None:
            return
        self.engine.unload_idle(cfg.idle_unload_seconds)
        if self.settled:
            return
        if not usable(cfg) or not cfg.extra.get(FIELD, True) or cfg.local_model not in (AUTOMATIC, REPO):
            self.settled = cfg.local_model != REPO or usable(cfg)  # a setting it can't serve still needs the save below
            if self.settled:
                return
        elif wanted(cfg) and not self.is_ready():
            self.fetch()
            self.settled = True  # the download saves it
            return
        elif cfg.local_model == (REPO if wanted(cfg) else AUTOMATIC):
            self.settled = True
            return
        try:
            self.save({})  # release() and check() pick the model
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
        self.notify("Downloading the English model (620 MB). Dictation works as before until it is ready.")
        try:
            download()
        except Exception as e:  # offline, or the disk is full: the next start tries again
            print(f"stentor: the English model didn't download ({type(e).__name__}); staying on {AUTOMATIC}", file=sys.stderr, flush=True)
            return
        self.ready = True
        if self.save is None:
            return
        try:
            self.save({"local_model": REPO} if self.picked else {})
        except ValueError:  # refused by the settings: tick() tries again
            self.settled = False
            return
        self.notify("The English model is ready. Your next dictation uses it.")
