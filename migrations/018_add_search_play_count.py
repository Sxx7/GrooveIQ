"""
Migration 018: Add search_play_count to track_interactions.

Adds one new column that powers the resurfacing "Special tracks" (candidate) card:

  - search_play_count INTEGER DEFAULT 0 — full listens that originated from a search
    (event context_type/surface == "search")

A searched-and-fully-played track is a strong but ambiguous intent signal (the user went
looking for it — but maybe on a friend's recommendation they won't actually keep), so it is
nominated to the resurfacing candidate card for verification rather than trusted outright. The
column is a strict subset of full_listen_count and is additive with DEFAULT 0, so rolling this
out on an existing DB is safe — existing rows backfill to 0 and earn no spurious nominations.

The same ALTER is also applied automatically by the in-app migration in
``app/db/session.py::_apply_column_migrations`` on startup. This standalone script is provided
for operators who prefer explicit migrations.

Idempotent: checks column existence before adding.

Usage:
    python migrations/018_add_search_play_count.py

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


_TABLE = "track_interactions"
_COLUMNS = [
    ("search_play_count", "INTEGER DEFAULT 0"),
]


def _sqlite_columns(cursor, table: str) -> dict:
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1]: row for row in cursor.fetchall()}


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cols = _sqlite_columns(cursor, _TABLE)
    if not cols:
        sys.exit(f"Table {_TABLE} does not exist — run the app once to create it.")

    for name, coltype in _COLUMNS:
        if name not in cols:
            cursor.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {name} {coltype}")
            print(f"  + column {_TABLE}.{name}")
        else:
            print(f"  ~ column {_TABLE}.{name} already exists")

    conn.commit()
    conn.close()
    print("\nMigration 018 complete.")


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
            WHERE table_name = %s AND column_name = %s
            """,
            (_TABLE, name),
        )
        return cursor.fetchone() is not None

    # PostgreSQL keeps the DEFAULT in the column definition.
    pg_types = {"INTEGER DEFAULT 0": "INTEGER DEFAULT 0"}
    for name, coltype in _COLUMNS:
        if not col_exists(name):
            cursor.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {name} {pg_types[coltype]}")
            print(f"  + column {_TABLE}.{name}")
        else:
            print(f"  ~ column {_TABLE}.{name} already exists")

    conn.close()
    print("\nMigration 018 complete.")


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
