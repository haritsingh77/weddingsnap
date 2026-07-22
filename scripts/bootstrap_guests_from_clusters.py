#!/usr/bin/env python3
"""
Create one guest profile per named cluster, each with its own access link.

Why this exists
---------------
The clustering already knows who is in which photo, and the names came from a
human. So a guest does not need to prove who they are with a selfie — they just
need a link that says which cluster is theirs. That removes the whole enrolment
step, and with it every "we couldn't detect a face" failure.

What it writes
--------------
  guests           one row per named cluster, with a unique access_token
  guest_clusters   guest -> cluster, so a household can hold several people
  guest_photos     the guest's personal photos

guest_photos is derived from the cluster and can always be rebuilt by re-running
this. Common photos are NOT written: get_guest_photos already returns anything
flagged is_common, so duplicating them here would just inflate the table.

Existing guests are matched by name and updated rather than duplicated, so this
is safe to run more than once.

Usage
-----
    python scripts/bootstrap_guests_from_clusters.py            # preview
    python scripts/bootstrap_guests_from_clusters.py --yes      # write
    python scripts/bootstrap_guests_from_clusters.py --yes --links links.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "backend"))


def get_client():
    import os

    from dotenv import load_dotenv

    load_dotenv(project_root / "backend" / ".env")
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL / SUPABASE_KEY not set")
    from supabase import create_client

    return create_client(url, key)


def fetch_all(sb, table: str, cols: str, page: int = 1000) -> list[dict]:
    out, offset = [], 0
    while True:
        rows = (sb.table(table).select(cols).range(offset, offset + page - 1).execute()).data or []
        out.extend(rows)
        if len(rows) < page:
            return out
        offset += page


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="actually write")
    ap.add_argument("--base-url", default="https://weddingsnap.vercel.app",
                    help="front of the guest link")
    ap.add_argument("--links", default="guest_links.csv", help="where to write the link list")
    ap.add_argument("--invite-code", default="WEDDING2024")
    args = ap.parse_args()

    sb = get_client()
    from app.auth_deps import new_access_token

    clusters = [c for c in fetch_all(sb, "clusters", "id, name") if c.get("name")]
    if not clusters:
        sys.exit("No named clusters — run apply_cluster_names.py --apply-db first.")

    faces = fetch_all(sb, "faces", "drive_id, cluster_id")
    photos = fetch_all(sb, "photos", "id, drive_path")
    drive_to_pid = {p["drive_path"]: p["id"] for p in photos}

    by_cluster: dict[int, set[str]] = {}
    for f in faces:
        if f.get("cluster_id") is not None and f.get("drive_id"):
            by_cluster.setdefault(f["cluster_id"], set()).add(f["drive_id"])

    existing = {g["name"].strip().lower(): g for g in fetch_all(sb, "guests", "id, name, access_token")}

    print(f"{len(clusters)} named cluster(s)\n")
    plan = []
    for c in sorted(clusters, key=lambda c: -len(by_cluster.get(c["id"], ()))):
        name = c["name"].strip()
        drive_ids = by_cluster.get(c["id"], set())
        prior = existing.get(name.lower())
        plan.append((c["id"], name, drive_ids, prior))
        state = "update" if prior else "create"
        print(f"  {state:<7} {name:<26} cluster {c['id']:<5} {len(drive_ids):>5} photos")

    total = sum(len(d) for _, _, d, _ in plan)
    print(f"\n  {len(plan)} guest(s), {total:,} guest_photos rows")

    if not args.yes:
        print("\nPreview only. Re-run with --yes to write.")
        return

    rows_out = []
    for cid, name, drive_ids, prior in plan:
        token = (prior or {}).get("access_token") or new_access_token()
        if prior:
            guest_id = prior["id"]
            sb.table("guests").update({"access_token": token}).eq("id", guest_id).execute()
        else:
            res = sb.table("guests").insert({
                "name": name,
                "phone": "",
                "invite_code": args.invite_code,
                "access_token": token,
            }).execute()
            guest_id = res.data[0]["id"]

        sb.table("guest_clusters").upsert(
            {"guest_id": guest_id, "cluster_id": cid, "label": name},
            on_conflict="guest_id,cluster_id",
        ).execute()

        pids = [drive_to_pid[d] for d in drive_ids if d in drive_to_pid]
        gp = [{"guest_id": guest_id, "photo_id": p} for p in pids]
        for i in range(0, len(gp), 500):
            sb.table("guest_photos").upsert(
                gp[i : i + 500], on_conflict="guest_id,photo_id"
            ).execute()

        rows_out.append({"name": name, "photos": len(pids),
                         "link": f"{args.base_url.rstrip('/')}/g/{token}"})
        print(f"  {name:<26} {len(pids):>5} photos  ->  /g/{token[:12]}...")

    out = project_root / args.links
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "photos", "link"])
        w.writeheader()
        w.writerows(rows_out)
    print(f"\nWrote {len(rows_out)} link(s) -> {out.name}")
    print("Each link logs that guest straight in. Treat it like a password.")


if __name__ == "__main__":
    main()
