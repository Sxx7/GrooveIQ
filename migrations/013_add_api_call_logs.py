"""
Migration 013: Add api_call_logs table.

Backs the per-user HTTP request/response logging feature surfaced under
Monitor → User Diagnostics → API calls (issue #79). Captures method, path,
truncated request body, status, duration, and a redacted response summary
for every /v1/* call so the dashboard can replay the frontend's traffic
for debugging.

The table is also created automatically by ``Base.metadata.create_all`` on
fresh databases; this script exists for operators who manage their schema
explicitly or want to add the table to a long-running database without
restarting the app.

Idempotent: uses CREATE TABLE IF NOT EXISTS.

Usage:
    python migrations/013_add_api_call_logs.py

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
    CREATE TABLE IF NOT EXISTS api_call_logs (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at           BIGINT NOT NULL,
        user_id              VARCHAR(128),
        request_id           VARCHAR(64),
        method               VARCHAR(8) NOT NULL,
        path                 VARCHAR(512) NOT NULL,
        route_template       VARCHAR(512),
        query_string         TEXT,
        request_body         TEXT,
        status_code          INTEGER NOT NULL,
        duration_ms          INTEGER NOT NULL DEFAULT 0,
        response_summary     TEXT,
        response_size_bytes  INTEGER,
        error                TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_created_at ON api_call_logs (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_user_id ON api_call_logs (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_request_id ON api_call_logs (request_id)",
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_path ON api_call_logs (path)",
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_status_code ON api_call_logs (status_code)",
    "CREATE INDEX IF NOT EXISTS idx_api_call_user_time ON api_call_logs (user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_api_call_path_time ON api_call_logs (path, created_at)",
]


_POSTGRES_DDL = [
    """
    CREATE TABLE IF NOT EXISTS api_call_logs (
        id                   SERIAL PRIMARY KEY,
        created_at           BIGINT NOT NULL,
        user_id              VARCHAR(128),
        request_id           VARCHAR(64),
        method               VARCHAR(8) NOT NULL,
        path                 VARCHAR(512) NOT NULL,
        route_template       VARCHAR(512),
        query_string         TEXT,
        request_body         JSONB,
        status_code          INTEGER NOT NULL,
        duration_ms          INTEGER NOT NULL DEFAULT 0,
        response_summary     JSONB,
        response_size_bytes  INTEGER,
        error                TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_created_at ON api_call_logs (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_user_id ON api_call_logs (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_request_id ON api_call_logs (request_id)",
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_path ON api_call_logs (path)",
    "CREATE INDEX IF NOT EXISTS ix_api_call_logs_status_code ON api_call_logs (status_code)",
    "CREATE INDEX IF NOT EXISTS idx_api_call_user_time ON api_call_logs (user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_api_call_path_time ON api_call_logs (path, created_at)",
]


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    for stmt in _SQLITE_DDL:
        cursor.execute(stmt)
        print(f"  + {stmt.strip().splitlines()[0][:80]}")
    conn.commit()
    conn.close()
    print("\nMigration 013 complete.")


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
    print("\nMigration 013 complete.")


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
