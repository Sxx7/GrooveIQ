"""
Migration 024: Add the Discogs-VINet version embedding column to track_features.

Adds one new nullable column:

  - version_embedding TEXT          — base64-encoded 512-dim Discogs-VINet vector

Additive and default NULL. VINet is gated behind ``VINET_ENABLED`` (feature
flag) and the version FAISS index is a no-op until at least one track has a
``version_embedding``, so rolling this out is safe on an existing DB.

The same ALTER is also applied automatically by the in-app migration in
``app/db/session.py::_apply_column_migrations`` on startup. This standalone
script is provided for operators who prefer explicit migrations.

Idempotent: checks column existence before adding.

Usage:
    python migrations/024_add_version_embedding_column.py

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
    ("version_embedding", "TEXT"),
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
    print("\nMigration 024 complete.")


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

    pg_types = {"TEXT": "TEXT", "REAL": "DOUBLE PRECISION"}
    for name, coltype in _COLUMNS:
        if not col_exists(name):
            cursor.execute(f"ALTER TABLE track_features ADD COLUMN {name} {pg_types[coltype]}")
            print(f"  + column track_features.{name}")
        else:
            print(f"  ~ column track_features.{name} already exists")

    conn.close()
    print("\nMigration 024 complete.")


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
