"""
Face registration and matching routes.
"""

from __future__ import annotations

import logging
from datetime import datetime

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Response

from app.auth_deps import guest_or_admin, require_admin
from app.database import supabase
from app.services.face_service import (
    get_filename_map,
    load_encodings,
    resolve_drive_ids,
    resolve_one_drive_id,
)

log = logging.getLogger(__name__)

# Same reasoning as photos.py: auth declared once at the router so a new route
# is protected by default. Cluster rename/merge/delete and the People tab are
# admin-only on top of this — the frontend used to gate those in the browser
# from the guest's typed name, so anyone registering as "saurav" got them.
router = APIRouter(
    prefix="/faces",
    tags=["faces"],
    dependencies=[Depends(guest_or_admin)],
)


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("/register", deprecated=True)
async def register_face():
    """Gone: guests no longer scan their face.

    Identity now comes from a per-guest link, because the clustering already
    knows who is in which photo and a human named the clusters. That is more
    accurate than matching a selfie and removes the whole class of "we couldn't
    detect a face" failures.

    Recognition itself is not gone, it just runs where the GPU is. To add
    somebody new: preprocess locally, name their cluster, and give them a link.
    Keeping it off the server is what lets the deployed image drop InsightFace,
    ONNX and the 52 MB encodings file — about 1.8 GB of resident memory, and the
    difference between fitting a free host and not.
    """
    raise HTTPException(
        status_code=410,
        detail="Face scanning has been replaced by a personal link. Please use the link you were sent.",
    )


def _cluster_faces_scalable(X: np.ndarray, threshold: float, backend: str) -> np.ndarray:
    """
    Scalable face clustering using FAISS ANN + Union-Find.
    Memory: O(n * k) instead of O(n²) — works for 100k+ faces.

    For InsightFace ArcFace (512-d):
      - Normalise to unit sphere
      - Use IndexFlatIP (inner product == cosine on unit vectors)
      - Union pairs whose cosine similarity >= (1 - threshold)

    For dlib (128-d):
      - DBSCAN with ball_tree, PCA-reduced to 64-d when n > 5000
    """
    n, d = X.shape
    if n == 0:
        return np.array([], dtype=int)

    if backend == "insightface":
        try:
            import faiss  # bundled with insightface / faiss-cpu

            # Normalise embeddings to unit sphere
            norms = np.linalg.norm(X, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1.0, norms)
            X_norm = (X / norms).astype(np.float32)

            ip_threshold = float(1.0 - threshold)   # cosine sim >= this → same person
            k = min(50, n)                            # neighbours to probe

            index = faiss.IndexFlatIP(d)
            index.add(X_norm)
            D, I = index.search(X_norm, k)           # D[i][j] = cosine similarity

            # Union-Find ---------------------------------------------------
            parent = list(range(n))

            def find(x: int) -> int:
                while parent[x] != x:
                    parent[x] = parent[parent[x]]   # path compression
                    x = parent[x]
                return x

            def union(x: int, y: int) -> None:
                rx, ry = find(x), find(y)
                if rx != ry:
                    parent[rx] = ry

            for i in range(n):
                for j_pos in range(1, k):            # skip self at j_pos=0
                    j = int(I[i][j_pos])
                    if j == -1:
                        break
                    if D[i][j_pos] >= ip_threshold:
                        union(i, j)

            # Assign contiguous labels
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
            log.warning("faiss not available — falling back to DBSCAN for InsightFace.")
            # Fall through to DBSCAN path

    # dlib / fallback: DBSCAN with dimensionality reduction when large
    from sklearn.decomposition import PCA
    from sklearn.cluster import DBSCAN

    X_f = X.astype(np.float32)
    if n > 5000 and d > 64:
        n_components = min(64, d, n - 1)
        X_f = PCA(n_components=n_components).fit_transform(X_f)

    db = DBSCAN(eps=threshold, min_samples=2, algorithm="ball_tree", n_jobs=-1)
    return db.fit_predict(X_f)


_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")
_db_clusters_cache: dict | None = None


