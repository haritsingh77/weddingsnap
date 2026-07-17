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


def sync_faces(sb, records: list[dict], fname_to_drive: dict[str, str]) -> list[dict]:
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
    return face_rows


# ── Clustering (stable identity) ─────────────────────────────────────────────

def _fetch_all(sb, table: str, cols: str, page: int = 1000) -> list[dict]:
    out, offset = [], 0
    while True:
        res = sb.table(table).select(cols).range(offset, offset + page - 1).execute()
        rows = res.data or []
        out.extend(rows)
        if len(rows) < page:
            return out
        offset += page


def _face_key(row: dict) -> tuple:
    bbox = row.get("bbox") or []
    return (row["filename"], tuple(int(v) for v in bbox), row.get("frame_idx"))


def _cluster_labels(embeddings: np.ndarray, threshold: float) -> np.ndarray:
    """kNN graph + union-find over cosine distance (mirrors the backend's
    FAISS approach, but on sklearn so the script has no faiss dependency)."""
    from sklearn.neighbors import NearestNeighbors

    n = len(embeddings)
    X = embeddings / (np.linalg.norm(embeddings, axis=1, keepdims=True) + 1e-8)
    k = min(50, n)
    nn = NearestNeighbors(n_neighbors=k, metric="cosine").fit(X)
    dists, idxs = nn.kneighbors(X)

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j_pos in range(1, k):
            if dists[i][j_pos] <= threshold:
                ri, rj = find(i), find(int(idxs[i][j_pos]))
                if ri != rj:
                    parent[ri] = rj

    return np.array([find(i) for i in range(n)])


def snapshot_old_clusters(sb) -> tuple[dict, dict]:
    """Capture (face key → cluster_id) and (cluster_id → name) BEFORE the faces
    wipe, so re-clustered groups can reclaim their old identity and names."""
    key_to_cluster: dict[tuple, int] = {}
    try:
        for row in _fetch_all(sb, "faces", "filename, bbox, frame_idx, cluster_id"):
            if row.get("cluster_id") is not None:
                key_to_cluster[_face_key(row)] = row["cluster_id"]
        names = {
            c["id"]: c.get("name")
            for c in _fetch_all(sb, "clusters", "id, name")
        }
        print(f"Snapshot: {len(key_to_cluster):,} clustered faces, {len(names)} clusters")
        return key_to_cluster, names
    except Exception as e:
        print(f"! Cluster snapshot failed ({e}) — clusters will be created fresh")
        return {}, {}


def assign_clusters(sb, face_rows: list[dict], snapshot: tuple[dict, dict]) -> None:
    import os
    from collections import Counter

    threshold = float(
        os.getenv("CLUSTER_THRESHOLD", os.getenv("ARCFACE_MATCH_THRESHOLD", "0.4"))
    )
    old_key_to_cluster, old_names = snapshot

    # Map local rows (which carry embeddings) to the DB ids just inserted.
    db_faces = _fetch_all(sb, "faces", "id, filename, bbox, frame_idx")
    key_to_ids: dict[tuple, list[int]] = {}
    for row in db_faces:
        key_to_ids.setdefault(_face_key(row), []).append(row["id"])

    ids, keys, embs = [], [], []
    for row in face_rows:
        key = _face_key(row)
        pool = key_to_ids.get(key)
        if not pool:
            continue
        ids.append(pool.pop(0))
        keys.append(key)
        embs.append(row["embedding"])

    if len(embs) < 2:
        print("Clustering skipped: fewer than 2 faces")
        return

    print(f"Clustering {len(embs):,} faces (threshold={threshold})...")
    labels = _cluster_labels(np.asarray(embs, dtype=np.float32), threshold)

    groups: dict[int, list[int]] = {}
    for idx, label in enumerate(labels):
        groups.setdefault(int(label), []).append(idx)
    groups = {lbl: idxs for lbl, idxs in groups.items() if len(idxs) >= 2}

    reused = created = preserved_names = 0
    used_old: set[int] = set()
    active_cluster_ids: list[int] = []

    for idxs in groups.values():
        # Reclaim the old cluster this group most overlaps with (stable identity)
        overlap = Counter(
            old_key_to_cluster[keys[i]]
            for i in idxs
            if keys[i] in old_key_to_cluster and old_key_to_cluster[keys[i]] not in used_old
        )
        cluster_id = None
        if overlap:
            best_cid, best_n = overlap.most_common(1)[0]
            if best_n >= max(2, len(idxs) // 4):
                cluster_id = best_cid
                used_old.add(best_cid)
                reused += 1
                if old_names.get(best_cid):
                    preserved_names += 1

        # Representative: largest face in the group (bbox = top, right, bottom, left)
        def _area(i: int) -> int:
            bbox = keys[i][1]
            if len(bbox) == 4:
                top, right, bottom, left = bbox
                return abs((bottom - top) * (right - left))
            return 0
        rep_idx = max(idxs, key=_area)
        rep_face_id = ids[rep_idx]

        if cluster_id is None:
            res = sb.table("clusters").insert({"rep_face_id": rep_face_id}).execute()
            cluster_id = res.data[0]["id"]
            created += 1
        else:
            sb.table("clusters").update({"rep_face_id": rep_face_id}).eq("id", cluster_id).execute()

        active_cluster_ids.append(cluster_id)
        member_ids = [ids[i] for i in idxs]
        for i in range(0, len(member_ids), 200):
            sb.table("faces").update({"cluster_id": cluster_id}).in_(
                "id", member_ids[i : i + 200]
            ).execute()

    # Drop unnamed clusters that no longer have any faces (named ones are kept —
    # an admin's label shouldn't vanish because one re-run failed to re-match it)
    stale = [
        cid for cid, name in old_names.items()
        if cid not in set(active_cluster_ids) and not name
    ]
    for i in range(0, len(stale), 200):
        sb.table("clusters").delete().in_("id", stale[i : i + 200]).execute()

    print(
        f"Clusters: {len(groups)} groups → {reused} reused (identity preserved, "
        f"{preserved_names} named), {created} new, {len(stale)} stale removed"
    )


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
    ap.add_argument("--no-cluster", action="store_true",
                    help="skip cluster assignment (faces sync only)")
    args = ap.parse_args()

    sb = get_client()

    pkl = Path(args.encodings)
    if not pkl.exists():
        sys.exit(f"Encodings file not found: {pkl}")
    with open(pkl, "rb") as f:
        records = pickle.load(f)
    print(f"Loaded {len(records):,} photo records from {pkl}")

    # Snapshot cluster identity BEFORE the faces wipe so names survive re-sync
    snapshot = ({}, {}) if args.no_cluster else snapshot_old_clusters(sb)

    face_rows = sync_faces(sb, records, load_filename_map(sb))

    if not args.no_cluster:
        assign_clusters(sb, face_rows, snapshot)

    if args.migrate_disassociations:
        migrate_disassociations(sb)

    print("\nDone. The backend will use pgvector matching on its next registration.")


if __name__ == "__main__":
    main()
