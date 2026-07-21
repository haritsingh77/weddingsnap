"""
Embedding distance helpers — supports dlib (128-d L2) and InsightFace (512-d cosine).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np


def drive_record_path(file_id: str, file_name: str) -> str:
    """Build the pkl record `path` for a Drive file.

    Filenames are NOT unique on Drive — 5,730 of 11,049 files share a basename
    with a DIFFERENT photo (two Sony bodies both emitted DSC0xxxx.JPG). Keying
    records on the basename alone made faces from one photo resolve to another
    photo's Drive file, so guests saw photos they weren't in and missed ones
    they were.

    The file id goes in the middle rather than the tail so that
    Path(path).name still returns the bare filename — every existing
    basename lookup keeps working, while the full path becomes unique.
    """
    return f"GoogleDrive/{file_id}/{file_name}"


def drive_id_from_path(path: str) -> Optional[str]:
    """Recover the Drive file id from a record path, or None for legacy
    'GoogleDrive/<name>' records written before the id was carried through."""
    parts = str(path).split("/")
    if len(parts) >= 3 and parts[0] == "GoogleDrive":
        return parts[-2]
    return None


def detect_backend_from_records(records: list[dict]) -> str:
    if not records:
        return "dlib"
    for r in records[:20]:
        if r.get("backend") == "insightface":
            return "insightface"
        if r.get("embedding_dim") == 512:
            return "insightface"
    return "dlib"


def load_encodings_meta(folder: Path) -> dict:
    path = folder / "encodings_meta.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def default_tolerance(backend: str) -> float:
    """Threshold for RETRIEVAL — matching a guest selfie against the gallery."""
    import os
    if backend == "insightface":
        return float(os.getenv("ARCFACE_MATCH_THRESHOLD", "0.5"))
    return float(os.getenv("DLIB_MATCH_THRESHOLD", "0.5"))


def cluster_tolerance(backend: str) -> float:
    """Threshold for CLUSTERING — grouping gallery faces into people.

    Deliberately separate from (and stricter than) the retrieval threshold.
    Retrieval compares one probe against the gallery, so a loose cut only
    costs a few wrong photos. Clustering joins faces transitively via
    union-find, so a single bridging pair welds two people together
    permanently. Measured on 936 photos: at 0.40 there were zero clusters
    containing two faces from the same photo (an impossible grouping, so a
    direct error signal); loosening the cut is what starts that chaining.
    """
    import os
    if backend == "insightface":
        return float(os.getenv("CLUSTER_THRESHOLD", "0.4"))
    return float(os.getenv("DLIB_CLUSTER_THRESHOLD", os.getenv("DLIB_MATCH_THRESHOLD", "0.5")))


def embedding_distance(a: np.ndarray, b: np.ndarray, backend: str) -> float:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if backend == "insightface":
        a_n = a / (np.linalg.norm(a) + 1e-8)
        b_n = b / (np.linalg.norm(b) + 1e-8)
        return float(1.0 - np.dot(a_n, b_n))
    return float(np.linalg.norm(a - b))


def compute_confidence(distance: float, tolerance: float, backend: str) -> float:
    if backend == "insightface":
        # cosine distance 0 = identical, tolerance ~0.4 good match
        return round(max(0.0, min(100.0, (1.0 - distance / max(tolerance, 1e-6)) * 100)), 1)
    return round(max(0.0, min(100.0, (1.0 - distance) * 100)), 1)
