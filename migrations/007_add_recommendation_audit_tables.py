"""
Migration 007: Add recommendation_request_audits + recommendation_candidate_audits.

Creates the two append-only tables that back the always-on recommendation
audit & replay feature. Both tables are also created automatically by
``Base.metadata.create_all`` when the app starts on a fresh database; this
script exists for operators who manage their schema explicitly or want to
add the tables to a long-running database without restarting the app.

Idempotent: uses CREATE TABLE IF NOT EXISTS.

Usage:
    python migrations/007_add_recommendation_audit_tables.py

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
    CREATE TABLE IF NOT EXISTS recommendation_request_audits (
        request_id            VARCHAR(64) PRIMARY KEY,
        user_id               VARCHAR(255) NOT NULL,
        created_at            BIGINT NOT NULL,
        surface               VARCHAR(32) NOT NULL,
        seed_track_id         VARCHAR(255),
        context_id            VARCHAR(255),
        model_version         VARCHAR(64) NOT NULL,
        config_version        INTEGER NOT NULL DEFAULT 0,
        request_context       TEXT,
        candidates_total      INTEGER NOT NULL DEFAULT 0,
        candidates_by_source  TEXT,
        duration_ms           INTEGER NOT NULL DEFAULT 0,
        limit_requested       INTEGER NOT NULL DEFAULT 25
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recommendation_candidate_audits (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id            VARCHAR(64) NOT NULL,
        track_id              VARCHAR(255) NOT NULL,
        sources               TEXT,
        raw_score             REAL NOT NULL DEFAULT 0.0,
        pre_rerank_position   INTEGER NOT NULL DEFAULT -1,
        final_score           REAL,
        final_position        INTEGER,
        shown                 BOOLEAN NOT NULL DEFAULT 0,
        reranker_actions      TEXT,
        feature_vector        TEXT,
        FOREIGN KEY (request_id)
            REFERENCES recommendation_request_audits (request_id)
            ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_reco_audit_user ON recommendation_request_audits (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_audit_created ON recommendation_request_audits (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_reco_audit_user_time ON recommendation_request_audits (user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_reco_audit_request ON recommendation_candidate_audits (request_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_audit_track ON recommendation_candidate_audits (track_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_audit_shown ON recommendation_candidate_audits (shown)",
    "CREATE INDEX IF NOT EXISTS idx_reco_audit_candidate_track ON recommendation_candidate_audits (request_id, track_id)",
]


_POSTGRES_DDL = [
    """
    CREATE TABLE IF NOT EXISTS recommendation_request_audits (
        request_id            VARCHAR(64) PRIMARY KEY,
        user_id               VARCHAR(255) NOT NULL,
        created_at            BIGINT NOT NULL,
        surface               VARCHAR(32) NOT NULL,
        seed_track_id         VARCHAR(255),
        context_id            VARCHAR(255),
        model_version         VARCHAR(64) NOT NULL,
        config_version        INTEGER NOT NULL DEFAULT 0,
        request_context       JSONB,
        candidates_total      INTEGER NOT NULL DEFAULT 0,
        candidates_by_source  JSONB,
        duration_ms           INTEGER NOT NULL DEFAULT 0,
        limit_requested       INTEGER NOT NULL DEFAULT 25
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recommendation_candidate_audits (
        id                    SERIAL PRIMARY KEY,
        request_id            VARCHAR(64) NOT NULL
            REFERENCES recommendation_request_audits (request_id) ON DELETE CASCADE,
        track_id              VARCHAR(255) NOT NULL,
        sources               JSONB,
        raw_score             DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        pre_rerank_position   INTEGER NOT NULL DEFAULT -1,
        final_score           DOUBLE PRECISION,
        final_position        INTEGER,
        shown                 BOOLEAN NOT NULL DEFAULT FALSE,
        reranker_actions      JSONB,
        feature_vector        JSONB
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_reco_audit_user ON recommendation_request_audits (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_audit_created ON recommendation_request_audits (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_reco_audit_user_time ON recommendation_request_audits (user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_reco_audit_request ON recommendation_candidate_audits (request_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_audit_track ON recommendation_candidate_audits (track_id)",
    "CREATE INDEX IF NOT EXISTS ix_reco_audit_shown ON recommendation_candidate_audits (shown)",
    "CREATE INDEX IF NOT EXISTS idx_reco_audit_candidate_track ON recommendation_candidate_audits (request_id, track_id)",
]


def migrate_sqlite(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    for stmt in _SQLITE_DDL:
        cursor.execute(stmt)
        print(f"  + {stmt.strip().splitlines()[0][:80]}")
    conn.commit()
    conn.close()
    print("\nMigration 007 complete.")


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
    print("\nMigration 007 complete.")


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
