"""
Migration 022: Generic notification outbox + per-type device prefs (P1/P2/P3).

Adds the producer-agnostic notification pipeline that new-release push (P1),
download-finished (goal B), newly-added media (goal C), and recommendations
(goal F) all feed:

  * ``notification_events``     — global, one row per notifiable occurrence
                                  (event_type + dedup_key + captured title/body/data).
  * ``notification_deliveries`` — per-user fan-out + dispatch state, with the
                                  UNIQUE(user_id, dedup_key) that enforces
                                  cross-type dedup (goal D) and attempt-counted
                                  backoff (attempt_count / next_retry_at).

Also extends existing tables (additive, nullable):
  * ``devices``           — notif_new_media / notif_download_finished /
                            notif_recommendations (per-type toggles), plus
                            device_guid + device_name (stable frontend identity, goal E).
  * ``download_requests`` — user_id (attribute a manual download to the requester, goal B).

All of the above is also applied automatically at app startup:
``Base.metadata.create_all`` creates the two new tables and
``app/db/session.py::_apply_column_migrations`` adds the new columns. This script
exists for operators who manage their schema explicitly.

Idempotent: CREATE TABLE / INDEX IF NOT EXISTS; ADD COLUMN is wrapped per-stmt
(SQLite/PG raise on a duplicate column, which is swallowed). Index names match the
``create_all`` auto-generated names so the fresh-DB and existing-DB paths converge.

Usage:
    python migrations/022_add_notification_outbox.py

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


# --- New tables (CREATE ... IF NOT EXISTS is idempotent on both engines) ---
_CREATE_SQLITE = [
    """
    CREATE TABLE IF NOT EXISTS notification_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type  VARCHAR(32) NOT NULL,
        dedup_key   VARCHAR(255),
        title       VARCHAR(255) NOT NULL,
        body        VARCHAR(1024) NOT NULL,
        data        JSON,
        created_at  INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_notification_events_event_type ON notification_events (event_type)",
    "CREATE INDEX IF NOT EXISTS ix_notification_events_dedup_key ON notification_events (dedup_key)",
    """
    CREATE TABLE IF NOT EXISTS notification_deliveries (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         VARCHAR(128) NOT NULL,
        event_id        INTEGER NOT NULL REFERENCES notification_events (id),
        event_type      VARCHAR(32) NOT NULL,
        dedup_key       VARCHAR(255),
        dispatch_state  VARCHAR(16) NOT NULL DEFAULT 'pending',
        attempt_count   INTEGER NOT NULL DEFAULT 0,
        next_retry_at   INTEGER,
        last_error      VARCHAR(255),
        notified_at     INTEGER,
        created_at      INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_notification_deliveries_user_id ON notification_deliveries (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_notification_deliveries_event_id ON notification_deliveries (event_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_notif_delivery_user_dedup ON notification_deliveries (user_id, dedup_key)",
    "CREATE INDEX IF NOT EXISTS ix_notif_delivery_state ON notification_deliveries (dispatch_state, next_retry_at)",
]

_CREATE_POSTGRES = [
    """
    CREATE TABLE IF NOT EXISTS notification_events (
        id          SERIAL PRIMARY KEY,
        event_type  VARCHAR(32) NOT NULL,
        dedup_key   VARCHAR(255),
        title       VARCHAR(255) NOT NULL,
        body        VARCHAR(1024) NOT NULL,
        data        JSON,
        created_at  INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_notification_events_event_type ON notification_events (event_type)",
    "CREATE INDEX IF NOT EXISTS ix_notification_events_dedup_key ON notification_events (dedup_key)",
    """
    CREATE TABLE IF NOT EXISTS notification_deliveries (
        id              SERIAL PRIMARY KEY,
        user_id         VARCHAR(128) NOT NULL,
        event_id        INTEGER NOT NULL REFERENCES notification_events (id),
        event_type      VARCHAR(32) NOT NULL,
        dedup_key       VARCHAR(255),
        dispatch_state  VARCHAR(16) NOT NULL DEFAULT 'pending',
        attempt_count   INTEGER NOT NULL DEFAULT 0,
        next_retry_at   INTEGER,
        last_error      VARCHAR(255),
        notified_at     INTEGER,
        created_at      INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_notification_deliveries_user_id ON notification_deliveries (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_notification_deliveries_event_id ON notification_deliveries (event_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_notif_delivery_user_dedup ON notification_deliveries (user_id, dedup_key)",
    "CREATE INDEX IF NOT EXISTS ix_notif_delivery_state ON notification_deliveries (dispatch_state, next_retry_at)",
]

# --- Additive columns on existing tables. (table, column, DDL type). Applied
# with per-statement try/except because ADD COLUMN is not IF-NOT-EXISTS-safe. ---
_ADD_COLUMNS = [
    ("devices", "notif_new_media", "BOOLEAN"),
    ("devices", "notif_download_finished", "BOOLEAN"),
    ("devices", "notif_recommendations", "BOOLEAN"),
    ("devices", "device_guid", "VARCHAR(64)"),
    ("devices", "device_name", "VARCHAR(128)"),
    ("download_requests", "user_id", "VARCHAR(128)"),
    ("track_features", "new_media_notified_at", "INTEGER"),
]

_ADD_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_devices_device_guid ON devices (device_guid)",
    "CREATE INDEX IF NOT EXISTS ix_download_requests_user_id ON download_requests (user_id)",
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
    for stmt in _CREATE_SQLITE:
        _run(cur, stmt)
    for table, column, col_type in _ADD_COLUMNS:
        _run(cur, f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    for stmt in _ADD_INDEXES:
        _run(cur, stmt)
    conn.commit()
    conn.close()
    print("\nMigration 022 complete.")


def migrate_postgres(database_url: str) -> None:
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is required for PostgreSQL migrations: pip install psycopg2-binary")

    url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()
    for stmt in _CREATE_POSTGRES:
        _run(cur, stmt)
    for table, column, col_type in _ADD_COLUMNS:
        # PG supports IF NOT EXISTS for ADD COLUMN — use it for a clean run.
        _run(cur, f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}")
    for stmt in _ADD_INDEXES:
        _run(cur, stmt)
    conn.close()
    print("\nMigration 022 complete.")


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
