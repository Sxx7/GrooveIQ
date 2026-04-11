"""
Migration 004: Add onboarding_preferences column to users table.

Stores explicit user preferences (favourite artists, genres, moods, etc.)
for cold-start taste profile seeding.

Idempotent: checks if column exists before adding.

Usage:
    python migrations/004_add_onboarding_preferences.py

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


def _sqlite_column_exists(cursor, table: str, column: str) -> bool:
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    if not _sqlite_column_exists(cursor, "users", "onboarding_preferences"):
        cursor.execute("ALTER TABLE users ADD COLUMN onboarding_preferences TEXT")
        print("  + column users.onboarding_preferences")
    else:
        print("  ~ column users.onboarding_preferences already exists")

    conn.commit()
    conn.close()
    print("\nMigration 004 complete.")


def migrate_postgres(database_url: str) -> None:
    try:
        import psycopg2
    except ImportError:
        sys.exit("psycopg2 is required for PostgreSQL migrations: pip install psycopg2-binary")

    url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cursor = conn.cursor()

    cursor.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'onboarding_preferences'
    """)
    if not cursor.fetchone():
        cursor.execute("ALTER TABLE users ADD COLUMN onboarding_preferences JSONB")
        print("  + column users.onboarding_preferences")
    else:
        print("  ~ column users.onboarding_preferences already exists")

    conn.close()
    print("\nMigration 004 complete.")


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
