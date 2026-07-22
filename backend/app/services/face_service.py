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

    # 1. Explicit override (FACE_BACKEND=insightface|dlib) — set this on Railway.
    if settings.FACE_BACKEND in ("insightface", "dlib"):
        _active_backend = settings.FACE_BACKEND
        return _active_backend

    # 2. Populated faces table implies 512-d ArcFace — required on hosted
    #    deploys where no pkl ships in the image, so record sniffing would
    #    wrongly fall back to dlib and encode selfies at the wrong dimension.
    try:
        if _db_match_available():
            _active_backend = "insightface"
            return _active_backend
    except Exception:
        pass

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
        # Match the size the gallery was detected at. Shrinking to 1000 first
        # found only 5 of 12 sampled faces; at 2048 all 12 were found, median
        # detection score 0.838 — so most "we couldn't detect a face" rejections
        # were pixels being thrown away, not bad photos, and no threshold change
        # would have fixed them.
        #
        # Matching the preprocessing size also keeps enrolment and gallery
        # detection consistent, so a selfie is measured the same way as the
        # photos it will be compared against.
        import os
        max_dim = int(os.getenv("SELFIE_MAX_DIMENSION", "2048"))
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        img_array = np.array(img)

        if backend == "insightface":
            try:
                from scripts.face_engine.pipeline import get_pipeline
                from scripts.face_engine.config import PreprocessConfig
            except ImportError as e:
                # Expected on the deployed server: InsightFace, ONNX and the
                # encodings are ~1.8 GB resident and only needed to enrol a NEW
                # face. Guests use per-guest links instead, so recognition runs
                # on the machine with the GPU and the result is synced to the
                # database. Fail clearly rather than with a bare ImportError.
                log.warning("Face recognition unavailable in this deployment: %s", e)
                raise RuntimeError(
                    "Face recognition is not installed on this server. Run the "
                    "preprocessing locally and sync the result instead."
                ) from e
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
            # Calibrated against real photos rather than picked by feel. At 0.72
            # / 110px the gate rejected a 0.891-confidence face for being 3px
            # short, and a large 280x403 face for 0.017 of score — 6 of 12
            # sampled photos, none of them actually bad.
            #
            # 80px is the meaningful floor: ArcFace normalises every face to
            # 112x112, so ~110px is already native resolution and 80px is only a
            # mild upscale. Below that there genuinely isn't enough detail for a
            # reference selfie, which is what this gate is for.
            import os
            min_score = float(os.getenv("SELFIE_MIN_DET_SCORE", "0.65"))
            min_px = int(os.getenv("SELFIE_MIN_FACE_PX", "80"))
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

    # NOTE: is_common records are deliberately INCLUDED here.
    #
    # is_common is set by the preprocessor for any photo with >= 4 faces
    # (group_photo_threshold). Skipping those meant a guest was never
    # personally matched in any group shot — measured at 34% of photos with
    # buffalo_l and 47% with buffalo_s. Those are precisely the photos guests
    # most want to find themselves in, and they were only ever reachable via
    # the undifferentiated "everyone sees these" common bucket.
    #
    # is_common now controls PRESENTATION only (also shown to all guests);
    # it no longer suppresses personal matching.
    for record in all_records:
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

    from collections import Counter

    from scripts.face_engine.matching import drive_record_path

    def _key(row: dict) -> str:
        """Identify a hit by Drive id when the RPC provides one.

        Filenames are not unique on Drive, so keying on filename merges
        distinct photos. If the match_faces() SQL function hasn't been updated
        to select drive_id, this degrades to the old (ambiguous) filename key.
        """
        drive_id = row.get("drive_id")
        fname = row.get("filename") or ""
        return drive_record_path(drive_id, fname) if drive_id else fname

    best: dict[str, float] = {}
    # cluster of each MATCHED face, straight from the RPC — see _expand_via_clusters
    cluster_hits: Counter = Counter()
    missing_drive_id = False
    for enc in guest_encodings:
        rows = supabase.rpc(
            "match_faces",
            {"q": np.asarray(enc, dtype=float).tolist(), "k": 1000},
        ).execute()
        for row in rows.data or []:
            d = float(row["distance"])
            if d <= tolerance:
                if not row.get("drive_id"):
                    missing_drive_id = True
                key = _key(row)
                if key not in best or d < best[key]:
                    best[key] = d
                if row.get("cluster_id") is not None:
                    cluster_hits[row["cluster_id"]] += 1

    if missing_drive_id:
        log.warning(
            "match_faces() returned rows without drive_id — falling back to "
            "filename keys, which collide for ~52%% of this corpus. Update the "
            "RPC to also select faces.drive_id."
        )

    best = _expand_via_clusters(supabase, best, tolerance, cluster_hits)

    sorted_personal = sorted(best.items(), key=lambda x: x[1])
    personal_photos = [f for f, _ in sorted_personal]
    confidence_map = {
        f: compute_confidence(d, tolerance, "insightface") for f, d in sorted_personal
    }

    # drive_path holds the Drive file id (see sync_encodings_to_db.sync_faces),
    # so build the same unambiguous record path the pkl path produces.
    #
    # Paged deliberately: PostgREST caps a single select at 1000 rows and does
    # so silently, which capped the group photos every guest saw at exactly
    # 1000 out of 3,682 — about two thirds of them simply missing, with no error.
    common_rows, offset = [], 0
    while True:
        page = (
            supabase.table("photos")
            .select("drive_path, filename")
            .eq("is_common", True)
            .not_.is_("filename", "null")
            .range(offset, offset + 999)
            .execute()
        ).data or []
        common_rows.extend(page)
        if len(page) < 1000:
            break
        offset += 1000

    common_photos = [
        drive_record_path(r["drive_path"], r["filename"]) if r.get("drive_path")
        else r["filename"]
        for r in common_rows
    ]

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
            # Says what to change rather than blaming the light. The gate also
            # fires on a face that WAS found but is small or low-confidence, and
            # "try better lighting" sends someone to re-shoot a photo that was
            # fine — the actual fix is usually to get closer or crop in.
            "message": (
                "We couldn't get a clear enough face from that photo. "
                "Try one where your face is larger and looking towards the "
                "camera — a close-up selfie works best."
            ),
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


