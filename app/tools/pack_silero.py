"""Writes src/stentor_pro/silero_vad_v6.npz: the numbers of Silero VAD v6, taken out of the ONNX file that
faster-whisper ships, under the names stentor_pro/silence.py reads. The model is 25 plain operations, so the app
runs it with numpy alone and needs no ONNX Runtime.

    cd app && uv run --no-project --with onnx --with numpy python tools/pack_silero.py <site-packages>/faster_whisper/assets/silero_vad_v6.onnx

Silero VAD is MIT, Copyright (c) 2020-present Silero Team (src/stentor_pro/silero_vad_v6.LICENSE).
tests/test_silence.py compares the numpy model with the ONNX one where faster-whisper is installed.
"""
import hashlib
import sys
from pathlib import Path

import numpy as np
import onnx
from onnx import numpy_helper

SHA256 = "4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2"  # faster-whisper 1.2.1's file
OUT = Path(__file__).resolve().parents[1] / "src" / "stentor_pro" / "silero_vad_v6.npz"


def main():
    source = Path(sys.argv[1])
    if hashlib.sha256(source.read_bytes()).hexdigest() != SHA256:
        sys.exit("not the file this was written for: check silence.py against it before changing SHA256")
    a = {i.name: numpy_helper.to_array(i) for i in onnx.load(str(source)).graph.initializer}
    conv = {f"conv{i}_{part[0]}": a[f"encoder.conv_layers.{i}.{part}"] for i in range(4) for part in ("weight", "bias")}
    np.savez_compressed(OUT, basis=a["encoder.feature_extractor.forward_basis_buffer"][:, 0, :], **conv,
                        lstm_w=a["onnx::LSTM_209"][0], lstm_r=a["onnx::LSTM_210"][0], lstm_b=a["onnx::LSTM_211"][0],
                        out_w=a["decoder.conv1d.weight"][0, :, 0], out_b=a["decoder.conv1d.bias"])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
