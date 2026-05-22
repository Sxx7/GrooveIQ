"""One-shot SQLite -> PostgreSQL data migration for GrooveIQ.

Builds the schema on the destination, copies the durable (non-regenerable)
tables in foreign-key order, and resets primary-key sequences so subsequent
app inserts don't collide on `id`.

Run once, inside the GrooveIQ image (so `app.models.db` is importable and
every SQLAlchemy column type round-trips correctly):

    docker compose run --rm \
        -e MIGRATE_SRC_URL=sqlite+aiosqlite:////data/grooveiq.db \
        grooveiq python migrations/002_sqlite_to_postgres.py

The destination defaults to the `postgres` compose service, built from
POSTGRES_PASSWORD in the environment; override with MIGRATE_DST_URL.

Re-runnable: every target table is TRUNCATEd before the copy.

NOT copied -- these are disposable or rebuilt by the pipeline / library scan:
listen_sessions, track_interactions, recommendation_request_audits,
recommendation_candidate_audits, api_call_logs, scan_logs, scrobble_queue,
cover_art_cache, library_scan_state.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Allow `import app...` when run by file path (sys.path[0] is this file's dir).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, insert, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.models.db import Base  # noqa: E402

# Durable tables in FK-dependency order (parents before children). The only
# cross-table FK among these is playlist_tracks -> playlists; everything else
# is independent. Every table here has an integer autoincrement `id` PK.
TABLES = [
    "users",
    "track_features",
    "listen_events",
    "playlists",
    "playlist_tracks",
    "download_requests",
    "lidarr_backfill_requests",
    "chart_entries",
    "discovery_requests",
    "fill_library_requests",
    "algorithm_configs",
    "download_routing_configs",
    "lidarr_backfill_configs",
]

BATCH = 1000


def _dst_url() -> str:
    url = os.environ.get("MIGRATE_DST_URL")
    if url:
        return url
    pw = os.environ.get("POSTGRES_PASSWORD")
    if not pw:
        sys.exit("ERROR: set MIGRATE_DST_URL, or POSTGRES_PASSWORD to use the default host")
    return f"postgresql+asyncpg://grooveiq:{pw}@postgres:5432/grooveiq"


async def _copy_table(src, dst, name: str) -> int:
    """Keyset-paginate one table from src into dst. Returns the row count."""
    table = Base.metadata.tables[name]
    last_id = 0
    copied = 0
    async with src.connect() as s:
        total = (await s.execute(select(func.count()).select_from(table))).scalar_one()
        while True:
            rows = [
                dict(r._mapping)
                for r in (
                    await s.execute(
                        select(table)
                        .where(table.c.id > last_id)
                        .order_by(table.c.id)
                        .limit(BATCH)
                    )
                ).all()
            ]
            if not rows:
                break
            async with dst.begin() as d:
                await d.execute(insert(table), rows)
            last_id = rows[-1]["id"]
            copied += len(rows)
    if copied != total:
        sys.exit(f"ERROR: {name}: copied {copied} of {total} rows")
    return total


async def main() -> int:
    src_url = os.environ.get("MIGRATE_SRC_URL", "sqlite+aiosqlite:////data/grooveiq.db")
    dst_url = _dst_url()
    print(f"src: {src_url}")
    print(f"dst: postgresql+asyncpg://grooveiq:***@{dst_url.split('@', 1)[-1]}")

    src = create_async_engine(src_url)
    dst = create_async_engine(dst_url)
    try:
        # Build the schema with a bare create_all in its own transaction. This
        # deliberately skips _apply_column_migrations: on a fresh DB create_all
        # already emits every current column, and a failed ALTER would abort
        # the whole transaction on PostgreSQL. TRUNCATE makes re-runs idempotent.
        async with dst.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                text("TRUNCATE TABLE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")
            )
        print("schema: create_all + truncate complete")

        grand_total = 0
        for name in TABLES:
            total = await _copy_table(src, dst, name)
            grand_total += total
            # Reset the SERIAL sequence so the app's next insert gets MAX(id)+1.
            # `name` comes from the hardcoded TABLES list, so the f-string is safe.
            if total > 0:
                async with dst.begin() as d:
                    await d.execute(
                        text(
                            f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), "
                            f"(SELECT MAX(id) FROM {name}))"
                        )
                    )
            print(f"  {name}: {total} rows")

        print(f"migration complete -- {grand_total} rows across {len(TABLES)} tables.")
        return 0
    finally:
        await src.dispose()
        await dst.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
