"""
Abstract face backend interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

import numpy as np


@dataclass
class FaceDetection:
    """Single detected face."""
    bbox: tuple[int, int, int, int]  # left, top, right, bottom
    encoding: np.ndarray
    det_score: float = 1.0

    @property
    def location_dlib(self) -> tuple[int, int, int, int]:
        """(top, right, bottom, left) for legacy face_recognition consumers."""
        left, top, right, bottom = self.bbox
        return (top, right, bottom, left)


class FaceBackend(ABC):
    name: str = "base"
    embedding_dim: int = 128

    @abstractmethod
    def warmup(self) -> None:
        ...

    @abstractmethod
    def detect_and_encode(self, rgb_image: np.ndarray) -> List[FaceDetection]:
        ...

    def detect_and_encode_batch(
        self, rgb_images: List[np.ndarray]
    ) -> List[List[FaceDetection]]:
        """Default: sequential; GPU backends may override."""
        return [self.detect_and_encode(img) for img in rgb_images]

    @staticmethod
    def distance(a: np.ndarray, b: np.ndarray, backend_name: str) -> float:
        """Distance between two embeddings (lower = more similar)."""
        if backend_name == "insightface":
            a_n = a / (np.linalg.norm(a) + 1e-8)
            b_n = b / (np.linalg.norm(b) + 1e-8)
            return float(1.0 - np.dot(a_n, b_n))
        return float(np.linalg.norm(a - b))
