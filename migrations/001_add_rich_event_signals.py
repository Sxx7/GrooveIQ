"""
Migration 001: Add rich event signal columns to listen_events.

SQLite requires one ALTER TABLE per column and does not support
IF NOT EXISTS on ADD COLUMN, so we catch "duplicate column" errors
to make this script idempotent (safe to re-run).

Usage:
    python migrations/001_add_rich_event_signals.py

Reads DATABASE_URL from the environment / .env (same as the app).
Falls back to sqlite:////data/grooveiq.db if unset.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env if present (same behavior as the app)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# -- columns to add ----------------------------------------------------------
# (column_name, sql_type)
NEW_COLUMNS = [
    # Impression & exposure
    ("surface",            "VARCHAR(64)"),
    ("position",           "INTEGER"),
    ("request_id",         "VARCHAR(128)"),
    ("model_version",      "VARCHAR(64)"),
    # Sessionization
    ("session_position",   "INTEGER"),
    # Satisfaction / dwell
    ("dwell_ms",           "INTEGER"),
    # Pause buckets
    ("pause_duration_ms",  "INTEGER"),
    # Seek intensity
    ("num_seekfwd",        "INTEGER"),
    ("num_seekbk",         "INTEGER"),
    # Shuffle state
    ("shuffle",            "BOOLEAN"),
    # Context / source
    ("context_type",       "VARCHAR(32)"),
    ("context_id",         "VARCHAR(128)"),
    ("context_switch",     "BOOLEAN"),
    # Start / end reason codes
    ("reason_start",       "VARCHAR(32)"),
    ("reason_end",         "VARCHAR(32)"),
    # Cross-device identity
    ("device_id",          "VARCHAR(128)"),
    ("device_type",        "VARCHAR(32)"),
    # Local time context
    ("hour_of_day",        "INTEGER"),
    ("day_of_week",        "INTEGER"),
    ("timezone",           "VARCHAR(64)"),
    # Audio output
    ("output_type",        "VARCHAR(32)"),
    ("output_device_name", "VARCHAR(128)"),
    ("bluetooth_connected","BOOLEAN"),
    # Location
    ("latitude",           "REAL"),
    ("longitude",          "REAL"),
    ("location_label",     "VARCHAR(32)"),
]

# Indexes to create after columns exist
NEW_INDEXES = [
    ("ix_events_request_id", "request_id"),
    ("ix_events_device_id",  "device_id"),
]

TABLE = "listen_events"


def migrate_sqlite(db_path: str) -> None:
    """Run migration against a SQLite database file."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    added = 0
    skipped = 0

    for col_name, col_type in NEW_COLUMNS:
        try:
            cursor.execute(f"ALTER TABLE {TABLE} ADD COLUMN {col_name} {col_type}")
            added += 1
            print(f"  + {col_name} ({col_type})")
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                skipped += 1
            else:
                raise

    for idx_name, col_name in NEW_INDEXES:
        try:
            cursor.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {TABLE} ({col_name})")
            print(f"  + index {idx_name}")
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()
    print(f"\nDone: {added} columns added, {skipped} already existed.")


def migrate_postgres(database_url: str) -> None:
    """Run migration against a PostgreSQL database."""
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is required for PostgreSQL migrations: pip install psycopg2-binary")

    # Convert async URL to sync
    url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cursor = conn.cursor()

    added = 0
    for col_name, col_type in NEW_COLUMNS:
        pg_type = col_type.replace("REAL", "DOUBLE PRECISION")
        try:
            cursor.execute(f"ALTER TABLE {TABLE} ADD COLUMN {col_name} {pg_type}")
            added += 1
            print(f"  + {col_name} ({pg_type})")
        except Exception as e:
            if "already exists" in str(e).lower():
                conn.rollback() if not conn.autocommit else None
            else:
                raise

    for idx_name, col_name in NEW_INDEXES:
        cursor.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {TABLE} ({col_name})")
        print(f"  + index {idx_name}")

    conn.close()
    print(f"\nDone: {added} columns added.")


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:////data/grooveiq.db")
    print(f"Migrating: {database_url}\n")

    if "sqlite" in database_url:
        # Extract file path from URL like sqlite+aiosqlite:////data/grooveiq.db
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
