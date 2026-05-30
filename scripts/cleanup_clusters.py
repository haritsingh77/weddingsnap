"""
WeddingSnap Face Cluster Cleanup
=================================
Audits all face clusters produced by the preprocessor:

  Phase 1 — Bad cluster detection
    • Loads every cluster's representative face crop (local L1 → Supabase fallback)
    • Re-runs InsightFace detection on the 150×150 crop
    • Flags clusters where no face is found → saves crops to cluster_review/no_face/

  Phase 2 — Near-duplicate cluster merging
    • Computes the mean (centroid) embedding per cluster
    • Finds pairs whose cosine similarity ≥ --merge-threshold (default 0.70)
    • Auto-merges them by updating cluster_merges.json (both local + Supabase)
    • Borderline pairs (similarity ≥ --suggest-threshold, default 0.60) saved as
      side-by-side images in cluster_review/merge_candidates/

Usage:
    # Dry run first — no files written, no merges applied
    python scripts/cleanup_clusters.py --dry-run

    # Full run with defaults
    python scripts/cleanup_clusters.py

    # Strict merge threshold
    python scripts/cleanup_clusters.py --merge-threshold 0.75

    # Custom output folder
    python scripts/cleanup_clusters.py --output-dir D:/cluster_review
"""

import sys
import io
import json
import time
import pickle
import logging
import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

# ── Project paths ──────────────────────────────────────────────────────────────
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "backend"))

