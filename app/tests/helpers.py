import numpy as np

from stentor import audio


class FakeFeedback:
    def __init__(self):
        self.log = []

    def level(self, v): pass
    def recording(self): self.log.append("recording")
    def working(self): self.log.append("working")
    def done(self): self.log.append("done")
    def error(self, m, brief="", icon="alert"): self.log.append(("error", brief or m))
    def notify(self, m): self.log.append(("notify", m))
    def clear_problem(self): pass


class FakeRecorder:
    def __init__(self, on_level):
        t = np.arange(audio.SAMPLE_RATE) / audio.SAMPLE_RATE
        self.audio = (np.sin(2 * np.pi * 220 * t) * 0.3).astype(np.float32)
        self.samples = 3 * audio.SAMPLE_RATE  # past the release tail, so finish() doesn't wait for more

    def stop(self):
        return np.concatenate([np.zeros(8000, np.float32), self.audio, np.zeros(8000, np.float32)])


class Pasted:
    """Records what Dictation pastes, with the options it chose."""

    def __init__(self):
        self.calls, self.copied = [], []

    def paste(self, text, app=None, shift=None, submit=False):
        self.calls.append({"text": text, "app": app, "shift": shift, "submit": submit})
        return ""

    def copy(self, text):
        self.copied.append(text)
        return ""


def settle(d):
    with d.lock:
        workers = list(d.threads)
    for worker in workers:
        worker.join(5)


def vowel(seconds=1.0, pitch=120.0, formants=((350, 60), (800, 80), (2400, 120))) -> np.ndarray:
    """Something a voice detector takes for a voice: a buzz through three resonances, its pitch gliding and its
    loudness rising and falling like syllables."""
    rate = audio.SAMPLE_RATE
    n = int(seconds * rate)
    t = np.arange(n) / rate
    buzz = (np.cumsum(pitch * (1 + 0.08 * np.sin(2 * np.pi * 2.5 * t))) / rate) % 1.0 - 0.5
    out = np.zeros(n)
    for freq, width in formants:
        r = np.exp(-np.pi * width / rate)
        a1, a2 = 2 * r * np.cos(2 * np.pi * freq / rate), -r * r
        y = np.zeros(n)
        for i in range(n):
            y[i] = buzz[i] + a1 * y[i - 1] + a2 * y[i - 2]  # the first two read zeros from the end
        out += y / np.abs(y).max()
    out *= 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t) ** 2
    return (0.3 * out / np.abs(out).max()).astype(np.float32)
