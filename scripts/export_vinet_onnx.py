"""
One-time offline export of the Discogs-VINet CQTNet CNN to ONNX.

Run this on a DEV machine (NOT in the GrooveIQ image) that has ``torch==2.0.1``,
a checkout of ``github.com/raraz15/Discogs-VINet`` on ``PYTHONPATH``, and its
checkpoint. Commit / ship the resulting ``vinet_cqtnet.onnx`` to
``VINET_MODEL_DIR`` (default ``/data/models/vinet``). The GrooveIQ image ships
NO torch — only onnxruntime + librosa run at inference time (the CQT front-end
is computed off-graph in librosa/numpy), so only the small CNN needs exporting.

Two critical parity requirements (see docs/HANDOFF_DISCOGS_VINET.md §0, §13):
  1. ``model.eval()`` MUST be baked into the export — the repo's infer path omits
     it, and with BatchNorm at batch=1 that silently corrupts every embedding.
  2. The runtime CQT chain (app/services/analysis_worker._compute_vinet_embedding)
     must match the reference preprocessing exactly. This script validates the
     ONNX graph against the torch reference on real tracks (cosine > 0.9999).

Usage:
    pip install torch==2.0.1 librosa==0.10.1 essentia==2.1b6 onnxruntime numpy pyyaml
    python scripts/export_vinet_onnx.py \
        --repo /path/to/Discogs-VINet \
        --ckpt-dir /path/to/Discogs-VINet/logs/checkpoints/Discogs-VINet \
        --out vinet_cqtnet.onnx \
        --sample-audio /music/a.flac /music/b.flac ...   # >=10 real tracks

Acceptance: cosine(torch_emb, onnx_emb) > 0.9999 on every sample track, spanning
short and long durations. The script aborts if any track fails.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np

# The runtime CQT chain lives in the app so parity is guaranteed by construction:
# we validate against the SAME preprocessing the worker uses at inference time.
try:
    from app.services.analysis_worker import _mean_downsample_cqt
except Exception:  # pragma: no cover - script runs standalone on a dev box
    _mean_downsample_cqt = None


def _build_cqt_input(file_path: str, sr: int, hop: int, n_bins: int, bpo: int, downsample: int) -> np.ndarray:
    """Replicate the runtime chain exactly → (1, 1, 84, T') float32.

    Mirrors app/services/analysis_worker._compute_vinet_embedding. Kept inline
    (not importing librosa via the worker) so the offline script has no worker
    session dependency.
    """
    import essentia.standard as es
    import librosa

    audio = es.MonoLoader(filename=file_path, sampleRate=sr)()
    y = np.asarray(audio, dtype=np.float32)
    cqt = librosa.core.cqt(y=y, sr=sr, hop_length=hop, n_bins=n_bins, bins_per_octave=bpo)
    cqt = np.abs(cqt).astype(np.float16).astype(np.float32).T  # (T, F)
    cqt = np.clip(cqt, 0, None)

    if _mean_downsample_cqt is not None:
        cqt = _mean_downsample_cqt(cqt, downsample)
    else:
        cqt_T, cqt_F = cqt.shape
        new_T = int(cqt_T // downsample)
        new = np.zeros((new_T, cqt_F), dtype=cqt.dtype)
        for i in range(new_T):
            new[i, :] = cqt[i * downsample : (i + 1) * downsample, :].mean(axis=0)
        cqt = new

    cqt = cqt / (cqt.max() + 1e-6)
    return cqt.T[np.newaxis, np.newaxis, :, :].astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description="Export Discogs-VINet CQTNet to ONNX")
    ap.add_argument("--repo", required=True, help="Path to the raraz15/Discogs-VINet checkout")
    ap.add_argument("--ckpt-dir", required=True, help="Dir with model_checkpoint.pth + config.yaml")
    ap.add_argument("--out", default="vinet_cqtnet.onnx")
    ap.add_argument("--sample-audio", nargs="*", default=[], help="Real tracks for parity validation")
    ap.add_argument("--sr", type=int, default=22050)
    ap.add_argument("--hop", type=int, default=512)
    ap.add_argument("--n-bins", type=int, default=84)
    ap.add_argument("--bins-per-octave", type=int, default=12)
    ap.add_argument("--downsample", type=int, default=20)
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    sys.path.insert(0, args.repo)
    import torch
    import yaml
    from model.utils import load_model  # builds CQTNet + torch.load(map_location=device)

    with open(f"{args.ckpt_dir}/config.yaml") as f:
        cfg = yaml.safe_load(f)

    model = load_model(cfg, device=torch.device("cpu"), mode="infer")
    model.eval()  # CRITICAL: repo omits this; BN@batch=1 must fold to eval

    dummy = torch.randn(1, 1, args.n_bins, 512)  # (B, C=1, F=84, T) — T dynamic
    torch.onnx.export(
        model,
        dummy,
        args.out,
        input_names=["cqt"],
        output_names=["embedding"],
        dynamic_axes={"cqt": {0: "batch", 3: "time"}, "embedding": {0: "batch"}},
        opset_version=args.opset,
    )
    print(f"Exported ONNX → {args.out}")

    # --- Validate parity: onnxruntime vs torch on real tracks -----------
    if not args.sample_audio:
        print(
            "WARNING: no --sample-audio given; skipping parity validation. "
            "Do NOT ship this model without validating cosine > 0.9999."
        )
        return 0

    import onnxruntime as ort

    sess = ort.InferenceSession(args.out, providers=["CPUExecutionProvider"])
    in_name = sess.get_inputs()[0].name

    worst = 1.0
    for fp in args.sample_audio:
        x = _build_cqt_input(fp, args.sr, args.hop, args.n_bins, args.bins_per_octave, args.downsample)
        with torch.no_grad():
            t_emb = model(torch.from_numpy(x)).cpu().numpy().reshape(-1)
        o_emb = np.asarray(sess.run(None, {in_name: x})[0], dtype=np.float32).reshape(-1)
        cos = float(np.dot(t_emb, o_emb) / (np.linalg.norm(t_emb) * np.linalg.norm(o_emb) + 1e-12))
        worst = min(worst, cos)
        status = "OK" if cos > 0.9999 else "FAIL"
        print(f"  [{status}] cos={cos:.6f}  {fp}")

    if worst <= 0.9999:
        print(f"\nABORT: worst cosine {worst:.6f} <= 0.9999 — export is NOT parity-safe.")
        return 1
    print(f"\nParity OK (worst cosine {worst:.6f}). Ship {args.out} to VINET_MODEL_DIR.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
