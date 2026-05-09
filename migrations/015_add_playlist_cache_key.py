"""
Migration 015: Add daily idempotency cache_key column to playlists.

Issue #89 — POST /v1/playlists creates a persistent row on every call. All
strategies (text/mood/energy_curve/flow/key_compatible/path) are deterministic
with the same params, so when a frontend re-issues the same request (Play
button, daily refresh), the table accumulates duplicate playlists. The route
now derives a per-(owner, strategy, params, UTC-day) cache_key and returns
the existing row on a hit. This script adds the column + index for operators
who already ran migration 014 and want to upgrade an existing schema.

Fresh databases get this column automatically via Base.metadata.create_all
and _apply_column_migrations in app/db/session.py.

Idempotent: SQLite ALTERs that fail (column already exists) are swallowed;
Postgres uses ADD COLUMN IF NOT EXISTS.

Usage:
    python migrations/015_add_playlist_cache_key.py
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


_SQLITE_STMTS = [
    "ALTER TABLE playlists ADD COLUMN cache_key VARCHAR(64)",
    "CREATE INDEX IF NOT EXISTS ix_playlists_cache_key ON playlists (cache_key)",
]


_POSTGRES_STMTS = [
    "ALTER TABLE playlists ADD COLUMN IF NOT EXISTS cache_key VARCHAR(64)",
    "CREATE INDEX IF NOT EXISTS ix_playlists_cache_key ON playlists (cache_key)",
]


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    for stmt in _SQLITE_STMTS:
        try:
            cursor.execute(stmt)
            print(f"  + {stmt[:80]}")
        except sqlite3.OperationalError as exc:
            # "duplicate column name" on ALTER, or "already exists" on CREATE INDEX — both fine.
            print(f"  · {stmt[:80]}  ({exc})")
    conn.commit()
    conn.close()
    print("\nMigration 015 complete.")


def migrate_postgres(database_url: str) -> None:
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is required for PostgreSQL migrations: pip install psycopg2-binary")

    url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cursor = conn.cursor()
    for stmt in _POSTGRES_STMTS:
        cursor.execute(stmt)
        print(f"  + {stmt[:80]}")
    conn.close()
    print("\nMigration 015 complete.")


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
