"""
Migration 020: Add release_events + user_release_notifications.

Creates the two tables that back the followed-artist new-release detection +
feed (P1):

  - ``release_events``              : global, one row per detected release.
    ``available_at`` is stamped ONLY when the release's tracks have
    ``media_server_id`` (streamable) — the "only notify for playable music"
    guarantee. ``release_key`` is unique for idempotent detection.
  - ``user_release_notifications``  : per-user fanout + feed rows. The unique
    ``(user_id, release_event_id)`` makes the fanout idempotent.

Both tables are also created automatically by ``Base.metadata.create_all``
when the app starts on a fresh database; this script exists for operators who
manage their schema explicitly or want to add the tables to a long-running
database without restarting the app.

Idempotent: uses CREATE TABLE / INDEX IF NOT EXISTS. Index names match the
``create_all`` auto-generated names (``ix_<table>_<column>``) so the fresh-DB
and existing-DB paths converge.

Usage:
    python migrations/020_add_release_events_tables.py

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
    CREATE TABLE IF NOT EXISTS release_events (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        release_key            VARCHAR(255) NOT NULL,
        release_group_mbid     VARCHAR(36),
        artist_mbid            VARCHAR(36),
        artist_name            VARCHAR(512) NOT NULL,
        artist_name_norm       VARCHAR(512),
        album_title            VARCHAR(512) NOT NULL,
        album_title_norm       VARCHAR(512),
        kind                   VARCHAR(16) NOT NULL DEFAULT 'album',
        first_release_date     INTEGER,
        detected_at            INTEGER NOT NULL,
        acquisition_state      VARCHAR(16) NOT NULL,
        available_at           INTEGER,
        track_count_total      INTEGER,
        track_count_available  INTEGER NOT NULL DEFAULT 0,
        cover_url              VARCHAR(1024),
        source                 VARCHAR(24) NOT NULL,
        last_acq_task_id       VARCHAR(64),
        created_at             INTEGER NOT NULL,
        updated_at             INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_release_notifications (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id            VARCHAR(64) NOT NULL,
        release_event_id   INTEGER NOT NULL REFERENCES release_events(id),
        created_at         INTEGER NOT NULL,
        eligible           BOOLEAN NOT NULL DEFAULT 1,
        dispatch_state     VARCHAR(16) NOT NULL DEFAULT 'pending',
        notified_at        INTEGER,
        seen_at            INTEGER
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_release_events_release_key ON release_events (release_key)",
    "CREATE INDEX IF NOT EXISTS ix_release_events_artist_mbid ON release_events (artist_mbid)",
    "CREATE INDEX IF NOT EXISTS ix_release_events_available_at ON release_events (available_at)",
    "CREATE INDEX IF NOT EXISTS ix_release_events_acquisition_state ON release_events (acquisition_state)",
    "CREATE INDEX IF NOT EXISTS ix_release_events_reconcile ON release_events (artist_name_norm, album_title_norm)",
    "CREATE INDEX IF NOT EXISTS ix_user_release_notifications_user_id ON user_release_notifications (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_user_release_notifications_release_event_id ON user_release_notifications (release_event_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_urn_user_release ON user_release_notifications (user_id, release_event_id)",
    "CREATE INDEX IF NOT EXISTS ix_urn_user_seen ON user_release_notifications (user_id, seen_at)",
]


_POSTGRES_DDL = [
    """
    CREATE TABLE IF NOT EXISTS release_events (
        id                     SERIAL PRIMARY KEY,
        release_key            VARCHAR(255) NOT NULL,
        release_group_mbid     VARCHAR(36),
        artist_mbid            VARCHAR(36),
        artist_name            VARCHAR(512) NOT NULL,
        artist_name_norm       VARCHAR(512),
        album_title            VARCHAR(512) NOT NULL,
        album_title_norm       VARCHAR(512),
        kind                   VARCHAR(16) NOT NULL DEFAULT 'album',
        first_release_date     INTEGER,
        detected_at            INTEGER NOT NULL,
        acquisition_state      VARCHAR(16) NOT NULL,
        available_at           INTEGER,
        track_count_total      INTEGER,
        track_count_available  INTEGER NOT NULL DEFAULT 0,
        cover_url              VARCHAR(1024),
        source                 VARCHAR(24) NOT NULL,
        last_acq_task_id       VARCHAR(64),
        created_at             INTEGER NOT NULL,
        updated_at             INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_release_notifications (
        id                 SERIAL PRIMARY KEY,
        user_id            VARCHAR(64) NOT NULL,
        release_event_id   INTEGER NOT NULL REFERENCES release_events(id),
        created_at         INTEGER NOT NULL,
        eligible           BOOLEAN NOT NULL DEFAULT TRUE,
        dispatch_state     VARCHAR(16) NOT NULL DEFAULT 'pending',
        notified_at        INTEGER,
        seen_at            INTEGER
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_release_events_release_key ON release_events (release_key)",
    "CREATE INDEX IF NOT EXISTS ix_release_events_artist_mbid ON release_events (artist_mbid)",
    "CREATE INDEX IF NOT EXISTS ix_release_events_available_at ON release_events (available_at)",
    "CREATE INDEX IF NOT EXISTS ix_release_events_acquisition_state ON release_events (acquisition_state)",
    "CREATE INDEX IF NOT EXISTS ix_release_events_reconcile ON release_events (artist_name_norm, album_title_norm)",
    "CREATE INDEX IF NOT EXISTS ix_user_release_notifications_user_id ON user_release_notifications (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_user_release_notifications_release_event_id ON user_release_notifications (release_event_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_urn_user_release ON user_release_notifications (user_id, release_event_id)",
    "CREATE INDEX IF NOT EXISTS ix_urn_user_seen ON user_release_notifications (user_id, seen_at)",
]


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    for stmt in _SQLITE_DDL:
        cursor.execute(stmt)
        print(f"  + {stmt.strip().splitlines()[0][:80]}")
    conn.commit()
    conn.close()
    print("\nMigration 020 complete.")


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
    print("\nMigration 020 complete.")


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
