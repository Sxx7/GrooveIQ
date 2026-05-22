"""Parametric audio fixture *families* for embedding-quality testing.

``synth.py`` provides eight one-off fixtures, each targeting a distinct
historical bug. That shape is right for Layer 1 (per-fixture output bands)
but wrong for testing the *embedding*: an embedding-quality test needs
multiple tracks that genuinely sound alike, grouped into classes, so it
can measure whether same-class tracks land closer together in embedding
space than different-class tracks do.

This module generates four acoustically distinct *classes*, each with
three internally-varied *variants*:

    percussive  — kick/snare/hi-hat drum loops (broadband transients,
                  strong rhythm, no sustained pitch)
    tonal       — plucked-string arpeggios (clear pitch, exponential
                  decay envelopes, harmonic, sparse)
    distorted   — saturated power chords with palm-mute gating
                  (harmonic-rich, high energy, low-mid register)
    noise       — broadband noise textures (no pitch, no rhythm)

Variants within a class differ in tempo / key / seed / filtering but
share the dominant acoustic character. A *working* audio embedding places
the variants of one class near each other and far from the other
classes; a collapsed embedding (the pre-2.8 EffNet front-end bug) places
everything roughly equidistant. ``test_embedding_quality.py`` turns that
difference into a pass/fail signal.

All generators are deterministic (seeded RNG, fixed waveform maths) —
identical bytes on every run and platform.
"""

from __future__ import annotations

import numpy as np
from scipy import signal as sps

from tests.fixtures.audio.synth import SR

# >10 s clears the analyser's "too short" guard; <15 s means the EffNet
# mel front end consumes the whole clip rather than a centre crop.
DUR = 12.0
_N = int(DUR * SR)


def _normalize(x: np.ndarray, target_peak: float) -> np.ndarray:
    """Scale ``x`` so its peak magnitude equals ``target_peak``."""
    peak = float(np.max(np.abs(x)))
    if peak < 1e-8:
        return x.astype(np.float32)
    return (x * (target_peak / peak)).astype(np.float32)


# ---------------------------------------------------------------------------
# Class generators
# ---------------------------------------------------------------------------


def _percussive(bpm: float, seed: int) -> np.ndarray:
    """Kick + snare + hi-hat loop at *bpm*. Broadband transients, strong
    rhythm, no sustained pitch."""
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    sixteenth = beat / 4
    out = np.zeros(_N, dtype=np.float32)

    def kick(start_idx: int) -> None:
        if start_idx >= _N:
            return
        end = min(_N, start_idx + int(0.15 * SR))
        t = np.arange(end - start_idx, dtype=np.float32) / SR
        env = np.exp(-t * 35.0)
        freq = 120.0 - (60.0 * t / 0.15)  # pitch sweep for punch
        sig = np.sin(2 * np.pi * np.cumsum(freq) / SR)
        out[start_idx:end] += (sig * env * 0.9).astype(np.float32)

    def snare(start_idx: int) -> None:
        if start_idx >= _N:
            return
        end = min(_N, start_idx + int(0.12 * SR))
        t = np.arange(end - start_idx, dtype=np.float32) / SR
        env = np.exp(-t * 25.0)
        noise = rng.standard_normal(end - start_idx)
        b, a = sps.butter(4, [800 / (SR / 2), 4000 / (SR / 2)], btype="band")
        noise_f = sps.lfilter(b, a, noise)
        tone = np.sin(2 * np.pi * 200.0 * t)
        out[start_idx:end] += ((noise_f * 0.7 + tone * 0.3) * env * 0.6).astype(np.float32)

    def hat(start_idx: int) -> None:
        if start_idx >= _N:
            return
        end = min(_N, start_idx + int(0.04 * SR))
        t = np.arange(end - start_idx, dtype=np.float32) / SR
        env = np.exp(-t * 80.0)
        noise = rng.standard_normal(end - start_idx)
        b, a = sps.butter(4, 6000 / (SR / 2), btype="high")
        noise_f = sps.lfilter(b, a, noise)
        out[start_idx:end] += (noise_f * env * 0.25).astype(np.float32)

    for bar in range(int(DUR / (4 * beat)) + 1):
        bar_start = bar * 4 * beat
        for b_off in (0.0, 2.0):  # kick on 1, 3
            kick(int((bar_start + b_off * beat) * SR))
        for b_off in (1.0, 3.0):  # snare on 2, 4
            snare(int((bar_start + b_off * beat) * SR))
        for s in range(16):
            hat(int((bar_start + s * sixteenth) * SR))
    return _normalize(out, 0.85)


def _tonal(notes: list[float], bpm: float) -> np.ndarray:
    """Plucked-string arpeggio over *notes* at *bpm*. Clear pitch,
    exponential decay envelope, three harmonics, sparse."""
    beat = 60.0 / bpm
    out = np.zeros(_N, dtype=np.float32)
    step = 0
    while step * beat < DUR:
        start_idx = int(step * beat * SR)
        if start_idx >= _N:
            break
        end = min(_N, start_idx + int(beat * SR * 0.95))
        t = np.arange(end - start_idx, dtype=np.float32) / SR
        env = np.exp(-t * 2.5)
        freq = notes[step % len(notes)]
        sig = (
            np.sin(2 * np.pi * freq * t)
            + 0.4 * np.sin(2 * np.pi * 2 * freq * t)
            + 0.15 * np.sin(2 * np.pi * 3 * freq * t)
        )
        out[start_idx:end] += (sig * env * 0.5).astype(np.float32)
        step += 1
    return _normalize(out, 0.7)


