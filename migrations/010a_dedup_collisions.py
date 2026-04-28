"""
Migration 010a: Pre-#37 deduplication pass — merge same-file TrackFeatures pairs.

Some files were scanned twice: once before media-server sync (which created a
row with ``track_id`` = 16-hex hash) and once after (which created a *second*
row with the new Navidrome song ID as ``track_id`` and the original hash in
``external_track_id``). The bug is closed by #37 going forward, but it
already produced ~335 duplicate row pairs in production that would trip
migration 010's collision detection.

For each collision pair (synced_row, legacy_row) where:

    synced_row.external_track_id == legacy_row.track_id
    (length 22 base62 vs. 16-hex respectively)

this script merges them into one row by:

    1. Cascading every reference to ``synced_row.track_id`` (events,
       interactions, playlist tracks, scrobbles, chart matches, reco audits)
       over to ``legacy_row.track_id``. The (user_id, track_id) UNIQUE
       collision case is handled inline, mirroring the same merge in
       migrations/010.
    2. Setting ``legacy_row.media_server_id`` = ``synced_row.track_id``
       (the Navidrome song ID) so the legacy row inherits the sync mapping.
    3. Deleting ``synced_row``.

After this pass, migration 010 sees zero collisions and runs cleanly.

The script is idempotent — re-running it after a successful pass is a no-op
because the synced rows have been deleted.

Usage:
    python migrations/010a_dedup_collisions.py                  # dry-run
    python migrations/010a_dedup_collisions.py --execute        # apply

Reads DATABASE_URL from the environment / .env.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# Tables whose track_id columns reference TrackFeatures.track_id and need
# cascading when a synced row is collapsed into a legacy row.
CASCADE = [
    ("listen_events", "track_id"),
    ("playlists", "seed_track_id"),
    ("playlist_tracks", "track_id"),
    ("scrobble_queue", "track_id"),
    ("chart_entries", "matched_track_id"),
    ("recommendation_request_audits", "seed_track_id"),
    ("recommendation_candidate_audits", "track_id"),
]

TI_SUM_COLS = [
    "play_count", "skip_count", "like_count", "dislike_count", "repeat_count",
    "playlist_add_count", "queue_add_count", "early_skip_count",
    "mid_skip_count", "full_listen_count", "total_seekfwd", "total_seekbk",
    "total_dwell_ms",
]
TI_MAX_COLS = ["first_played_at", "last_played_at", "last_event_id", "updated_at"]


def open_sqlite(database_url: str) -> sqlite3.Connection:
    if "sqlite" not in database_url:
        sys.exit("Only SQLite is supported by this script. PostgreSQL users: open a follow-up.")
    path = database_url.split("///", 1)[-1]
    if not Path(path).exists():
        sys.exit(f"Database file not found: {path}")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_exists(cur, table: str) -> bool:
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    return cur.fetchone() is not None


def find_pairs(cur) -> list[dict]:
    """Return [{synced_id, synced_tid, hex, legacy_id, legacy_tid, file_path_match}]."""
    cur.execute("""
        SELECT
            synced.id, synced.track_id, synced.external_track_id, synced.file_path,
            legacy.id, legacy.track_id, legacy.file_path
        FROM track_features synced
        JOIN track_features legacy ON legacy.track_id = synced.external_track_id
        WHERE synced.id != legacy.id
    """)
    pairs = []
    for row in cur.fetchall():
        pairs.append({
            "synced_id": row[0],
            "synced_tid": row[1],
            "hex": row[2],
            "synced_fp": row[3],
            "legacy_id": row[4],
            "legacy_tid": row[5],
            "legacy_fp": row[6],
            "same_file": row[3] == row[6],
        })
    return pairs


def estimate_cascade(cur, synced_tids: set[str]) -> dict:
    if not synced_tids:
        return {f"{t}.{c}": 0 for t, c in CASCADE + [("track_interactions", "track_id")]}
    out = {}
    chunk = 500
    ids_list = list(synced_tids)
    for table, col in CASCADE + [("track_interactions", "track_id")]:
        if not table_exists(cur, table):
            out[f"{table}.{col}"] = -1  # missing
            continue
        n = 0
        for i in range(0, len(ids_list), chunk):
            sub = ids_list[i : i + chunk]
            ph = ",".join(["?"] * len(sub))
            cur.execute(f"SELECT count(*) FROM {table} WHERE {col} IN ({ph})", sub)
            n += cur.fetchone()[0]
        out[f"{table}.{col}"] = n
    return out


def merge_track_interactions(conn, pairs: list[dict]) -> int:
    """Inline-merge per-user collisions for track_interactions before the bulk UPDATE.

    For each (synced_tid, legacy_tid) pair: if any user has rows on both,
    sum count cols, max timestamp cols, drop the synced-side row.
    """
    cur = conn.cursor()
    if not pairs:
        return 0
    relevant = set()
    pair_map = {}  # synced_tid -> legacy_tid
    for p in pairs:
        relevant.add(p["synced_tid"])
        relevant.add(p["legacy_tid"])
        pair_map[p["synced_tid"]] = p["legacy_tid"]
    if not relevant:
        return 0
    cols = (
        ["id", "user_id", "track_id"] + TI_SUM_COLS + TI_MAX_COLS + ["avg_completion"]
    )
    col_csv = ", ".join(cols)
    rows = []
    chunk = 500
    rel = list(relevant)
    for i in range(0, len(rel), chunk):
        sub = rel[i : i + chunk]
        ph = ",".join(["?"] * len(sub))
        cur.execute(f"SELECT {col_csv} FROM track_interactions WHERE track_id IN ({ph})", sub)
        rows.extend(cur.fetchall())

    by_user: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        rec = dict(zip(cols, row))
        by_user[rec["user_id"]][rec["track_id"]] = rec

    merged = 0
    now = int(time.time())
    for user_id, by_track in by_user.items():
        for synced_tid, legacy_tid in pair_map.items():
            if synced_tid == legacy_tid:
                continue
            old_row = by_track.get(synced_tid)
            new_row = by_track.get(legacy_tid)
            if old_row is None or new_row is None:
                continue
            for c in TI_SUM_COLS:
                new_row[c] = (new_row.get(c) or 0) + (old_row.get(c) or 0)
            if old_row.get("avg_completion") is not None and new_row.get("avg_completion") is None:
                new_row["avg_completion"] = old_row["avg_completion"]
            for c in TI_MAX_COLS:
                v_old, v_new = old_row.get(c), new_row.get(c)
                if v_old is not None and (v_new is None or v_old > v_new):
                    new_row[c] = v_old
            new_row["updated_at"] = max(new_row.get("updated_at") or 0, now)
            set_cols = TI_SUM_COLS + TI_MAX_COLS + ["avg_completion"]
            assigns = ", ".join(f"{c} = ?" for c in set_cols)
            vals = [new_row.get(c) for c in set_cols] + [new_row["id"]]
            cur.execute(f"UPDATE track_interactions SET {assigns} WHERE id = ?", vals)
            cur.execute("DELETE FROM track_interactions WHERE id = ?", (old_row["id"],))
            merged += 1
    return merged


def apply_dedup(conn, pairs: list[dict]) -> dict:
    cur = conn.cursor()
    metrics = {"pairs": len(pairs), "ti_merged": 0, "cascade": {}, "deleted": 0, "media_server_id_set": 0}

    # Step 1: temp mapping table (synced_tid -> legacy_tid).
    cur.execute("DROP TABLE IF EXISTS _dedup_map")
    cur.execute("CREATE TEMP TABLE _dedup_map (synced_tid TEXT PRIMARY KEY, legacy_tid TEXT NOT NULL)")
    cur.executemany(
        "INSERT INTO _dedup_map (synced_tid, legacy_tid) VALUES (?, ?)",
        [(p["synced_tid"], p["legacy_tid"]) for p in pairs],
    )

    # Step 2: track_interactions inline merge for (user_id, track_id) collisions.
    metrics["ti_merged"] = merge_track_interactions(conn, pairs)

    # Step 3: bulk-rename track_interactions remainders
    cur.execute(
        "UPDATE track_interactions "
        "SET track_id = (SELECT legacy_tid FROM _dedup_map WHERE _dedup_map.synced_tid = track_interactions.track_id) "
        "WHERE track_id IN (SELECT synced_tid FROM _dedup_map)"
    )
    metrics["cascade"]["track_interactions.track_id"] = cur.rowcount

    # Step 4: cascade across all other tables
    for table, col in CASCADE:
        if not table_exists(cur, table):
            metrics["cascade"][f"{table}.{col}"] = "-"
            continue
        cur.execute(
            f"UPDATE {table} "
            f"SET {col} = (SELECT legacy_tid FROM _dedup_map WHERE _dedup_map.synced_tid = {table}.{col}) "
            f"WHERE {col} IN (SELECT synced_tid FROM _dedup_map)"
        )
        metrics["cascade"][f"{table}.{col}"] = cur.rowcount

    # Step 5: legacy row inherits the synced row's track_id (the Navidrome ID)
    # via media_server_id. Only set if currently NULL (don't clobber).
    cur.execute(
        "UPDATE track_features "
        "SET media_server_id = (SELECT synced_tid FROM _dedup_map WHERE _dedup_map.legacy_tid = track_features.track_id) "
        "WHERE track_id IN (SELECT legacy_tid FROM _dedup_map) "
        "  AND media_server_id IS NULL"
    )
    metrics["media_server_id_set"] = cur.rowcount

    # Step 6: delete the synced rows
    cur.execute(
        "DELETE FROM track_features WHERE track_id IN (SELECT synced_tid FROM _dedup_map)"
    )
    metrics["deleted"] = cur.rowcount

    return metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migration 010a — pre-#37 deduplication of same-file TrackFeatures pairs."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the dedup. Default is dry-run (read-only).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:////data/grooveiq.db")
    print(f"Migration 010a — track_features dedup")
    print(f"Database:  {database_url}")
    print(f"Dry run:   {not args.execute}")

    conn = open_sqlite(database_url)
    try:
        cur = conn.cursor()
        pairs = find_pairs(cur)
        print(f"\nCollision pairs found: {len(pairs)}")
        if not pairs:
            print("Nothing to do — no same-file duplicate pairs detected.")
            return

        same_file = sum(1 for p in pairs if p["same_file"])
        print(f"  same file_path:        {same_file}")
        print(f"  different paths:       {len(pairs) - same_file}")

        if len(pairs) - same_file > 0:
            print("\nWARNING: some pairs have DIFFERENT file_paths. Sample:")
            for p in pairs:
                if not p["same_file"]:
                    print(f"  synced #{p['synced_id']} {p['synced_fp']!r}")
                    print(f"  legacy #{p['legacy_id']} {p['legacy_fp']!r}")
                    print()
                    break

        # Sample
        print("\nSample pairs (first 3):")
        for p in pairs[:3]:
            print(f"  synced #{p['synced_id']:>6}  track_id={p['synced_tid'][:24]:24s}")
            print(f"  legacy #{p['legacy_id']:>6}  track_id={p['legacy_tid'][:24]:24s}")
            print(f"  shared file: {p['synced_fp']}")
            print()

        # Cascade estimate
        synced_tids = {p["synced_tid"] for p in pairs}
        cascade = estimate_cascade(cur, synced_tids)
        print("Cascade estimate (rows on synced-side track_ids):")
        for k, v in cascade.items():
            print(f"  {k:50s} {v:>10}")

        if not args.execute:
            print("\nDry run — no changes written. Pass --execute to apply.")
            return

        print("\nApplying dedup...")
        t0 = time.time()
        try:
            metrics = apply_dedup(conn, pairs)
            conn.commit()
        except Exception:
            conn.rollback()
            print("\nERROR: rolled back.")
            raise
        elapsed = time.time() - t0
        print(f"\nDedup applied in {elapsed:.1f}s.")
        print(f"  pairs processed:                  {metrics['pairs']}")
        print(f"  track_interactions merged:        {metrics['ti_merged']}")
        for k, v in metrics["cascade"].items():
            print(f"  cascade {k:40s} {v}")
        print(f"  legacy rows gaining media_server_id: {metrics['media_server_id_set']}")
        print(f"  synced rows deleted:              {metrics['deleted']}")
        print(
            "\nNext step: run migrations/010_track_id_schema_rework.py — it should "
            "now see zero collisions."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