def _clusters_from_db() -> dict | None:
    """Build the cluster map from the faces table.

    sync_encodings_to_db.py already clustered everything and stored the answer
    in faces.cluster_id. Re-deriving it here meant downloading the 52 MB pkl and
    re-clustering 27,377 faces on every cold request — about 11 seconds, and the
    reason the People tab felt slow. It also forced numpy, faiss and
    scikit-learn into the deployed image for work that was already done.

    Returns None when the table is empty, so a machine with only a pkl (i.e. the
    preprocessing box) still falls through to the in-memory path below.
    """
    global _db_clusters_cache
    if _db_clusters_cache is not None:
        return _db_clusters_cache

    from scripts.face_engine.matching import drive_record_path

    rows, offset = [], 0
    while True:
        page = (
            supabase.table("faces")
            .select("filename, drive_id, bbox, frame_idx, cluster_id")
            .not_.is_("cluster_id", "null")
            .range(offset, offset + 999)
            .execute()
        ).data or []
        rows.extend(page)
        if len(page) < 1000:
            break
        offset += 1000

    if not rows:
        return None

    # clusters.name is the source of truth now. cluster_names.json is keyed by
    # the pkl's positional labels, which have nothing to do with these ids —
    # looking names up there produced "Person #N" for all 69 named people.
    names: dict[str, str] = {}
    try:
        for c in (supabase.table("clusters").select("id, name").execute()).data or []:
            if c.get("name"):
                names[str(c["id"])] = c["name"]
    except Exception as e:
        log.warning("Could not read cluster names: %s", e)

    clusters: dict = {}
    for r in rows:
        cid = str(r["cluster_id"])
        name = r.get("filename") or ""
        path = drive_record_path(r["drive_id"], name) if r.get("drive_id") else name
        member = {
            "path": path,
            "location": tuple(r["bbox"]) if r.get("bbox") else None,
            "frame_idx": r.get("frame_idx"),
            "is_video": name.lower().endswith(_VIDEO_EXTS),
        }
        entry = clusters.setdefault(cid, {"members": [], "photos": set()})
        entry["members"].append(member)
        entry["photos"].add(path)

    result = {}
    for cid, data in clusters.items():
        # Prefer a still for the thumbnail — a video frame is usually motion-blurred
        rep = next((m for m in data["members"] if not m["is_video"]), data["members"][0])
        result[cid] = {
            "representative": rep,
            "photos": sorted(data["photos"]),
            "count": len(data["photos"]),
            "members": data["members"],
            "db_name": names.get(cid),
        }

    result = dict(sorted(result.items(), key=lambda kv: kv[1]["count"], reverse=True))
    log.info("Clusters from DB: %d people, %d faces", len(result), len(rows))
    _db_clusters_cache = result
    return result


def get_face_clusters() -> dict:
    db = _clusters_from_db()
    if db is not None:
        return db
    return _get_face_clusters_from_pkl()


def _get_face_clusters_from_pkl() -> dict:
    """
    Cluster all precomputed face encodings in face_encodings.pkl.
    Uses FAISS-based nearest-neighbour graph + Union-Find — O(n*k) memory,
    works for 100k+ faces without OOM.
    """
    global _cached_clusters, _cached_clusters_key

    from app.services.drive_cache import LOCAL_CACHE_DIR
    import os

    local_pkl = LOCAL_CACHE_DIR / "face_encodings.pkl"
    mtime = 0
    size = 0
    if local_pkl.exists():
        try:
            mtime = os.path.getmtime(local_pkl)
            size  = os.path.getsize(local_pkl)
        except Exception:
            pass

    if _cached_clusters is not None and _cached_clusters_key == (mtime, size):
        return _cached_clusters

    # Only clear LRU cache when the pkl file has actually changed on disk
    if _cached_clusters_key != (mtime, size):
        load_encodings.cache_clear()

    try:
        all_records = load_encodings()
    except Exception as e:
        log.warning(f"Could not load encodings for clustering: {e}")
        return {}

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
                "is_video":  record["path"].lower().endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")),
            })

    if not X:
        return {}

    X_arr = np.array(X)

    backend = "dlib"
    cluster_threshold = settings.FACE_MATCH_TOLERANCE
    try:
        from scripts.face_engine.matching import detect_backend_from_records, cluster_tolerance
        backend = detect_backend_from_records(all_records)
        cluster_threshold = cluster_tolerance(backend)
    except Exception:
        pass

    log.info("Clustering %d face vectors (backend=%s, threshold=%.3f) ...", len(X_arr), backend, cluster_threshold)
    labels = _cluster_faces_scalable(X_arr, cluster_threshold, backend)
    log.info("Clustering done. Unique clusters: %d", len(set(labels)))

    clusters: dict = {}
    for idx, label in enumerate(labels):
        if label == -1:
            continue   # noise point
        label_str = str(label)
        origin = origins[idx]
        if label_str not in clusters:
            clusters[label_str] = {"members": [], "photos": set()}
        clusters[label_str]["members"].append(origin)
        clusters[label_str]["photos"].add(origin["path"])

    # Drop one-off faces before they become browsable "people". At 30k+ faces a
    # wedding crowd yields thousands of clusters, most of them a stranger caught
    # once in a background — they swamp the People tab and hide the real guests.
    # A genuine attendee appears in several photos, so require a floor.
    import os
    min_photos = int(os.getenv("MIN_CLUSTER_PHOTOS", "3"))
    total_raw = len(clusters)
    clusters = {
        lbl: data for lbl, data in clusters.items()
        if len(data["photos"]) >= min_photos
    }
    log.info(
        "Clusters: %d raw → %d shown (min %d photos each)",
        total_raw, len(clusters), min_photos,
    )

    result: dict = {}
    for label_str, data in clusters.items():
        # Prefer a photo (not video) as the representative thumbnail
        rep = next((m for m in data["members"] if not m["is_video"]), data["members"][0])
        result[label_str] = {
            "representative": rep,
            "photos":  sorted(data["photos"]),
            "count":   len(data["photos"]),
            "members": data["members"],
        }

    # Sort by photo count descending
    sorted_result = dict(sorted(result.items(), key=lambda x: x[1]["count"], reverse=True))

    _cached_clusters     = sorted_result
    _cached_clusters_key = (mtime, size)
    return sorted_result


