"""
Embedding distance helpers — supports dlib (128-d L2) and InsightFace (512-d cosine).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np


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
    import os
    if backend == "insightface":
        return float(os.getenv("ARCFACE_MATCH_THRESHOLD", "0.4"))
    return float(os.getenv("DLIB_MATCH_THRESHOLD", "0.5"))


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
