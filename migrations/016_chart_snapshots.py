"""
Migration 016: Daily chart snapshots (issue #75).

Last.fm's chart endpoints (chart.getTopTracks, geo.*, tag.*) carry no time
window — they return opaque cumulative snapshots. To track position over time
we stop overwriting charts on each build and instead append a new day's rows,
keyed by a UTC calendar date. This script:

  1. Adds chart_entries.snapshot_date (ISO 'YYYY-MM-DD' string — chosen over a
     DATE type so it sorts chronologically in SQLite and `?as_of=` is an exact
     string match).
  2. Backfills existing rows from their fetched_at epoch (going-forward feature,
     so this just stamps the single pre-existing snapshot per chart).
  3. Adds a read index (chart_type, scope, snapshot_date, position) and a UNIQUE
     index (chart_type, scope, position, snapshot_date).

The backfill must run before the unique index: legacy rows were DELETE→INSERT,
so there is exactly one row per (chart_type, scope, position), all with NULL
snapshot_date — stamping them avoids any NULL-collision ambiguity.

Fresh databases get all of this automatically via Base.metadata.create_all and
_apply_column_migrations in app/db/session.py. This script is only for operators
upgrading an existing schema out-of-band.

Idempotent: a duplicate ADD COLUMN is swallowed; CREATE INDEX uses IF NOT EXISTS.

Usage:
    python migrations/016_chart_snapshots.py
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
    "ALTER TABLE chart_entries ADD COLUMN snapshot_date VARCHAR(10)",
    "UPDATE chart_entries SET snapshot_date = strftime('%Y-%m-%d', fetched_at, 'unixepoch') "
    "WHERE snapshot_date IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_chart_snapshot ON chart_entries (chart_type, scope, snapshot_date, position)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_chart_unique_snapshot "
    "ON chart_entries (chart_type, scope, position, snapshot_date)",
]


_POSTGRES_STMTS = [
    "ALTER TABLE chart_entries ADD COLUMN IF NOT EXISTS snapshot_date VARCHAR(10)",
    "UPDATE chart_entries SET snapshot_date = "
    "to_char(to_timestamp(fetched_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD') WHERE snapshot_date IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_chart_snapshot ON chart_entries (chart_type, scope, snapshot_date, position)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_chart_unique_snapshot "
    "ON chart_entries (chart_type, scope, position, snapshot_date)",
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
    print("\nMigration 016 complete.")


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
    print("\nMigration 016 complete.")


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