def _distorted(roots: list[float], bpm: float, drive: float = 4.0) -> np.ndarray:
    """Distorted power chord over *roots* with 16th-note palm-mute gating
    at *bpm*. Harmonic-rich saturation, high energy, low-mid register."""
    sixteenth = (60.0 / bpm) / 4
    t = np.arange(_N, dtype=np.float32) / SR

    raw = np.zeros(_N, dtype=np.float32)
    for f in roots:
        raw += sps.sawtooth(2 * np.pi * f * t).astype(np.float32) * 0.4
    distorted = np.tanh(raw * drive).astype(np.float32)
    b, a = sps.butter(2, 80 / (SR / 2), btype="high")
    distorted = sps.lfilter(b, a, distorted).astype(np.float32)

    env = np.zeros(_N, dtype=np.float32)
    i = 0
    while True:
        start = int(i * sixteenth * SR)
        if start >= _N:
            break
        end = min(_N, start + int(sixteenth * SR * 0.7))
        x = np.arange(end - start, dtype=np.float32) / SR
        env[start:end] = np.exp(-x * 18.0)
        i += 1
    return _normalize(distorted * env, 0.85)


def _noise(kind: str, seed: int) -> np.ndarray:
    """Broadband noise texture. *kind* selects the spectral shaping:
    ``white`` (flat), ``low`` (low-pass rumble), ``band`` (mid hiss).
    No pitch, no rhythm in any variant."""
    rng = np.random.default_rng(seed)
    raw = rng.standard_normal(_N).astype(np.float32)
    if kind == "white":
        out = raw
    elif kind == "low":
        b, a = sps.butter(4, 1200 / (SR / 2), btype="low")
        out = sps.lfilter(b, a, raw).astype(np.float32)
    elif kind == "band":
        b, a = sps.butter(4, [1500 / (SR / 2), 5000 / (SR / 2)], btype="band")
        out = sps.lfilter(b, a, raw).astype(np.float32)
    else:
        raise ValueError(f"unknown noise kind: {kind!r}")
    return _normalize(out, 0.6)


# ---------------------------------------------------------------------------
# Family registry
# ---------------------------------------------------------------------------
#
# {class_name: {variant_name: zero-arg generator}}. Three variants per
# class — enough for stable intra-class statistics (3 intra-class pairs
# per class) and a meaningful nearest-neighbour purity check, while
# keeping the end-to-end test fast (12 short clips through real ONNX).

FAMILIES: dict[str, dict[str, object]] = {
    "percussive": {
        "perc_100bpm": lambda: _percussive(100.0, seed=1001),
        "perc_120bpm": lambda: _percussive(120.0, seed=1002),
        "perc_144bpm": lambda: _percussive(144.0, seed=1003),
    },
    "tonal": {
        "tonal_c_major": lambda: _tonal([261.63, 329.63, 392.00, 523.25], 70.0),
        "tonal_a_minor": lambda: _tonal([220.00, 261.63, 329.63, 440.00], 84.0),
        "tonal_g_major": lambda: _tonal([196.00, 246.94, 293.66, 392.00], 96.0),
    },
    "distorted": {
        "dist_e_160bpm": lambda: _distorted([82.41, 123.47, 164.81], 160.0),
        "dist_a_138bpm": lambda: _distorted([110.00, 164.81, 220.00], 138.0),
        "dist_d_112bpm": lambda: _distorted([73.42, 110.00, 146.83], 112.0),
    },
    "noise": {
        "noise_white": lambda: _noise("white", seed=2001),
        "noise_lowpass": lambda: _noise("low", seed=2002),
        "noise_bandpass": lambda: _noise("band", seed=2003),
    },
}


def build_families() -> dict[str, dict[str, np.ndarray]]:
    """Render every variant. Returns ``{class_name: {variant_name: audio}}``,
    each ``audio`` a float32 mono ndarray at ``SR``."""
    return {cls: {name: fn() for name, fn in variants.items()} for cls, variants in FAMILIES.items()}


def write_families(out_dir: str) -> dict[str, dict[str, str]]:
    """Write every variant as a 16-bit PCM WAV under *out_dir*.

    Returns ``{class_name: {variant_name: path}}``.
    """
    import os

    from scipy.io import wavfile

    os.makedirs(out_dir, exist_ok=True)
    paths: dict[str, dict[str, str]] = {}
    for cls, variants in build_families().items():
        paths[cls] = {}
        for name, audio in variants.items():
            path = os.path.join(out_dir, f"{name}.wav")
            pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
            wavfile.write(path, SR, pcm)
            paths[cls][name] = path
    return paths
