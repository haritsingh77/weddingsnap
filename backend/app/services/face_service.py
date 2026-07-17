"""
Face matching service.
Loads precomputed encodings and matches guest selfies against them.

Supports:
  - InsightFace ArcFace (512-d, cosine) — GPU preprocess pipeline
  - dlib / face_recognition (128-d, L2) — legacy
"""

import pickle
import logging
from pathlib import Path
from typing import Optional, List
from functools import lru_cache

import numpy as np
from PIL import Image, ImageOps
import io

from app.config import settings

log = logging.getLogger(__name__)

_active_backend: Optional[str] = None


def _get_active_backend(records: list) -> str:
    global _active_backend
    if _active_backend:
        return _active_backend
    try:
        from scripts.face_engine.matching import detect_backend_from_records, load_encodings_meta
        meta = load_encodings_meta(Path(settings.ENCODINGS_CACHE_PATH).parent)
        if meta.get("backend"):
            _active_backend = meta["backend"]
            return _active_backend
    except Exception:
        pass
    try:
        from scripts.face_engine.matching import detect_backend_from_records
        _active_backend = detect_backend_from_records(records)
    except Exception:
        _active_backend = "dlib"
    return _active_backend


def _match_tolerance(backend: str) -> float:
    if backend == "insightface":
        return settings.ARCFACE_MATCH_TOLERANCE
    return settings.FACE_MATCH_TOLERANCE


@lru_cache(maxsize=1)
def load_encodings() -> list[dict]:
    from app.services.drive_cache import get_cached_file

    data_bytes = get_cached_file("face_encodings.pkl")
    if not data_bytes:
        cache_path = Path(settings.ENCODINGS_CACHE_PATH)
        if cache_path.exists():
            log.info("Loading encodings from local path: %s", cache_path)
            with open(cache_path, "rb") as f:
                data = pickle.load(f)
            log.info("Loaded %s photo records", f"{len(data):,}")
            return data
        log.warning("No face_encodings.pkl found.")
        return []

    log.info("Loading encodings from Supabase Storage...")
    data = pickle.loads(data_bytes)
    log.info("Loaded %s photo records", f"{len(data):,}")
    return data


def encode_selfie(image_bytes: bytes) -> Optional[np.ndarray]:
    """Encode guest selfie using the same backend as preprocessed encodings."""
    records = load_encodings()
    backend = _get_active_backend(records)

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img = ImageOps.exif_transpose(img)
        w, h = img.size
        if max(w, h) > 1000:
            scale = 1000 / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        img_array = np.array(img)

        if backend == "insightface":
            from scripts.face_engine.pipeline import get_pipeline
            from scripts.face_engine.config import PreprocessConfig
            pipeline = get_pipeline(PreprocessConfig())
            detections = pipeline.backend.detect_and_encode(img_array)
            if not detections:
                return None
            largest = max(
                detections,
                key=lambda d: (d.bbox[2] - d.bbox[0]) * (d.bbox[3] - d.bbox[1]),
            )
            # Stricter quality gate for enrollment: a weak reference selfie
            # degrades every subsequent match. Require a confident, reasonably
            # sized face. (Gallery detection uses a looser 0.65 gate.)
            import os
            min_score = float(os.getenv("SELFIE_MIN_DET_SCORE", "0.72"))
            min_px = int(os.getenv("SELFIE_MIN_FACE_PX", "110"))
            face_w = largest.bbox[2] - largest.bbox[0]
            face_h = largest.bbox[3] - largest.bbox[1]
            if largest.det_score < min_score or min(face_w, face_h) < min_px:
                log.info(
                    "Selfie rejected: low quality (score=%.2f, size=%dx%d)",
                    largest.det_score, face_w, face_h,
                )
                return None
            return largest.encoding

        import face_recognition
        locations = face_recognition.face_locations(img_array, model="cnn")
        if not locations:
            locations = face_recognition.face_locations(img_array, model="hog")
        if not locations:
            return None
        if len(locations) > 1:
            largest = max(locations, key=lambda loc: (loc[2] - loc[0]) * (loc[1] - loc[3]))
            locations = [largest]
        encodings = face_recognition.face_encodings(img_array, locations, num_jitters=20)
        return encodings[0] if encodings else None

    except Exception as e:
        log.error("Failed to encode selfie: %s", e)
        return None


def encode_multiple_selfies(images_bytes: List[bytes]) -> List[np.ndarray]:
    encodings = []
    for idx, img_bytes in enumerate(images_bytes):
        enc = encode_selfie(img_bytes)
        if enc is not None:
            encodings.append(enc)
            log.info("Selfie %d/%d: encoded", idx + 1, len(images_bytes))
        else:
            log.warning("Selfie %d/%d: no face", idx + 1, len(images_bytes))
    return encodings


