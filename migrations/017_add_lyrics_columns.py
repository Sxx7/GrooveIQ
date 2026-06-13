"""
Migration 017: Add lyrics-acquisition columns to track_features.

Adds nine new nullable columns that hold the result of the lyrics cascade
(embedded tags -> LRCLIB -> ASR; see app/services/lyrics.py):

  - lyrics_plain      TEXT         — newline-joined plain lyrics
  - lyrics_synced     TEXT         — LRC ("[mm:ss.xx] line"), null = unsynced
  - lyrics_source     VARCHAR(16)  — embedded|lrclib|asr|instrumental|none
  - lyrics_quality    INTEGER      — display-quality rank (higher = better)
  - lyrics_language   VARCHAR(8)   — ISO 639-1
  - is_explicit       BOOLEAN      — profanity-lexicon flag (Phase D)
  - lyrics_embedding  TEXT         — base64 float32 ONNX text vector (Phase D)
  - lyrics_version    VARCHAR(16)  — acquisition pipeline version
  - lyrics_fetched_at INTEGER      — unix ts of last resolution

All columns are additive and default NULL. The lyrics feature is gated behind
``LYRICS_ENABLED`` (off by default), so rolling this out is safe on an existing
DB. ``lyrics_version`` is intentionally decoupled from ``ANALYSIS_VERSION`` so
refreshing lyrics never triggers a full Essentia re-scan.

The same ALTERs are also applied automatically by the in-app migration in
``app/db/session.py::_apply_column_migrations`` on startup. This standalone
script is provided for operators who prefer explicit migrations.

Idempotent: checks column existence before adding.

Usage:
    python migrations/017_add_lyrics_columns.py

Reads DATABASE_URL from the environment / .env (same as the app).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


_COLUMNS = [
    ("lyrics_plain", "TEXT"),
    ("lyrics_synced", "TEXT"),
    ("lyrics_source", "VARCHAR(16)"),
    ("lyrics_quality", "INTEGER"),
    ("lyrics_language", "VARCHAR(8)"),
    ("is_explicit", "BOOLEAN"),
    ("lyrics_embedding", "TEXT"),
    ("lyrics_version", "VARCHAR(16)"),
    ("lyrics_fetched_at", "INTEGER"),
]


def _sqlite_columns(cursor, table: str) -> dict:
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1]: row for row in cursor.fetchall()}


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cols = _sqlite_columns(cursor, "track_features")
    if not cols:
        sys.exit("Table track_features does not exist — run the app once to create it.")

    for name, coltype in _COLUMNS:
        if name not in cols:
            cursor.execute(f"ALTER TABLE track_features ADD COLUMN {name} {coltype}")
            print(f"  + column track_features.{name}")
        else:
            print(f"  ~ column track_features.{name} already exists")

    conn.commit()
    conn.close()
    print("\nMigration 017 complete.")


def migrate_postgres(database_url: str) -> None:
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is required for PostgreSQL migrations: pip install psycopg2-binary")

    url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cursor = conn.cursor()

    def col_exists(name: str) -> bool:
        cursor.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'track_features' AND column_name = %s
            """,
            (name,),
        )
        return cursor.fetchone() is not None

    pg_types = {
        "TEXT": "TEXT",
        "INTEGER": "INTEGER",
        "BOOLEAN": "BOOLEAN",
        "VARCHAR(16)": "VARCHAR(16)",
        "VARCHAR(8)": "VARCHAR(8)",
    }
    for name, coltype in _COLUMNS:
        if not col_exists(name):
            cursor.execute(
                f"ALTER TABLE track_features ADD COLUMN {name} {pg_types[coltype]}"
            )
            print(f"  + column track_features.{name}")
        else:
            print(f"  ~ column track_features.{name} already exists")

    conn.close()
    print("\nMigration 017 complete.")


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:////data/grooveiq.db")
    print(f"Migrating: {database_url}\n")

    if "sqlite" in database_url:
        db_path = database_url.split("///", 1)[-1]
        if not Path(db_path).exists():
            sys.exit(f"Database file not found: {db_path}")
        migrate_sqlite(db_path)
    elif "postgresql" in database_url:
        migrate_postgres(database_url)
    else:
        sys.exit(f"Unsupported DATABASE_URL scheme: {database_url}")


if __name__ == "__main__":
    main()
