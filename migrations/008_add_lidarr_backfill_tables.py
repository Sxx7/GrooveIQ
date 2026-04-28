"""
Migration 008: Add lidarr_backfill_configs + lidarr_backfill_requests.

Creates the two tables that back the Lidarr backfill engine:

  - ``lidarr_backfill_configs``  : versioned policy snapshots (mirrors the
    ``algorithm_configs`` / ``download_routing_configs`` shape).
  - ``lidarr_backfill_requests`` : per-album state machine. The
    ``(created_at)`` index supports the rate-limit window query that runs on
    every scheduler tick; ``(status, next_retry_at)`` supports candidate
    selection.

Both tables are also created automatically by ``Base.metadata.create_all``
when the app starts on a fresh database; this script exists for operators
who manage their schema explicitly or want to add the tables to a
long-running database without restarting the app.

Idempotent: uses CREATE TABLE / INDEX IF NOT EXISTS.

Usage:
    python migrations/008_add_lidarr_backfill_tables.py

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
    CREATE TABLE IF NOT EXISTS lidarr_backfill_configs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        version     INTEGER NOT NULL,
        name        VARCHAR(256),
        config      TEXT NOT NULL,
        is_active   BOOLEAN NOT NULL DEFAULT 0,
        created_at  INTEGER NOT NULL,
        created_by  VARCHAR(128)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lidarr_backfill_requests (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        lidarr_album_id     INTEGER NOT NULL UNIQUE,
        mb_album_id         VARCHAR(64),
        artist              VARCHAR(255) NOT NULL,
        album_title         VARCHAR(255) NOT NULL,
        source              VARCHAR(16) NOT NULL,
        match_score         REAL,
        picked_service      VARCHAR(32),
        picked_album_id     VARCHAR(64),
        streamrip_task_id   VARCHAR(64),
        status              VARCHAR(24) NOT NULL,
        attempt_count       INTEGER NOT NULL DEFAULT 0,
        last_attempt_at     INTEGER,
        next_retry_at       INTEGER,
        last_error          VARCHAR(1024),
        created_at          INTEGER NOT NULL,
        updated_at          INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_lbf_config_active ON lidarr_backfill_configs (is_active)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_config_version ON lidarr_backfill_configs (version)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_lidarr_album_id ON lidarr_backfill_requests (lidarr_album_id)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_mb_album_id ON lidarr_backfill_requests (mb_album_id)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_status ON lidarr_backfill_requests (status)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_next_retry ON lidarr_backfill_requests (next_retry_at)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_status_retry ON lidarr_backfill_requests (status, next_retry_at)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_created ON lidarr_backfill_requests (created_at)",
]


_POSTGRES_DDL = [
    """
    CREATE TABLE IF NOT EXISTS lidarr_backfill_configs (
        id          SERIAL PRIMARY KEY,
        version     INTEGER NOT NULL,
        name        VARCHAR(256),
        config      JSONB NOT NULL,
        is_active   BOOLEAN NOT NULL DEFAULT FALSE,
        created_at  INTEGER NOT NULL,
        created_by  VARCHAR(128)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lidarr_backfill_requests (
        id                  SERIAL PRIMARY KEY,
        lidarr_album_id     INTEGER NOT NULL UNIQUE,
        mb_album_id         VARCHAR(64),
        artist              VARCHAR(255) NOT NULL,
        album_title         VARCHAR(255) NOT NULL,
        source              VARCHAR(16) NOT NULL,
        match_score         DOUBLE PRECISION,
        picked_service      VARCHAR(32),
        picked_album_id     VARCHAR(64),
        streamrip_task_id   VARCHAR(64),
        status              VARCHAR(24) NOT NULL,
        attempt_count       INTEGER NOT NULL DEFAULT 0,
        last_attempt_at     INTEGER,
        next_retry_at       INTEGER,
        last_error          VARCHAR(1024),
        created_at          INTEGER NOT NULL,
        updated_at          INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_lbf_config_active ON lidarr_backfill_configs (is_active)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_config_version ON lidarr_backfill_configs (version)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_lidarr_album_id ON lidarr_backfill_requests (lidarr_album_id)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_mb_album_id ON lidarr_backfill_requests (mb_album_id)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_status ON lidarr_backfill_requests (status)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_next_retry ON lidarr_backfill_requests (next_retry_at)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_status_retry ON lidarr_backfill_requests (status, next_retry_at)",
    "CREATE INDEX IF NOT EXISTS ix_lbf_created ON lidarr_backfill_requests (created_at)",
]


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    for stmt in _SQLITE_DDL:
        cursor.execute(stmt)
        print(f"  + {stmt.strip().splitlines()[0][:80]}")
    conn.commit()
    conn.close()
    print("\nMigration 008 complete.")


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
    print("\nMigration 008 complete.")


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