def compute_confidence(distance: float, tolerance: float, backend: str = "dlib") -> float:
    try:
        from scripts.face_engine.matching import compute_confidence as _cc
        return _cc(distance, tolerance, backend)
    except Exception:
        return round(max(0.0, min(100.0, (1.0 - distance) * 100)), 1)


@lru_cache(maxsize=1)
def get_flat_encodings():
    all_records = load_encodings()
    backend = _get_active_backend(all_records)
    flat_encodings = []
    paths = []

    for record in all_records:
        if record.get("is_common", False):
            continue
        for enc in record.get("encodings", []):
            flat_encodings.append(enc)
            paths.append(record["path"])

    if not flat_encodings:
        return np.empty((0, 128)), [], backend

    return np.array(flat_encodings), paths, backend


def _aggregate_guest_encodings(
    guest_encodings: List[np.ndarray], backend: str
) -> List[np.ndarray]:
    """
    Combine a guest's multiple selfie embeddings into the query set used for matching.

    Modes (env SELFIE_AGGREGATE):
      - "centroid": mean of L2-normalized embeddings, renormalized → one robust
        template. Best precision on look-alikes; a single bad angle can't fire
        a false match on its own. Default for ArcFace.
      - "min": keep every embedding, best (closest) angle wins. Highest recall,
        lowest precision. Original behaviour.
      - "both": centroid + all individual embeddings (union). Recall-leaning.

    dlib (128-d L2) always uses "min" — its embeddings don't average cleanly.
    """
    import os

    if len(guest_encodings) <= 1 or backend != "insightface":
        return guest_encodings

    mode = os.getenv("SELFIE_AGGREGATE", "centroid").lower()
    if mode == "min":
        return guest_encodings

    stacked = np.array(guest_encodings, dtype=np.float64)
    unit = stacked / (np.linalg.norm(stacked, axis=1, keepdims=True) + 1e-8)
    centroid = unit.mean(axis=0)
    centroid = centroid / (np.linalg.norm(centroid) + 1e-8)

    if mode == "both":
        return [centroid, *guest_encodings]
    return [centroid]


# ── Phase 1: in-database ANN matching (pgvector) ─────────────────────────────

_db_faces_populated = False  # sticky once true; re-checked while false


def _db_match_available() -> bool:
    """True when the faces table exists and has rows (migration run + synced)."""
    global _db_faces_populated
    if _db_faces_populated:
        return True
    try:
        from app.database import supabase
        res = supabase.table("faces").select("id", count="exact").limit(1).execute()
        if (res.count or 0) > 0:
            _db_faces_populated = True
    except Exception as e:
        log.debug(f"faces table unavailable, using pkl matching: {e}")
    return _db_faces_populated


def _find_matching_photos_db(
    guest_encodings: List[np.ndarray], tolerance: float
) -> dict:
    """
    ANN matching via the match_faces() RPC (HNSW cosine search in Postgres).
    Replaces the O(faces × selfies) Python loop. Returns filenames, so the
    downstream resolve_drive_ids() flow is unchanged.
    """
    from app.database import supabase

    best: dict[str, float] = {}
    for enc in guest_encodings:
        rows = supabase.rpc(
            "match_faces",
            {"q": np.asarray(enc, dtype=float).tolist(), "k": 1000},
        ).execute()
        for row in rows.data or []:
            d = float(row["distance"])
            if d <= tolerance:
                fname = row["filename"]
                if fname not in best or d < best[fname]:
                    best[fname] = d

    sorted_personal = sorted(best.items(), key=lambda x: x[1])
    personal_photos = [f for f, _ in sorted_personal]
    confidence_map = {
        f: compute_confidence(d, tolerance, "insightface") for f, d in sorted_personal
    }

    common_res = (
        supabase.table("photos")
        .select("filename")
        .eq("is_common", True)
        .not_.is_("filename", "null")
        .execute()
    )
    common_photos = [r["filename"] for r in common_res.data or []]

    return {
        "personal_photos": personal_photos,
        "common_photos": common_photos,
        "total_matches": len(personal_photos),
        "common_count": len(common_photos),
        "confidence_map": confidence_map,
        "match_backend": "insightface",
    }


