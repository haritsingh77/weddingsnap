"""
2-stage face pipeline: resize → fast detect → embed.
Image/video helpers, batch runner, memory cleanup.
"""

from __future__ import annotations

import gc
import logging
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np
from PIL import Image, ImageOps

from scripts.face_engine.backends.base import FaceBackend, FaceDetection
from scripts.face_engine.backends.dlib_backend import DlibBackend
from scripts.face_engine.backends.insightface_backend import InsightFaceBackend
from scripts.face_engine.config import PreprocessConfig
from scripts.face_engine.gpu import configure_onnx_memory, log_device_info

log = logging.getLogger(__name__)

SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
SUPPORTED_EXTENSIONS = SUPPORTED_IMAGE_EXTENSIONS | SUPPORTED_VIDEO_EXTENSIONS


def sort_media_priority(files: list[Path]) -> list[Path]:
    """Images first, then videos by ascending file size."""
    images, videos = [], []
    for f in files:
        if f.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS:
            videos.append(f)
        else:
            images.append(f)
    images.sort(key=lambda p: p.name.lower())
    videos.sort(key=lambda p: p.stat().st_size if p.exists() else 0)
    return images + videos


_pipeline_singleton: Optional["FacePipeline"] = None


def get_pipeline(config: Optional[PreprocessConfig] = None) -> "FacePipeline":
    global _pipeline_singleton
    if _pipeline_singleton is None:
        _pipeline_singleton = FacePipeline(config or PreprocessConfig())
    return _pipeline_singleton


