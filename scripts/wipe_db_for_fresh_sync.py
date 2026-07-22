#!/usr/bin/env python3
"""
Back up and then wipe the matching-related DB state for a clean re-sync.

Why a wipe rather than letting sync overwrite
---------------------------------------------
sync_encodings_to_db.py UPSERTS photos on drive_path, so rows from an older run
survive untouched if the new run doesn't happen to include them. After
re-preprocessing the whole album that leaves stale photos in the gallery with
ids nothing else references. Clearing first makes the DB exactly reflect the new
encodings.

Cascades that matter (from supabase_migration_phase1.sql)
---------------------------------------------------------
    guest_photo_disassociations.photo_id -> photos(id)   ON DELETE CASCADE
    guest_photo_disassociations.guest_id -> guests(id)   ON DELETE CASCADE
    clusters.guest_id                    -> guests(id)   ON DELETE SET NULL
    faces.cluster_id                     -> clusters(id) ON DELETE SET NULL

So deleting photos or guests silently removes the "not me" decisions. That is
data a human entered deliberately, so it is backed up and called out rather than
quietly dropped.

Guest selfies live in Storage as selfie_<guest_id>.jpg. Deleting a guest does
not delete the blob, it just orphans it — so those are downloaded first.

Usage
-----
    python scripts/wipe_db_for_fresh_sync.py           # back up + report only
    python scripts/wipe_db_for_fresh_sync.py --yes     # actually wipe
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
BUCKET = "weddingsnap-cache"

# Deleted in FK-safe order: children before the rows they reference.
WIPE_ORDER = [
    ("guest_photos", "photo_id", 0),
    ("guest_photo_disassociations", "photo_id", 0),
    ("faces", "id", 0),
    ("clusters", "id", 0),
    ("photos", "id", 0),
    ("guests", None, None),          # uuid pk — needs per-row delete
]
KEEP = ["invite_codes"]              # the event itself, not matching state


def get_client():
    import os
    from dotenv import load_dotenv

    load_dotenv(project_root / "backend" / ".env")
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL / SUPABASE_KEY not set")
    from supabase import create_client

    return create_client(url, key)


def fetch_all(sb, table: str) -> list[dict]:
    rows, start = [], 0
    while True:
        res = sb.table(table).select("*").range(start, start + 999).execute()
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < 1000:
            return rows
        start += 1000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually delete")
    args = ap.parse_args()

    sb = get_client()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = project_root / "backups" / f"full_wipe_{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    print(f"=== wipe_db_for_fresh_sync [{'APPLY' if args.yes else 'BACKUP + REPORT'}] ===\n")

    tables = [t for t, _, _ in WIPE_ORDER] + KEEP
    snapshot = {}
    for t in tables:
        try:
            rows = fetch_all(sb, t)
        except Exception as e:
            print(f"  {t:<30} could not read ({str(e)[:40]})")
            continue
        snapshot[t] = rows
        (out / f"{t}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        note = "  <- KEPT" if t in KEEP else ""
        print(f"  {t:<30} {len(rows):>5} rows backed up{note}")

    # Selfies would be orphaned by deleting guests — keep the actual images.
    guests = snapshot.get("guests", [])
    saved = 0
    for g in guests:
        name = f"selfie_{g['id']}.jpg"
        try:
            data = sb.storage.from_(BUCKET).download(name)
        except Exception:
            continue
        (out / name).write_bytes(data)
        saved += 1
    print(f"\n  guest selfies downloaded: {saved}")

    print(f"\nBackup -> {out}")

    print("\nWHAT THIS REMOVES")
    for t, _, _ in WIPE_ORDER:
        print(f"  {t:<30} {len(snapshot.get(t, [])):>5}")
    print(f"  {'(kept) invite_codes':<30} {len(snapshot.get('invite_codes', [])):>5}")

    dis = snapshot.get("guest_photo_disassociations", [])
    if dis:
        print(f"\n  NOTE: {len(dis)} \"not me\" decision(s) will go with them. They point at")
        print("  photo ids from the old run, which the re-sync regenerates, so they")
        print("  could not have been carried across anyway.")
    if guests:
        print(f"\n  NOTE: {len(guests)} guest account(s) will be deleted — those people")
        print("  re-register and re-upload a selfie. Their stored selfies are saved")
        print("  in the backup folder above.")

    if not args.yes:
        print("\nNothing deleted. Re-run with --yes to apply.")
        return

    print("\nDeleting...")
    for table, col, floor in WIPE_ORDER:
        try:
            if col is None:                      # uuid pk, delete row by row
                for row in snapshot.get(table, []):
                    sb.table(table).delete().eq("id", row["id"]).execute()
            else:
                sb.table(table).delete().gte(col, floor).execute()
            left = len(fetch_all(sb, table))
            print(f"  {table:<30} {left} row(s) remain")
        except Exception as e:
            print(f"  {table:<30} FAILED: {str(e)[:70]}")

    print(f"\nDone. Backup in {out}")
    print("Next: sync_encodings_to_db.py, then apply_cluster_names.py --apply-db")


if __name__ == "__main__":
    main()