from pydantic import BaseModel


class RenameClusterRequest(BaseModel):
    name: str


class SetProfilePicRequest(BaseModel):
    drive_id: str


@router.get("/clusters", dependencies=[Depends(require_admin)])
def get_clusters():
    """Get list of recognized face clusters with counts, names, and thumbnail links."""
    from app.services.drive_cache import get_cached_json
    from app.database import supabase
    from app.config import settings

    # 1. Fetch registered guests with photo counts
    guests_list = []
    registered_names = set()
    try:
        guests_res = supabase.table("guests").select("*").order("name").execute()
        guests = guests_res.data or []
        
        # Paged: PostgREST caps a single select at 1000 rows and does so
        # silently. With 23,813 guest_photos rows that counted only the first
        # 1000, so nearly every guest showed 0 photos in the People tab.
        counts: dict[str, int] = {}
        offset = 0
        while True:
            page = (
                supabase.table("guest_photos")
                .select("guest_id")
                .range(offset, offset + 999)
                .execute()
            ).data or []
            for row in page:
                counts[row["guest_id"]] = counts.get(row["guest_id"], 0) + 1
            if len(page) < 1000:
                break
            offset += 1000

        for guest in guests:
            guest_id = guest["id"]
            cnt = counts.get(guest_id, 0)
            guests_list.append({
                "id": f"guest_{guest_id}",
                "name": guest["name"],
                "count": cnt,
                "thumbnail_url": f"/faces/guests/{guest_id}/selfie",
                "is_guest": True
            })
            registered_names.add(guest["name"].strip().lower())
    except Exception as e:
        log.error(f"Error fetching guests for clusters tab: {e}")

    # 2. Fetch raw face clusters
    clusters = get_face_clusters()
    names_data = get_cached_json("cluster_names.json") or {}
    merges_data = get_cached_json("cluster_merges.json") or {}
    mapping = get_filename_map()

    # Apply cluster merges: combine photos from source clusters into target
    absorbed_ids = set()
    for target_id, source_ids in merges_data.items():
        if target_id not in clusters:
            continue
        for src_id in source_ids:
            if src_id in clusters and src_id != target_id:
                # Merge photos
                merged_photos = set(clusters[target_id]["photos"]) | set(clusters[src_id]["photos"])
                clusters[target_id]["photos"] = sorted(list(merged_photos))
                clusters[target_id]["count"] = len(merged_photos)
                absorbed_ids.add(src_id)

    reps_data = get_cached_json("cluster_representatives.json") or {}
    ui_clusters = []
    for cid, cdata in clusters.items():
        if cid in absorbed_ids:
            continue  # skip clusters that were merged into another

        if cid in reps_data:
            rep = reps_data[cid]
        else:
            rep = cdata["representative"]

        drive_id = resolve_one_drive_id(rep["path"])
        loc = rep["location"]
        rep_key = f"{drive_id}_{loc[0]}_{loc[1]}_{loc[2]}_{loc[3]}"

        name = (
            cdata.get("db_name")
            or names_data.get(rep_key)
            or names_data.get(cid, f"Person #{cid}")
        )

        # Skip this cluster if it's already named after a registered guest
        if name.strip().lower() in registered_names:
            continue

        is_merged = cid in merges_data
        merged_source_ids = merges_data.get(cid, [])

        ui_clusters.append(
            {
                "id": cid,
                "name": name,
                "count": cdata["count"],
                "thumbnail_url": f"/faces/clusters/{cid}/thumbnail",
                "is_guest": False,
                "is_merged": is_merged,
                "merged_sources": merged_source_ids,
            }
        )

    # Sort raw clusters by count descending, slice to top 100 to reduce UI noise, and append after guests
    ui_clusters = sorted(ui_clusters, key=lambda x: x["count"], reverse=True)
    ui_clusters = ui_clusters[:100]
    return guests_list + ui_clusters


