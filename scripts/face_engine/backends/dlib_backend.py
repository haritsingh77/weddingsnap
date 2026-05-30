"""
dlib / face_recognition fallback backend (CPU or CUDA dlib if compiled).
"""

from __future__ import annotations

import logging
from typing import List

import numpy as np

from scripts.face_engine.backends.base import FaceBackend, FaceDetection

log = logging.getLogger(__name__)


class DlibBackend(FaceBackend):
    name = "dlib"
    embedding_dim = 128

    def __init__(self, model: str = "cnn"):
        self.model = model

    def warmup(self) -> None:
        import face_recognition
        import dlib

        cuda = getattr(dlib, "DLIB_USE_CUDA", False)
        log.info(
            "dlib backend ready (model=%s, CUDA=%s, dim=%d)",
            self.model,
            cuda,
            self.embedding_dim,
        )
        # Touch library
        _ = face_recognition

    def detect_and_encode(self, rgb_image: np.ndarray) -> List[FaceDetection]:
        import face_recognition

        locations = face_recognition.face_locations(rgb_image, model=self.model)
        if not locations:
            return []

        encodings = face_recognition.face_encodings(rgb_image, locations, num_jitters=1)
        detections: List[FaceDetection] = []
        for (top, right, bottom, left), enc in zip(locations, encodings):
            detections.append(
                FaceDetection(
                    bbox=(left, top, right, bottom),
                    encoding=np.asarray(enc, dtype=np.float64),
                    det_score=1.0,
                )
            )
        return detections
