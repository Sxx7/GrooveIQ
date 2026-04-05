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
ANALYSIS_VERSION = "2.1"

# Must match FAISS index dimension (faiss_index._EMBEDDING_DIM).
EMBEDDING_DIM = 64


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