@router.post("/clusters/{cluster_id}/rename", dependencies=[Depends(require_admin)])
def rename_cluster(cluster_id: str, body: RenameClusterRequest):
    """Rename a face cluster — persisted to Google Drive cache."""
    if cluster_id.startswith("guest_"):
        guest_id = cluster_id.replace("guest_", "")
        new_name = body.name.strip()
        
        # 1. Fetch old name to sync in cluster_names.json
        old_name = ""
        try:
            old_res = supabase.table("guests").select("name").eq("id", guest_id).execute()
            if old_res.data:
                old_name = old_res.data[0]["name"]
        except Exception as old_err:
            log.error(f"Failed to fetch old guest name: {old_err}")
            
        # 2. Update database
        supabase.table("guests").update({"name": new_name}).eq("id", guest_id).execute()
        
        # 3. Synchronize occurrences inside cluster_names.json
        if old_name and old_name.strip().lower() != new_name.lower():
            try:
                from app.services.drive_cache import get_cached_json, save_cached_json
                names_data = get_cached_json("cluster_names.json") or {}
                updated_any = False
                for k, v in list(names_data.items()):
                    if v.strip().lower() == old_name.strip().lower():
                        names_data[k] = new_name
                        updated_any = True
                if updated_any:
                    save_cached_json("cluster_names.json", names_data)
            except Exception as sync_err:
                log.error(f"Failed to sync guest rename in cluster_names.json: {sync_err}")
                
        return {"success": True, "cluster_id": cluster_id, "name": new_name}

    from app.services.drive_cache import get_cached_json, save_cached_json

    clusters = get_face_clusters()
    if cluster_id not in clusters:
        raise HTTPException(status_code=404, detail="Cluster not found")

    rep = clusters[cluster_id]["representative"]
    from app.services.face_service import resolve_one_drive_id
    drive_id = resolve_one_drive_id(rep["path"])
    loc = rep["location"]
    rep_key = f"{drive_id}_{loc[0]}_{loc[1]}_{loc[2]}_{loc[3]}"

    data = get_cached_json("cluster_names.json") or {}
    new_name = body.name.strip()
    data[rep_key] = new_name
    data[cluster_id] = new_name  # for backward compatibility
    save_cached_json("cluster_names.json", data)

    # Auto-associate with any registered guests matching this name
    try:
        from app.database import supabase

        if new_name:
            guests_res = supabase.table("guests").select("id, name").execute()
            if guests_res.data:
                for guest in guests_res.data:
                    if guest["name"].strip().lower() == new_name.lower():
                        from app.services.face_service import associate_guest_by_name

                        associate_guest_by_name(guest["id"], guest["name"])
    except Exception as assoc_err:
        log.error(
            f"Failed to auto-associate newly renamed cluster with existing guest: {assoc_err}"
        )

    return {"success": True, "cluster_id": cluster_id, "name": new_name}


class MergeClusterRequest(BaseModel):
    target_id: str
    source_ids: list


@router.post("/clusters/merge", dependencies=[Depends(require_admin)])
def merge_clusters(body: MergeClusterRequest):
    """
    Merge one or more source clusters into a target cluster.
    Stored in cluster_merges.json — does not touch face_encodings.pkl.
    """
    from app.services.drive_cache import get_cached_json, save_cached_json

    if not body.source_ids:
        raise HTTPException(status_code=400, detail="source_ids must not be empty")
    if body.target_id in body.source_ids:
        raise HTTPException(status_code=400, detail="target_id must not be in source_ids")

    merges = get_cached_json("cluster_merges.json") or {}

    existing_sources = merges.get(body.target_id, [])
    new_sources = list(set(existing_sources + body.source_ids))
    new_sources = [s for s in new_sources if s != body.target_id]  # safety

    # If any source was itself a merge target, absorb its children too
    for src_id in list(body.source_ids):
        if src_id in merges:
            for sub_src in merges[src_id]:
                if sub_src not in new_sources and sub_src != body.target_id:
                    new_sources.append(sub_src)
            del merges[src_id]

    merges[body.target_id] = new_sources
    save_cached_json("cluster_merges.json", merges)
    log.info(f"Merged clusters {body.source_ids} into target {body.target_id}")
    return {"success": True, "target_id": body.target_id, "merged_sources": new_sources}


