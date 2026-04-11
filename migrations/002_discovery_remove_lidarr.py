"""
Migration 002: Migrate discovery_requests from Lidarr to Spotizerr schema.

Adds: track_title, spotify_id, task_id columns
Drops: lidarr_artist_id, artist_mbid columns (SQLite: ignored if not present)

SQLite doesn't support DROP COLUMN before 3.35, so we skip drop errors.
This migration is idempotent (safe to re-run).

Usage:
    python migrations/002_discovery_remove_lidarr.py

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


def _get_db_path() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:////data/grooveiq.db")
    if "sqlite" not in url:
        print("This migration only supports SQLite. For Postgres, use ALTER TABLE directly.")
        sys.exit(1)
    # Extract file path from sqlite URL
    path = url.split("///")[-1]
    return path


def migrate():
    db_path = _get_db_path()
    print(f"Migrating: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # New columns to add
    new_columns = [
        ("track_title", "VARCHAR(512)"),
        ("spotify_id", "VARCHAR(64)"),
        ("task_id", "VARCHAR(128)"),
    ]

    for col_name, col_type in new_columns:
        try:
            cursor.execute(f"ALTER TABLE discovery_requests ADD COLUMN {col_name} {col_type}")
            print(f"  Added column: {col_name}")
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                print(f"  Column {col_name} already exists, skipping.")
            else:
                raise

    # Create index on spotify_id if it doesn't exist
    try:
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_discovery_spotify ON discovery_requests (spotify_id)"
        )
        print("  Created index: ix_discovery_spotify")
    except sqlite3.OperationalError:
        print("  Index ix_discovery_spotify already exists, skipping.")

    # Update old statuses: "sent" -> "downloading", "in_lidarr" -> "downloaded"
    cursor.execute(
        "UPDATE discovery_requests SET status = 'downloading' WHERE status = 'sent'"
    )
    updated_sent = cursor.rowcount
    cursor.execute(
        "UPDATE discovery_requests SET status = 'downloaded' WHERE status = 'in_lidarr'"
    )
    updated_lidarr = cursor.rowcount

    if updated_sent or updated_lidarr:
        print(f"  Migrated statuses: {updated_sent} sent→downloading, {updated_lidarr} in_lidarr→downloaded")

    # Try to drop old columns (SQLite 3.35+)
    for old_col in ("lidarr_artist_id", "artist_mbid"):
        try:
            cursor.execute(f"ALTER TABLE discovery_requests DROP COLUMN {old_col}")
            print(f"  Dropped column: {old_col}")
        except sqlite3.OperationalError:
            print(f"  Could not drop {old_col} (SQLite < 3.35 or column missing) — harmless, ignored.")

    # Drop old unique constraint on artist_mbid if possible
    # SQLite can't drop constraints without recreating the table, so we skip this.

    conn.commit()
    conn.close()
    print("Migration 002 complete.")


if __name__ == "__main__":
    migrate()
