"""
One-time offline export of the Discogs-VINet CQTNet CNN to ONNX.

Run this on a DEV machine (NOT in the GrooveIQ image) with a checkout of
``github.com/raraz15/Discogs-VINet`` (which ships its ~59 MB checkpoint in-repo)
plus ``torch`` + ``onnx`` installed. Ship the resulting ``vinet_cqtnet.onnx`` to
``VINET_MODEL_DIR`` (default ``/data/models/vinet``). The GrooveIQ image ships NO
torch — only onnxruntime + librosa run at inference (the CQT front-end is
computed off-graph in librosa/numpy), so only the small CNN needs exporting.

This script was validated against the current upstream ``main`` (2026-07). Notes
learned the hard way, all handled below:

  * The shipped checkpoint predates a module rename: its keys are ``features.*``
    / ``proj.0.weight`` while the current ``CQTNet`` uses ``front_end.*`` /
    ``proj.lin.weight``. We remap (indices match — cosmetic rename).
  * The shipped checkpoint was trained with ``CONV_CHANNEL=32`` (channels
    32->64->128->256->512), NOT the 40 in ``configs/mirex2024-full.yaml`` (a
    later "replicate" run). We auto-detect it from the checkpoint's first conv.
  * The repo's ``load_model`` omits ``model.eval()`` — with BatchNorm at batch=1
    that silently corrupts every embedding. We bake ``eval()`` in.
  * torch>=2.6 defaults ``weights_only=True`` (rejects a full training
    checkpoint) and its dynamo exporter needs ``onnxscript``; we shim
    ``weights_only=False`` and use the legacy exporter (``dynamo=False``, needs
    only ``onnx``).

Usage:
    pip install torch onnx numpy pyyaml onnxruntime  # + librosa essentia for --sample-audio
    python scripts/export_vinet_onnx.py \
        --repo /path/to/Discogs-VINet \
        --out vinet_cqtnet.onnx \
        [--sample-audio /music/a.flac /music/b.flac ...]

Acceptance: cosine(torch_emb, onnx_emb) > 0.9999 (the script aborts otherwise).
Validation runs on random in-range tensors by default; ``--sample-audio`` adds a
realistic end-to-end check through the exact app preprocessing.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np

# The runtime CQT chain lives in the app so parity is guaranteed by construction.
try:
    from app.services.analysis_worker import _mean_downsample_cqt
except Exception:  # pragma: no cover - script runs standalone on a dev box
    _mean_downsample_cqt = None


def _downsample(cqt: np.ndarray, factor: int) -> np.ndarray:
    if _mean_downsample_cqt is not None:
        return _mean_downsample_cqt(cqt, factor)
    new_t = int(cqt.shape[0] // factor)
    out = np.zeros((new_t, cqt.shape[1]), dtype=cqt.dtype)
    for i in range(new_t):
        out[i, :] = cqt[i * factor : (i + 1) * factor, :].mean(axis=0)
    return out


def _build_cqt_input(fp, sr, hop, n_bins, bpo, downsample, context_length) -> np.ndarray:
    """Replicate the app's _compute_vinet_embedding chain -> (1,1,84,T') float32."""
    import essentia.standard as es
    import librosa

    audio = np.asarray(es.MonoLoader(filename=fp, sampleRate=sr)(), dtype=np.float32)
    cqt = librosa.core.cqt(y=audio, sr=sr, hop_length=hop, n_bins=n_bins, bins_per_octave=bpo)
    cqt = np.abs(cqt).astype(np.float16).astype(np.float32).T  # (T, F)
    if context_length > 0 and cqt.shape[0] < context_length:
        cqt = np.pad(cqt, ((0, context_length - cqt.shape[0]), (0, 0)), "constant", constant_values=0)
    cqt = _downsample(cqt, downsample)
    cqt = np.clip(cqt, 0, None)
    cqt = cqt / (cqt.max() + 1e-6)
    return cqt.T[np.newaxis, np.newaxis, :, :].astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description="Export Discogs-VINet CQTNet to ONNX")
    ap.add_argument("--repo", required=True, help="Path to the raraz15/Discogs-VINet checkout")
    ap.add_argument("--config", default="configs/mirex2024-full.yaml", help="Config (relative to --repo)")
    ap.add_argument(
        "--checkpoint",
        default="logs/checkpoints/Discogs-VINet/model_checkpoint.pth",
        help="Checkpoint .pth (relative to --repo)",
    )
    ap.add_argument("--out", default="vinet_cqtnet.onnx")
    ap.add_argument("--sample-audio", nargs="*", default=[], help="Real tracks for an end-to-end parity check")
    ap.add_argument("--sr", type=int, default=22050)
    ap.add_argument("--hop", type=int, default=512)
    ap.add_argument("--n-bins", type=int, default=84)
    ap.add_argument("--bins-per-octave", type=int, default=12)
    ap.add_argument("--downsample", type=int, default=20)
    ap.add_argument("--context-length", type=int, default=7600)
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    sys.path.insert(0, repo)
    os.chdir(repo)  # checkpoint/config paths are relative to the repo root

    import torch
    import yaml

    # torch>=2.6 defaults weights_only=True, which rejects a full training
    # checkpoint (optimizer/scheduler state). Force the legacy behaviour.
    _orig_load = torch.load
    torch.load = lambda *a, **k: _orig_load(*a, **{**k, "weights_only": False})

    from model.utils import build_model

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    # Auto-detect CONV_CHANNEL / EMBEDDING_SIZE from the checkpoint tensors so we
    # match the SHIPPED weights regardless of what the config says.
    ckpt = torch.load(args.checkpoint, map_location="cpu")
    sd = ckpt["model_state_dict"]
    first_conv = sd.get("features.0.weight", sd.get("front_end.0.weight"))
    proj_w = sd.get("proj.0.weight", sd.get("proj.lin.weight"))
    cfg["MODEL"]["CONV_CHANNEL"] = int(first_conv.shape[0])
    cfg["MODEL"]["EMBEDDING_SIZE"] = int(proj_w.shape[0])
    print(f"detected CONV_CHANNEL={cfg['MODEL']['CONV_CHANNEL']} EMBEDDING_SIZE={cfg['MODEL']['EMBEDDING_SIZE']}")

    model = build_model(cfg, device=torch.device("cpu"))

    # Remap the pre-rename checkpoint keys onto the current module names.
    remapped = {}
    for k, v in sd.items():
        nk = k
        if nk.startswith("features."):
            nk = "front_end." + nk[len("features.") :]
        if nk.startswith("proj.0."):
            nk = "proj.lin." + nk[len("proj.0.") :]
        remapped[nk] = v
    missing, unexpected = model.load_state_dict(remapped, strict=False)
    real_missing = [k for k in missing if not k.endswith("num_batches_tracked")]
    real_unexpected = [k for k in unexpected if not k.endswith("num_batches_tracked")]
    if real_missing or real_unexpected:
        print(f"ABORT: state_dict mismatch beyond rename. missing={real_missing} unexpected={real_unexpected}")
        return 2

    model.eval()  # CRITICAL: repo omits this; BN@batch=1 corrupts every embedding

    out = os.path.abspath(args.out)
    dummy = torch.randn(1, 1, args.n_bins, 512)  # (B, C=1, F=84, T) — T dynamic
    torch.onnx.export(
        model,
        dummy,
        out,
        input_names=["cqt"],
        output_names=["embedding"],
        dynamic_axes={"cqt": {0: "batch", 3: "time"}, "embedding": {0: "batch"}},
        opset_version=args.opset,
        dynamo=False,  # legacy TorchScript exporter (needs only `onnx`, not onnxscript)
    )
    print(f"exported -> {out} ({os.path.getsize(out) / 1e6:.1f} MB)")

    # --- Validate: onnxruntime vs torch (cosine > 0.9999) ----------------
    import onnxruntime as ort

    sess = ort.InferenceSession(out, providers=["CPUExecutionProvider"])
    in_name = sess.get_inputs()[0].name

    inputs = []
    rng = np.random.RandomState(0)
    # Random in-range tensors (>=256 downsampled frames — the CNN needs its
    # ~380-frame context; shorter inputs are padded in the app before this stage).
    for t in (256, 430, 640, 860):
        inputs.append(rng.rand(1, 1, args.n_bins, t).astype(np.float32))
    for fp in args.sample_audio:
        inputs.append(
            _build_cqt_input(
                fp, args.sr, args.hop, args.n_bins, args.bins_per_octave, args.downsample, args.context_length
            )
        )

    worst = 1.0
    for x in inputs:
        with torch.no_grad():
            t_emb = model(torch.from_numpy(x)).cpu().numpy().reshape(-1)
        o_emb = np.asarray(sess.run(None, {in_name: x})[0], dtype=np.float32).reshape(-1)
        cos = float(np.dot(t_emb, o_emb) / (np.linalg.norm(t_emb) * np.linalg.norm(o_emb) + 1e-12))
        worst = min(worst, cos)
        print(f"  T={x.shape[3]:5d}  cos(torch,onnx)={cos:.7f}")

    if worst <= 0.9999:
        print(f"\nABORT: worst cosine {worst:.7f} <= 0.9999 — export is NOT parity-safe.")
        return 1
    print(f"\nParity OK (worst cosine {worst:.7f}). Ship {out} to VINET_MODEL_DIR.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
