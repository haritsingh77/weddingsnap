#!/usr/bin/env python3
"""
Extract one face crop per cluster, so clusters can be named by renaming files.

Why this beats collecting outside photos
----------------------------------------
Naming clusters needs a human to recognise a face — but it does NOT need a new
photo. The gallery already contains every face, and the .pkl already contains
its embedding. So the crop written here is purely something for a person to
look at; nothing is re-encoded and the GPU is never touched.

The workflow is:

    1. run this            -> out/0001_c1234_281photos.jpg, 0002_...
    2. rename the files    -> "Mahima Singh.jpg", "Saurav.jpg"
       (delete any you don't care about; give two files the SAME name if they
        are the same person and the clusters should merge)
    3. run apply_cluster_names.py

Files are numbered by cluster size, so the people who appear in the most photos
sort to the top and you can stop whenever the tail stops being worth naming.

Crops come from the 400px thumbnails the preprocessor already cached, so this
does no Drive traffic and can run while preprocessing is still going.

Usage
-----
    python scripts/extract_cluster_faces.py \
        --encodings backend/encodings/face_encodings.pkl \
        --out cluster_faces --limit 60
"""

from __future__ import annotations

import argparse
import collections
import json
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from scripts.label_clusters import cluster_faces, unit

# The preprocessor downsizes to max_image_dimension before detecting, so face
# boxes live in that coordinate space, while thumbnails are 400px on the long
# edge of the ORIGINAL. Wedding-camera originals are always larger than this,
# so the resized long edge is the config value.
RESIZED_MAX_DIM = 2048
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")


def thumb_dirs() -> list[Path]:
    import os
    dirs = []
    ssd = os.getenv("WEDDINGSNAP_SSD_ROOT", "").strip()
    if ssd:
        dirs.append(Path(ssd) / "api_cache")
    dirs += [
        Path("D:/weddingsnap_cache/api_cache"),
        Path("C:/weddingsnap_cache/api_cache"),
        Path(os.getenv("LOCALAPPDATA", ".")) / "weddingsnap" / "api_cache",
    ]
    return [d for d in dirs if d.is_dir()]


def find_thumb(drive_id: str, dirs: list[Path]) -> Path | None:
    for d in dirs:
        p = d / f"thumb_{drive_id}_400.jpg"
        if p.exists():
            return p
    return None


def load_name_map(dirs: list[Path]) -> dict[str, str]:
    """filename -> drive id, for records written before drive_id was carried
    through. Ambiguous where basenames collide, but this only picks which face
    to *show*, so a wrong pick costs a confusing thumbnail, not bad data."""
    for d in dirs:
        p = d / "drive_filename_map.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    try:
        import os
        from dotenv import load_dotenv
        load_dotenv(project_root / "backend" / ".env")
        from supabase import create_client
        sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
        raw = sb.storage.from_("weddingsnap-cache").download("drive_filename_map.json")
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        print(f"  ! could not load drive_filename_map.json ({str(e)[:60]})")
        return {}