@router.delete("/clusters/{cluster_id}/unmerge", dependencies=[Depends(require_admin)])
def unmerge_cluster(cluster_id: str):
    """
    Dissolve a cluster merge — restores source clusters as independent entries.
    """
    from app.services.drive_cache import get_cached_json, save_cached_json

    merges = get_cached_json("cluster_merges.json") or {}
    if cluster_id in merges:
        del merges[cluster_id]
        save_cached_json("cluster_merges.json", merges)
        log.info(f"Unmerged cluster {cluster_id}")
    return {"success": True, "cluster_id": cluster_id}


@router.get("/clusters/{cluster_id}/photos", dependencies=[Depends(require_admin)])
def get_cluster_photos(cluster_id: str):
    """Get all photos/videos featuring the person in the specified cluster or guest album."""
    if cluster_id.startswith("guest_"):
        guest_id = cluster_id.replace("guest_", "")
        
        # Fetch guest personal photos
        # Paged for the same reason as above — a guest with more than 1000
        # photos would silently see only the first 1000 of them.
        photo_ids, offset = [], 0
        while True:
            page = (
                supabase.table("guest_photos")
                .select("photo_id")
                .eq("guest_id", guest_id)
                .range(offset, offset + 999)
                .execute()
            ).data or []
            photo_ids.extend(r["photo_id"] for r in page)
            if len(page) < 1000:
                break
            offset += 1000
        if not photo_ids:
            return []

        rows = []
        for i in range(0, len(photo_ids), 200):
            rows.extend(
                (
                    supabase.table("photos")
                    .select("id, drive_path, is_common, face_count")
                    .in_("id", photo_ids[i : i + 200])
                    .execute()
                ).data or []
            )

        class _R:
            pass
        res = _R()
        res.data = rows

        from app.routes.photos import get_drive_id_to_mime_map
        mime_map = get_drive_id_to_mime_map()

        photos_list = []
        for photo in res.data:
            drive_id = photo.get("drive_path")
            if not drive_id:
                continue

            mime_type = mime_map.get(drive_id, "image/jpeg")
            is_video = mime_type.startswith("video/")
            is_common = photo.get("is_common", False)

            photos_list.append({
                "drive_id": drive_id,
                "is_common": is_common,
                "thumb_url": f"/photos/thumb/{drive_id}",
                "stream_url": f"/photos/stream/{drive_id}",
                "is_video": is_video,
                "mime_type": mime_type,
            })
        return photos_list

    clusters = get_face_clusters()
    if cluster_id not in clusters:
        raise HTTPException(status_code=404, detail="Face cluster not found")

    paths = clusters[cluster_id]["photos"]
    resolved = []
    from app.services.face_service import resolve_one_drive_id

    for path in paths:
        filename = Path(path).name
        drive_id = resolve_one_drive_id(path)
        if drive_id:
            is_video = filename.lower().endswith(
                (".mp4", ".mov", ".avi", ".mkv", ".webm")
            )
            mime_type = "video/mp4" if is_video else "image/jpeg"

            resolved.append(
                {
                    "drive_id": drive_id,
                    "is_common": False,  # default to False for clustering
                    "thumb_url": f"/photos/thumb/{drive_id}",
                    "stream_url": f"/photos/stream/{drive_id}",
                    "is_video": is_video,
                    "mime_type": mime_type,
                }
            )
    return resolved


