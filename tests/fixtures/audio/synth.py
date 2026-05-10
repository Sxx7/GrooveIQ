"""Deterministic audio fixtures for the analysis pipeline.

Each function returns ``(samplerate, np.float32 mono ndarray)``. The
``test_analysis_fixtures`` runner writes them to a session-scoped temp dir
and feeds each through the production ``_analyze_file()`` pipeline, then
asserts every numeric output is inside the band declared in
``manifest.json``.

Synthesised rather than sampled real audio because:
- 100% reproducible, no network at test time, no licensing concerns.
- ONNX classifiers respond to broad acoustic features (energy, spectral
  shape, rhythm, modulation envelope) which we *can* synthesise faithfully
  enough to drive distinct mood / danceability / instrumentalness scores.
- Each fixture is precisely targeted at one or more historical bug class.
  Real audio would couple the test to whatever incidental properties the
  recording happens to have.
"""

from __future__ import annotations

import numpy as np
from scipy import signal as sps

SR = 16000  # matches EffNet's expected input rate
DUR_DEFAULT = 12.0  # >10s so the analyser's "too short" guard doesn't fire


def _t(duration: float = DUR_DEFAULT) -> np.ndarray:
    return np.arange(int(duration * SR), dtype=np.float32) / SR


def _norm(x: np.ndarray, target_peak: float = 0.8) -> np.ndarray:
    peak = float(np.max(np.abs(x)))
    if peak < 1e-8:
        return x.astype(np.float32)
    return (x * (target_peak / peak)).astype(np.float32)


def silence() -> tuple[int, np.ndarray]:
    """Pure zeros. Pins #42 (zero-norm embedding → None) and the no-NaN
    contract on degenerate input."""
    return SR, np.zeros(int(DUR_DEFAULT * SR), dtype=np.float32)


def pure_sine() -> tuple[int, np.ndarray]:
    """440Hz concert-A sine. Single-tone, fully deterministic — exercises
    key/mode detection and confirms the embedding is non-degenerate even
    when the spectrum collapses to one peak."""
    return SR, (np.sin(2 * np.pi * 440.0 * _t()) * 0.6).astype(np.float32)


def too_short() -> tuple[int, np.ndarray]:
    """5s clip — the analyser must short-circuit with
    ``analysis_error="Track too short (<10 s)"`` and not write garbage to
    the other columns."""
    return SR, (np.sin(2 * np.pi * 440.0 * _t(5.0)) * 0.5).astype(np.float32)


def white_noise() -> tuple[int, np.ndarray]:
    """Broadband white noise. Spectrally flat = no tonal content; embedding
    must still be non-degenerate, no classifier may produce NaN."""
    rng = np.random.default_rng(20240101)
    return SR, (rng.standard_normal(int(DUR_DEFAULT * SR)) * 0.2).astype(np.float32)


def drum_loop() -> tuple[int, np.ndarray]:
    """120 BPM kick+snare+hi-hat. Strong rhythm = BPM extraction lands in
    [110, 130]; broadband transients = healthy ``energy`` and danceability."""
    rng = np.random.default_rng(20240102)
    bpm = 120.0
    beat = 60.0 / bpm
    sixteenth = beat / 4
    n = int(DUR_DEFAULT * SR)
    out = np.zeros(n, dtype=np.float32)

    def kick(start_idx: int) -> None:
        if start_idx >= n:
            return
        ln = int(0.15 * SR)
        end = min(n, start_idx + ln)
        t = np.arange(end - start_idx, dtype=np.float32) / SR
        env = np.exp(-t * 35.0)
        freq = 120.0 - (60.0 * t / 0.15)  # pitch sweep for punch
        sig = np.sin(2 * np.pi * np.cumsum(freq) / SR)
        out[start_idx:end] += (sig * env * 0.9).astype(np.float32)

    def snare(start_idx: int) -> None:
        if start_idx >= n:
            return
        ln = int(0.12 * SR)
        end = min(n, start_idx + ln)
        t = np.arange(end - start_idx, dtype=np.float32) / SR
        env = np.exp(-t * 25.0)
        noise = rng.standard_normal(end - start_idx)
        b, a = sps.butter(4, [800 / (SR / 2), 4000 / (SR / 2)], btype="band")
        noise_f = sps.lfilter(b, a, noise)
        tone = np.sin(2 * np.pi * 200.0 * t)
        out[start_idx:end] += ((noise_f * 0.7 + tone * 0.3) * env * 0.6).astype(np.float32)

    def hat(start_idx: int) -> None:
        if start_idx >= n:
            return
        ln = int(0.04 * SR)
        end = min(n, start_idx + ln)
        t = np.arange(end - start_idx, dtype=np.float32) / SR
        env = np.exp(-t * 80.0)
        noise = rng.standard_normal(end - start_idx)
        b, a = sps.butter(4, 6000 / (SR / 2), btype="high")
        noise_f = sps.lfilter(b, a, noise)
        out[start_idx:end] += (noise_f * env * 0.25).astype(np.float32)

    for bar in range(int(DUR_DEFAULT / (4 * beat)) + 1):
        bar_start = bar * 4 * beat
        for b_off in (0.0, 2.0):  # kick on 1, 3
            kick(int((bar_start + b_off * beat) * SR))
        for b_off in (1.0, 3.0):  # snare on 2, 4
            snare(int((bar_start + b_off * beat) * SR))
        for s in range(16):
            hat(int((bar_start + s * sixteenth) * SR))
    return SR, _norm(out, 0.85)


