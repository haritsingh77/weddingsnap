#!/usr/bin/env python3
"""
Name face clusters from a handful of reference photos.

The problem
-----------
A full wedding gallery yields ~30k face vectors and a few thousand raw
clusters. Naming those by hand is impossible. But the distribution is very
skewed: on a measured 2,961-face sample, 74% of clusters were singletons
(strangers in backgrounds, motion blur, partial profiles) while the top 28
clusters held 80% of all faces.

So the work isn't 30k faces — it's a few hundred real people, and a few dozen
of them cover most of the gallery.

The approach
------------
Supply one clear photo per person you care about. Each reference is matched
against every cluster, so a SINGLE reference photo names an entire cluster —
potentially hundreds of gallery photos at once.

Two useful by-products fall out of the same comparison:

  merge candidates   One reference matching several clusters means that person
                     was split by lighting/angle/age-of-photo. Those clusters
                     are merge candidates — this is the fragmentation problem,
                     detected automatically instead of by eye.

  ambiguities        Two different references matching one cluster means either
                     the cluster is impure (two people welded together), or the
                     two references are of people who genuinely look alike.
                     Either way a human should look.

Nothing is written unless you pass --apply.

Usage
-----
    # 1. Put one clear photo per person in a folder, named after them:
    #      references/Mahima Singh.jpg
    #      references/Saurav.jpg
    #
    # 2. Preview (writes nothing):
    python scripts/label_clusters.py --references references/ \
        --encodings backend/encodings/face_encodings.pkl

    # 3. Apply the proposed names to the DB clusters table:
    python scripts/label_clusters.py --references references/ \
        --encodings backend/encodings/face_encodings.pkl --apply

Note: encoding reference photos loads the face model onto the GPU. If the
preprocessor is running, either wait for it or pass --cpu.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}


# ── pure matching core (no I/O, no GPU — unit-testable) ──────────────────────

def unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    if v.ndim == 1:
        return v / (np.linalg.norm(v) + 1e-8)
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-8)


def cluster_faces(X: np.ndarray, threshold: float) -> np.ndarray:
    """kNN graph + union-find, mirroring sync_encodings_to_db._cluster_labels."""
    from sklearn.neighbors import NearestNeighbors

    n = len(X)
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
        for j in range(1, k):
            if dists[i][j] <= threshold:
                ri, rj = find(i), find(int(idxs[i][j]))
                if ri != rj:
                    parent[ri] = rj

    return np.array([find(i) for i in range(n)])


def match_references(
    refs_by_person: dict[str, np.ndarray],
    X: np.ndarray,
    groups: dict[int, list[int]],
    threshold: float,
    min_support: float,
) -> dict:
    """Score every (person, cluster) pair.

    A person may have several reference photos. A gallery face counts as
    matching that person if it is within `threshold` of ANY of them — one photo
    captures a single angle and lighting, so several together cover the range a
    person actually appears in across a wedding day.

    support = fraction of the cluster's faces that match. Using support rather
    than just the single closest face means one freak near-match can't name a
    whole cluster; the person has to agree with a real share of its members.
    """
    proposals: dict[int, list[dict]] = defaultdict(list)

    for name, embs in refs_by_person.items():
        for cid, members in groups.items():
            # distance from each member to its closest reference for this person
            d = (1.0 - (X[members] @ embs.T)).min(axis=1)
            support = float((d <= threshold).mean())
            if support >= min_support:
                proposals[cid].append({
                    "name": name,
                    "support": support,
                    "min_dist": float(d.min()),
                    "median_dist": float(np.median(d)),
                    "n_refs": int(len(embs)),
                })

    for cid in proposals:
        proposals[cid].sort(key=lambda p: (-p["support"], p["min_dist"]))
    return dict(proposals)


# ── I/O helpers ──────────────────────────────────────────────────────────────

def load_gallery(pkl: Path) -> tuple[np.ndarray, list[str], list[str]]:
    with open(pkl, "rb") as f:
        records = pickle.load(f)

    embs, photos, drive_ids = [], [], []
    for r in records:
        name = Path(r["path"]).name
        did = r.get("drive_id") or ""
        for e in r.get("encodings", []):
            embs.append(e)
            photos.append(name)
            drive_ids.append(did)
    if not embs:
        sys.exit("No encodings in that pkl.")
    return unit(np.vstack(embs).astype(np.float32)), photos, drive_ids


def person_name_for(path: Path, root: Path) -> str:
    """Derive the person's name from a reference file's location.

    Both layouts work, so photos can be dropped in whichever way is convenient:

        references/Mahima Singh.jpg            -> "Mahima Singh"
        references/Mahima Singh_2.jpg          -> "Mahima Singh"   (grouped)
        references/Mahima Singh/anything.jpg   -> "Mahima Singh"   (grouped)
    """
    if path.parent != root:
        return path.parent.name
    stem = path.stem
    # trailing "_2" / "-3" / " 2" is a photo counter, not part of the name
    import re
    return re.sub(r"[\s_-]*\d+$", "", stem).strip() or stem


def encode_references(folder: Path, use_cpu: bool) -> dict[str, np.ndarray]:
    """Embed every reference photo, grouped by person (largest face per photo)."""
    from PIL import Image, ImageOps

    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in IMAGE_EXTS)
    if not files:
        sys.exit(f"No reference images in {folder}")

    if use_cpu:
        import os
        os.environ["CUDA_VISIBLE_DEVICES"] = ""

    from scripts.face_engine.config import PreprocessConfig
    from scripts.face_engine.pipeline import get_pipeline

    pipeline = get_pipeline(PreprocessConfig())

    by_person: dict[str, list[np.ndarray]] = defaultdict(list)
    for p in files:
        try:
            img = ImageOps.exif_transpose(Image.open(p).convert("RGB"))
            w, h = img.size
            if max(w, h) > 1600:
                s = 1600 / max(w, h)
                img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
            dets = pipeline.backend.detect_and_encode(np.array(img))
            if not dets:
                print(f"  ! no face found in {p.name} — skipped")
                continue
            largest = max(
                dets, key=lambda d: (d.bbox[2] - d.bbox[0]) * (d.bbox[3] - d.bbox[1])
            )
            by_person[person_name_for(p, folder)].append(largest.encoding)
            if len(dets) > 1:
                print(f"  · {p.name}: {len(dets)} faces, used the largest "
                      f"— crop it if that's the wrong person")
        except Exception as e:
            print(f"  ! failed on {p.name}: {e}")

    if not by_person:
        sys.exit("No usable reference faces.")

    out = {n: unit(np.vstack(v).astype(np.float32)) for n, v in by_person.items()}
    solo = [n for n, v in out.items() if len(v) == 1]
    print(f"  {len(out)} people from {sum(len(v) for v in out.values())} photo(s)")
    if solo:
        print(f"  note: {len(solo)} person(s) have only one reference photo — "
              f"2-3 covers more angles and lighting: {', '.join(solo[:6])}"
              + (" ..." if len(solo) > 6 else ""))
    return out


def main() -> None:
    global pickle
    import pickle

    ap = argparse.ArgumentParser()
    ap.add_argument("--references", required=True, help="folder of <Name>.jpg photos")
    ap.add_argument("--encodings", default="backend/encodings/face_encodings.pkl")
    ap.add_argument("--cluster-threshold", type=float, default=0.40)
    ap.add_argument("--match-threshold", type=float, default=0.50,
                    help="cosine distance for a reference to count as matching a face")
    ap.add_argument("--min-support", type=float, default=0.30,
                    help="fraction of a cluster's faces a reference must match")
    ap.add_argument("--min-photos", type=int, default=3,
                    help="ignore clusters seen in fewer distinct photos")
    ap.add_argument("--out", default="cluster_name_proposals.json")
    ap.add_argument("--cpu", action="store_true", help="force CPU (leaves the GPU alone)")
    ap.add_argument("--apply", action="store_true", help="write names to the DB clusters table")
    args = ap.parse_args()

    X, photos, _ = load_gallery(Path(args.encodings))
    print(f"Gallery: {len(X):,} faces from {len(set(photos)):,} photos")

    labels = cluster_faces(X, args.cluster_threshold)
    all_groups: dict[int, list[int]] = defaultdict(list)
    for i, lb in enumerate(labels):
        all_groups[int(lb)].append(i)

    groups = {
        cid: m for cid, m in all_groups.items()
        if len({photos[i] for i in m}) >= args.min_photos
    }
    print(f"Clusters: {len(all_groups):,} raw -> {len(groups):,} with >={args.min_photos} photos")

    print(f"\nEncoding references from {args.references} ...")
    refs_by_person = encode_references(Path(args.references), args.cpu)

    proposals = match_references(
        refs_by_person, X, groups, args.match_threshold, args.min_support
    )

    # ── report ───────────────────────────────────────────────────────────────
    named, ambiguous = {}, {}
    for cid, cands in proposals.items():
        if len(cands) > 1 and cands[1]["support"] >= 0.8 * cands[0]["support"]:
            ambiguous[cid] = cands
        named[cid] = cands[0]

    by_name: dict[str, list[int]] = defaultdict(list)
    for cid, best in named.items():
        by_name[best["name"]].append(cid)

    print(f"\n{'='*64}\nPROPOSED NAMES\n{'='*64}")
    for name, cids in sorted(by_name.items(), key=lambda kv: -sum(len(groups[c]) for c in kv[1])):
        faces = sum(len(groups[c]) for c in cids)
        pics = len({photos[i] for c in cids for i in groups[c]})
        print(f"  {name:<24} {len(cids)} cluster(s) · {faces:,} faces · {pics:,} photos")
        for c in cids:
            b = named[c]
            print(f"      cluster {c:<8} support {b['support']:.0%}  min_d {b['min_dist']:.3f}")

    merges = {n: c for n, c in by_name.items() if len(c) > 1}
    if merges:
        print(f"\n{'='*64}\nMERGE CANDIDATES (same person split across clusters)\n{'='*64}")
        for n, cids in merges.items():
            print(f"  {n}: merge clusters {cids}")

    if ambiguous:
        print(f"\n{'='*64}\nAMBIGUOUS — review these by hand\n{'='*64}")
        for cid, cands in ambiguous.items():
            top = ", ".join(f"{c['name']} ({c['support']:.0%})" for c in cands[:3])
            print(f"  cluster {cid}: {top}")

    unnamed = sorted(
        (c for c in groups if c not in named),
        key=lambda c: -len(groups[c]),
    )
    if unnamed:
        covered = sum(len(groups[c]) for c in named)
        total = sum(len(groups[c]) for c in groups)
        print(f"\n{'='*64}\nUNNAMED — biggest first, add a reference photo for these\n{'='*64}")
        print(f"  coverage: {covered:,}/{total:,} faces named ({covered/total:.0%})")
        for c in unnamed[:15]:
            pics = len({photos[i] for i in groups[c]})
            print(f"  cluster {c:<8} {len(groups[c]):>4} faces · {pics:>4} photos")
        if len(unnamed) > 15:
            print(f"  ... and {len(unnamed)-15} more")

    out = Path(args.out)
    out.write_text(json.dumps({
        "proposals": {str(c): named[c] for c in named},
        "merge_candidates": {n: cids for n, cids in merges.items()},
        "ambiguous": {str(c): v for c, v in ambiguous.items()},
        "params": vars(args),
    }, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")

    if args.apply:
        apply_to_db(named, groups, photos)
    else:
        print("Preview only — nothing written to the database. Re-run with --apply.")


def apply_to_db(named: dict, groups: dict, photos: list[str]) -> None:
    """Set clusters.name for DB clusters whose membership matches ours.

    Matches on photo overlap rather than cluster index, because the local
    label numbers here are positional and carry no meaning in the DB.
    """
    import os
    from dotenv import load_dotenv

    load_dotenv(project_root / "backend" / ".env")
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL / SUPABASE_KEY not set")
    from supabase import create_client

    sb = create_client(url, key)

    rows, offset = [], 0
    while True:
        res = sb.table("faces").select("id, filename, cluster_id").range(offset, offset + 999).execute()
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000

    db_by_cluster: dict[int, set[str]] = defaultdict(set)
    for r in rows:
        if r.get("cluster_id") is not None:
            db_by_cluster[r["cluster_id"]].add(r["filename"])

    applied = 0
    for cid, best in named.items():
        local_photos = {photos[i] for i in groups[cid]}
        overlaps = sorted(
            ((len(local_photos & files), db_cid) for db_cid, files in db_by_cluster.items()),
            reverse=True,
        )
        if not overlaps or overlaps[0][0] < max(2, len(local_photos) // 4):
            print(f"  ! cluster {cid} ({best['name']}): no confident DB match, skipped")
            continue
        sb.table("clusters").update({"name": best["name"]}).eq("id", overlaps[0][1]).execute()
        applied += 1
    print(f"Applied {applied} name(s) to the clusters table.")


if __name__ == "__main__":
    main()
