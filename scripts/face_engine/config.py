"""
Preprocessor configuration — tunable via environment variables.
"""

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path


def _default_ssd_root() -> Path:
    """Prefer explicit SSD path; fall back to project cache on drive."""
    env = os.getenv("WEDDINGSNAP_SSD_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if platform.system() == "Windows":
        for candidate in [
            Path(os.getenv("LOCALAPPDATA", "")) / "weddingsnap",
            Path("C:/weddingsnap_cache"),
            Path(__file__).resolve().parents[2] / "cache_ssd",
        ]:
            if candidate and str(candidate.parent):
                return candidate.resolve()
    return Path(__file__).resolve().parents[2] / "cache_ssd"


@dataclass
class PreprocessConfig:
    # ── Storage (SSD) ─────────────────────────────────────────────────────────
    ssd_root: Path = field(default_factory=_default_ssd_root)
    temp_dir_name: str = "temp_preprocess"
    model_cache_dir_name: str = "models"
    per_file_cache_dir_name: str = "per_file_cache"

    # ── Backend ───────────────────────────────────────────────────────────────
    # insightface | dlib | auto (try insightface, fall back to dlib)
    backend: str = field(
        default_factory=lambda: os.getenv("FACE_BACKEND", "auto").lower()
    )
    insightface_model: str = field(
        default_factory=lambda: os.getenv("INSIGHTFACE_MODEL", "buffalo_s")
    )
    # hog | cnn — only used when backend is dlib
    dlib_model: str = field(
        default_factory=lambda: os.getenv("DLIB_MODEL", "cnn")
    )

    # ── Image ─────────────────────────────────────────────────────────────────
    max_image_dimension: int = field(
        default_factory=lambda: int(os.getenv("MAX_IMAGE_DIMENSION", "1600"))
    )

    # ── Video ─────────────────────────────────────────────────────────────────
    video_sample_interval_sec: float = field(
        default_factory=lambda: float(os.getenv("VIDEO_SAMPLE_INTERVAL_SEC", "2.5"))
    )
    video_blur_threshold: float = field(
        default_factory=lambda: float(os.getenv("VIDEO_BLUR_THRESHOLD", "80.0"))
    )
    video_duplicate_hist_threshold: float = field(
        default_factory=lambda: float(os.getenv("VIDEO_DUPLICATE_HIST_THRESHOLD", "0.92"))
    )

    # ── Batching & concurrency ──────────────────────────────────────────────────
    batch_size: int = field(
        default_factory=lambda: int(os.getenv("PREPROCESS_BATCH_SIZE", "4"))
    )
    max_workers: int = field(
        default_factory=lambda: int(os.getenv("PREPROCESS_MAX_WORKERS", "1"))
    )
    checkpoint_every: int = field(
        default_factory=lambda: int(os.getenv("PREPROCESS_CHECKPOINT_EVERY", "25"))
    )

    # ── Memory / stability ────────────────────────────────────────────────────
    gc_every_n_files: int = field(
        default_factory=lambda: int(os.getenv("PREPROCESS_GC_EVERY", "10"))
    )
    gpu_mem_fraction: float = field(
        default_factory=lambda: float(os.getenv("GPU_MEM_FRACTION", "0.85"))
    )
    thermal_pause_sec: float = field(
        default_factory=lambda: float(os.getenv("THERMAL_PAUSE_SEC", "30"))
    )
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("PREPROCESS_MAX_RETRIES", "2"))
    )

    # ── Matching metadata ─────────────────────────────────────────────────────
    group_photo_threshold: int = 4
    # ArcFace cosine distance threshold (InsightFace)
    arcface_match_threshold: float = field(
        default_factory=lambda: float(os.getenv("ARCFACE_MATCH_THRESHOLD", "0.4"))
    )
    # dlib L2 threshold (legacy)
    dlib_match_threshold: float = field(
        default_factory=lambda: float(os.getenv("DLIB_MATCH_THRESHOLD", "0.5"))
    )

    @property
    def temp_dir(self) -> Path:
        return self.ssd_root / self.temp_dir_name

    @property
    def model_cache_dir(self) -> Path:
        return self.ssd_root / self.model_cache_dir_name

    @property
    def per_file_cache_dir(self) -> Path:
        return self.ssd_root / self.per_file_cache_dir_name

    def ensure_dirs(self) -> None:
        for d in (self.ssd_root, self.temp_dir, self.model_cache_dir, self.per_file_cache_dir):
            d.mkdir(parents=True, exist_ok=True)
