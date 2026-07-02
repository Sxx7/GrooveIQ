"""
Migration 021: Add devices (new-release push registry, P2).

Creates the ``devices`` table — grooveiq's registry of per-user notification
targets. One row per device, keyed by the unique ``apns_token`` (NULL for
Apprise-only devices; NULLs are distinct so several apprise-only rows coexist).
``disabled_at`` is stamped on 410 Unregistered. grooveiq owns the token
registry; the relay is stateless and holds no tokens.

Also created automatically by ``Base.metadata.create_all`` when the app starts;
this script exists for operators who manage their schema explicitly or want to
add the table to a long-running database without restarting the app.

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS. Index names match the
``create_all`` auto-generated names (``ix_devices_user_id`` for the indexed
``user_id``; ``ix_devices_apns_token`` for the unique+indexed ``apns_token``) so
the fresh-DB and existing-DB paths converge.

Usage:
    python migrations/021_add_devices.py

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
    CREATE TABLE IF NOT EXISTS devices (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id             VARCHAR(128) NOT NULL,
        platform            VARCHAR(16) NOT NULL DEFAULT 'ios',
        apns_token          VARCHAR(200),
        apns_environment    VARCHAR(16) NOT NULL DEFAULT 'production',
        apprise_urls        JSON,
        notif_new_releases  BOOLEAN NOT NULL DEFAULT 1,
        created_at          INTEGER NOT NULL,
        last_seen_at        INTEGER NOT NULL,
        disabled_at         INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_devices_user_id ON devices (user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_devices_apns_token ON devices (apns_token)",
]


_POSTGRES_DDL = [
    """
    CREATE TABLE IF NOT EXISTS devices (
        id                  SERIAL PRIMARY KEY,
        user_id             VARCHAR(128) NOT NULL,
        platform            VARCHAR(16) NOT NULL DEFAULT 'ios',
        apns_token          VARCHAR(200),
        apns_environment    VARCHAR(16) NOT NULL DEFAULT 'production',
        apprise_urls        JSON,
        notif_new_releases  BOOLEAN NOT NULL DEFAULT TRUE,
        created_at          INTEGER NOT NULL,
        last_seen_at        INTEGER NOT NULL,
        disabled_at         INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_devices_user_id ON devices (user_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_devices_apns_token ON devices (apns_token)",
]


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    for stmt in _SQLITE_DDL:
        cursor.execute(stmt)
        print(f"  + {stmt.strip().splitlines()[0][:80]}")
    conn.commit()
    conn.close()
    print("\nMigration 021 complete.")


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
    print("\nMigration 021 complete.")


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
