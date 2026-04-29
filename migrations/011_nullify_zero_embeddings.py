"""
Migration 011: Nullify all-zero EffNet embeddings written by the pre-#42 worker.

Before #42, ``app/services/analysis_worker._build_embedding`` would silently
base64-encode an all-zero vector when the EffNet projection produced one
(silent intros, sub-1s clips, encoder edge cases). The DB stored the zero,
FAISS rejected it on the ``norm < 1e-9`` check, and ~36% of the library
went invisible to similarity / radio / song-path / music-map.

This script:

  1. Reads every ``track_features`` row that has a non-NULL ``embedding``.
  2. Decodes the base64 → float32 vector.
  3. If ``np.any(vec) == False`` (all zeros), nulls ``embedding`` AND
     ``file_hash`` so the next ``POST /v1/library/scan`` re-analyses the file
     with the new (post-#42) code path. The new code returns ``None`` for
     degenerate output, the row is upserted with ``embedding = NULL`` and a
     fresh ``file_hash``, and subsequent scans skip it cleanly (no retry loop).

Idempotent: re-running after a successful pass is a no-op (no all-zero rows
remain). Dry-run by default; pass ``--execute`` to apply.

Usage:
    python migrations/011_nullify_zero_embeddings.py              # dry-run
    python migrations/011_nullify_zero_embeddings.py --execute    # apply

Reads DATABASE_URL from the environment / .env.
"""

from __future__ import annotations

import argparse
import base64
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import numpy as np


def open_sqlite(database_url: str) -> sqlite3.Connection:
    if "sqlite" not in database_url:
        sys.exit("Only SQLite is supported by this script. PostgreSQL users: open a follow-up.")
    path = database_url.split("///", 1)[-1]
    if not Path(path).exists():
        sys.exit(f"Database file not found: {path}")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def is_zero_embedding(b64: str) -> bool:
    try:
        vec = np.frombuffer(base64.b64decode(b64), dtype=np.float32)
    except Exception:
        return False
    if vec.size == 0:
        return True
    return not bool(np.any(vec))


def find_zero_rows(cur) -> list[tuple[int, str, str]]:
    cur.execute(
        "SELECT id, track_id, embedding FROM track_features WHERE embedding IS NOT NULL"
    )
    out: list[tuple[int, str, str]] = []
    for row_id, track_id, embedding in cur.fetchall():
        if embedding and is_zero_embedding(embedding):
            out.append((row_id, track_id, embedding))
    return out


def nullify_rows(conn, row_ids: list[int]) -> int:
    if not row_ids:
        return 0
    cur = conn.cursor()
    chunk = 500
    n = 0
    for i in range(0, len(row_ids), chunk):
        sub = row_ids[i : i + chunk]
        ph = ",".join(["?"] * len(sub))
        cur.execute(
            f"UPDATE track_features "
            f"SET embedding = NULL, file_hash = NULL "
            f"WHERE id IN ({ph})",
            sub,
        )
        n += cur.rowcount
    return n


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migration 011 — nullify all-zero EffNet embeddings (#42)."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the cleanup. Default is dry-run (read-only).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:////data/grooveiq.db")
    print(f"Migration 011 — nullify zero embeddings (#42)")
    print(f"Database:  {database_url}")
    print(f"Dry run:   {not args.execute}")

    conn = open_sqlite(database_url)
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM track_features WHERE embedding IS NOT NULL")
        total_with_emb = cur.fetchone()[0]
        print(f"\nTotal rows with embedding: {total_with_emb}")

        print("Scanning for all-zero embeddings (this can take a minute on large libraries)...")
        t0 = time.time()
        zero_rows = find_zero_rows(cur)
        scan_elapsed = time.time() - t0
        print(f"Scan complete in {scan_elapsed:.1f}s")
        print(f"\nAll-zero embeddings found: {len(zero_rows)} ({100*len(zero_rows)/max(total_with_emb,1):.1f}%)")

        if not zero_rows:
            print("Nothing to do — no all-zero embeddings detected.")
            return

        print("\nSample of affected rows (first 5):")
        for row_id, track_id, _ in zero_rows[:5]:
            print(f"  id={row_id:>7}  track_id={track_id[:32]:32s}")

        if not args.execute:
            print("\nDry run — no changes written. Pass --execute to apply.")
            return

        print(f"\nNullifying embedding + file_hash on {len(zero_rows)} rows...")
        t0 = time.time()
        try:
            row_ids = [r[0] for r in zero_rows]
            n = nullify_rows(conn, row_ids)
            conn.commit()
        except Exception:
            conn.rollback()
            print("\nERROR: rolled back.")
            raise
        elapsed = time.time() - t0
        print(f"\nDone in {elapsed:.1f}s. {n} rows updated.")
        print(
            "\nNext step: POST /v1/library/scan to re-analyse those files with the "
            "post-#42 code. New analyses that still produce a degenerate vector "
            "will simply persist embedding=NULL (no infinite retry loop)."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
