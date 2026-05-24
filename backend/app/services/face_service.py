"""
Face matching service.
Loads precomputed encodings and matches a guest selfie against them.
"""

import pickle
import logging
from pathlib import Path
from typing import Optional
from functools import lru_cache

# pyrefly: ignore [missing-import]
import numpy as np
import face_recognition
from PIL import Image, ImageOps
import io

from app.config import settings

log = logging.getLogger(__name__)


# ── Load encodings ────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def load_encodings() -> list[dict]:
    """
    Load face_encodings.pkl from Supabase Storage / local cache.
    """
    from app.services.drive_cache import get_cached_file
    
    data_bytes = get_cached_file("face_encodings.pkl")
    if not data_bytes:
        # Fall back to local path if it exists (e.g. during development/tests)
        cache_path = Path(settings.ENCODINGS_CACHE_PATH)
        if cache_path.exists():
            log.info(f"Loading encodings from local path fallback: {cache_path}...")
            with open(cache_path, "rb") as f:
                data = pickle.load(f)
            log.info(f"Loaded {len(data):,} photo records from local file")
            return data
        log.warning("No face_encodings.pkl found in Supabase Storage or local path. Returning empty list.")
        return []

    log.info("Loading encodings from Supabase Storage cache...")
    data = pickle.loads(data_bytes)
    log.info(f"Loaded {len(data):,} photo records")
    return data



# ── Selfie processing ─────────────────────────────────────────────────────────


def encode_selfie(image_bytes: bytes) -> Optional[np.ndarray]:
    """
    Take a guest's selfie (raw bytes) and return their face encoding.
    Returns None if no face detected.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img = ImageOps.exif_transpose(img)

        # Resize for speed
        w, h = img.size
        if max(w, h) > 800:
            scale = 800 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        img_array = np.array(img)
        locations = face_recognition.face_locations(img_array, model="hog")

        if not locations:
            return None

        if len(locations) > 1:
            # If multiple faces in selfie, pick the largest (closest to camera)
            largest = max(
                locations, key=lambda loc: (loc[2] - loc[0]) * (loc[1] - loc[3])
            )
            locations = [largest]

        encodings = face_recognition.face_encodings(img_array, locations)
        return encodings[0] if encodings else None

    except Exception as e:
        log.error(f"Failed to encode selfie: {e}")
        return None


# ── Matching ──────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def get_flat_encodings():
    """
    Builds a flat list of face encodings and their corresponding photo paths.
    Kept in memory via lru_cache for instant lookups.
    """
    all_records = load_encodings()
    flat_encodings = []
    paths = []

    for record in all_records:
        if record.get("is_common", False):
            continue
        for enc in record.get("encodings", []):
            flat_encodings.append(enc)
            paths.append(record["path"])

    if not flat_encodings:
        return np.empty((0, 128)), []

    return np.array(flat_encodings), paths


def find_matching_photos(guest_encoding: np.ndarray, tolerance: float = None) -> dict:
    """
    Compare guest encoding against all precomputed encodings using vectorized operations.
    """
    if tolerance is None:
        tolerance = settings.FACE_MATCH_TOLERANCE

    all_records = load_encodings()

    # Collect all common photos
    common_photos = [
        record["path"] for record in all_records if record.get("is_common", False)
    ]

    personal_photos = []
    flat_encs, paths = get_flat_encodings()

    if len(flat_encs) > 0:
        # Distance calculation vectorized over all face encodings in one operation
        distances = np.linalg.norm(flat_encs - guest_encoding, axis=1)
        matching_indices = np.where(distances <= tolerance)[0]

        # Deduplicate paths (a face can match multiple encodings or photos)
        matched_set = {paths[idx] for idx in matching_indices}
        personal_photos = list(matched_set)

    log.info(
        f"Match complete — {len(personal_photos)} personal, "
        f"{len(common_photos)} common photos"
    )

    return {
        "personal_photos": personal_photos,
        "common_photos": common_photos,
        "total_matches": len(personal_photos),
        "common_count": len(common_photos),
    }


# ── Main entry point ──────────────────────────────────────────────────────────


def match_guest_selfie(image_bytes: bytes) -> dict:
    """
    Full pipeline: selfie bytes → matched photo paths.
    This is what the API route calls.
    """
    # Step 1 — encode the selfie
    guest_encoding = encode_selfie(image_bytes)
    if guest_encoding is None:
        return {
            "success": False,
            "error": "no_face_detected",
            "message": "We couldn't detect a face in your photo. Please try again in good lighting.",
        }

    # Step 2 — match against all wedding photos
    results = find_matching_photos(guest_encoding)

    if results["total_matches"] == 0 and results["common_count"] == 0:
        return {
            "success": False,
            "error": "no_matches",
            "message": "We couldn't find you in the wedding photos. Please try a clearer selfie.",
        }

    return {"success": True, **results}


from app.services.drive_service import build_filename_to_id_map
from functools import lru_cache


@lru_cache(maxsize=1)
def get_filename_map() -> dict:
    log.info("Building filename → Drive ID map...")
    mapping = build_filename_to_id_map()
    log.info(f"Mapped {len(mapping):,} files")
    return mapping


def resolve_drive_ids(local_paths: list[str]) -> list[str]:
    """Convert local file paths to Drive file IDs."""
    mapping = get_filename_map()
    drive_ids = []
    for path in local_paths:
        filename = Path(path).name
        if filename in mapping:
            drive_ids.append(mapping[filename])
        else:
            log.warning(f"No Drive ID found for: {filename}")
    return drive_ids


def associate_guest_by_name(guest_id: str, name: str) -> int:
    """
    Checks if the guest's name matches any face clusters named by the admin.
    If yes, links those photos to this guest automatically.
    Returns the number of photos linked.
    """
    guest_name = name.strip().lower()
    if not guest_name:
        return 0

    from app.services.drive_cache import get_cached_json
    from app.database import supabase

    names_data = get_cached_json("cluster_names.json")
    if not names_data:
        return 0

    try:
        matching_cluster_ids = [cid for cid, cname in names_data.items() if cname.strip().lower() == guest_name]
        if not matching_cluster_ids:
            return 0

        # Avoid circular imports by importing get_face_clusters locally
        from app.routes.faces import get_face_clusters
        clusters = get_face_clusters()
        
        member_filenames = []
        for cid in matching_cluster_ids:
            if cid in clusters:
                for file_path in clusters[cid]["photos"]:
                    member_filenames.append(Path(file_path).name)

        if not member_filenames:
            return 0

        filename_map = get_filename_map()
        drive_ids = [filename_map[fname] for fname in member_filenames if fname in filename_map]

        if not drive_ids:
            return 0

        photos_to_upsert = [{"drive_path": d, "is_common": False, "face_count": 1} for d in drive_ids]
        upserted = supabase.table("photos").upsert(
            photos_to_upsert,
            on_conflict="drive_path"
        ).execute()

        if upserted.data:
            drive_to_id = {p["drive_path"]: p["id"] for p in upserted.data}
            photo_rows = []
            for drive_id in drive_ids:
                pid = drive_to_id.get(drive_id)
                if pid:
                    photo_rows.append({"guest_id": guest_id, "photo_id": pid})
            if photo_rows:
                supabase.table("guest_photos").upsert(
                    photo_rows,
                    on_conflict="guest_id,photo_id"
                ).execute()
                log.info(f"Auto-linked {len(photo_rows)} photos to guest '{name}' ({guest_id}) by face cluster name match")
                return len(photo_rows)
    except Exception as e:
        log.error(f"Failed to auto-associate named cluster with guest '{name}': {e}")

    return 0