def get_face_crop_bytes(rep: dict) -> bytes:
    """Download, crop, and cache a face representative image."""
    from app.services.drive_cache import get_cached_file, save_cached_file, LOCAL_CACHE_DIR

    path_str = rep["path"]
    location = rep["location"]  # [top, right, bottom, left]
    is_video = rep["is_video"]
    frame_idx = rep.get("frame_idx")

    filename = Path(path_str).name
    from app.services.face_service import resolve_one_drive_id
    drive_id = resolve_one_drive_id(path_str)
    if not drive_id:
        raise HTTPException(
            status_code=404, detail="Source media file not found in Google Drive"
        )

    # ── Use stable, unique cache key based on file ID and face location ─────────
    cache_key = f"face_cluster_{drive_id}_{location[0]}_{location[1]}_{location[2]}_{location[3]}.jpg"

    # ── 1. Check Drive-backed cache ───────────────────────────────────────────
    cached_data = get_cached_file(cache_key)
    if cached_data:
        return cached_data

    # ── 2. Generate fresh thumbnail ───────────────────────────────────────────
    try:
        # Try to load size-400 thumbnail from cache first to avoid heavy download
        thumb_key = f"thumb_{drive_id}_400.jpg"
        thumb_data = get_cached_file(thumb_key)

        img = None
        if thumb_data:
            try:
                img = Image.open(io.BytesIO(thumb_data)).convert("RGB")
                # HOG detector ran on 1200px max image size. Scale coordinates dynamically.
                w, h = img.size
                scale = max(w, h) / 1200.0
                top, right, bottom, left = [int(c * scale) for c in location]
            except Exception as thumb_err:
                log.warning(f"Could not crop from size-400 thumbnail: {thumb_err}")
                img = None

        if img is None:
            # Fall back to downloading the full file from Google Drive if thumbnail is missing or crop failed
            if is_video:
                # Use local cache directory to ensure write permission on Windows
                temp_video = LOCAL_CACHE_DIR / f"temp_thumb_{drive_id}.tmp"
                temp_video.parent.mkdir(parents=True, exist_ok=True)

                success = download_file_from_drive(drive_id, temp_video)
                if not success:
                    raise Exception("Could not download video from Google Drive")

                cap = cv2.VideoCapture(str(temp_video))
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx or 0)
                ret, frame = cap.read()
                cap.release()

                if temp_video.exists():
                    try:
                        temp_video.unlink()
                    except Exception:
                        pass

                if not ret:
                    raise Exception("Could not decode video frame")

                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb_frame)
            else:
                data = download_file_to_memory(drive_id)
                if not data:
                    raise Exception("Could not download image from Google Drive")
                img = Image.open(io.BytesIO(data)).convert("RGB")

            if not is_video:
                img = ImageOps.exif_transpose(img)

            w, h = img.size
            if max(w, h) > 1200:
                scale = 1200 / max(w, h)
                img = img.resize(
                    (int(w * scale), int(h * scale)), Image.Resampling.LANCZOS
                )
                w, h = img.size

            top, right, bottom, left = location

        w, h = img.size
        fh = bottom - top
        fw = right - left
        pad_y = int(fh * 0.45)
        pad_x = int(fw * 0.45)

        cropped = img.crop(
            (
                max(0, left - pad_x),
                max(0, top - pad_y),
                min(w, right + pad_x),
                min(h, bottom + pad_y),
            )
        )
        cropped = cropped.resize((150, 150), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=90)
        result_bytes = buf.getvalue()

        # ── 3. Save to Drive cache (persistent) + L1 ─────────────────────────
        save_cached_file(cache_key, result_bytes, mime_type="image/jpeg")

        return result_bytes

    except Exception as e:
        log.error(f"Error creating thumbnail for face representative: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to generate face thumbnail: {e}"
        )


@router.get("/guests/{guest_id}/selfie")
def get_guest_selfie_public(guest_id: str):
    """Public endpoint to serve guest reference selfies without admin password for gallery views."""
    from app.services.drive_cache import get_cached_file, get_cached_json
    
    # Check if guest has a custom profile picture override
    reps_data = get_cached_json("cluster_representatives.json") or {}
    guest_key = f"guest_{guest_id}"
    if guest_key in reps_data:
        # Check custom upload override
        if reps_data[guest_key].get("is_custom_upload"):
            avatar_name = reps_data[guest_key]["avatar_path"]
            avatar_data = get_cached_file(avatar_name)
            if avatar_data:
                return Response(content=avatar_data, media_type="image/jpeg")

        try:
            img_bytes = get_face_crop_bytes(reps_data[guest_key])
            return Response(content=img_bytes, media_type="image/jpeg")
        except Exception as e:
            log.error(f"Failed to generate custom profile pic for guest {guest_id}: {e}")

    selfie_data = get_cached_file(f"selfie_{guest_id}.jpg")
    if not selfie_data:
        raise HTTPException(status_code=404, detail="Selfie not found")
    return Response(content=selfie_data, media_type="image/jpeg")


@router.get("/members/{member_id}/selfie")
def get_member_selfie_public(member_id: str):
    """Public endpoint to serve family member reference selfies without admin password for gallery views."""
    from app.services.drive_cache import get_cached_file
    selfie_data = get_cached_file(f"selfie_member_{member_id}.jpg")
    if not selfie_data:
        raise HTTPException(status_code=404, detail="Selfie not found")
    return Response(content=selfie_data, media_type="image/jpeg")



