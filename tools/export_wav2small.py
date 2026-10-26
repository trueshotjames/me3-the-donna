"""Export audEERING's Wav2Small (PyTorch weights on Hugging Face) to models/wav2small.onnx. Run once, on the laptop.

    py -3.13 -m venv .venv-export
    .venv-export\\Scripts\\pip install torch --index-url https://download.pytorch.org/whl/cpu
    .venv-export\\Scripts\\pip install onnx onnxruntime numpy safetensors huggingface_hub
    .venv-export\\Scripts\\python tools\\export_wav2small.py

The network is copied from the model card (huggingface.co/audeering/wav2small, "Wav2Small2.0",
17K parameters): a 64-point DFT as two Conv1d, a 26-band log-mel filterbank, seven 13-channel
Conv2d blocks with attention pooling, a 338 -> 3 linear head. Only the weights are downloaded
(model.safetensors, 69 KB); the card's own code is not executed, and transformers is not needed,
so a transformers release that breaks the card's `from_pretrained` does not break this script.

Output: models/wav2small.onnx (input "signal" float32 [batch, samples] at 16 kHz, output "adv"
[batch, 3] = arousal, dominance, valence, nominally 0..1). The Pi runs it with onnxruntime only.
The script checks the ONNX output against PyTorch on 0.6, 1, 3 and 5 s signals before it succeeds.

Options:
    --weights PATH     use a local model.safetensors instead of downloading
    --out PATH         where to write the .onnx (default models/wav2small.onnx)

Licence of the weights: CC BY-NC-SA 4.0, research use (fine for coursework).
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "models" / "wav2small.onnx"
REPO = "audeering/wav2small"
WEIGHTS_URL = f"https://huggingface.co/{REPO}/resolve/main/model.safetensors"
N_EXPECTED_PARAMS = 17_000  # from the model card; checked loosely below


def build_model():
    """Wav2Small as a plain torch.nn.Module (same module names as the checkpoint)."""
    import torch
    from torch import nn

    class Spectrogram(nn.Module):
        # Asymmetric DFT (64 bins over 64 samples, hop 32) as two frozen Conv1d; weights come from the checkpoint.
        def __init__(self, n_fft=64, n_time=64, hop_length=32):
            super().__init__()
            out_channels = n_fft // 2 + 1
            self.conv_real = nn.Conv1d(1, out_channels, n_time, stride=hop_length, padding=0, bias=False)
            self.conv_imag = nn.Conv1d(1, out_channels, n_time, stride=hop_length, padding=0, bias=False)
            for p in self.parameters():
                p.requires_grad = False

        def forward(self, x):
            x = x[:, None, :]
            real = self.conv_real(x)
            imag = self.conv_imag(x)
            return real**2 + imag**2  # [batch, freq, frames]

    class LogmelFilterBank(nn.Module):
        def __init__(self, n_fft=64, n_mels=26):
            super().__init__()
            self.register_buffer("melW", torch.zeros(n_fft // 2 + 1, n_mels))
            self.register_buffer("amin", torch.tensor([1e-10]))

        def forward(self, x):
            x = torch.matmul(x[:, None, :, :].transpose(2, 3), self.melW)  # [batch, 1, frames, mel]
            x = torch.where(x > self.amin, x, self.amin)
            return 10 * torch.log10(x)

    class Conv(nn.Module):
        def __init__(self, c_in, c_out, k=3, stride=1, padding=1):
            super().__init__()
            self.conv = nn.Conv2d(c_in, c_out, k, stride=stride, padding=padding, bias=False)
            self.norm = nn.BatchNorm2d(c_out)

        def forward(self, x):
            return torch.relu(self.norm(self.conv(x)))

    class Vgg7(nn.Module):
        def __init__(self):
            super().__init__()
            self.l1 = Conv(1, 13)
            self.l2 = Conv(13, 13)
            self.l3 = Conv(13, 13)
            self.maxpool_A = nn.MaxPool2d(3, stride=2, padding=1)
            self.l4 = Conv(13, 13)
            self.l5 = Conv(13, 13)
            self.l6 = Conv(13, 13)
            self.l7 = Conv(13, 13)
            self.lin = nn.Conv2d(13, 13, 1, padding=0, stride=1)
            self.sof = nn.Conv2d(13, 13, 1, padding=0, stride=1)
            self.spectrogram_extractor = Spectrogram()
            self.logmel_extractor = LogmelFilterBank()

        def forward(self, x):
            # wav2vec2-style pre-normalisation (mean 0, variance 1 over the signal), not in place.
            x = x - x.mean(1, keepdim=True)
            var = (x * x).mean(1, keepdim=True)
            x = x / torch.sqrt(var + 1e-7)
            x = self.spectrogram_extractor(x)
            x = self.logmel_extractor(x)
            x = self.l1(x)
            x = self.l2(x)
            x = self.l3(x)
            x = self.maxpool_A(x)
            x = self.l4(x)
            x = self.l5(x)
            x = self.l6(x)
            x = self.l7(x)
            x = self.lin(x) * self.sof(x).softmax(2)  # [batch, ch, frames, mel]
            x = x.sum(2)  # [batch, ch, mel]
            x = torch.cat([x, torch.bmm(x, x.transpose(1, 2))], 2)  # [batch, 13, 13 + 13]
            return x.reshape(-1, 338)

    class Wav2Small(nn.Module):
        def __init__(self):
            super().__init__()
            self.vgg7 = Vgg7()
            self.adv = nn.Linear(338, 3)  # 0 = arousal, 1 = dominance, 2 = valence

        def forward(self, x):
            return self.adv(self.vgg7(x))

    return Wav2Small()


def fetch_weights(dest: Path) -> Path:
    """model.safetensors into dest; huggingface_hub if installed (uses its cache), else plain HTTPS."""
    if dest.exists() and dest.stat().st_size > 10_000:
        return dest
    try:
        from huggingface_hub import hf_hub_download

        src = Path(hf_hub_download(REPO, "model.safetensors"))
        dest.write_bytes(src.read_bytes())
    except ImportError:
        print(f"downloading {WEIGHTS_URL}")
        with urllib.request.urlopen(WEIGHTS_URL, timeout=60) as r:  # noqa: S310 - fixed https URL
            dest.write_bytes(r.read())
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--weights", type=Path, help="local model.safetensors (default: download from Hugging Face)")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--random-weights", action="store_true", help=argparse.SUPPRESS)  # export-path self-test
    a = ap.parse_args()

    import numpy as np
    import torch

    torch.manual_seed(0)
    model = build_model().eval()
    n_params = sum(p.numel() for p in model.parameters())
    a.out.parent.mkdir(parents=True, exist_ok=True)

    if a.random_weights:
        print("self-test with random weights: the .onnx written here is NOT the real model")
        with torch.no_grad():
            for name, buf in model.named_buffers():
                if name.endswith("melW"):
                    buf.copy_(torch.rand_like(buf))
            for m in model.modules():
                if isinstance(m, torch.nn.BatchNorm2d):
                    m.running_var.uniform_(0.5, 2.0)
                    m.running_mean.uniform_(-0.5, 0.5)
    else:
        from safetensors.torch import load_file

        weights = a.weights or fetch_weights(a.out.parent / "wav2small.safetensors")
        state = load_file(str(weights))
        missing, unexpected = model.load_state_dict(state, strict=False)
        if missing or unexpected:
            print(f"checkpoint does not match the architecture.\n  missing: {missing}\n  unexpected: {unexpected}", file=sys.stderr)
            return 1
        print(f"loaded {weights.name}: {len(state)} tensors, {n_params} parameters")
    if not (0.5 * N_EXPECTED_PARAMS < n_params < 5 * N_EXPECTED_PARAMS):
        print(f"unexpected parameter count {n_params}; the architecture in this script is off", file=sys.stderr)
        return 1

    x = torch.randn(1, 16000 * 3)
    with torch.no_grad():
        print(f"torch output on noise: {[round(v, 3) for v in model(x).flatten().tolist()]}")

    export_kwargs = dict(
        input_names=["signal"],
        output_names=["adv"],
        dynamic_axes={"signal": {0: "batch", 1: "samples"}, "adv": {0: "batch"}},
        opset_version=17,
    )
    try:
        torch.onnx.export(model, (x,), str(a.out), dynamo=False, **export_kwargs)
    except TypeError:  # torch < 2.5 has no `dynamo` argument; the classic exporter is the default there
        torch.onnx.export(model, (x,), str(a.out), **export_kwargs)

    import onnxruntime as ort

    sess = ort.InferenceSession(str(a.out), providers=["CPUExecutionProvider"])
    assert sess.get_inputs()[0].name == "signal", sess.get_inputs()[0].name
    rng = np.random.default_rng(0)
    worst = 0.0
    for secs in (0.6, 1, 3, 5):
        sig = (rng.standard_normal((1, int(16000 * secs))) * 0.1).astype(np.float32)
        with torch.no_grad():
            r = model(torch.from_numpy(sig)).numpy().reshape(-1)
        o = sess.run(None, {"signal": sig})[0].reshape(-1)
        diff = float(np.abs(r - o).max())
        worst = max(worst, diff)
        print(f"{secs:>3} s: torch {np.round(r, 3)} onnx {np.round(o, 3)} max diff {diff:.6f}")
    if worst > 1e-3:
        print("ONNX output differs from PyTorch; do not use this export", file=sys.stderr)
        return 1
    # Silence must not produce NaN (the pre-normalisation divides by sqrt(var + 1e-7)).
    o = sess.run(None, {"signal": np.zeros((1, 16000 * 3), np.float32)})[0]
    if not np.all(np.isfinite(o)):
        print(f"non-finite output on silence: {o}", file=sys.stderr)
        return 1
    # Batched call must equal single calls (dynamic batch axis).
    sig2 = (rng.standard_normal((2, 16000)) * 0.1).astype(np.float32)
    o2 = sess.run(None, {"signal": sig2})[0]
    o1 = np.concatenate([sess.run(None, {"signal": sig2[i : i + 1]})[0] for i in range(2)])
    if np.abs(o2 - o1).max() > 1e-4:
        print("batched output differs from single calls", file=sys.stderr)
        return 1
    print(f"wrote {a.out} ({a.out.stat().st_size // 1024} KB)")
    if a.random_weights:
        a.out.unlink()
        print("self-test only: removed the random-weight .onnx again")
    return 0


if __name__ == "__main__":
    sys.exit(main())