def _expand_via_clusters(supabase, best: dict[str, float], tolerance: float,
                         cluster_hits: "Counter | None" = None) -> dict[str, float]:
    """Add the rest of a cluster once the selfie clearly belongs to it.

    A selfie is one photo, so direct matching only finds faces within `tolerance`
    of that single shot. Clustering is transitive — A~B and B~C puts all three
    together — so a person's cluster legitimately spans angles and lighting no
    single selfie is close to. Measured: a selfie matched 395 photos while that
    person's cluster held 1,654.

    So: when enough matched faces land in one cluster, take the whole cluster.
    The support threshold matters because clusters can be impure — requiring a
    real number of independent hits, not one lucky face, keeps a stray match
    from dragging in a stranger's entire album.
    """
    import os

    from scripts.face_engine.matching import drive_record_path

    min_hits = int(os.getenv("CLUSTER_EXPAND_MIN_HITS", "5"))
    if not best or min_hits <= 0 or not cluster_hits:
        return best

    try:
        # Only clusters the MATCHED faces belong to. Counting by photo instead
        # would credit every face in the frame, so anyone standing next to the
        # guest would have their whole cluster pulled in (395 -> 7,972 photos).
        strong = [c for c, n in cluster_hits.items() if n >= min_hits]
        if not strong:
            return best

        added = 0
        for cid in strong:
            rows, offset = [], 0
            while True:
                page = (
                    supabase.table("faces")
                    .select("drive_id, filename")
                    .eq("cluster_id", cid)
                    .range(offset, offset + 999)
                    .execute()
                ).data or []
                rows.extend(page)
                if len(page) < 1000:
                    break
                offset += 1000
            for r in rows:
                if not r.get("drive_id") or not r.get("filename"):
                    continue
                key = drive_record_path(r["drive_id"], r["filename"])
                if key not in best:
                    # Just inside tolerance: a real match, but ranked below
                    # everything the selfie matched directly.
                    best[key] = tolerance
                    added += 1

        log.info(
            "Cluster expansion: %d cluster(s) with >=%d hits -> +%d photos (%d total)",
            len(strong), min_hits, added, len(best),
        )
    except Exception as e:
        log.warning("Cluster expansion skipped: %s", e)

    return best


def resolve_one_drive_id(path: str, default: str = "") -> str:
    """Single-path variant of resolve_drive_ids. Prefers the id embedded in the
    record path; falls back to the (ambiguous) basename map for legacy records."""
    from scripts.face_engine.matching import drive_id_from_path

    drive_id = drive_id_from_path(path)
    if drive_id:
        return drive_id
    return get_filename_map().get(Path(path).name, default)


def resolve_drive_ids(local_paths: list[str]) -> list[str]:
    """Map record paths to Drive file ids.

    Record paths written after the drive-id fix embed the id
    ('GoogleDrive/<id>/<name>'), so they resolve exactly. Legacy records only
    have a basename, and basenames are not unique on Drive — over half the
    corpus shares one with a different photo — so that path falls back to the
    name map and is logged as ambiguous.
    """
    from scripts.face_engine.matching import drive_id_from_path

    mapping = get_filename_map()
    drive_ids = []
    ambiguous = 0
    for path in local_paths:
        drive_id = drive_id_from_path(path)
        if drive_id:
            drive_ids.append(drive_id)
            continue
        filename = Path(path).name
        if filename in mapping:
            drive_ids.append(mapping[filename])
            ambiguous += 1
        else:
            log.warning("No Drive ID for: %s", filename)
    if ambiguous:
        log.warning(
            "%d path(s) resolved by basename (legacy pkl) — may point at the "
            "wrong photo where filenames collide; re-preprocess to fix.",
            ambiguous,
        )
    return drive_ids


def associate_guest_by_name(guest_id: str, name: str) -> int:
    """Attach photos from any identically-named face cluster to this guest.

    Matching on name is inherently ambiguous: two guests called "Ravi Singh"
    both match a cluster named "Ravi Singh", and both would receive the other's
    photos. Auth now keeps such guests as separate records, but this lookup
    still can't tell them apart, so it declines to guess and logs instead —
    those guests get photos from selfie matching rather than by name.
    """
    guest_name = name.strip().lower()
    if not guest_name:
        return 0

    from app.services.drive_cache import get_cached_json
    from app.database import supabase

    try:
        escaped = name.strip().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        same_name = (
            supabase.table("guests").select("id").ilike("name", escaped).execute()
        ).data or []
        if len(same_name) > 1:
            log.warning(
                "Skipping name-association for %r — %d guests share this name, so "
                "cluster photos cannot be attributed by name without mixing albums.",
                name, len(same_name),
            )
            return 0
    except Exception as e:
        log.debug("Could not check for duplicate guest names: %s", e)

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

        member_paths = []
        for cid in matching_cluster_ids:
            if cid in clusters:
                member_paths.extend(clusters[cid]["photos"])

        if not member_paths:
            return 0

        # Resolve on the full record path, not the basename — over half the
        # corpus shares a basename with a different photo, so a name lookup
        # would associate the guest with photos they aren't in.
        drive_ids = resolve_drive_ids(member_paths)
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