@router.get("/clusters/{cluster_id}/thumbnail")
def get_cluster_thumbnail(cluster_id: str):
    """Return a cropped square face thumbnail of the person in the cluster."""
    from app.services.drive_cache import get_cached_json, get_cached_file
    
    # Check custom representative override
    reps_data = get_cached_json("cluster_representatives.json") or {}
    if cluster_id in reps_data:
        # Check custom upload override
        if reps_data[cluster_id].get("is_custom_upload"):
            avatar_name = reps_data[cluster_id]["avatar_path"]
            avatar_data = get_cached_file(avatar_name)
            if avatar_data:
                return Response(content=avatar_data, media_type="image/jpeg")

        rep = reps_data[cluster_id]
    else:
        clusters = get_face_clusters()
        if cluster_id not in clusters:
            raise HTTPException(status_code=404, detail="Face cluster not found")
        rep = clusters[cluster_id]["representative"]
        
    img_bytes = get_face_crop_bytes(rep)
    return Response(content=img_bytes, media_type="image/jpeg")


def auto_name_cluster_for_guest(guest_name: str, selfie_bytes: bytes):
    """
    Find which face cluster matches the guest's selfie, and name that cluster in cluster_names.json.
    """
    try:
        from app.services.face_service import encode_selfie, load_encodings, get_filename_map
        import numpy as np
        from sklearn.cluster import AgglomerativeClustering
        from pathlib import Path
        from app.services.drive_cache import get_cached_json, save_cached_json
        from app.config import settings

        guest_enc = encode_selfie(selfie_bytes)
        if guest_enc is None:
            return

        all_records = load_encodings()
        X = []
        origins = []
        for record in all_records:
            encs = record.get("encodings", [])
            locs = record.get("locations", [])
            frames = record.get("frame_indices", [None] * len(encs))
            for enc, loc, frame in zip(encs, locs, frames):
                X.append(enc)
                origins.append({
                    "path": record["path"],
                    "location": loc,
                    "frame_idx": frame,
                    "is_video": record["path"].lower().endswith((".mp4", ".mov", ".avi", ".mkv", ".webm")),
                })

        if not X:
            return

        # Compare selfie encoding against all faces
        distances = np.linalg.norm(np.array(X) - guest_enc, axis=1)
        matching_indices = np.where(distances <= settings.FACE_MATCH_TOLERANCE)[0]
        if len(matching_indices) == 0:
            return

        # Run Agglomerative clustering to get the exact labels/clusters
        agg = AgglomerativeClustering(
            distance_threshold=settings.FACE_MATCH_TOLERANCE,
            n_clusters=None,
            linkage="complete",
            metric="euclidean",
        ).fit(np.array(X))
        labels = agg.labels_

        # Find the most frequent cluster label among matches
        matched_labels = [labels[idx] for idx in matching_indices if labels[idx] != -1]
        if not matched_labels:
            return

        from collections import Counter
        best_label = str(Counter(matched_labels).most_common(1)[0][0])

        # Find the representative face for this label (just like in get_face_clusters)
        label_members = []
        for idx, label in enumerate(labels):
            if str(label) == best_label:
                label_members.append(origins[idx])

        if not label_members:
            return

        rep = None
        for member in label_members:
            if not member["is_video"]:
                rep = member
                break
        if not rep:
            rep = label_members[0]

        # Get stable key for representative face
        from app.services.face_service import resolve_one_drive_id
        drive_id = resolve_one_drive_id(rep["path"])
        loc = rep["location"]
        rep_key = f"{drive_id}_{loc[0]}_{loc[1]}_{loc[2]}_{loc[3]}"

        # Save to cluster_names.json
        names_data = get_cached_json("cluster_names.json") or {}
        names_data[rep_key] = guest_name.strip()
        names_data[best_label] = guest_name.strip()  # fallback compat
        save_cached_json("cluster_names.json", names_data)
        log.info(f"Auto-named face cluster {best_label} to '{guest_name}' based on selfie matching")

    except Exception as e:
        log.warning(f"Could not auto-name face cluster: {e}")


@router.get("/guests-list", dependencies=[Depends(require_admin)])
def get_guests_list():
    """Get a simple list of guest names and IDs for manual sharing dropdown."""
    try:
        res = supabase.table("guests").select("id, name").order("name").execute()
        return res.data or []
    except Exception as e:
        log.error(f"Error getting guests list: {e}")
        return []