def find_matching_photos(
    guest_encodings: List[np.ndarray],
    tolerance: float = None,
) -> dict:
    all_records = load_encodings()
    backend = _get_active_backend(all_records)
    if tolerance is None:
        tolerance = _match_tolerance(backend)

    angles_captured = len(guest_encodings)
    guest_encodings = _aggregate_guest_encodings(guest_encodings, backend)

    # Prefer in-database ANN matching when faces are synced (512-d ArcFace only).
    if backend == "insightface" and _db_match_available():
        try:
            result = _find_matching_photos_db(guest_encodings, tolerance)
            result["selfie_angles_used"] = angles_captured
            log.info(
                "DB match — %d personal, %d common (pgvector)",
                result["total_matches"], result["common_count"],
            )
            return result
        except Exception as e:
            log.warning(f"DB matching failed, falling back to pkl scan: {e}")

    common_photos = [r["path"] for r in all_records if r.get("is_common", False)]
    personal_matches: dict[str, dict] = {}
    flat_encs, paths, enc_backend = get_flat_encodings()

    if len(flat_encs) > 0 and len(guest_encodings) > 0:
        try:
            from scripts.face_engine.matching import embedding_distance
            min_distances = np.full(len(flat_encs), np.inf)
            for guest_enc in guest_encodings:
                dists = np.array([
                    embedding_distance(flat_encs[i], guest_enc, enc_backend)
                    for i in range(len(flat_encs))
                ])
                min_distances = np.minimum(min_distances, dists)
        except Exception:
            min_distances = np.min(
                np.array([np.linalg.norm(flat_encs - g, axis=1) for g in guest_encodings]),
                axis=0,
            )

        for idx in np.where(min_distances <= tolerance)[0]:
            path = paths[idx]
            dist = float(min_distances[idx])
            if path not in personal_matches or dist < personal_matches[path]["distance"]:
                personal_matches[path] = {
                    "distance": dist,
                    "confidence": compute_confidence(dist, tolerance, enc_backend),
                }

    sorted_personal = sorted(
        personal_matches.items(), key=lambda x: x[1]["confidence"], reverse=True
    )
    personal_photos = [path for path, _ in sorted_personal]
    confidence_map = {path: meta["confidence"] for path, meta in sorted_personal}

    if sorted_personal:
        confidences = [m["confidence"] for _, m in sorted_personal]
        log.info(
            "Match — %d personal, %d common | backend=%s | avg conf %.1f%%",
            len(personal_photos),
            len(common_photos),
            enc_backend,
            sum(confidences) / len(confidences),
        )

    return {
        "personal_photos": personal_photos,
        "common_photos": common_photos,
        "total_matches": len(personal_photos),
        "common_count": len(common_photos),
        "confidence_map": confidence_map,
        "selfie_angles_used": angles_captured,
        "match_backend": enc_backend,
    }


def match_guest_selfie(
    image_bytes: bytes,
    tolerance: float = None,
    extra_selfie_bytes: List[bytes] = None,
) -> dict:
    all_selfie_bytes = [image_bytes]
    if extra_selfie_bytes:
        all_selfie_bytes.extend(extra_selfie_bytes)

    guest_encodings = encode_multiple_selfies(all_selfie_bytes)
    if not guest_encodings:
        return {
            "success": False,
            "error": "no_face_detected",
            "message": "We couldn't detect a face in your photo. Please try again in good lighting.",
        }

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
    log.info("Mapped %s files", f"{len(mapping):,}")
    return mapping


def resolve_drive_ids(local_paths: list[str]) -> list[str]:
    mapping = get_filename_map()
    drive_ids = []
    for path in local_paths:
        filename = Path(path).name
        if filename in mapping:
            drive_ids.append(mapping[filename])
        else:
            log.warning("No Drive ID for: %s", filename)
    return drive_ids


def associate_guest_by_name(guest_id: str, name: str) -> int:
    guest_name = name.strip().lower()
    if not guest_name:
        return 0

    from app.services.drive_cache import get_cached_json
    from app.database import supabase

    names_data = get_cached_json("cluster_names.json")
    if not names_data:
        return 0

    try:
        matching_cluster_ids = [
            cid for cid, cname in names_data.items() if cname.strip().lower() == guest_name
        ]
        if not matching_cluster_ids:
            return 0

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
            photos_to_upsert, on_conflict="drive_path"
        ).execute()

        if upserted.data:
            from app.services.face_state import get_disassociated_photo_ids
            disassociated_set = get_disassociated_photo_ids(guest_id)

            drive_to_id = {p["drive_path"]: p["id"] for p in upserted.data}
            photo_rows = []
            for drive_id in drive_ids:
                pid = drive_to_id.get(drive_id)
                if pid and pid not in disassociated_set:
                    photo_rows.append({"guest_id": guest_id, "photo_id": pid})
            if photo_rows:
                supabase.table("guest_photos").upsert(
                    photo_rows, on_conflict="guest_id,photo_id"
                ).execute()
                return len(photo_rows)
    except Exception as e:
        log.error("Auto-associate failed for '%s': %s", name, e)
    return 0