from app.config import settings
from app.services.drive_cache import (
    LOCAL_CACHE_DIR,
    get_cached_file,
    get_cached_json,
    save_cached_json,
    save_cached_file,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_pkl() -> list[dict]:
    """
    Load face_encodings.pkl.

    Priority:
      1. Local path (settings.ENCODINGS_CACHE_PATH) — the pkl is 72 MB and skips
         Supabase upload, so it only lives locally.
      2. Supabase via get_cached_file() — future-proof if the file ever fits.
    """
    local_pkl = Path(settings.ENCODINGS_CACHE_PATH)
    if local_pkl.exists():
        log.info("Loading face_encodings.pkl from local path: %s", local_pkl)
        with open(local_pkl, "rb") as f:
            data = pickle.load(f)
        log.info("Loaded %s photo records from local pkl", f"{len(data):,}")
        return data

    log.info("Local pkl not found — trying Supabase…")
    raw = get_cached_file("face_encodings.pkl")
    if raw:
        data = pickle.loads(raw)
        log.info("Loaded %s photo records from Supabase", f"{len(data):,}")
        return data

    log.error("face_encodings.pkl not found locally or in Supabase.")
    return []


def cluster_faces_faiss(X: np.ndarray, threshold: float) -> np.ndarray:
    """
    FAISS ANN + Union-Find clustering (same algorithm as faces.py).
    threshold = cosine DISTANCE (not similarity).
    Falls back to DBSCAN if faiss is not installed.
    """
    n, d = X.shape
    if n == 0:
        return np.array([], dtype=int)

    try:
        import faiss

        norms = np.linalg.norm(X, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        X_norm = (X / norms).astype(np.float32)

        ip_threshold = float(1.0 - threshold)   # cosine similarity lower bound
        k = min(50, n)

        index = faiss.IndexFlatIP(d)
        index.add(X_norm)
        D, I = index.search(X_norm, k)

        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[rx] = ry

        for i in range(n):
            for j_pos in range(1, k):
                j = int(I[i][j_pos])
                if j == -1:
                    break
                if D[i][j_pos] >= ip_threshold:
                    union(i, j)

        root_to_label: dict[int, int] = {}
        labels = np.full(n, -1, dtype=int)
        next_lbl = 0
        for i in range(n):
            r = find(i)
            if r not in root_to_label:
                root_to_label[r] = next_lbl
                next_lbl += 1
            labels[i] = root_to_label[r]

        return labels

    except ImportError:
        log.warning("faiss not available — falling back to DBSCAN (slower)")
        from sklearn.cluster import DBSCAN
        from sklearn.decomposition import PCA
        X_f = X.astype(np.float32)
        if n > 5000 and d > 64:
            n_components = min(64, d, n - 1)
            X_f = PCA(n_components=n_components).fit_transform(X_f)
        db = DBSCAN(eps=threshold, min_samples=2, algorithm="ball_tree", n_jobs=-1)
        return db.fit_predict(X_f)


def build_clusters(all_records: list[dict], threshold: float) -> dict:
    """
    Replicate the clustering logic from faces.py → get_face_clusters().
    Returns {label_str: {representative, photos, members, embeddings}}
    """
    X = []
    origins = []
    for record in all_records:
        encs   = record.get("encodings", [])
        locs   = record.get("locations", [])
        frames = record.get("frame_indices", [None] * len(encs))
        for enc, loc, frame in zip(encs, locs, frames):
            X.append(enc)
            origins.append({
                "path":      record["path"],
                "location":  loc,
                "frame_idx": frame,
                "is_video":  record["path"].lower().endswith(
                    (".mp4", ".mov", ".avi", ".mkv", ".webm")
                ),
            })

    if not X:
        return {}

    X_arr = np.array(X, dtype=np.float32)
    log.info("Clustering %d face vectors (threshold=%.3f)…", len(X_arr), threshold)
    labels = cluster_faces_faiss(X_arr, threshold)
    log.info("Clustering done — %d unique clusters", len(set(labels)))

    raw: dict = {}
    for idx, label in enumerate(labels):
        if label == -1:
            continue
        lbl = str(label)
        origin = origins[idx]
        emb    = X_arr[idx]
        if lbl not in raw:
            raw[lbl] = {"members": [], "photos": set(), "embeddings": []}
        raw[lbl]["members"].append(origin)
        raw[lbl]["photos"].add(origin["path"])
        raw[lbl]["embeddings"].append(emb)

    clusters = {}
    for lbl, data in raw.items():
        rep = next((m for m in data["members"] if not m["is_video"]), data["members"][0])
        clusters[lbl] = {
            "representative": rep,
            "photos":         sorted(data["photos"]),
            "count":          len(data["photos"]),
            "members":        data["members"],
            "embeddings":     data["embeddings"],   # list of np arrays
        }

    return dict(sorted(clusters.items(), key=lambda x: x[1]["count"], reverse=True))


def get_filename_map() -> dict:
    """
    Filename → Drive ID map.  Checks local cache JSON first, then Supabase.
    Falls back to building from Drive if neither is available.
    """
    raw = get_cached_file("drive_filename_map.json")
    if raw:
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            pass
    log.warning("drive_filename_map.json not found — falling back to Drive API")
    try:
        from app.services.drive_service import build_filename_to_id_map
        return build_filename_to_id_map()
    except Exception as e:
        log.error("Could not build filename map: %s", e)
        return {}


def get_face_crop_bytes(rep: dict, filename_map: dict) -> bytes | None:
    """
    Fetch / generate the 150×150 face crop for a representative member.

    Priority chain (mirrors get_face_crop_bytes in faces.py):
      1. Local L1 cache  (face_cluster_{drive_id}_*.jpg)
      2. Supabase Storage (same key)
      3. Generate from 400px thumbnail  (local L1 → Supabase)
      4. Generate from full Drive download (last resort)
    """
    import cv2
    from app.services.drive_service import download_file_to_memory, download_file_from_drive

    path_str  = rep["path"]
    location  = rep["location"]   # [top, right, bottom, left]
    is_video  = rep["is_video"]
    frame_idx = rep.get("frame_idx")

    filename = Path(path_str).name
    drive_id = filename_map.get(filename, "")
    if not drive_id:
        log.debug("No Drive ID for %s — skipping crop", filename)
        return None

    # ── 1 & 2: Stable cache key — try local then Supabase ─────────────────
    cache_key = (
        f"face_cluster_{drive_id}_"
        f"{location[0]}_{location[1]}_{location[2]}_{location[3]}.jpg"
    )
    cached = get_cached_file(cache_key)   # checks local L1 first, then Supabase
    if cached:
        return cached

    # ── 3: Generate from 400px thumbnail (local L1 → Supabase) ───────────
    img = None
    thumb_key = f"thumb_{drive_id}_400.jpg"
    thumb_data = get_cached_file(thumb_key)   # local L1 → Supabase
    if thumb_data:
        try:
            img = Image.open(io.BytesIO(thumb_data)).convert("RGB")
            w, h = img.size
            scale = max(w, h) / 1200.0
            top, right, bottom, left = [int(c * scale) for c in location]
        except Exception as e:
            log.debug("Thumbnail crop failed for %s: %s", filename, e)
            img = None

    # ── 4: Full Drive download fallback ───────────────────────────────────
    if img is None:
        try:
            if is_video:
                tmp = LOCAL_CACHE_DIR / f"cleanup_tmp_{drive_id}.tmp"
                ok = download_file_from_drive(drive_id, tmp)
                if not ok:
                    return None
                cap = cv2.VideoCapture(str(tmp))
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx or 0)
                ret, frame = cap.read()
                cap.release()
                try:
                    tmp.unlink()
                except Exception:
                    pass
                if not ret:
                    return None
                img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            else:
                data = download_file_to_memory(drive_id)
                if not data:
                    return None
                img = Image.open(io.BytesIO(data)).convert("RGB")
                img = ImageOps.exif_transpose(img)

            w, h = img.size
            if max(w, h) > 1200:
                scale = 1200 / max(w, h)
                img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)

            top, right, bottom, left = location

        except Exception as e:
            log.warning("Full-download fallback failed for %s: %s", filename, e)
            return None

    # ── Crop + resize ──────────────────────────────────────────────────────
    try:
        w, h = img.size
        fh = bottom - top
        fw = right - left
        pad_y = int(fh * 0.45)
        pad_x = int(fw * 0.45)
        cropped = img.crop((
            max(0, left - pad_x),
            max(0, top  - pad_y),
            min(w, right + pad_x),
            min(h, bottom + pad_y),
        ))
        cropped = cropped.resize((150, 150), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        log.warning("Crop failed for %s: %s", filename, e)
        return None


def detect_face_in_crop(crop_bytes: bytes, insight_app) -> tuple[bool, float]:
    """
    Run InsightFace detection on a 150×150 JPEG crop.
    Returns (face_found: bool, best_det_score: float).
    """
    try:
        img = Image.open(io.BytesIO(crop_bytes)).convert("RGB")
        # Upscale to help small-crop detection
        img = img.resize((300, 300), Image.Resampling.LANCZOS)
        arr = np.array(img)
        faces = insight_app.get(arr)
        if not faces:
            return False, 0.0
        best_score = float(max(f.det_score for f in faces))
        return True, best_score
    except Exception as e:
        log.debug("InsightFace detection failed: %s", e)
        return False, 0.0


def load_insight_app():
    """Load InsightFace FaceAnalysis app (reuse GPU model already on disk)."""
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(
        name="buffalo_s",
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    app.prepare(ctx_id=0, det_size=(320, 320))
    return app


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    a = a / (np.linalg.norm(a) + 1e-8)
    b = b / (np.linalg.norm(b) + 1e-8)
    return float(np.dot(a, b))


def save_image(path: Path, img_bytes: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(img_bytes)


def make_side_by_side(bytes_a: bytes, bytes_b: bytes, label_a: str, label_b: str) -> bytes:
    """Create a 320×160 side-by-side JPEG of two 150×150 crops."""
    from PIL import ImageDraw, ImageFont
    a = Image.open(io.BytesIO(bytes_a)).convert("RGB").resize((150, 150))
    b = Image.open(io.BytesIO(bytes_b)).convert("RGB").resize((150, 150))
    canvas = Image.new("RGB", (320, 180), (30, 30, 30))
    canvas.paste(a, (5, 5))
    canvas.paste(b, (165, 5))
    draw = ImageDraw.Draw(canvas)
    draw.text((5,  158), f"#{label_a}", fill=(200, 200, 200))
    draw.text((165, 158), f"#{label_b}", fill=(200, 200, 200))
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WeddingSnap Face Cluster Cleanup")
    parser.add_argument("--dry-run",           action="store_true",
                        help="Report only — write no files, apply no merges")
    parser.add_argument("--apply-deletions",   action="store_true",
                        help="Permanently delete face encodings for clusters whose representative crops remain in the no_face review folder")
    parser.add_argument("--merge-threshold",   type=float, default=0.70,
                        help="Cosine similarity for auto-merge (default 0.70)")
    parser.add_argument("--suggest-threshold", type=float, default=0.60,
                        help="Cosine similarity for suggested merge (default 0.60)")
    parser.add_argument("--min-cluster-size",  type=int,   default=1,
                        help="Skip clusters with fewer photos than this (default 1)")
    parser.add_argument("--output-dir",        type=str,
                        default=str(project_root / "cluster_review"),
                        help="Root folder for review images and report")
    parser.add_argument("--det-score",         type=float, default=0.40,
                        help="InsightFace detection score threshold (default 0.40)")
    args = parser.parse_args()

    out_dir          = Path(args.output_dir)
    no_face_dir      = out_dir / "no_face"
    low_conf_dir     = out_dir / "low_conf"
    merge_cands_dir  = out_dir / "merge_candidates"
    report_path      = out_dir / "report.json"

    if not args.dry_run:
        for d in [no_face_dir, low_conf_dir, merge_cands_dir]:
            d.mkdir(parents=True, exist_ok=True)

    # ── Load data ──────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("WeddingSnap Face Cluster Cleanup")
    log.info("  dry-run:          %s", args.dry_run)
    log.info("  merge-threshold:  %.2f", args.merge_threshold)
    log.info("  suggest-threshold:%.2f", args.suggest_threshold)
    log.info("  output-dir:       %s", out_dir)
    log.info("=" * 60)

    all_records = load_pkl()
    if not all_records:
        log.error("No records loaded — aborting.")
        return

    filename_map = get_filename_map()
    log.info("Filename map: %d entries", len(filename_map))

    # Determine clustering threshold from backend
    try:
        from scripts.face_engine.matching import detect_backend_from_records, default_tolerance
        backend = detect_backend_from_records(all_records)
        cluster_threshold = default_tolerance(backend)
    except Exception:
        backend = "insightface"
        cluster_threshold = 0.40
    log.info("Backend: %s | cluster threshold: %.3f", backend, cluster_threshold)

    clusters = build_clusters(all_records, cluster_threshold)
    total_clusters = len(clusters)
    log.info("Total clusters: %d", total_clusters)

    if args.apply_deletions:
        log.info("=" * 60)
        log.info("Applying cluster deletions based on remaining files in no_face/...")
        log.info("=" * 60)

        # 1. Scan remaining files in no_face/ review folder
        no_face_path = out_dir / "no_face"
        if not no_face_path.exists():
            log.error("no_face review directory does not exist: %s", no_face_path)
            return

        remaining_files = list(no_face_path.glob("cluster_*.jpg"))
        log.info("Found %d remaining files in %s", len(remaining_files), no_face_path)

        deleted_cluster_ids = set()
        for f in remaining_files:
            filename = f.name
            parts = filename[len("cluster_"):].rsplit("_n", 1)
            if len(parts) == 2:
                cid = parts[0]
                deleted_cluster_ids.add(cid)

        if not deleted_cluster_ids:
            log.info("No cluster IDs found for deletion. Have you reviewed and kept the folder as-is?")
            return

        log.info("Parsed %d unique cluster IDs for permanent deletion", len(deleted_cluster_ids))

        X = []
        # We need to trace the exact global index to record reference and list index inside that record
        face_mappings = [] # list of (record_dict, face_idx_in_record)
        for r_idx, record in enumerate(all_records):
            encs = record.get("encodings", [])
            for f_idx in range(len(encs)):
                X.append(encs[f_idx])
                face_mappings.append((r_idx, f_idx))

        if not X:
            log.warning("No face encodings found in face_encodings.pkl.")
            return

        X_arr = np.array(X, dtype=np.float32)
        log.info("Clustering %d face vectors to perform deletion mapping...", len(X_arr))
        labels = cluster_faces_faiss(X_arr, cluster_threshold)

        # Map global face indexes to delete
        deleted_count = 0
        records_to_delete_faces = {} # r_idx -> list of f_idx to delete

        for global_idx, label in enumerate(labels):
            if label == -1:
                continue
            cid = str(label)
            if cid in deleted_cluster_ids:
                r_idx, f_idx = face_mappings[global_idx]
                if r_idx not in records_to_delete_faces:
                    records_to_delete_faces[r_idx] = []
                records_to_delete_faces[r_idx].append(f_idx)
                deleted_count += 1

        if deleted_count == 0:
            log.info("No face detections match the deleted cluster IDs. Nothing to delete.")
            return

        log.info("Flagged %d face detections for deletion from %d image/video records",
                 deleted_count, len(records_to_delete_faces))

        # 3. Perform the actual deletion from all_records in-place (sorted descending)
        for r_idx, f_indexes in records_to_delete_faces.items():
            record = all_records[r_idx]
            for f_idx in sorted(f_indexes, reverse=True):
                record["encodings"].pop(f_idx)
                record["locations"].pop(f_idx)
                if "frame_indices" in record:
                    record["frame_indices"].pop(f_idx)

        # 4. Save a backup of the original face_encodings.pkl first
        local_pkl = Path(settings.ENCODINGS_CACHE_PATH)
        backup_pkl = local_pkl.with_suffix(".pkl.bak")
        try:
            import shutil
            if local_pkl.exists():
                shutil.copy2(local_pkl, backup_pkl)
                log.info("Created backup of local face_encodings.pkl at: %s", backup_pkl)
        except Exception as backup_err:
            log.warning("Failed to create backup: %s", backup_err)

        # 5. Save the updated face_encodings.pkl and upload to Supabase
        try:
            with open(local_pkl, "wb") as f:
                pickle.dump(all_records, f)
            log.info("Saved updated face_encodings.pkl locally.")
            
            save_cached_file("face_encodings.pkl", pickle.dumps(all_records), mime_type="application/octet-stream")
            log.info("Uploaded updated face_encodings.pkl to Supabase.")
            
            # Clear face service loading caches
            try:
                from app.services.face_service import load_encodings
                load_encodings.cache_clear()
                log.info("Cleared backend face encodings loading cache.")
            except Exception:
                pass
                
        except Exception as save_err:
            log.error("Failed to save/upload updated encodings: %s", save_err)
            return

        # 6. Delete the no_face review directory as it is no longer needed
        try:
            import shutil
            shutil.rmtree(no_face_path)
            log.info("Deleted the review folder: %s", no_face_path)
        except Exception as rm_err:
            log.warning("Failed to delete review folder: %s", rm_err)

        log.info("Deletion complete! Successfully cleaned up %d bad face detections from the database.", deleted_count)
        return

    # ── Load InsightFace ───────────────────────────────────────────────────
    log.info("Loading InsightFace model…")
    insight_app = load_insight_app()
    log.info("InsightFace ready.")

    # ── Phase 1: Bad cluster detection ────────────────────────────────────
    log.info("\n── Phase 1: Re-detecting faces on representative crops ──")

    stats = {
        "good":     [],
        "no_face":  [],
        "low_conf": [],
        "no_crop":  [],   # could not fetch/generate crop at all
    }
    good_centroids = {}   # cluster_id → normalized centroid embedding

    for idx, (cid, cdata) in enumerate(clusters.items()):
        if cdata["count"] < args.min_cluster_size:
            continue

        if idx % 100 == 0:
            log.info("  Progress: %d / %d clusters", idx, total_clusters)

        rep        = cdata["representative"]
        crop_bytes = get_face_crop_bytes(rep, filename_map)

        if crop_bytes is None:
            log.debug("  Cluster %s — no crop available", cid)
            stats["no_crop"].append(cid)
            continue

        found, score = detect_face_in_crop(crop_bytes, insight_app)

        if not found or score < args.det_score:
            label = "no_face" if not found else "low_conf"
            stats[label].append({"id": cid, "score": score, "count": cdata["count"]})
            log.debug("  Cluster %s — %s (score=%.2f, photos=%d)",
                      cid, label, score, cdata["count"])
            if not args.dry_run:
                dest_dir = no_face_dir if label == "no_face" else low_conf_dir
                save_image(
                    dest_dir / f"cluster_{cid}_n{cdata['count']}.jpg",
                    crop_bytes
                )
        else:
            stats["good"].append(cid)
            # Compute normalized centroid
            embs = np.array(cdata["embeddings"], dtype=np.float32)
            centroid = embs.mean(axis=0)
            norm = np.linalg.norm(centroid)
            if norm > 0:
                centroid /= norm
            good_centroids[cid] = centroid

    log.info("\nPhase 1 results:")
    log.info("  Good (face confirmed): %d", len(stats["good"]))
    log.info("  No face detected:      %d", len(stats["no_face"]))
    log.info("  Low confidence:        %d", len(stats["low_conf"]))
    log.info("  No crop available:     %d", len(stats["no_crop"]))

    # ── Phase 2: Near-duplicate cluster merging ────────────────────────────
    log.info("\n-- Phase 2: Finding near-duplicate clusters --")

    good_ids   = list(good_centroids.keys())
    centroids  = np.array([good_centroids[cid] for cid in good_ids], dtype=np.float32)

    auto_merges    = []   # list of (target_id, source_id, similarity)
    suggest_merges = []

    if len(centroids) > 1:
        # Brute-force cosine similarity matrix — only hundreds of cluster
        # centroids so numpy is fast enough without FAISS here.
        # centroids are already unit-normalized.
        sim_matrix = centroids @ centroids.T   # shape (m, m)

        seen_pairs: set[tuple] = set()
        m = len(good_ids)

        for i in range(m):
            for j in range(i + 1, m):
                sim = float(sim_matrix[i, j])
                if sim <= args.suggest_threshold:
                    continue

                cid_a = good_ids[i]
                cid_b = good_ids[j]
                pair  = tuple(sorted([cid_a, cid_b]))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                # Larger cluster absorbs smaller
                if clusters[cid_a]["count"] >= clusters[cid_b]["count"]:
                    target, source = cid_a, cid_b
                else:
                    target, source = cid_b, cid_a

                if sim >= args.merge_threshold:
                    auto_merges.append({
                        "target": target,
                        "source": source,
                        "similarity": round(sim, 4),
                        "target_photos": clusters[target]["count"],
                        "source_photos": clusters[source]["count"],
                    })
                else:
                    suggest_merges.append({
                        "target": target,
                        "source": source,
                        "similarity": round(sim, 4),
                        "target_photos": clusters[target]["count"],
                        "source_photos": clusters[source]["count"],
                    })

    log.info("  Auto-merges (sim >= %.2f):    %d pairs", args.merge_threshold,  len(auto_merges))
    log.info("  Suggested merges (sim >= %.2f): %d pairs", args.suggest_threshold, len(suggest_merges))

    # ── Save side-by-side images for suggested merges ──────────────────────
    if not args.dry_run and suggest_merges:
        log.info("Saving side-by-side images for %d suggested merge pairs…",
                 len(suggest_merges))
        saved = 0
        for m in suggest_merges:
            ta, tb = m["target"], m["source"]
            crop_a = get_face_crop_bytes(clusters[ta]["representative"], filename_map)
            crop_b = get_face_crop_bytes(clusters[tb]["representative"], filename_map)
            if crop_a and crop_b:
                img_bytes = make_side_by_side(crop_a, crop_b,
                                               f"{ta}_n{clusters[ta]['count']}",
                                               f"{tb}_n{clusters[tb]['count']}")
                save_image(
                    merge_cands_dir / f"sim{m['similarity']:.3f}__{ta}__{tb}.jpg",
                    img_bytes
                )
                saved += 1
        log.info("  Saved %d side-by-side images", saved)

    # ── Apply auto-merges to cluster_merges.json ───────────────────────────
    if auto_merges and not args.dry_run:
        log.info("Applying %d auto-merges to cluster_merges.json…", len(auto_merges))

        # Load existing merges from local L1 → Supabase
        merges_data: dict = get_cached_json("cluster_merges.json") or {}

        for m in auto_merges:
            target, source = m["target"], m["source"]
            existing = merges_data.get(target, [])
            if source not in existing:
                existing.append(source)
            merges_data[target] = existing

        # Save back to both local L1 and Supabase
        save_cached_json("cluster_merges.json", merges_data)
        log.info("  cluster_merges.json updated (local + Supabase)")
    elif auto_merges and args.dry_run:
        log.info("[DRY RUN] Would apply %d auto-merges (skipped)", len(auto_merges))

    # ── Write report ───────────────────────────────────────────────────────
    report = {
        "generated_at":        time.strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run":             args.dry_run,
        "total_records":       len(all_records),
        "total_clusters":      total_clusters,
        "backend":             backend,
        "cluster_threshold":   cluster_threshold,
        "merge_threshold":     args.merge_threshold,
        "suggest_threshold":   args.suggest_threshold,
        "phase1": {
            "good":            len(stats["good"]),
            "no_face":         len(stats["no_face"]),
            "low_conf":        len(stats["low_conf"]),
            "no_crop":         len(stats["no_crop"]),
            "no_face_details": stats["no_face"],
            "low_conf_details":stats["low_conf"],
        },
        "phase2": {
            "auto_merges":     auto_merges,
            "suggested_merges":suggest_merges,
        },
    }

    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log.info("\nReport written to: %s", report_path)

    log.info("\n%s", "=" * 60)
    log.info("Done.")
    log.info("  Total clusters:        %d", total_clusters)
    log.info("  Good:                  %d", len(stats["good"]))
    log.info("  No-face (review):      %d  -> %s",
             len(stats["no_face"]), no_face_dir if not args.dry_run else "(dry run)")
    log.info("  Low-conf (review):     %d  -> %s",
             len(stats["low_conf"]), low_conf_dir if not args.dry_run else "(dry run)")
    log.info("  Auto-merged:           %d pairs", len(auto_merges))
    log.info("  Suggested merges:      %d pairs -> %s",
             len(suggest_merges), merge_cands_dir if not args.dry_run else "(dry run)")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
