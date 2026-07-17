#!/usr/bin/env python3
"""
Phase 1 sync: face_encodings.pkl → Supabase Postgres (photos + faces tables).

Run after preprocessing (and after applying supabase_migration_phase1.sql in
the Supabase SQL editor). Once faces rows exist, the backend automatically
switches matching from the in-memory pkl scan to the pgvector ANN RPC.

Usage:
    python scripts/sync_encodings_to_db.py \
        --encodings backend/encodings/face_encodings.pkl

    # also fold legacy disassociated_photos.json into the new table:
    python scripts/sync_encodings_to_db.py \
        --encodings backend/encodings/face_encodings.pkl \
        --migrate-disassociations

Credentials: SUPABASE_URL / SUPABASE_KEY from the environment or backend/.env.
The Drive filename→id map is read from the weddingsnap-cache Storage bucket
(drive_filename_map.json), same as the backend uses.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

import numpy as np

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

BUCKET = "weddingsnap-cache"
BATCH = 100  # rows per insert request (512-d embeddings ≈ 10 KB each as JSON)


def get_client():
    import os
    from dotenv import load_dotenv

    load_dotenv(project_root / "backend" / ".env")
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL / SUPABASE_KEY not set (env or backend/.env)")
    from supabase import create_client

    return create_client(url, key)


def load_filename_map(sb) -> dict[str, str]:
    """filename → Drive file id, from the same Storage cache the backend uses."""
    try:
        data = sb.storage.from_(BUCKET).download("drive_filename_map.json")
        mapping = json.loads(data.decode("utf-8"))
        print(f"Loaded Drive filename map: {len(mapping):,} entries")
        return mapping
    except Exception as e:
        print(f"! No drive_filename_map.json in Storage ({e}) — drive_id will be null")
        return {}


def sync_faces(sb, records: list[dict], fname_to_drive: dict[str, str]) -> None:
    # Photos table rows (needs a drive id — drive_path is the unique key)
    photos_rows: dict[str, dict] = {}
    face_rows: list[dict] = []
    skipped_dim = 0

    for rec in records:
        filename = Path(rec["path"]).name
        drive_id = fname_to_drive.get(filename)
        encodings = rec.get("encodings", [])
        locations = rec.get("locations", [None] * len(encodings))
        frames = rec.get("frame_indices", [None] * len(encodings))

        if drive_id:
            photos_rows[drive_id] = {
                "drive_path": drive_id,
                "filename": filename,
                "is_common": bool(rec.get("is_common", False)),
                "face_count": int(rec.get("face_count", len(encodings))),
            }

        for enc, loc, frame in zip(encodings, locations, frames):
            vec = np.asarray(enc, dtype=float)
            if vec.shape[0] != 512:
                skipped_dim += 1
                continue
            face_rows.append({
                "filename": filename,
                "drive_id": drive_id,
                "embedding": vec.tolist(),
                "bbox": [int(v) for v in loc] if loc is not None else None,
                "frame_idx": int(frame) if frame is not None else None,
            })

    if skipped_dim:
        print(f"! Skipped {skipped_dim} non-512-d encodings (dlib?). "
              f"The faces table is ArcFace-only; re-preprocess with insightface.")
    if not face_rows:
        sys.exit("No 512-d faces to sync — nothing done.")

    print(f"Upserting {len(photos_rows):,} photos rows...")
    rows = list(photos_rows.values())
    for i in range(0, len(rows), 500):
        sb.table("photos").upsert(rows[i : i + 500], on_conflict="drive_path").execute()

    print("Replacing faces table (delete + insert)...")
    sb.table("faces").delete().gte("id", 0).execute()
    for i in range(0, len(face_rows), BATCH):
        sb.table("faces").insert(face_rows[i : i + BATCH]).execute()
        done = min(i + BATCH, len(face_rows))
        print(f"  faces: {done:,}/{len(face_rows):,}", end="\r")
    print(f"\nSynced {len(face_rows):,} faces from {len(records):,} photo records.")


def migrate_disassociations(sb) -> None:
    """Fold legacy disassociated_photos.json into guest_photo_disassociations."""
    try:
        data = sb.storage.from_(BUCKET).download("disassociated_photos.json")
        legacy = json.loads(data.decode("utf-8"))
    except Exception as e:
        print(f"No legacy disassociated_photos.json to migrate ({e})")
        return

    migrated = skipped = 0
    for guest_id, photo_ids in legacy.items():
        for raw in photo_ids:
            try:
                pid = int(raw)
            except (TypeError, ValueError):
                skipped += 1
                continue
            try:
                sb.table("guest_photo_disassociations").upsert(
                    {"guest_id": guest_id, "photo_id": pid},
                    on_conflict="guest_id,photo_id",
                ).execute()
                migrated += 1
            except Exception as e:
                # e.g. FK violation for a since-deleted photo — skip, keep going
                print(f"  ! skipped guest={guest_id} photo={pid}: {e}")
                skipped += 1
    print(f"Disassociations migrated: {migrated}, skipped: {skipped}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--encodings", default="backend/encodings/face_encodings.pkl")
    ap.add_argument("--migrate-disassociations", action="store_true")
    args = ap.parse_args()

    sb = get_client()

    pkl = Path(args.encodings)
    if not pkl.exists():
        sys.exit(f"Encodings file not found: {pkl}")
    with open(pkl, "rb") as f:
        records = pickle.load(f)
    print(f"Loaded {len(records):,} photo records from {pkl}")

    sync_faces(sb, records, load_filename_map(sb))

    if args.migrate_disassociations:
        migrate_disassociations(sb)

    print("\nDone. The backend will use pgvector matching on its next registration.")


if __name__ == "__main__":
    main()