class FacePipeline:
    def __init__(self, config: PreprocessConfig):
        self.config = config
        self.config.ensure_dirs()
        configure_onnx_memory(config.gpu_mem_fraction)
        self.device_info = log_device_info()
        self.backend: FaceBackend = self._create_backend()
        self.backend.warmup()

    def _create_backend(self) -> FaceBackend:
        choice = self.config.backend
        if choice == "auto":
            try:
                backend = InsightFaceBackend(
                    model_name=self.config.insightface_model,
                    model_root=self.config.model_cache_dir,
                )
                backend.warmup()
                return backend
            except Exception as e:
                log.warning("InsightFace unavailable (%s), falling back to dlib", e)
                choice = "dlib"

        if choice == "insightface":
            return InsightFaceBackend(
                model_name=self.config.insightface_model,
                model_root=self.config.model_cache_dir,
            )
        if choice == "dlib":
            return DlibBackend(model=self.config.dlib_model)
        raise ValueError(f"Unknown FACE_BACKEND: {choice}")

    @property
    def backend_name(self) -> str:
        return self.backend.name

    @property
    def model_label(self) -> str:
        if self.backend_name == "insightface":
            return f"insightface/{self.config.insightface_model}"
        return self.config.dlib_model

    def load_rgb_image(self, path: Path) -> Optional[np.ndarray]:
        try:
            img = Image.open(path).convert("RGB")
            img = ImageOps.exif_transpose(img)
            arr = np.array(img, dtype=np.uint8)
            del img
            return self.resize_rgb(arr)
        except Exception as e:
            log.warning("Skipped %s: %s", path.name, e)
            return None

    def resize_rgb(self, rgb: np.ndarray) -> np.ndarray:
        h, w = rgb.shape[:2]
        max_dim = self.config.max_image_dimension
        if max(h, w) <= max_dim:
            return rgb
        scale = max_dim / max(h, w)
        new_w, new_h = int(w * scale), int(h * scale)
        return cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)

    def _detections_to_record(
        self,
        path: Path,
        detections: List[FaceDetection],
        extra: Optional[dict] = None,
    ) -> Optional[dict]:
        if not detections:
            return None

        locations = [d.location_dlib for d in detections]
        encodings = [d.encoding for d in detections]
        face_count = len(detections)

        common_keywords = {"venue", "decor", "ceremony", "stage", "mandap", "common"}
        is_common = (
            face_count >= self.config.group_photo_threshold
            or any(kw in path.parent.name.lower() for kw in common_keywords)
        )

        record = {
            "path": str(path),
            "locations": locations,
            "encodings": encodings,
            "face_count": face_count,
            "is_common": is_common,
            "detection_model": self.model_label,
            "backend": self.backend_name,
            "embedding_dim": self.backend.embedding_dim,
        }
        if extra:
            record.update(extra)
        return record

    def encode_photo(self, path: Path) -> Optional[dict]:
        rgb = self.load_rgb_image(path)
        if rgb is None:
            return None
        try:
            detections = self.backend.detect_and_encode(rgb)
            return self._detections_to_record(path, detections)
        finally:
            del rgb

    @staticmethod
    def _laplacian_variance(gray: np.ndarray) -> float:
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    @staticmethod
    def _histogram_similarity(a: np.ndarray, b: np.ndarray) -> float:
        hist_a = cv2.calcHist([a], [0, 1, 2], None, [8, 8, 8], [0, 256] * 3)
        hist_b = cv2.calcHist([b], [0, 1, 2], None, [8, 8, 8], [0, 256] * 3)
        cv2.normalize(hist_a, hist_a)
        cv2.normalize(hist_b, hist_b)
        return float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL))

    def encode_video(self, path: Path) -> Optional[dict]:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            log.warning("Could not open video: %s", path.name)
            return None

        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            interval_frames = max(1, int(fps * self.config.video_sample_interval_sec))
            unique_samples: list[tuple] = []
            max_faces_in_frame = 0
            last_hist: Optional[np.ndarray] = None
            frame_idx = 0

            while True:
                ret, frame = cap.read()
                if not ret:
                    break

                if frame_idx % interval_frames != 0:
                    frame_idx += 1
                    continue

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                rgb = self.resize_rgb(rgb)

                gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
                if self._laplacian_variance(gray) < self.config.video_blur_threshold:
                    frame_idx += 1
                    continue

                if last_hist is not None:
                    sim = self._histogram_similarity(rgb, last_hist)
                    if sim >= self.config.video_duplicate_hist_threshold:
                        frame_idx += 1
                        continue
                last_hist = rgb.copy()

                detections = self.backend.detect_and_encode(rgb)
                if detections:
                    max_faces_in_frame = max(max_faces_in_frame, len(detections))
                    for det in detections:
                        if not unique_samples:
                            unique_samples.append((det, frame_idx))
                            continue
                        existing = [s[0].encoding for s in unique_samples]
                        dists = [
                            self.backend.distance(det.encoding, e, self.backend_name)
                            for e in existing
                        ]
                        threshold = (
                            self.config.arcface_match_threshold
                            if self.backend_name == "insightface"
                            else self.config.dlib_match_threshold
                        )
                        if not any(d < threshold for d in dists):
                            unique_samples.append((det, frame_idx))

                del rgb, gray
                frame_idx += 1

            if not unique_samples:
                return None

            common_keywords = {"venue", "decor", "ceremony", "stage", "mandap", "common"}
            is_common = (
                max_faces_in_frame >= self.config.group_photo_threshold
                or any(kw in path.parent.name.lower() for kw in common_keywords)
            )

            return {
                "path": str(path),
                "locations": [s[0].location_dlib for s in unique_samples],
                "encodings": [s[0].encoding for s in unique_samples],
                "frame_indices": [s[1] for s in unique_samples],
                "face_count": max_faces_in_frame,
                "is_common": is_common,
                "detection_model": self.model_label,
                "backend": self.backend_name,
                "embedding_dim": self.backend.embedding_dim,
            }
        except Exception as e:
            log.error("Failed to encode video %s: %s", path.name, e)
            return None
        finally:
            cap.release()

    def encode_photo_batch(self, paths: List[Path]) -> List[Optional[dict]]:
        """Batch images: load → detect → release memory between items."""
        images: List[np.ndarray] = []
        valid_paths: List[Path] = []

        for p in paths:
            rgb = self.load_rgb_image(p)
            if rgb is not None:
                images.append(rgb)
                valid_paths.append(p)

        results: List[Optional[dict]] = [None] * len(paths)
        path_to_idx = {p: i for i, p in enumerate(paths)}

        if not images:
            return results

        batch_detections = self.backend.detect_and_encode_batch(images)
        for p, dets in zip(valid_paths, batch_detections):
            idx = path_to_idx[p]
            results[idx] = self._detections_to_record(p, dets)

        del images
        gc.collect()
        return results

    def maybe_gc(self, file_counter: int) -> None:
        if file_counter > 0 and file_counter % self.config.gc_every_n_files == 0:
            gc.collect()
