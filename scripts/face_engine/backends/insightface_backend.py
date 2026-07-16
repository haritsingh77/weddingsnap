"""
InsightFace backend: RetinaFace detection + ArcFace embeddings via ONNX Runtime GPU.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

import numpy as np

from scripts.face_engine.backends.base import FaceBackend, FaceDetection

log = logging.getLogger(__name__)

_pipeline = None


class InsightFaceBackend(FaceBackend):
    name = "insightface"
    embedding_dim = 512

    def __init__(
        self,
        model_name: str = "buffalo_l",
        model_root: Path | None = None,
        det_size: int | None = None,
    ):
        self.model_name = model_name
        self.model_root = model_root
        # Fall back to env/default when the caller doesn't pass one explicitly.
        self.det_size = det_size or int(os.getenv("INSIGHTFACE_DET_SIZE", "1024"))

    def warmup(self) -> None:
        global _pipeline
        if _pipeline is not None:
            return

        try:
            from insightface.app import FaceAnalysis
        except ImportError as e:
            raise RuntimeError(
                "insightface not installed. Run: pip install -r requirements-preprocess.txt"
            ) from e

        providers = self._select_providers()
        root = str(self.model_root) if self.model_root else None
        if root:
            os.environ.setdefault("INSIGHTFACE_HOME", root)

        log.info("Loading InsightFace model '%s' with providers: %s", self.model_name, providers)
        app = FaceAnalysis(name=self.model_name, providers=providers)
        # Larger det_size recovers small faces in group photos (see config.det_size).
        app.prepare(
            ctx_id=0 if "CUDA" in str(providers) else -1,
            det_size=(self.det_size, self.det_size),
        )
        log.info("InsightFace det_size=%dx%d", self.det_size, self.det_size)
        _pipeline = app
        log.info("InsightFace ready (embedding dim=%d)", self.embedding_dim)

    @staticmethod
    def _select_providers() -> list:
        try:
            import onnxruntime as ort
            available = ort.get_available_providers()
        except ImportError:
            return ["CPUExecutionProvider"]

        if "CUDAExecutionProvider" in available:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"]
        if "DmlExecutionProvider" in available:
            return ["DmlExecutionProvider", "CPUExecutionProvider"]
        return ["CPUExecutionProvider"]

    def detect_and_encode(self, rgb_image: np.ndarray) -> List[FaceDetection]:
        global _pipeline
        if _pipeline is None:
            self.warmup()

        faces = _pipeline.get(rgb_image)
        detections: List[FaceDetection] = []
        for face in faces:
            if face.embedding is None:
                continue
            bbox = face.bbox.astype(int)
            left, top, right, bottom = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
            
            # ── Filter out small background faces & low-score detections ──────
            width = right - left
            height = bottom - top
            score = float(getattr(face, "det_score", 1.0))
            if score < 0.65 or width < 75 or height < 75:
                continue

            detections.append(
                FaceDetection(
                    bbox=(left, top, right, bottom),
                    encoding=np.asarray(face.embedding, dtype=np.float32),
                    det_score=score,
                )
            )
        return detections
