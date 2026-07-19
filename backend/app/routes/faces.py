"""
Face registration and matching routes.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Response

from app.database import supabase
from app.services.face_service import match_guest_selfie, resolve_drive_ids

log = logging.getLogger(__name__)
router = APIRouter(prefix="/faces", tags=["faces"])


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("/register")
async def register_face(
    guest_id: str = Form(...),
    selfie: UploadFile = File(...),
    selfie2: UploadFile = File(None),
    selfie3: UploadFile = File(None),
    selfie4: UploadFile = File(None),
    selfie5: UploadFile = File(None),
):
    """
    Step 2 of guest flow.
    Guest uploads 1–5 selfies (different angles) → system finds all their photos.
    More angles = better accuracy.
    """
    try:
        # Validate guest exists
        guest = supabase.table("guests").select("*").eq("id", guest_id).execute()
        if not guest.data:
            raise HTTPException(status_code=404, detail="Guest not found")

        # Read primary selfie bytes
        image_bytes = await selfie.read()
        if len(image_bytes) > 10 * 1024 * 1024:  # 10MB limit
            raise HTTPException(status_code=400, detail="Selfie too large. Max 10MB.")

        # Read extra angle selfies
        extra_selfie_bytes = []
        for extra_upload in [selfie2, selfie3, selfie4, selfie5]:
            if extra_upload is not None:
                extra_bytes = await extra_upload.read()
                if len(extra_bytes) > 0:
                    extra_selfie_bytes.append(extra_bytes)

        log.info(
            f"Guest {guest_id}: received {1 + len(extra_selfie_bytes)} selfie angle(s)"
        )

        # Save primary selfie as reference (used in Recognized Faces panel)
        from app.services.drive_cache import save_cached_file
        save_cached_file(f"selfie_{guest_id}.jpg", image_bytes)

        # Run face matching with all angles
        match_result = match_guest_selfie(
            image_bytes,
            extra_selfie_bytes=extra_selfie_bytes if extra_selfie_bytes else None
        )

        if not match_result["success"]:
            raise HTTPException(status_code=422, detail=match_result["message"])

        # Auto-name corresponding cluster in Recognized Faces panel
        auto_name_cluster_for_guest(guest.data[0]["name"], image_bytes)

        # Resolve local file paths → Drive file IDs
        personal_ids = resolve_drive_ids(match_result["personal_photos"])
        common_ids = resolve_drive_ids(match_result["common_photos"])

        # ── Deduplicate before upsert ─────────────────────────────────────────
        unique_photos: dict[str, dict] = {}
        for drive_id in personal_ids:
            if drive_id:
                unique_photos[drive_id] = {
                    "drive_path": drive_id,
                    "is_common": False,
                    "face_count": 1,
                }
        for drive_id in common_ids:
            if drive_id:
                unique_photos[drive_id] = {
                    "drive_path": drive_id,
                    "is_common": True,
                    "face_count": 4,
                }

        photos_to_upsert = list(unique_photos.values())
        log.info(
            f"Guest {guest_id}: {len(personal_ids)} personal + "
            f"{len(common_ids)} common → {len(photos_to_upsert)} unique photos to upsert"
        )

        photo_rows: list[dict] = []
        if photos_to_upsert:
            upserted = (
                supabase.table("photos")
                .upsert(photos_to_upsert, on_conflict="drive_path")
                .execute()
            )

            if upserted.data:
                drive_to_id = {p["drive_path"]: p["id"] for p in upserted.data}

                from app.services.face_state import get_disassociated_photo_ids
                disassociated_set = get_disassociated_photo_ids(guest_id)

                # Build guest_photos mapping rows — use a set to deduplicate
                seen_photo_ids: set[str] = set()
                for drive_id in list(personal_ids) + list(common_ids):
                    pid = drive_to_id.get(drive_id)
                    if pid and pid not in seen_photo_ids and pid not in disassociated_set:
                        seen_photo_ids.add(pid)
                        photo_rows.append({"guest_id": guest_id, "photo_id": pid})

                if photo_rows:
                    supabase.table("guest_photos").upsert(
                        photo_rows, on_conflict="guest_id,photo_id"
                    ).execute()

        # Update guest last_login
        supabase.table("guests").update(
            {
                "last_login": datetime.utcnow().isoformat(),
            }
        ).eq("id", guest_id).execute()

        # Confidence summary
        confidence_map = match_result.get("confidence_map", {})
        confidences = list(confidence_map.values())
        avg_confidence = round(sum(confidences) / len(confidences), 1) if confidences else 0
        high_conf_count = sum(1 for c in confidences if c >= 70)

        log.info(
            f"Guest {guest_id} matched: "
            f"{len(personal_ids)} personal + {len(common_ids)} common photos | "
            f"Avg confidence: {avg_confidence}% | "
            f"Angles used: {match_result.get('selfie_angles_used', 1)}"
        )

        return {
            "success": True,
            "personal_count": len(personal_ids),
            "common_count": len(common_ids),
            "total": len(personal_ids) + len(common_ids),
            "angles_used": match_result.get("selfie_angles_used", 1),
            "avg_confidence": avg_confidence,
            "high_confidence_matches": high_conf_count,
            "message": (
                f"Found {len(personal_ids)} photos of you and {len(common_ids)} group photos! "
                f"Average match confidence: {avg_confidence}%"
            ),
        }

    except Exception as e:
        import traceback

        tb = traceback.format_exc()
        log.error(f"Error in register_face:\n{tb}")
        try:
            with open("register_error.log", "w") as f:
                f.write(tb)
        except Exception:
            pass
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")


# ── Face Clustering & Recognized Faces (Google Photos style) ─────────────────

import io
from pathlib import Path
import cv2
import numpy as np
from PIL import Image, ImageOps
from sklearn.cluster import AgglomerativeClustering
from fastapi import Response
from googleapiclient.http import MediaIoBaseDownload

from app.services.face_service import load_encodings, get_filename_map
from app.services.drive_service import get_drive_service, download_file_to_memory, download_file_from_drive
from app.config import settings

CACHE_THUMB_DIR = Path("cache/thumbnails")


_cached_clusters = None
_cached_clusters_key = None


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


def get_face_clusters() -> dict:
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
        from scripts.face_engine.matching import detect_backend_from_records, default_tolerance
        backend = detect_backend_from_records(all_records)
        cluster_threshold = default_tolerance(backend)
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


@router.get("/clusters")
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
        
        # Get count of matched photos per guest
        gp_res = supabase.table("guest_photos").select("guest_id").execute()
        counts = {}
        for row in gp_res.data:
            gid = row["guest_id"]
            counts[gid] = counts.get(gid, 0) + 1

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

        filename = Path(rep["path"]).name
        drive_id = mapping.get(filename, "")
        loc = rep["location"]
        rep_key = f"{drive_id}_{loc[0]}_{loc[1]}_{loc[2]}_{loc[3]}"

        name = names_data.get(rep_key) or names_data.get(cid, f"Person #{cid}")

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


@router.post("/clusters/{cluster_id}/rename")
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
    filename = Path(rep["path"]).name
    mapping = get_filename_map()
    drive_id = mapping.get(filename, "")
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


@router.post("/clusters/merge")
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


@router.delete("/clusters/{cluster_id}/unmerge")
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


@router.get("/clusters/{cluster_id}/photos")
def get_cluster_photos(cluster_id: str):
    """Get all photos/videos featuring the person in the specified cluster or guest album."""
    if cluster_id.startswith("guest_"):
        guest_id = cluster_id.replace("guest_", "")
        
        # Fetch guest personal photos
        gp_res = supabase.table("guest_photos").select("photo_id").eq("guest_id", guest_id).execute()
        if not gp_res.data:
            return []

        photo_ids = [row["photo_id"] for row in gp_res.data]
        res = supabase.table("photos").select("id, drive_path, is_common, face_count").in_("id", photo_ids).execute()

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
    mapping = get_filename_map()

    for path in paths:
        filename = Path(path).name
        if filename in mapping:
            drive_id = mapping[filename]
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
    mapping = get_filename_map()
    if filename not in mapping:
        raise HTTPException(
            status_code=404, detail="Source media file not found in Google Drive"
        )

    drive_id = mapping[filename]

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
        filename = Path(rep["path"]).name
        mapping = get_filename_map()
        drive_id = mapping.get(filename, "")
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


@router.get("/guests-list")
def get_guests_list():
    """Get a simple list of guest names and IDs for manual sharing dropdown."""
    try:
        res = supabase.table("guests").select("id, name").order("name").execute()
        return res.data or []
    except Exception as e:
        log.error(f"Error getting guests list: {e}")
        return []


@router.post("/clusters/{cluster_id}/set-profile-pic")
def set_cluster_profile_pic(cluster_id: str, body: SetProfilePicRequest):
    """Set custom profile picture for a raw cluster or guest."""
    from app.services.drive_cache import get_cached_json, save_cached_json, get_cached_file
    
    # 1. Load face encodings/records
    all_records = load_encodings()
    mapping = get_filename_map()
    
    # Find matching record in face_encodings
    matching_filename = None
    for fname, did in mapping.items():
        if did == body.drive_id:
            matching_filename = fname
            break
            
    if not matching_filename:
        raise HTTPException(status_code=404, detail="Photo not found in registry")
        
    matching_record = None
    for r in all_records:
        if Path(r["path"]).name == matching_filename:
            matching_record = r
            break
            
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


@router.post("/clusters/{cluster_id}/upload-profile-pic")
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

