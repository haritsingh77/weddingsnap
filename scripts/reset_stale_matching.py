#!/usr/bin/env python3
"""
Reset matching state that goes stale when the preprocessor is re-run.

Run this BETWEEN preprocessing and sync_encodings_to_db.py, when the new
preprocess uses a different face backend/model than the one that produced the
current state (e.g. the dlib 128-d → ArcFace buffalo_l 512-d switch).

Why this is needed
------------------
Two stores hold matching state keyed to the OLD run, and neither is cleaned up
by sync_encodings_to_db.py:

  cluster_names.json   Admin-assigned cluster names. Some keys are positional
                       cluster indices ("61"), which the next preprocess
                       reassigns to a DIFFERENT person — so old names land on
                       the wrong faces. Other keys embed a detector bbox, which
                       a different detector won't reproduce (dead, not wrong).

  guest_photos         Guest→photo assignments computed by the old matcher.
                       Re-matching ADDS rows and never removes these, so bad
                       matches from the less accurate run persist forever.

Deliberately NOT touched
------------------------
  photos                        Stable identity; sync + disassociations key off it.
  guests                        Registrations are not matching state.
  guest_photo_disassociations   Explicit human "not me" decisions, keyed on the
                                stable photo_id — these must survive.
  faces / clusters              sync_encodings_to_db.py wipes/rebuilds these itself.

Caveat: guest_photos has no provenance column, so manual /photos/share
assignments are indistinguishable from auto-matched ones and are cleared too.
Re-matching will not restore them.

Usage
-----
    python scripts/reset_stale_matching.py            # dry run (default)
    python scripts/reset_stale_matching.py --yes      # actually do it

Backups are written to backups/ as timestamped JSON before anything is cleared.
To restore guest_photos, upsert the rows from the backup file; to restore the
names, re-upload the backup as cluster_names.json.

Credentials: SUPABASE_URL / SUPABASE_KEY from the environment or backend/.env.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent

BUCKET = "weddingsnap-cache"
NAMES_BLOB = "cluster_names.json"
PAGE = 1000  # rows per read page


def get_client():
    import os

    from dotenv import load_dotenv

    load_dotenv(project_root / "backend" / ".env")
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL / SUPABASE_KEY not set (env or backend/.env)")
    from supabase import create_client

    return create_client(url, key)


def fetch_all(sb, table: str, cols: str) -> list[dict]:
    """Page through a table — Supabase caps a single select at 1000 rows."""
    rows, start = [], 0
    while True:
        res = sb.table(table).select(cols).range(start, start + PAGE - 1).execute()
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        start += PAGE


def backup(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"  backed up -> {path.name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--yes",
        action="store_true",
        help="actually clear (default is a dry run that only reports)",
    )
    ap.add_argument(
        "--keep-guest-photos",
        action="store_true",
        help="leave guest_photos alone; only reset cluster_names.json",
    )
    args = ap.parse_args()

    sb = get_client()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = project_root / "backups"
    mode = "APPLY" if args.yes else "DRY RUN"
    print(f"=== reset_stale_matching [{mode}] ===\n")

    # -- 1. cluster_names.json -----------------------------------------------
    print(f"{NAMES_BLOB}:")
    try:
        raw = sb.storage.from_(BUCKET).download(NAMES_BLOB)
        names = json.loads(raw.decode("utf-8"))
    except Exception as e:
        names = None
        print(f"  absent or unreadable ({str(e)[:60]}) - nothing to clear")

    if names is not None:
        positional = [k for k in names if k.isdigit()]
        print(f"  {len(names)} entries ({len(positional)} positional keys, "
              f"which would be REASSIGNED to different people)")
        for k, v in list(names.items())[:10]:
            print(f"    {k[:48]:<48} -> {v}")
        if len(names) > 10:
            print(f"    ... and {len(names) - 10} more")

        if args.yes:
            backup(backup_dir / f"cluster_names_{stamp}.json", names)
            sb.storage.from_(BUCKET).update(
                NAMES_BLOB,
                json.dumps({}).encode("utf-8"),
                {"content-type": "application/json", "upsert": "true"},
            )
            print("  cleared (reset to {})")
        else:
            print("  would back up, then reset to {}")

    # -- 2. guest_photos ------------------------------------------------------
    print("\nguest_photos:")
    if args.keep_guest_photos:
        print("  skipped (--keep-guest-photos)")
    else:
        rows = fetch_all(sb, "guest_photos", "guest_id, photo_id")
        print(f"  {len(rows)} rows (all produced by the previous matcher)")

        guests = {g["id"]: g["name"] for g in fetch_all(sb, "guests", "id, name")}
        per_guest: dict[str, int] = {}
        for r in rows:
            per_guest[r["guest_id"]] = per_guest.get(r["guest_id"], 0) + 1
        for gid, n in sorted(per_guest.items(), key=lambda x: -x[1]):
            label = guests.get(gid, gid)
            print(f"    {label[:30]:<30} {n} photos")

        if rows:
            if args.yes:
                backup(backup_dir / f"guest_photos_{stamp}.json", rows)
                # Composite PK (guest_id, photo_id) - no id column to filter on.
                # photo_id is a positive bigint, so this matches every row.
                sb.table("guest_photos").delete().gte("photo_id", 0).execute()
                left = fetch_all(sb, "guest_photos", "photo_id")
                print(f"  cleared ({len(left)} rows remain)")
            else:
                print("  would back up, then delete all rows")

    # -- Untouched, stated explicitly so the blast radius is visible ----------
    print("\nleft alone: photos, guests, guest_photo_disassociations, faces, clusters")

    if not args.yes:
        print("\nDry run - nothing changed. Re-run with --yes to apply.")
    else:
        print(f"\nDone. Backups in backups/ (stamp {stamp}).")
        print("Next: python scripts/sync_encodings_to_db.py")


if __name__ == "__main__":
    main()
