"""
Migration 019: Add followed_artists + monitored_artists.

Creates the two tables that back the Followed-Artists / new-release
notifications initiative (P0):

  - ``followed_artists``   : per-user follow edge (soft-delete). NULL
    ``unfollowed_at`` = active follow. Uniqueness per
    ``(user_id, coalesce(artist_mbid, artist_name_norm))`` is enforced at the
    app layer (``follow_service`` upsert), not a DB constraint, because
    SQLite/PG differ on partial/coalesce unique indexes. The composite index
    ``ix_followed_user_norm`` serves the name-fallback upsert + the
    ``GET /follows`` read.
  - ``monitored_artists``  : global watch set — one row per artist across all
    users (dedup). The P1 detection loop iterates THIS table, so N followers of
    one artist = 1 poll + 1 acquisition. ``artist_mbid`` and
    ``artist_name_norm`` are both unique (multiple NULLs distinct on SQLite +
    PG).

Both tables are also created automatically by ``Base.metadata.create_all``
when the app starts on a fresh database; this script exists for operators
who manage their schema explicitly or want to add the tables to a
long-running database without restarting the app.

Idempotent: uses CREATE TABLE / INDEX IF NOT EXISTS.

Usage:
    python migrations/019_add_follow_tables.py

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


_SQLITE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS followed_artists (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id           VARCHAR(128) NOT NULL,
        artist_mbid       VARCHAR(36),
        artist_name       VARCHAR(512) NOT NULL,
        artist_name_norm  VARCHAR(512) NOT NULL,
        image_url         VARCHAR(1024),
        source            VARCHAR(16) NOT NULL DEFAULT 'user',
        followed_at       INTEGER NOT NULL,
        unfollowed_at     INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS monitored_artists (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        artist_mbid            VARCHAR(36) UNIQUE,
        artist_name_norm       VARCHAR(512) NOT NULL UNIQUE,
        artist_name            VARCHAR(512) NOT NULL,
        lidarr_artist_id       INTEGER,
        lidarr_monitored       BOOLEAN NOT NULL DEFAULT 0,
        last_poll_at           INTEGER,
        last_seen_release_date INTEGER,
        active_follower_count  INTEGER NOT NULL DEFAULT 0,
        created_at             INTEGER NOT NULL
    )
    """,
    # indexes (mirror ORM index=True + the composite __table_args__ index):
    "CREATE INDEX IF NOT EXISTS ix_followed_artists_user_id ON followed_artists (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_followed_artists_artist_mbid ON followed_artists (artist_mbid)",
    "CREATE INDEX IF NOT EXISTS ix_followed_artists_artist_name_norm ON followed_artists (artist_name_norm)",
    "CREATE INDEX IF NOT EXISTS ix_followed_user_norm ON followed_artists (user_id, artist_name_norm)",
]


_POSTGRES_DDL = [
    """
    CREATE TABLE IF NOT EXISTS followed_artists (
        id                SERIAL PRIMARY KEY,
        user_id           VARCHAR(128) NOT NULL,
        artist_mbid       VARCHAR(36),
        artist_name       VARCHAR(512) NOT NULL,
        artist_name_norm  VARCHAR(512) NOT NULL,
        image_url         VARCHAR(1024),
        source            VARCHAR(16) NOT NULL DEFAULT 'user',
        followed_at       INTEGER NOT NULL,
        unfollowed_at     INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS monitored_artists (
        id                     SERIAL PRIMARY KEY,
        artist_mbid            VARCHAR(36) UNIQUE,
        artist_name_norm       VARCHAR(512) NOT NULL UNIQUE,
        artist_name            VARCHAR(512) NOT NULL,
        lidarr_artist_id       INTEGER,
        lidarr_monitored       BOOLEAN NOT NULL DEFAULT FALSE,
        last_poll_at           INTEGER,
        last_seen_release_date INTEGER,
        active_follower_count  INTEGER NOT NULL DEFAULT 0,
        created_at             INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_followed_artists_user_id ON followed_artists (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_followed_artists_artist_mbid ON followed_artists (artist_mbid)",
    "CREATE INDEX IF NOT EXISTS ix_followed_artists_artist_name_norm ON followed_artists (artist_name_norm)",
    "CREATE INDEX IF NOT EXISTS ix_followed_user_norm ON followed_artists (user_id, artist_name_norm)",
]


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    for stmt in _SQLITE_DDL:
        cursor.execute(stmt)
        print(f"  + {stmt.strip().splitlines()[0][:80]}")
    conn.commit()
    conn.close()
    print("\nMigration 019 complete.")


def migrate_postgres(database_url: str) -> None:
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is required for PostgreSQL migrations: pip install psycopg2-binary")

    url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cursor = conn.cursor()
    for stmt in _POSTGRES_DDL:
        cursor.execute(stmt)
        print(f"  + {stmt.strip().splitlines()[0][:80]}")
    conn.close()
    print("\nMigration 019 complete.")


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