def acoustic_arpeggio() -> tuple[int, np.ndarray]:
    """C-major arpeggio at 70 BPM with plucked-string envelope. Tonal +
    slow = key/mode populated, energy below the drum loop, danceability
    distinguishably lower."""
    bpm = 70.0
    beat = 60.0 / bpm
    n = int(DUR_DEFAULT * SR)
    out = np.zeros(n, dtype=np.float32)
    notes = [261.63, 329.63, 392.00, 523.25]  # C4, E4, G4, C5

    step = 0
    while step * beat < DUR_DEFAULT:
        start_idx = int(step * beat * SR)
        if start_idx >= n:
            break
        ln = int(beat * SR * 0.95)
        end = min(n, start_idx + ln)
        t = np.arange(end - start_idx, dtype=np.float32) / SR
        env = np.exp(-t * 2.5)
        freq = notes[step % 4]
        sig = (
            np.sin(2 * np.pi * freq * t)
            + 0.4 * np.sin(2 * np.pi * 2 * freq * t)
            + 0.15 * np.sin(2 * np.pi * 3 * freq * t)
        )
        out[start_idx:end] += (sig * env * 0.5).astype(np.float32)
        step += 1
    return SR, _norm(out, 0.7)


def speech_like() -> tuple[int, np.ndarray]:
    """Voice-shaped signal: ~120Hz glottal pulse train through three formant
    resonances (F1=500, F2=1500, F3=2500) with syllable-rate amplitude
    modulation and word-gap pauses.

    The smoking-gun fixture for #99. The voice/instrumental classifier
    should put most of its mass on the ``voice`` class, so when the column
    inversion is fixed the stored ``instrumentalness`` (col 0 =
    ``instrumental``) is LOW. Pre-fix code reads col 1 = ``voice`` and
    stores high ``instrumentalness`` for this fixture, which is the bug.
    """
    rng = np.random.default_rng(20240103)
    n = int(DUR_DEFAULT * SR)
    t = np.arange(n, dtype=np.float32) / SR

    f0 = 120.0
    jitter = rng.standard_normal(n).astype(np.float32) * 1.5
    phase = 2 * np.pi * np.cumsum(f0 + jitter) / SR
    excitation = np.where(np.cos(phase) > 0.85, 1.0, 0.0).astype(np.float32)
    excitation += rng.standard_normal(n).astype(np.float32) * 0.05

    voice = np.zeros(n, dtype=np.float32)
    for cf, bw in [(500.0, 80.0), (1500.0, 100.0), (2500.0, 120.0)]:
        b, a = sps.butter(2, [(cf - bw) / (SR / 2), (cf + bw) / (SR / 2)], btype="band")
        voice += sps.lfilter(b, a, excitation).astype(np.float32) * 0.4

    syll_env = 0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 3.5 * t + 0.7))
    word_gap = (np.sin(2 * np.pi * 0.4 * t) > 0.7).astype(np.float32)
    voice = voice * syll_env * (1.0 - 0.7 * word_gap)
    return SR, _norm(voice, 0.7)


def distorted_metal() -> tuple[int, np.ndarray]:
    """E-power-chord on distorted sawtooth waves with 16th-note palm-mute
    gating at 160 BPM. High energy, harmonic-rich, low acousticness — the
    band most likely to activate ``mood_aggressive``."""
    bpm = 160.0
    sixteenth = (60.0 / bpm) / 4
    n = int(DUR_DEFAULT * SR)
    t = np.arange(n, dtype=np.float32) / SR

    raw = np.zeros(n, dtype=np.float32)
    for f in (82.41, 123.47, 164.81):  # E2, B2, E3
        raw += sps.sawtooth(2 * np.pi * f * t).astype(np.float32) * 0.4
    distorted = np.tanh(raw * 4.0).astype(np.float32)
    b, a = sps.butter(2, 80 / (SR / 2), btype="high")
    distorted = sps.lfilter(b, a, distorted).astype(np.float32)

    env = np.zeros(n, dtype=np.float32)
    for i in range(int(DUR_DEFAULT / sixteenth) + 1):
        start = int(i * sixteenth * SR)
        if start >= n:
            break
        ln = int(sixteenth * SR * 0.7)
        end = min(n, start + ln)
        x = np.arange(end - start, dtype=np.float32) / SR
        env[start:end] = np.exp(-x * 18.0)
    return SR, _norm(distorted * env, 0.85)


FIXTURES = {
    "silence":            silence,
    "pure_sine":          pure_sine,
    "too_short":          too_short,
    "white_noise":        white_noise,
    "drum_loop":          drum_loop,
    "acoustic_arpeggio":  acoustic_arpeggio,
    "speech_like":        speech_like,
    "distorted_metal":    distorted_metal,
}


def write_all(out_dir: str) -> dict[str, str]:
    """Write every fixture as a 16-bit PCM WAV. Returns ``{name: path}``."""
    import os
    from scipy.io import wavfile

    os.makedirs(out_dir, exist_ok=True)
    paths: dict[str, str] = {}
    for name, fn in FIXTURES.items():
        sr, audio = fn()
        path = os.path.join(out_dir, f"{name}.wav")
        # 16-bit PCM = compatible with every audio backend (essentia, ffmpeg).
        pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
        wavfile.write(path, sr, pcm)
        paths[name] = path
    return paths
