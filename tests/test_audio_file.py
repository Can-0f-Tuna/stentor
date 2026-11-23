import sys
from subprocess import CompletedProcess

import numpy as np
import pytest

from stentor import audio, cli


def test_read_audio_file_with_ffmpeg(tmp_path, monkeypatch):
    path = tmp_path / "voice memo.m4a"
    path.write_bytes(b"not a WAV")
    monkeypatch.setitem(sys.modules, "av", None)
    monkeypatch.setattr(audio.shutil, "which", lambda name: "/bin/ffmpeg" if name == "ffmpeg" else None)

    def run(cmd, *, capture_output):
        assert cmd[0] == "/bin/ffmpeg" and cmd[cmd.index("-i") + 1] == str(path)
        assert cmd[cmd.index("-ac") + 1] == "1"
        assert cmd[cmd.index("-ar") + 1] == str(audio.SAMPLE_RATE)
        assert cmd[-3:] == ["-f", "s16le", "pipe:1"] and capture_output
        return CompletedProcess(cmd, 0, b"\x00\x80\x00\x00\x00\x40", b"")

    monkeypatch.setattr(audio.subprocess, "run", run)
    decoded = audio.read_audio_file(str(path))
    assert decoded.dtype == np.float32
    np.testing.assert_array_equal(decoded, [-1.0, 0.0, 0.5])


def test_transcribe_reports_missing_decoders(tmp_path, monkeypatch, capsys):
    path = tmp_path / "voice.mp3"
    path.write_bytes(b"not a WAV")
    monkeypatch.setenv("STENTOR_CONFIG_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "av", None)
    monkeypatch.setattr(audio.shutil, "which", lambda _: None)
    assert cli.main(["transcribe", str(path)]) == 1
    error = capsys.readouterr().err
    assert "PyAV" in error and "ffmpeg on PATH" in error


def test_read_audio_file_with_pyav(tmp_path, monkeypatch):
    av = pytest.importorskip("av")
    monkeypatch.setattr(audio.shutil, "which", lambda _: None)
    path = tmp_path / "stereo.flac"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("flac", rate=48000)
        stream.layout = "stereo"
        samples = np.tile([8192, -8192], 2400).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="stereo")
        frame.sample_rate = 48000
        for packet in stream.encode(frame) + stream.encode(None):
            container.mux(packet)

    decoded = audio.read_audio_file(str(path))
    assert decoded.shape == (800,) and decoded.dtype == np.float32
    np.testing.assert_array_equal(decoded, np.zeros(800))
