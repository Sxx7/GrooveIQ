"""
GrooveIQ – Audio analysis constants and utilities.

The actual analysis pipeline runs in long-lived worker processes managed
by ``analysis_worker.py``.  This module provides:

  - ``ANALYSIS_VERSION`` / ``EMBEDDING_DIM`` — shared constants
  - ``compute_file_hash()`` — fast file-identity hash for change detection
  - ``generate_track_id()`` — deterministic track ID from file path

These functions are imported by both the main process (scanner, DB upsert)
and worker subprocesses (hash check before analysis).  They intentionally
have **no** Essentia / ONNX / heavy-library imports.
"""

from __future__ import annotations

import hashlib
import os

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# Bump this whenever the analysis pipeline changes in a way that would
# produce different feature values.  The scanner compares stored versions
# against this constant to decide whether a track needs re-analysis.
#
# 2.4: fixed loudness (EBU R128 LUFS instead of un-normalised Stevens-law
# power-sum), valence (mood_happy proxy instead of unbounded approach-
# ability regression), and BPM clamping (drop RhythmExtractor2013's 738.28
# degenerate output rather than store it).
# 2.5: corrected mood_sad/relaxed/party column indices — those binary heads
# put the named class at col=1, not col=0, so the previous values were the
# *negation* of what the field name suggested.
# 2.6: replaced single-channel valence (`mood_happy`) with a composite of
# three EffNet mood heads weighted by their actual signal-to-noise on real
# libraries (#88). The `mood_happy` channel turned out to be pinned to
# [0, 0.46] across 67k tracks — useful for relative ranking, useless as
# an absolute UI value or as a ranker feature with variance.
ANALYSIS_VERSION = "2.6"

# Must match FAISS index dimension (faiss_index._EMBEDDING_DIM).
EMBEDDING_DIM = 64

# Mood labels emitted by the EffNet mood-classifier heads (see analysis_worker
# `mood_models`). The single source of truth API endpoints validate against —
# unknown labels passed to `?mood=` filters used to silently match nothing
# and surface as "no available tracks" in clients (e.g. iOS sending mood=
# "energetic"). Keep this in sync with `mood_models` in analysis_worker.py.
SUPPORTED_MOOD_LABELS: frozenset[str] = frozenset({"happy", "sad", "aggressive", "relaxed", "party"})


# ---------------------------------------------------------------------------
# File hashing
# ---------------------------------------------------------------------------


def compute_file_hash(path: str) -> str:
    """
    Fast file identity hash: SHA-256 of first 64 KB + file size + mtime.

    Reading the full file for SHA-256 is I/O-bound and redundant when the
    audio decoder will read it again for analysis.  Sampling the header
    plus stat metadata catches virtually all real-world changes (re-encodes,
    tag edits, file replacements) at a fraction of the cost.
    """
    stat = os.stat(path)
    h = hashlib.sha256()
    h.update(str(stat.st_size).encode())
    h.update(str(int(stat.st_mtime)).encode())
    with open(path, "rb") as f:
        h.update(f.read(65_536))  # first 64 KB
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Track ID generation
# ---------------------------------------------------------------------------


def generate_track_id(file_path: str) -> str:
    """
    Generate a stable track_id from the file's path relative to the music
    library root.

    Uses SHA-256 of the relative path so that:
    - Two files with the same name in different folders get different IDs.
    - The same file always gets the same ID across re-scans.
    - IDs are a fixed 16-char hex string (collision-safe for any realistic
      library size).
    """
    from app.core.config import settings

    try:
        rel = os.path.relpath(file_path, settings.MUSIC_LIBRARY_PATH)
    except ValueError:
        rel = file_path
    return hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16]
