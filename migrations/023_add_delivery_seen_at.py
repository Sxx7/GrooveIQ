"""
Migration 023: In-app feed read-state on notification_deliveries.

Adds ``notification_deliveries.seen_at`` (epoch seconds, nullable) — the read-state
for the unified in-app Activity notification feed. NULL = unseen, which drives the
bell-badge ``unseen_count``; it is stamped when the user views the notification in
the sheet. Distinct from ``notified_at`` (push send time): a delivery can be seen
in-app without ever being pushed (e.g. a budget-suppressed recommendation still
appears in the feed).

Also applied automatically at app startup via
``app/db/session.py::_apply_column_migrations``. This script exists for operators
who manage their schema explicitly.

Idempotent: ADD COLUMN is wrapped per-stmt (SQLite/PG raise on a duplicate column,
which is swallowed).

Usage:
    python migrations/023_add_delivery_seen_at.py

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


# --- Additive column on notification_deliveries. (table, column, DDL type).
# Applied with per-statement try/except because ADD COLUMN is not
# IF-NOT-EXISTS-safe on SQLite. ---
_ADD_COLUMNS = [
    ("notification_deliveries", "seen_at", "INTEGER"),
]


def _run(cursor, stmt: str) -> None:
    try:
        cursor.execute(stmt)
        print(f"  + {stmt.strip().splitlines()[0][:80]}")
    except Exception as exc:  # duplicate column / already-applied → idempotent
        print(f"  . skip ({exc})")


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    for table, column, col_type in _ADD_COLUMNS:
        _run(cur, f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    conn.commit()
    conn.close()
    print("\nMigration 023 complete.")


def migrate_postgres(database_url: str) -> None:
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is required for PostgreSQL migrations: pip install psycopg2-binary")

    url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()
    for table, column, col_type in _ADD_COLUMNS:
        # PG supports IF NOT EXISTS for ADD COLUMN — use it for a clean run.
        _run(cur, f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}")
    conn.close()
    print("\nMigration 023 complete.")


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
