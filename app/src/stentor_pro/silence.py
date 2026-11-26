"""A second look at a clip before it is transcribed: is anyone speaking in it?

The app's own gate measures loudness, and uneven room noise passes it: a fan with a key click on top, a door.
Whisper then writes "Thank you." over the noise. Silero VAD tells a voice from other sound. The model is small
(25 plain operations, 1 MB of numbers), so it runs here with numpy alone, on every install and for every
transcriber: Stentor's cloud, a key of your own, the local models.

The model: Silero VAD v6, MIT, Copyright (c) 2020-present Silero Team (silero_vad_v6.LICENSE). Its numbers come
out of the ONNX file faster-whisper 1.2.1 ships (tools/pack_silero.py in this package's folder). How windows of
speech become a yes or a no follows faster-whisper's get_speech_timestamps (MIT, Copyright (c) 2023 SYSTRAN), itself
after Silero's. The settings: threshold 0.4, speech of at least 160 ms.

The clip is brought to one level first, so a quiet word from a microphone set far too low is still heard. The numpy
model gives the ONNX model's answers (tests/test_silence.py compares them where faster-whisper is installed). A clip
with a voice in it costs a few milliseconds whatever its length, since the model stops at the first voice; noise costs
about a millisecond per second of audio, and about 1 MB of memory either way.

A steady tone, music and a voice from a loudspeaker can pass for speech: the check only ever removes clips.
"""
import sys
from pathlib import Path

import numpy as np

WEIGHTS = Path(__file__).with_name("silero_vad_v6.npz")
WINDOW = 512  # samples the model hears at a time: 32 ms
CONTEXT = 64  # samples of the window before, put in front of each
HIDDEN = 128
THRESHOLD = 0.4
QUIET = THRESHOLD - 0.15  # below this a stretch of speech may be ending
SHORTEST_SPEECH = 2560  # samples: 160 ms
LONGEST_PAUSE = 32000  # samples: a stretch of speech ends after 2 s under QUIET
LEVEL = 0.3  # the peak every clip is brought to
BLOCK = 64  # windows the model takes at once, 2 s: small arrays are also the fastest


def _sigmoid(x):
    return 1 / (1 + np.exp(-x))


class Silero:
    """The model: a spectrum of each window, four convolutions over it, one LSTM step, one number out."""

    def __init__(self, path=WEIGHTS):
        with np.load(path) as w:
            self.basis = w["basis"].T.copy()  # (256, 258): real parts of 129 frequencies, then the imaginary ones
            # (3 samples x channels in, channels out), the bias, the step
            self.convs = [(w[f"conv{i}_w"].transpose(2, 1, 0).reshape(-1, w[f"conv{i}_w"].shape[0]).copy(), w[f"conv{i}_b"], step)
                          for i, step in enumerate((1, 2, 2, 1))]
            self.w, self.r = w["lstm_w"].T.copy(), w["lstm_r"].T.copy()  # gates in ONNX order: in, out, forget, cell
            self.b = w["lstm_b"][:4 * HIDDEN] + w["lstm_b"][4 * HIDDEN:]
            self.out_w, self.out_b = w["out_w"].copy(), float(w["out_b"][0])

    def probabilities(self, audio: np.ndarray, block: int = BLOCK, gain: float = 1.0):
        """How likely a voice is in each 32 ms of 16 kHz audio played `gain` times louder, `block` windows at a
        time: a long recording costs no more memory than a short one, and whoever listens can stop at the first
        voice."""
        windows = len(audio) // WINDOW + 1  # the last one filled up with zeros, as the reference does
        h, c = np.zeros(HIDDEN, np.float32), np.zeros(HIDDEN, np.float32)
        before = np.zeros((1, CONTEXT), np.float32)
        for first in range(0, windows, block):
            last = min(first + block, windows)
            part = np.zeros((last - first) * WINDOW, np.float32)
            piece = audio[first * WINDOW:last * WINDOW]
            part[:len(piece)] = piece
            part = (part * np.float32(gain)).reshape(-1, WINDOW)
            if last == windows:
                part[-1, -CONTEXT:] = 0  # as the reference does
            x = np.concatenate([np.concatenate([before, part[:-1, -CONTEXT:]]), part], axis=1)
            before = part[-1:, -CONTEXT:]
            x = np.pad(x, ((0, 0), (128, 128)), mode="reflect")
            cut = np.lib.stride_tricks.sliding_window_view(x, 256, axis=1)[:, 128::128]  # four pieces a window
            spectrum = cut @ self.basis
            x = np.sqrt(spectrum[..., :129] ** 2 + spectrum[..., 129:] ** 2)  # (windows, 4, 129)
            for weight, bias, step in self.convs:
                taps = np.lib.stride_tricks.sliding_window_view(np.pad(x, ((0, 0), (1, 1), (0, 0))), 3, axis=1)[:, ::step]
                x = np.maximum(taps.transpose(0, 1, 3, 2).reshape(taps.shape[0], taps.shape[1], -1) @ weight + bias, 0)
            fed = x[:, 0, :] @ self.w + self.b
            out = np.empty(len(fed), np.float32)
            for i in range(len(fed)):
                gates = fed[i] + h @ self.r
                c = _sigmoid(gates[2 * HIDDEN:3 * HIDDEN]) * c + _sigmoid(gates[:HIDDEN]) * np.tanh(gates[3 * HIDDEN:])
                h = _sigmoid(gates[HIDDEN:2 * HIDDEN]) * np.tanh(c)
                out[i] = np.maximum(h, 0) @ self.out_w
            yield _sigmoid(out + self.out_b)


def has_speech(blocks, samples: int) -> bool:
    """Whether the windows hold a stretch of speech longer than SHORTEST_SPEECH. A stretch starts at a window at
    THRESHOLD or more, and ends where the windows went under QUIET and stayed off THRESHOLD for LONGEST_PAUSE, or
    at the end of the clip. Stops reading `blocks` once a stretch is long enough: nothing later can undo it."""
    start = quiet = None
    at = -WINDOW
    for block in blocks:
        for p in block:
            at += WINDOW
            if p >= THRESHOLD:
                quiet = None
                if start is None:
                    start = at
            elif start is not None and p < QUIET:
                if quiet is None:
                    quiet = at
                if at - quiet >= LONGEST_PAUSE:
                    start = quiet = None
            if start is not None and (at if quiet is None else quiet) - start > SHORTEST_SPEECH:
                return True
    return start is not None and samples - start > SHORTEST_SPEECH


class SpeechCheck:
    def __init__(self):
        self.model = None  # loaded at the first clip: a few milliseconds

    def hears(self, audio: np.ndarray, gain: float) -> bool:
        """Whether a voice is in the clip, played `gain` times louder."""
        if self.model is None:
            self.model = Silero()
        return has_speech(self.model.probabilities(audio, gain=gain), len(audio))

    def silent(self, audio: np.ndarray) -> bool:
        """True only when the check ran and found no voice. Anything else leaves the clip to the transcriber."""
        peak = max(float(audio.max()), -float(audio.min())) if len(audio) else 0.0
        if peak <= 0:
            return False
        try:
            return not self.hears(audio, LEVEL / peak)
        except Exception as e:  # the type only: nothing of the audio
            print(f"stentor: the voice check failed ({type(e).__name__})", file=sys.stderr, flush=True)
            return False
