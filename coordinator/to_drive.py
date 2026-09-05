"""Export collected battles from the coordinator database into files for Google Drive.

Deliberately dumb: it writes gzipped JSONL shards into a local folder, and something else
(the Drive desktop app, or rclone) syncs that folder. The coordinator never holds a Google
credential, so a stolen token cannot reach the operator's Drive, and volunteers never touch
it at all.

    python3 coordinator/to_drive.py --db work.db --out ~/CR-Collection

Re-running only writes shards that do not exist yet, so it is safe on a timer.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
from pathlib import Path

SHARD = 2000            # battles per file: ~13 MB raw, a few MB gzipped


def _rows_in(path):
    """How many battles a shard already holds. A short file is refilled, not trusted."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return sum(1 for _ in fh)
    except Exception:                                          # noqa: BLE001
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="work.db")
    ap.add_argument("--out", required=True, help="folder Google Drive syncs")
    ap.add_argument("--shard", type=int, default=SHARD)
    args = ap.parse_args()

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.db)

    total = db.execute("SELECT COUNT(*) FROM battle").fetchone()[0]
    written = skipped = 0
    for start in range(0, total, args.shard):
        path = out / f"battles-{start:07d}.jsonl.gz"
        # Skip a shard only if the FILE is genuinely full. Inferring fullness from the
        # battle count was wrong: a shard written while partial was later assumed complete
        # and skipped, so the battles that should have filled it were silently lost.
        if path.exists() and _rows_in(path) >= args.shard:
            skipped += 1
            continue
        rows = db.execute(
            "SELECT payload FROM battle ORDER BY replay_tag LIMIT ? OFFSET ?",
            (args.shard, start)).fetchall()
        if not rows:
            break
        tmp = path.with_suffix(".part")        # never let Drive see a half-written file
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            for (payload,) in rows:
                fh.write(payload if payload.endswith("\n") else payload + "\n")
        tmp.rename(path)
        written += 1
        state = "final" if len(rows) >= args.shard else "partial, refilled as more arrive"
        print(f"  {path.name}  ({len(rows)} battles, {state})")

    mb = sum(p.stat().st_size for p in out.glob("battles-*.jsonl.gz")) / 1e6
    print(f"\n{total} battles | {written} new shards, {skipped} already there | {mb:.1f} MB")
    print(f"Point Google Drive at {out} and it uploads on its own.")


if __name__ == "__main__":
    main()
