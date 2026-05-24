"""
Face matching service.
Loads precomputed encodings and matches guest selfies against them.

Supports:
  - Multi-angle selfies: up to 5 images → encodings averaged for best coverage
  - Confidence scoring: each match returns a 0–100% confidence based on distance
  - Vectorized matching using numpy for speed
"""

import pickle
import logging
from pathlib import Path
from typing import Optional, List
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
    Take a single guest selfie (raw bytes) and return their face encoding.
    Returns None if no face detected.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img = ImageOps.exif_transpose(img)

        # Resize for speed but keep enough detail for accuracy
        w, h = img.size
        if max(w, h) > 1000:
            scale = 1000 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        img_array = np.array(img)

        # Try CNN first for better accuracy on selfies, fall back to HOG
        locations = face_recognition.face_locations(img_array, model="cnn")
        if not locations:
            locations = face_recognition.face_locations(img_array, model="hog")

        if not locations:
            return None

        if len(locations) > 1:
            # Pick the largest face (closest to camera — most likely the guest)
            largest = max(
                locations, key=lambda loc: (loc[2] - loc[0]) * (loc[1] - loc[3])
            )
            locations = [largest]

        # num_jitters=3 on selfies for better encoding quality (3 slight jitters of face)
        encodings = face_recognition.face_encodings(img_array, locations, num_jitters=3)
        return encodings[0] if encodings else None

    except Exception as e:
        log.error(f"Failed to encode selfie: {e}")
        return None


def encode_multiple_selfies(images_bytes: List[bytes]) -> List[np.ndarray]:
    """
    Encode multiple selfie angles into a list of face encodings.
    Returns only successfully detected encodings (skips failures silently).
    """
    encodings = []
    for idx, img_bytes in enumerate(images_bytes):
        enc = encode_selfie(img_bytes)
        if enc is not None:
            encodings.append(enc)
            log.info(f"Selfie {idx+1}/{len(images_bytes)}: face encoded successfully")
        else:
            log.warning(f"Selfie {idx+1}/{len(images_bytes)}: no face detected, skipping")
    return encodings


def compute_confidence(distance: float, tolerance: float) -> float:
    """
    Convert an encoding distance to a human-readable confidence score (0–100%).

    Scoring:
      - distance = 0.00 → 100% (identical face)
      - distance = 0.40 → ~78%  (very close match)
      - distance = 0.55 → ~66%  (good match, at default tolerance)
      - distance = 0.65 → ~59%  (marginal match)
      - distance ≥ 1.00 → 0%
    """
    return round(max(0.0, min(100.0, (1.0 - distance) * 100)), 1)


# ── Matching ──────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def get_flat_encodings():
    """
    Builds a flat numpy array of all personal photo face encodings.
    Kept in memory via lru_cache for instant vectorized lookups.
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


def find_matching_photos(
    guest_encodings: List[np.ndarray],
    tolerance: float = None
) -> dict:
    """
    Compare all guest selfie encodings (multi-angle) against precomputed wedding photo
    encodings using vectorized operations. For each photo face, takes the MINIMUM
    distance across all selfie angles (best match wins).

    Returns matched photos with their confidence scores.
    """
    if tolerance is None:
        tolerance = settings.FACE_MATCH_TOLERANCE

    all_records = load_encodings()

    # Collect all common photos
    common_photos = [
        record["path"] for record in all_records if record.get("is_common", False)
    ]

    personal_matches: dict[str, dict] = {}  # path → {min_distance, confidence}
    flat_encs, paths = get_flat_encodings()

    if len(flat_encs) > 0 and len(guest_encodings) > 0:
        # Vectorized: compute distance from each guest encoding to all photo encodings
        # Shape: (num_selfies, num_photo_faces)
        all_distances = np.array([
            np.linalg.norm(flat_encs - guest_enc, axis=1)
            for guest_enc in guest_encodings
        ])

        # For each photo face, take the minimum distance across all selfie angles
        # This is the "best match" strategy for multi-angle
        min_distances = np.min(all_distances, axis=0)  # shape: (num_photo_faces,)

        matching_indices = np.where(min_distances <= tolerance)[0]

        for idx in matching_indices:
            path = paths[idx]
            dist = float(min_distances[idx])
            if path not in personal_matches or dist < personal_matches[path]["distance"]:
                personal_matches[path] = {
                    "distance": dist,
                    "confidence": compute_confidence(dist, tolerance),
                }

    # Sort personal matches by confidence descending
    sorted_personal = sorted(
        personal_matches.items(),
        key=lambda x: x[1]["confidence"],
        reverse=True
    )

    personal_photos = [path for path, _ in sorted_personal]
    confidence_map = {path: meta["confidence"] for path, meta in sorted_personal}

    # Log confidence distribution
    if sorted_personal:
        confidences = [meta["confidence"] for _, meta in sorted_personal]
        avg_conf = round(sum(confidences) / len(confidences), 1)
        high_conf = sum(1 for c in confidences if c >= 70)
        log.info(
            f"Match complete — {len(personal_photos)} personal, "
            f"{len(common_photos)} common photos | "
            f"Avg confidence: {avg_conf}% | High confidence (≥70%): {high_conf}"
        )
    else:
        log.info(f"Match complete — 0 personal, {len(common_photos)} common photos")

    return {
        "personal_photos": personal_photos,
        "common_photos": common_photos,
        "total_matches": len(personal_photos),
        "common_count": len(common_photos),
        "confidence_map": confidence_map,
        "selfie_angles_used": len(guest_encodings),
    }


# ── Main entry point ──────────────────────────────────────────────────────────


def match_guest_selfie(
    image_bytes: bytes,
    tolerance: float = None,
    extra_selfie_bytes: List[bytes] = None
) -> dict:
    """
    Full pipeline: one or more selfie images → matched photo paths with confidence scores.
    
    Args:
        image_bytes: Primary selfie image bytes (required).
        tolerance: Optional override for match sensitivity. Defaults to settings value.
        extra_selfie_bytes: Optional list of additional angle selfie bytes for multi-angle matching.
    """
    # Collect all selfie bytes (primary + extras)
    all_selfie_bytes = [image_bytes]
    if extra_selfie_bytes:
        all_selfie_bytes.extend(extra_selfie_bytes)

    # Encode all selfie angles
    guest_encodings = encode_multiple_selfies(all_selfie_bytes)

    if not guest_encodings:
        return {
            "success": False,
            "error": "no_face_detected",
            "message": "We couldn't detect a face in your photo. Please try again in good lighting.",
        }

    log.info(f"Using {len(guest_encodings)}/{len(all_selfie_bytes)} selfie angle(s) for matching")

    # Step 2 — match against all wedding photos
    results = find_matching_photos(guest_encodings, tolerance=tolerance)

    if results["total_matches"] == 0 and results["common_count"] == 0:
        return {
            "success": False,
            "error": "no_matches",
            "message": "We couldn't find you in the wedding photos. Please try a clearer selfie.",
        }

    return {"success": True, **results}


from app.services.drive_service import build_filename_to_id_map


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
