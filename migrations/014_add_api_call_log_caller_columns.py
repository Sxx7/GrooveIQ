"""
Migration 014: Add caller-identity columns to api_call_logs.

Issue #81 — distinguishes dashboard / mobile / CLI traffic in the per-user
API call log by capturing client IP, full User-Agent, and a derived
``source_class`` enum (``browser`` / ``mobile`` / ``cli`` / ``other``) at
write time.

Fresh databases get these columns automatically via
``Base.metadata.create_all`` and ``_apply_column_migrations`` in
``app/db/session.py``. This script is for operators who already ran
migration 013 and want to upgrade an existing schema explicitly.

Idempotent: SQLite ALTERs that fail (column already exists) are swallowed;
Postgres uses ``ADD COLUMN IF NOT EXISTS``.

Usage:
    python migrations/014_add_api_call_log_caller_columns.py
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
    "ALTER TABLE api_call_logs ADD COLUMN client_ip VARCHAR(64)",
    "ALTER TABLE api_call_logs ADD COLUMN user_agent VARCHAR(512)",
    "ALTER TABLE api_call_logs ADD COLUMN source_class VARCHAR(16)",
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_client_ip ON api_call_logs (client_ip)",
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_source_class ON api_call_logs (source_class)",
]


_POSTGRES_STMTS = [
    "ALTER TABLE api_call_logs ADD COLUMN IF NOT EXISTS client_ip VARCHAR(64)",
    "ALTER TABLE api_call_logs ADD COLUMN IF NOT EXISTS user_agent VARCHAR(512)",
    "ALTER TABLE api_call_logs ADD COLUMN IF NOT EXISTS source_class VARCHAR(16)",
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_client_ip ON api_call_logs (client_ip)",
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_source_class ON api_call_logs (source_class)",
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
    print("\nMigration 014 complete.")


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
    print("\nMigration 014 complete.")


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
