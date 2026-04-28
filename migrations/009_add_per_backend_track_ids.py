"""
Migration 009: Add per-backend external track ID columns to track_features.

Part of issue #37 (Track-ID schema rework). Phase 1 only — schema additions,
non-destructive and reversible. The data reshape (Phase 2) is implemented
separately in migrations/010_*.py once Phase 1 has shipped.

Adds six new nullable VARCHAR(64) columns + unique indexes:

  - media_server_id   — Navidrome song ID (set by /v1/library/sync)
  - spotify_id        — Spotify track ID
  - qobuz_id          — Qobuz numeric track ID
  - tidal_id          — Tidal track ID
  - deezer_id         — Deezer track ID
  - soundcloud_id     — SoundCloud track ID

Plus a plain (non-unique) index on the existing musicbrainz_track_id column
to speed up lookups.

The same ALTERs are applied automatically by the in-app migration in
``app/db/session.py::_apply_column_migrations`` on startup. This standalone
script is provided for operators who prefer explicit migrations.

Idempotent: checks column / index existence before mutating.

Usage:
    python migrations/009_add_per_backend_track_ids.py

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


_NEW_COLUMNS = [
    ("media_server_id", "VARCHAR(64)"),
    ("spotify_id", "VARCHAR(64)"),
    ("qobuz_id", "VARCHAR(64)"),
    ("tidal_id", "VARCHAR(64)"),
    ("deezer_id", "VARCHAR(64)"),
    ("soundcloud_id", "VARCHAR(64)"),
]

# Each per-backend ID column gets a UNIQUE index so duplicate detection at
# sync/download time is a single SQL constraint. NULLs are distinct in both
# SQLite and PostgreSQL UNIQUE indexes, which is what we want.
_UNIQUE_INDEXES = [name for name, _ in _NEW_COLUMNS]


def _sqlite_columns(cursor, table: str) -> dict:
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1]: row for row in cursor.fetchall()}


def _sqlite_indexes(cursor, table: str) -> set:
    cursor.execute(f"PRAGMA index_list({table})")
    return {row[1] for row in cursor.fetchall()}


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cols = _sqlite_columns(cursor, "track_features")
    if not cols:
        sys.exit("Table track_features does not exist — run the app once to create it.")

    for name, coltype in _NEW_COLUMNS:
        if name not in cols:
            cursor.execute(f"ALTER TABLE track_features ADD COLUMN {name} {coltype}")
            print(f"  + column track_features.{name}")
        else:
            print(f"  ~ column track_features.{name} already exists")

    indexes = _sqlite_indexes(cursor, "track_features")
    for col in _UNIQUE_INDEXES:
        idx_name = f"ix_track_features_{col}"
        if idx_name not in indexes:
            cursor.execute(
                f"CREATE UNIQUE INDEX {idx_name} ON track_features ({col})"
            )
            print(f"  + unique index {idx_name}")
        else:
            print(f"  ~ unique index {idx_name} already exists")

    mbid_idx = "ix_track_features_musicbrainz_track_id"
    if mbid_idx not in indexes:
        cursor.execute(
            f"CREATE INDEX {mbid_idx} ON track_features (musicbrainz_track_id)"
        )
        print(f"  + index {mbid_idx}")
    else:
        print(f"  ~ index {mbid_idx} already exists")

    conn.commit()
    conn.close()
    print("\nMigration 009 complete.")


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

    def index_exists(name: str) -> bool:
        cursor.execute(
            """
            SELECT 1 FROM pg_indexes
            WHERE tablename = 'track_features' AND indexname = %s
            """,
            (name,),
        )
        return cursor.fetchone() is not None

    pg_types = {"VARCHAR(64)": "VARCHAR(64)"}
    for name, coltype in _NEW_COLUMNS:
        if not col_exists(name):
            cursor.execute(
                f"ALTER TABLE track_features ADD COLUMN {name} {pg_types[coltype]}"
            )
            print(f"  + column track_features.{name}")
        else:
            print(f"  ~ column track_features.{name} already exists")

    for col in _UNIQUE_INDEXES:
        idx_name = f"ix_track_features_{col}"
        if not index_exists(idx_name):
            cursor.execute(
                f"CREATE UNIQUE INDEX {idx_name} ON track_features ({col})"
            )
            print(f"  + unique index {idx_name}")
        else:
            print(f"  ~ unique index {idx_name} already exists")

    mbid_idx = "ix_track_features_musicbrainz_track_id"
    if not index_exists(mbid_idx):
        cursor.execute(
            f"CREATE INDEX {mbid_idx} ON track_features (musicbrainz_track_id)"
        )
        print(f"  + index {mbid_idx}")
    else:
        print(f"  ~ index {mbid_idx} already exists")

    conn.close()
    print("\nMigration 009 complete.")


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