@router.post("/clusters/{cluster_id}/set-profile-pic", dependencies=[Depends(require_admin)])
def set_cluster_profile_pic(cluster_id: str, body: SetProfilePicRequest):
    """Set custom profile picture for a raw cluster or guest."""
    from app.services.drive_cache import get_cached_json, save_cached_json, get_cached_file
    
    # 1. Load face encodings/records
    all_records = load_encodings()

    # Match on the record's own Drive id. Resolving via basename picked the
    # first record with that filename, which is a different photo for the
    # ~52% of the corpus whose basenames collide.
    matching_record = None
    for r in all_records:
        if resolve_one_drive_id(r["path"]) == body.drive_id:
            matching_record = r
            break

    if matching_record is None:
        raise HTTPException(status_code=404, detail="Photo not found in registry")
            
    if not matching_record or not matching_record.get("encodings"):
        raise HTTPException(status_code=400, detail="No face encodings found in this photo")
        
    encs = matching_record["encodings"]
    locs = matching_record["locations"]
    frames = matching_record.get("frame_indices", [None] * len(encs))
    
    # Find the best face index
    best_idx = 0
    if len(encs) > 1:
        if cluster_id.startswith("guest_"):
            guest_id = cluster_id.replace("guest_", "")
            selfie_data = get_cached_file(f"selfie_{guest_id}.jpg")
            if selfie_data:
                try:
                    from app.services.face_service import encode_selfie
                    selfie_enc = encode_selfie(selfie_data)
                    if selfie_enc is not None:
                        dists = np.linalg.norm(np.array(encs) - selfie_enc, axis=1)
                        best_idx = int(np.argmin(dists))
                except Exception as selfie_err:
                    log.warning(f"Could not encode selfie for custom profile pic: {selfie_err}")
        else:
            clusters = get_face_clusters()
            if cluster_id in clusters:
                cluster_members = clusters[cluster_id].get("members", [])
                if cluster_members:
                    rep_member = clusters[cluster_id]["representative"]
                    rep_path = rep_member["path"]
                    rep_loc = rep_member["location"]
                    
                    rep_enc = None
                    for r in all_records:
                        if r["path"] == rep_path:
                            for enc, loc in zip(r["encodings"], r["locations"]):
                                if loc == rep_loc:
                                    rep_enc = enc
                                    break
                            if rep_enc is not None:
                                break
                    if rep_enc is not None:
                        dists = np.linalg.norm(np.array(encs) - rep_enc, axis=1)
                        best_idx = int(np.argmin(dists))

    # Form the member dict for the selected profile pic
    member = {
        "path": matching_record["path"],
        "location": locs[best_idx],
        "frame_idx": frames[best_idx],
        "is_video": matching_record["path"].lower().endswith((".mp4", ".mov", ".avi", ".mkv", ".webm"))
    }
    
    # Save in cluster_representatives.json
    reps_data = get_cached_json("cluster_representatives.json") or {}
    reps_data[cluster_id] = member
    save_cached_json("cluster_representatives.json", reps_data)
    
    return {"success": True, "representative": member}


@router.post("/clusters/{cluster_id}/upload-profile-pic", dependencies=[Depends(require_admin)])
async def upload_cluster_profile_pic(cluster_id: str, file: UploadFile = File(...)):
    """Upload a custom local photo from device to set as profile picture."""
    try:
        from app.services.drive_cache import save_cached_file, get_cached_json, save_cached_json
        from PIL import Image, ImageOps
        import io
        
        # 1. Read uploaded file bytes
        content = await file.read()
        
        # 2. Resize and crop to square 150x150
        img = Image.open(io.BytesIO(content)).convert("RGB")
        img = ImageOps.exif_transpose(img)
        
        # Crop to square
        w, h = img.size
        min_dim = min(w, h)
        left = (w - min_dim) // 2
        top = (h - min_dim) // 2
        right = (w + min_dim) // 2
        bottom = (h + min_dim) // 2
        img = img.crop((left, top, right, bottom))
        
        img = img.resize((150, 150), Image.Resampling.LANCZOS)
        
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        jpeg_bytes = buf.getvalue()
        
        # 3. Save as custom_avatar_{cluster_id}.jpg
        avatar_filename = f"custom_avatar_{cluster_id}.jpg"
        save_cached_file(avatar_filename, jpeg_bytes, mime_type="image/jpeg")
        
        # 4. Save in representatives index
        reps_data = get_cached_json("cluster_representatives.json") or {}
        reps_data[cluster_id] = {
            "is_custom_upload": True,
            "avatar_path": avatar_filename
        }
        save_cached_json("cluster_representatives.json", reps_data)
        
        return {"success": True, "avatar_url": f"/faces/clusters/{cluster_id}/thumbnail"}
    except Exception as e:
        log.error(f"Failed to upload custom profile picture: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to upload profile picture: {e}")

