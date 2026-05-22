"""
Face matching service.
Loads precomputed encodings and matches a guest selfie against them.
"""

import pickle
import logging
from pathlib import Path
from typing import Optional
from functools import lru_cache

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
    Load face_encodings.pkl into memory once and cache it.
    Each entry looks like:
    {
        "path": "/Volumes/.../photo.jpg",
        "encodings": [array, array, ...],  # one per face in photo
        "face_count": 2,
        "is_common": False
    }
    """
    cache_path = Path(settings.ENCODINGS_CACHE_PATH)
    if not cache_path.exists():
        raise FileNotFoundError(f"Encodings not found at {cache_path}. Run preprocess.py first.")

    log.info(f"Loading encodings from {cache_path}...")
    with open(cache_path, "rb") as f:
        data = pickle.load(f)
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
            largest = max(locations, key=lambda loc: (loc[2] - loc[0]) * (loc[1] - loc[3]))
            locations = [largest]

        encodings = face_recognition.face_encodings(img_array, locations)
        return encodings[0] if encodings else None

    except Exception as e:
        log.error(f"Failed to encode selfie: {e}")
        return None


# ── Matching ──────────────────────────────────────────────────────────────────

def find_matching_photos(
    guest_encoding: np.ndarray,
    tolerance: float = None
) -> dict:
    """
    Compare guest encoding against all precomputed encodings.

    Returns:
    {
        "personal_photos": ["drive_path_1", "drive_path_2", ...],
        "common_photos":   ["drive_path_x", "drive_path_y", ...],
        "total_matches":   42,
        "common_count":    150
    }
    """
    if tolerance is None:
        tolerance = settings.FACE_MATCH_TOLERANCE

    all_records = load_encodings()
    personal_photos = []
    common_photos = []

    for record in all_records:
        path = record["path"]
        is_common = record.get("is_common", False)

        # Common photos go to everyone regardless of face match
        if is_common:
            common_photos.append(path)
            continue

        # Compare guest face against all faces in this photo
        photo_encodings = record.get("encodings", [])
        if not photo_encodings:
            continue

        matches = face_recognition.compare_faces(
            photo_encodings,
            guest_encoding,
            tolerance=tolerance
        )

        if any(matches):
            personal_photos.append(path)

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
            "message": "We couldn't detect a face in your photo. Please try again in good lighting."
        }

    # Step 2 — match against all wedding photos
    results = find_matching_photos(guest_encoding)

    if results["total_matches"] == 0 and results["common_count"] == 0:
        return {
            "success": False,
            "error": "no_matches",
            "message": "We couldn't find you in the wedding photos. Please try a clearer selfie."
        }

    return {
        "success": True,
        **results
    }