def score_candidates(
    members: list[int],
    X: np.ndarray,
    meta: list[dict],
    dirs: list[Path],
    pad: float,
    size: int,
    shortlist: int,
    min_src: int,
) -> list[tuple[float, int, Image.Image]]:
    """Rank a cluster's faces by how good a *portrait* they make.

    Picking the largest face gave profiles, closed eyes and dark frames — size
    measures how close someone stood to the camera, not how well you can
    recognise them. Four signals instead:

      centrality  cosine similarity to the cluster mean. A profile or a
                  motion-blurred frame sits far from the mean because most
                  members are ordinary frontal shots, so this demotes exactly
                  the odd-angle faces that made the bad crops.
      sharpness   Laplacian variance of the actual crop — rejects blur.
      exposure    penalises crops that are crushed dark or blown out.
      size        kept, but only as a tiebreak; a big blurry face is worse
                  than a small sharp one.

    Each signal is normalised within the cluster, so the weights compare
    like with like.
    """
    import cv2

    centroid = X[members].mean(axis=0)
    centroid /= np.linalg.norm(centroid) + 1e-8
    centrality = X[members] @ centroid

    def area(i: int) -> float:
        b = meta[i]["bbox"]
        if not b:
            return 0.0
        top, right, bottom, left = b
        return abs((bottom - top) * (right - left))

    # Shortlist on cheap signals, then pay for image decoding only on those.
    order = sorted(
        range(len(members)),
        key=lambda k: (meta[members[k]]["is_video"], -centrality[k], -area(members[k])),
    )[:shortlist]

    cand = []
    for k in order:
        i = members[k]
        m = meta[i]
        if not m["drive_id"] or not m["bbox"]:
            continue
        tp = find_thumb(m["drive_id"], dirs)
        if not tp:
            continue
        try:
            raw = crop_face(tp, m["bbox"], pad, None)   # native thumbnail pixels
        except Exception:
            continue
        if raw is None:
            continue
        src_px = min(raw.size)
        if src_px < min_src:
            continue   # too few real pixels to ever make a usable portrait

        # Sharpness must be measured BEFORE upscaling. On the enlarged crop a
        # tiny blurry face scores deceptively well, because LANCZOS invents
        # edges — which is exactly how small distant faces won the first pass.
        g = cv2.cvtColor(np.array(raw), cv2.COLOR_RGB2GRAY)
        cand.append({
            "idx": i,
            "img": raw.resize((size, size), Image.LANCZOS),
            "centrality": float(centrality[k]),
            "sharpness": float(cv2.Laplacian(g, cv2.CV_64F).var()),
            "exposure": float(g.mean()),
            "src_px": float(src_px),
            "area": area(i),
            "is_video": m["is_video"],
        })

    if not cand:
        return []

    def norm(vals: list[float]) -> np.ndarray:
        a = np.asarray(vals, dtype=float)
        lo, hi = a.min(), a.max()
        return np.zeros_like(a) if hi - lo < 1e-9 else (a - lo) / (hi - lo)

    n_cen = norm([c["centrality"] for c in cand])
    n_shp = norm([np.log1p(c["sharpness"]) for c in cand])
    # Resolution in real thumbnail pixels, not detector-space area. This is what
    # actually decides whether the crop looks sharp once enlarged, so it carries
    # real weight rather than acting as a tiebreak.
    n_res = norm([c["src_px"] for c in cand])
    # ideal mid-grey exposure; 1.0 at 128, falling off toward 0 and 255
    n_exp = 1.0 - np.abs(np.asarray([c["exposure"] for c in cand]) - 128.0) / 128.0

    scored = []
    for j, c in enumerate(cand):
        s = (
            0.35 * n_cen[j]
            + 0.25 * n_shp[j]
            + 0.25 * n_res[j]
            + 0.15 * n_exp[j]
            - (0.20 if c["is_video"] else 0.0)
        )
        scored.append((float(s), c["idx"], c["img"]))
    scored.sort(key=lambda t: -t[0])
    return scored


