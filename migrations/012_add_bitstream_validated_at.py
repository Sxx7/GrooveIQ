"""
GrooveIQ migration 012 — add ``track_features.bitstream_validated_at``.

Issue #32 introduces an ffmpeg pre-flight decode check before audio files
are submitted to the analysis worker pool, so corrupt bitstreams are
caught in <100ms instead of hanging Essentia's MonoLoader for 5+ minutes.
A successful pre-flight (or successful analysis run) stamps the current
Unix epoch into this column so subsequent scans of an unchanged file
(matching ``file_hash``) skip the validation step.

Idempotent: re-running this script after a successful run is a no-op
because ``ALTER TABLE … ADD COLUMN`` is wrapped in a try/except.

Usage:
    python migrations/012_add_bitstream_validated_at.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from app.core.config import settings


def _sqlite_path() -> str:
    url = settings.DATABASE_URL
    if not url.startswith("sqlite"):
        raise SystemExit(
            "This migration only handles SQLite. For Postgres run:\n"
            "  ALTER TABLE track_features ADD COLUMN bitstream_validated_at INTEGER;"
        )
    return url.split("///", 1)[1]


def main() -> None:
    db_path = _sqlite_path()
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(track_features)")
        cols = {row[1] for row in cur.fetchall()}
        if "bitstream_validated_at" in cols:
            print("track_features.bitstream_validated_at already exists; nothing to do.")
            return
        cur.execute("ALTER TABLE track_features ADD COLUMN bitstream_validated_at INTEGER")
        conn.commit()
        print("Added track_features.bitstream_validated_at")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
