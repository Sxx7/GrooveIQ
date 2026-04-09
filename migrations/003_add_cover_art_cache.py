"""
Migration 003: Create cover_art_cache table.

Caches cover art URLs for tracks not (yet) in the local library.
Last.fm stopped distributing real images in ~2020, so chart entries
that don't match the local library need a fallback source.  This table
stores the result of looking up cover art via Spotizerr's Spotify search
(or other sources) keyed by normalised (artist, title).

Once a track enters the library and is synced to the media server,
the chart API prefers the media server cover URL.  The cached entry
is intentionally left in place as a resilience fallback.

Idempotent: uses CREATE TABLE IF NOT EXISTS.

Usage:
    python migrations/003_add_cover_art_cache.py

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


SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS cover_art_cache (
    artist_norm VARCHAR(256) NOT NULL,
    title_norm  VARCHAR(256) NOT NULL,
    url         VARCHAR(1024),
    source      VARCHAR(32)  NOT NULL,
    fetched_at  INTEGER      NOT NULL,
    PRIMARY KEY (artist_norm, title_norm)
);
"""

SQLITE_INDEX = """
CREATE INDEX IF NOT EXISTS ix_cover_art_fetched ON cover_art_cache (fetched_at);
"""

POSTGRES_DDL = """
CREATE TABLE IF NOT EXISTS cover_art_cache (
    artist_norm VARCHAR(256) NOT NULL,
    title_norm  VARCHAR(256) NOT NULL,
    url         VARCHAR(1024),
    source      VARCHAR(32)  NOT NULL,
    fetched_at  INTEGER      NOT NULL,
    PRIMARY KEY (artist_norm, title_norm)
);
"""

POSTGRES_INDEX = """
CREATE INDEX IF NOT EXISTS ix_cover_art_fetched ON cover_art_cache (fetched_at);
"""


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.executescript(SQLITE_DDL)
    print("  + table cover_art_cache")

    cursor.executescript(SQLITE_INDEX)
    print("  + index ix_cover_art_fetched")

    conn.commit()
    conn.close()
    print("\nMigration 003 complete.")


def migrate_postgres(database_url: str) -> None:
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is required for PostgreSQL migrations: pip install psycopg2-binary")

    url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cursor = conn.cursor()

    cursor.execute(POSTGRES_DDL)
    print("  + table cover_art_cache")

    cursor.execute(POSTGRES_INDEX)
    print("  + index ix_cover_art_fetched")

    conn.close()
    print("\nMigration 003 complete.")


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