def crop_face(thumb: Path, bbox, pad: float, size: int | None) -> Image.Image | None:
    """bbox is dlib-style (top, right, bottom, left) in resized-image space.

    size=None returns the crop at native thumbnail resolution, which is what
    quality checks must run on — see score_candidates.
    """
    im = Image.open(thumb).convert("RGB")
    tw, th = im.size
    scale = max(tw, th) / RESIZED_MAX_DIM

    top, right, bottom, left = (float(v) * scale for v in bbox)
    w, h = right - left, bottom - top
    if w <= 1 or h <= 1:
        return None

    # square box around the face, padded, clamped to the image
    cx, cy = (left + right) / 2, (top + bottom) / 2
    half = max(w, h) * (1 + pad) / 2
    box = (
        int(max(0, cx - half)), int(max(0, cy - half)),
        int(min(tw, cx + half)), int(min(th, cy + half)),
    )
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return None
    crop = im.crop(box)
    return crop if size is None else crop.resize((size, size), Image.LANCZOS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--encodings", default="backend/encodings/face_encodings.pkl")
    ap.add_argument("--out", default="cluster_faces")
    ap.add_argument("--cluster-threshold", type=float, default=0.40)
    ap.add_argument("--min-photos", type=int, default=3)
    ap.add_argument("--limit", type=int, default=60, help="how many clusters to export")
    ap.add_argument("--exclude-named", metavar="JSON", default=None,
                    help="cluster_names_by_photos.json — skip clusters already named, "
                         "so a second batch never re-issues work you have done")
    ap.add_argument("--size", type=int, default=256)
    ap.add_argument("--pad", type=float, default=0.45, help="context around the face")
    ap.add_argument("--candidates", type=int, default=1,
                    help="crops per cluster; >1 writes _alt1.. alternates to choose from")
    ap.add_argument("--shortlist", type=int, default=14,
                    help="faces per cluster to decode and score")
    ap.add_argument("--min-src", type=int, default=44,
                    help="reject faces smaller than this many thumbnail px")
    ap.add_argument("--impure-at", type=float, default=0.05,
                    help="same-photo duplicate rate above which a cluster is flagged MIXED")
    args = ap.parse_args()

    with open(args.encodings, "rb") as f:
        records = pickle.load(f)

    embs, meta = [], []
    for r in records:
        did = r.get("drive_id")
        name = Path(r["path"]).name
        locs = r.get("locations", [])
        for i, e in enumerate(r.get("encodings", [])):
            embs.append(e)
            meta.append({
                "drive_id": did,
                "photo": name,
                "bbox": locs[i] if i < len(locs) else None,
                "is_video": name.lower().endswith(VIDEO_EXTS),
            })
    if not embs:
        sys.exit("No encodings found.")

    X = unit(np.vstack(embs).astype(np.float32))
    print(f"{len(X):,} faces from {len(records):,} records — clustering...")
    labels = cluster_faces(X, args.cluster_threshold)

    groups = defaultdict(list)
    for i, lb in enumerate(labels):
        groups[int(lb)].append(i)
    groups = {
        c: m for c, m in groups.items()
        if len({meta[i]["photo"] for i in m}) >= args.min_photos
    }
    ranked = sorted(
        groups.items(),
        key=lambda kv: -len({meta[i]["photo"] for i in kv[1]}),
    )
    # Drop clusters already named in a previous batch. Matching is on photo
    # membership, not cluster id: ids are positional, so a re-run renumbers them
    # and an id-based skip would silently re-issue work already done.
    if args.exclude_named:
        saved = json.loads(Path(args.exclude_named).read_text(encoding="utf-8"))
        entries = [v for k, v in saved.items()
                   if not k.startswith("__") and isinstance(v, dict)]
        done_ids = {c for v in entries for c in v.get("clusters", [])}
        # Clusters explicitly dismissed by dropping their crop into ignore/.
        # Without this, a cluster you deliberately skipped comes back in every
        # future batch, because "deleted the file" and "never looked at it" are
        # otherwise indistinguishable.
        done_ids |= set(saved.get("__ignored_clusters__", []))
        done_photos = [set(v["photos"]) for v in entries]

        # Cluster ids are exact when regenerating from the SAME encodings, which
        # is the normal case for a follow-up batch. Photo overlap is only a
        # fallback for a different pkl, and it must be near-identical membership
        # (Jaccard) — plain intersection wrongly excluded 205 clusters for 63
        # names, because different people share the same group photos.
        before = len(ranked)
        keep = []
        for cid, members in ranked:
            if cid in done_ids:
                continue
            mine = {meta[i]["photo"] for i in members}
            if any(
                len(mine & d) / max(1, len(mine | d)) >= 0.5
                for d in done_photos
            ):
                continue
            keep.append((cid, members))
        ranked = keep
        print(f"  excluded {before - len(ranked)} already-named cluster(s) "
              f"from {len(entries)} names")

    print(f"{len(ranked):,} clusters with >={args.min_photos} photos; "
          f"exporting top {min(args.limit, len(ranked))}")

    dirs = thumb_dirs()
    if not dirs:
        sys.exit("No thumbnail cache directory found.")
    print(f"thumbnails from: {dirs[0]}")

    name_map = {}
    if not any(m["drive_id"] for m in meta):
        print("  records carry no drive_id (pre-fix pkl) — resolving by filename")
        name_map = load_name_map(dirs)
    for m in meta:
        if not m["drive_id"]:
            m["drive_id"] = name_map.get(m["photo"])

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.jpg"):
        old.unlink()

    def area(i: int) -> float:
        b = meta[i]["bbox"]
        if not b:
            return 0.0
        top, right, bottom, left = b
        return abs((bottom - top) * (right - left))

    manifest, written, missing = {}, 0, 0
    for rank, (cid, members) in enumerate(ranked[:args.limit], start=1):
        scored = score_candidates(
            members, X, meta, dirs, args.pad, args.size, args.shortlist, args.min_src
        )
        if not scored:
            missing += 1
            continue

        photos = len({meta[j]["photo"] for j in members})

        # Purity check.
        #
        # First signal: two faces from one still photo in the same cluster.
        # That looked conclusive ("a person appears once per frame") but it is
        # NOT — weddings display large printed portraits of the couple, so the
        # real face and the printed one both get detected, and the detector
        # also fires on things like fireworks. Both put two cluster members in
        # one photo without any second person being involved.
        #
        # So duplicates alone only raise suspicion. The confirming signal is
        # whether the cluster SPLITS at a tighter threshold: two genuinely
        # different people separate into two substantial groups, whereas one
        # person with portraits and false detections stays as a single mass.
        stills = [meta[j]["photo"] for j in members if not meta[j]["is_video"]]
        dup_rate = (len(stills) - len(set(stills))) / len(stills) if stills else 0.0
        impure = False
        if dup_rate > args.impure_at and len(members) >= 8:
            sub = cluster_faces(X[members], args.cluster_threshold * 0.7)
            counts = sorted(collections.Counter(sub.tolist()).values(), reverse=True)
            # second group must be a real share of the cluster, not a few strays
            impure = len(counts) > 1 and counts[1] >= 0.15 * len(members)

        photos = len({meta[j]["photo"] for j in members})
        for n, (score, i, img) in enumerate(scored[:args.candidates]):
            m = meta[i]
            # Only the first candidate gets the plain name — that is the one
            # meant to be renamed. Alternates are suffixed so they sort next to
            # it and can simply be deleted.
            suffix = "" if n == 0 else f"_alt{n}"
            # MIXED in the name so an impure cluster cannot be silently renamed
            # to one person — it holds at least two.
            warn = "_MIXED" if impure else ""
            fn = f"{rank:04d}_c{cid}_{photos}photos{warn}{suffix}.jpg"
            img.save(out / fn, quality=92)
            manifest[fn] = {
                "cluster": cid,
                "faces": len(members),
                "photos": photos,
                "score": round(score, 3),
                "dup_rate": round(dup_rate, 4),
                "impure": impure,
                "is_alternate": n > 0,
                "source_photo": m["photo"],
                "source_drive_id": m["drive_id"],
                "member_photos": sorted({meta[j]["photo"] for j in members}),
            }
            written += 1

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"\nWrote {written} crops to {out}/")
    if missing:
        print(f"  ({missing} cluster(s) had no usable cached thumbnail)")
    primaries = {m["cluster"]: m for m in manifest.values() if not m["is_alternate"]}
    covered_faces = sum(m["faces"] for m in primaries.values())
    covered_photos = len({p for m in primaries.values() for p in m["member_photos"]})
    total_photos = len({r["path"] for r in records})
    print(f"  these {len(primaries)} clusters cover {covered_faces:,} faces "
          f"across {covered_photos:,}/{total_photos:,} photos "
          f"({covered_photos/total_photos:.0%})")
    print(f"\nNext: rename each file to the person's name (e.g. 'Mahima Singh.jpg'),")
    print(f"delete any you don't want, then run apply_cluster_names.py")


if __name__ == "__main__":
    main()
