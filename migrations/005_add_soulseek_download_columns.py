"""
Migration 005: Add Soulseek-related columns to download_requests table.

The slskd (Soulseek) integration added several new columns to the
DownloadRequest model that are missing from databases initialised before
that commit:

  - source            VARCHAR(32) NOT NULL DEFAULT 'spotdl'
  - slskd_username    VARCHAR(256)
  - slskd_filename    VARCHAR(1024)
  - slskd_transfer_id VARCHAR(128)

It also relaxed download_requests.spotify_id from NOT NULL to nullable
so that Soulseek downloads (which have no Spotify ID) can be persisted.
For SQLite, dropping the NOT NULL constraint requires recreating the
table; we do this only when the existing column is NOT NULL.

Idempotent: checks for column existence before adding.

Usage:
    python migrations/005_add_soulseek_download_columns.py

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


def _sqlite_columns(cursor, table: str) -> dict:
    cursor.execute(f"PRAGMA table_info({table})")
    # row: (cid, name, type, notnull, dflt_value, pk)
    return {row[1]: row for row in cursor.fetchall()}


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cols = _sqlite_columns(cursor, "download_requests")
    if not cols:
        sys.exit("Table download_requests does not exist — run the app once to create it.")

    if "source" not in cols:
        cursor.execute(
            "ALTER TABLE download_requests ADD COLUMN source VARCHAR(32) NOT NULL DEFAULT 'spotdl'"
        )
        print("  + column download_requests.source")
    else:
        print("  ~ column download_requests.source already exists")

    if "slskd_username" not in cols:
        cursor.execute("ALTER TABLE download_requests ADD COLUMN slskd_username VARCHAR(256)")
        print("  + column download_requests.slskd_username")
    else:
        print("  ~ column download_requests.slskd_username already exists")

    if "slskd_filename" not in cols:
        cursor.execute("ALTER TABLE download_requests ADD COLUMN slskd_filename VARCHAR(1024)")
        print("  + column download_requests.slskd_filename")
    else:
        print("  ~ column download_requests.slskd_filename already exists")

    if "slskd_transfer_id" not in cols:
        cursor.execute("ALTER TABLE download_requests ADD COLUMN slskd_transfer_id VARCHAR(128)")
        print("  + column download_requests.slskd_transfer_id")
    else:
        print("  ~ column download_requests.slskd_transfer_id already exists")

    # Re-check after adds so we can decide whether spotify_id needs relaxing.
    cols = _sqlite_columns(cursor, "download_requests")
    spotify_id_row = cols.get("spotify_id")
    if spotify_id_row and spotify_id_row[3] == 1:
        # notnull == 1 → need to recreate the table to drop NOT NULL on spotify_id.
        print("  * relaxing download_requests.spotify_id NOT NULL constraint (table rebuild)")
        cursor.executescript(
            """
            BEGIN;
            CREATE TABLE download_requests__new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                spotify_id VARCHAR(64),
                task_id VARCHAR(128),
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                source VARCHAR(32) NOT NULL DEFAULT 'spotdl',
                track_title VARCHAR(512),
                artist_name VARCHAR(512),
                album_name VARCHAR(512),
                cover_url VARCHAR(1024),
                slskd_username VARCHAR(256),
                slskd_filename VARCHAR(1024),
                slskd_transfer_id VARCHAR(128),
                requested_by VARCHAR(128),
                error_message TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER
            );
            INSERT INTO download_requests__new (
                id, spotify_id, task_id, status, source,
                track_title, artist_name, album_name, cover_url,
                slskd_username, slskd_filename, slskd_transfer_id,
                requested_by, error_message, created_at, updated_at
            )
            SELECT
                id, spotify_id, task_id, status, source,
                track_title, artist_name, album_name, cover_url,
                slskd_username, slskd_filename, slskd_transfer_id,
                requested_by, error_message, created_at, updated_at
            FROM download_requests;
            DROP TABLE download_requests;
            ALTER TABLE download_requests__new RENAME TO download_requests;
            CREATE INDEX IF NOT EXISTS ix_download_requests_spotify_id ON download_requests (spotify_id);
            CREATE INDEX IF NOT EXISTS ix_download_requests_task_id ON download_requests (task_id);
            CREATE INDEX IF NOT EXISTS ix_download_status ON download_requests (status);
            CREATE INDEX IF NOT EXISTS ix_download_created ON download_requests (created_at);
            COMMIT;
            """
        )
    else:
        print("  ~ download_requests.spotify_id already nullable")

    conn.commit()
    conn.close()
    print("\nMigration 005 complete.")


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
            WHERE table_name = 'download_requests' AND column_name = %s
            """,
            (name,),
        )
        return cursor.fetchone() is not None

    if not col_exists("source"):
        cursor.execute(
            "ALTER TABLE download_requests ADD COLUMN source VARCHAR(32) NOT NULL DEFAULT 'spotdl'"
        )
        print("  + column download_requests.source")
    else:
        print("  ~ column download_requests.source already exists")

    for col, ddl in [
        ("slskd_username", "ALTER TABLE download_requests ADD COLUMN slskd_username VARCHAR(256)"),
        ("slskd_filename", "ALTER TABLE download_requests ADD COLUMN slskd_filename VARCHAR(1024)"),
        (
            "slskd_transfer_id",
            "ALTER TABLE download_requests ADD COLUMN slskd_transfer_id VARCHAR(128)",
        ),
    ]:
        if not col_exists(col):
            cursor.execute(ddl)
            print(f"  + column download_requests.{col}")
        else:
            print(f"  ~ column download_requests.{col} already exists")

    cursor.execute(
        """
        SELECT is_nullable FROM information_schema.columns
        WHERE table_name = 'download_requests' AND column_name = 'spotify_id'
        """
    )
    row = cursor.fetchone()
    if row and row[0] == "NO":
        cursor.execute("ALTER TABLE download_requests ALTER COLUMN spotify_id DROP NOT NULL")
        print("  * relaxed download_requests.spotify_id NOT NULL constraint")
    else:
        print("  ~ download_requests.spotify_id already nullable")

    conn.close()
    print("\nMigration 005 complete.")


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
