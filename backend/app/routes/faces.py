"""
Face registration and matching routes.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, UploadFile, File, Form

from app.database import supabase
from app.services.face_service import match_guest_selfie, resolve_drive_ids

log = logging.getLogger(__name__)
router = APIRouter(prefix="/faces", tags=["faces"])


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/register")
async def register_face(
    guest_id: str = Form(...),
    selfie: UploadFile = File(...),
):
    """
    Step 2 of guest flow.
    Guest uploads selfie → system finds all their photos.
    """
    try:
        # Validate guest exists
        guest = supabase.table("guests").select("*").eq("id", guest_id).execute()
        if not guest.data:
            raise HTTPException(status_code=404, detail="Guest not found")

        # Read selfie bytes
        image_bytes = await selfie.read()
        if len(image_bytes) > 10 * 1024 * 1024:  # 10MB limit
            raise HTTPException(status_code=400, detail="Selfie too large. Max 10MB.")

        # Run face matching
        match_result = match_guest_selfie(image_bytes)

        if not match_result["success"]:
            raise HTTPException(status_code=422, detail=match_result["message"])

        # Resolve local file paths → Drive file IDs
        personal_ids = resolve_drive_ids(match_result["personal_photos"])
        common_ids   = resolve_drive_ids(match_result["common_photos"])

        # ── Deduplicate before upsert ─────────────────────────────────────────
        # A photo can appear in both personal_ids and common_ids.
        # Build a dict keyed on drive_id so each ID appears exactly once.
        # Common status takes precedence (overrides personal).
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
            upserted = supabase.table("photos").upsert(
                photos_to_upsert,
                on_conflict="drive_path"
            ).execute()

            if upserted.data:
                drive_to_id = {p["drive_path"]: p["id"] for p in upserted.data}

                # Build guest_photos mapping rows — use a set to deduplicate
                seen_photo_ids: set[str] = set()
                for drive_id in list(personal_ids) + list(common_ids):
                    pid = drive_to_id.get(drive_id)
                    if pid and pid not in seen_photo_ids:
                        seen_photo_ids.add(pid)
                        photo_rows.append({"guest_id": guest_id, "photo_id": pid})

                if photo_rows:
                    supabase.table("guest_photos").upsert(
                        photo_rows,
                        on_conflict="guest_id,photo_id"
                    ).execute()

        # Update guest last_login
        supabase.table("guests").update({
            "last_login": datetime.utcnow().isoformat(),
        }).eq("id", guest_id).execute()

        log.info(
            f"Guest {guest_id} matched: "
            f"{len(personal_ids)} personal + {len(common_ids)} common photos"
        )

        return {
            "success": True,
            "personal_count": len(personal_ids),
            "common_count": len(common_ids),
            "total": len(personal_ids) + len(common_ids),
            "message": f"Found {len(personal_ids)} photos of you and {len(common_ids)} group photos!"
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
        raise HTTPException(
            status_code=500,
            detail=f"Internal Server Error: {str(e)}"
        )


# ── Face Clustering & Recognized Faces (Google Photos style) ─────────────────

import io
from pathlib import Path
import cv2
import numpy as np
from PIL import Image, ImageOps
from sklearn.cluster import DBSCAN
from fastapi import Response
from googleapiclient.http import MediaIoBaseDownload

from app.services.face_service import load_encodings, get_filename_map
from app.services.drive_service import get_drive_service

CACHE_THUMB_DIR = Path("cache/thumbnails")

def get_face_clusters() -> dict:
    """
    Cluster all precomputed face encodings in face_encodings.pkl using DBSCAN.
    """
    try:
        # Clear the LRU cache to make sure we load the newly preprocessed face encodings
        load_encodings.cache_clear()
        all_records = load_encodings()
    except Exception as e:
        log.warning(f"Could not load encodings for clustering: {e}")
        return {}

    X = []
    origins = []
    for photo_idx, record in enumerate(all_records):
        encs = record.get("encodings", [])
        locs = record.get("locations", [])
        frames = record.get("frame_indices", [None] * len(encs))
        
        for face_idx, (enc, loc, frame) in enumerate(zip(encs, locs, frames)):
            X.append(enc)
            origins.append({
                "path": record["path"],
                "location": loc,
                "frame_idx": frame,
                "is_video": record["path"].lower().endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm'))
            })

    if not X:
        return {}

    X = np.array(X)
    # DBSCAN clustering (0.48 distance tolerance corresponds well to HOG face distance)
    db = DBSCAN(eps=0.48, min_samples=1, metric="euclidean").fit(X)
    labels = db.labels_

    clusters = {}
    for idx, label in enumerate(labels):
        if label == -1:
            continue  # ignore noise
        
        label_str = str(label)
        origin = origins[idx]
        
        if label_str not in clusters:
            clusters[label_str] = {
                "members": [],
                "photos": set()
            }
        
        clusters[label_str]["members"].append(origin)
        clusters[label_str]["photos"].add(origin["path"])

    result = {}
    for label_str, data in clusters.items():
        # Prefer photo files over video files as the representative thumbnail
        rep = None
        for member in data["members"]:
            if not member["is_video"]:
                rep = member
                break
        if not rep:
            rep = data["members"][0]

        result[label_str] = {
            "representative": rep,
            "photos": sorted(list(data["photos"])),
            "count": len(data["photos"])
        }

    # Sort clusters descending by how many photos the person appears in
    return dict(sorted(result.items(), key=lambda x: x[1]["count"], reverse=True))


from pydantic import BaseModel

class RenameClusterRequest(BaseModel):
    name: str


@router.get("/clusters")
def get_clusters():
    """Get list of recognized face clusters with counts, names, and thumbnail links."""
    from app.services.drive_cache import get_cached_json
    clusters = get_face_clusters()
    names_data = get_cached_json("cluster_names.json") or {}

    ui_clusters = []
    for cid, cdata in clusters.items():
        name = names_data.get(cid, f"Person #{cid}")
        ui_clusters.append({
            "id": cid,
            "name": name,
            "count": cdata["count"],
            "thumbnail_url": f"/faces/clusters/{cid}/thumbnail"
        })
    return ui_clusters


@router.post("/clusters/{cluster_id}/rename")
def rename_cluster(cluster_id: str, body: RenameClusterRequest):
    """Rename a face cluster — persisted to Google Drive cache."""
    from app.services.drive_cache import get_cached_json, save_cached_json

    data = get_cached_json("cluster_names.json") or {}
    new_name = body.name.strip()
    data[cluster_id] = new_name
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
        log.error(f"Failed to auto-associate newly renamed cluster with existing guest: {assoc_err}")

    return {"success": True, "cluster_id": cluster_id, "name": new_name}


@router.get("/clusters/{cluster_id}/photos")
def get_cluster_photos(cluster_id: str):
    """Get all photos/videos featuring the person in the specified cluster."""
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
            is_video = filename.lower().endswith(('.mp4', '.mov', '.avi', '.mkv', '.webm'))
            mime_type = "video/mp4" if is_video else "image/jpeg"
            
            resolved.append({
                "drive_id": drive_id,
                "is_common": False,  # default to False for clustering
                "thumb_url": f"/photos/thumb/{drive_id}",
                "stream_url": f"/photos/stream/{drive_id}",
                "is_video": is_video,
                "mime_type": mime_type
            })
    return resolved


@router.get("/clusters/{cluster_id}/thumbnail")
def get_cluster_thumbnail(cluster_id: str):
    """Return a cropped square face thumbnail of the person in the cluster.
    Cache hierarchy: local /tmp (L1) → Drive cache folder (persistent).
    """
    from app.services.drive_cache import get_cached_file, save_cached_file

    cache_key = f"face_cluster_{cluster_id}.jpg"

    # ── 1. Check Drive-backed cache ───────────────────────────────────────────
    cached_data = get_cached_file(cache_key)
    if cached_data:
        return Response(content=cached_data, media_type="image/jpeg")

    # ── 2. Generate fresh thumbnail ───────────────────────────────────────────
    clusters = get_face_clusters()
    if cluster_id not in clusters:
        raise HTTPException(status_code=404, detail="Face cluster not found")

    rep = clusters[cluster_id]["representative"]
    path_str = rep["path"]
    location = rep["location"]  # [top, right, bottom, left]
    is_video = rep["is_video"]
    frame_idx = rep["frame_idx"]

    filename = Path(path_str).name
    mapping = get_filename_map()
    if filename not in mapping:
        raise HTTPException(status_code=404, detail="Source media file not found in Google Drive")

    drive_id = mapping[filename]

    try:
        service = get_drive_service()
        if is_video:
            # Use /tmp for temp video file (works on hosted servers)
            temp_video = Path(f"/tmp/weddingsnap_cache/temp_thumb_{cluster_id}.tmp")
            temp_video.parent.mkdir(parents=True, exist_ok=True)

            request = service.files().get_media(fileId=drive_id)
            with open(temp_video, "wb") as f:
                downloader = MediaIoBaseDownload(f, request, chunksize=1024 * 1024 * 5)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

            cap = cv2.VideoCapture(str(temp_video))
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx or 0)
            ret, frame = cap.read()
            cap.release()

            if temp_video.exists():
                temp_video.unlink()

            if not ret:
                raise Exception("Could not decode video frame")

            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb_frame)
        else:
            request = service.files().get_media(fileId=drive_id)
            img_bytes = io.BytesIO()
            downloader = MediaIoBaseDownload(img_bytes, request, chunksize=1024 * 1024 * 2)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            img_bytes.seek(0)
            img = Image.open(img_bytes).convert("RGB")

        if not is_video:
            img = ImageOps.exif_transpose(img)

        w, h = img.size
        if max(w, h) > 1200:
            scale = 1200 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
            w, h = img.size

        top, right, bottom, left = location
        fh = bottom - top
        fw = right - left
        pad_y = int(fh * 0.45)
        pad_x = int(fw * 0.45)

        cropped = img.crop((
            max(0, left - pad_x),
            max(0, top - pad_y),
            min(w, right + pad_x),
            min(h, bottom + pad_y),
        ))
        cropped = cropped.resize((150, 150), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=90)
        result_bytes = buf.getvalue()

        # ── 3. Save to Drive cache (persistent) + L1 ─────────────────────────
        save_cached_file(cache_key, result_bytes, mime_type="image/jpeg")

        return Response(content=result_bytes, media_type="image/jpeg")

    except Exception as e:
        log.error(f"Error creating thumbnail for face cluster {cluster_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate face thumbnail: {e}")