"""
WeddingSnap optimized face preprocessing engine.

Primary backend: InsightFace + ONNX Runtime GPU (RetinaFace + ArcFace).
Fallback backend: face_recognition / dlib (CPU or CUDA dlib if built).
"""

from scripts.face_engine.config import PreprocessConfig
from scripts.face_engine.gpu import log_device_info, get_device_info
from scripts.face_engine.pipeline import FacePipeline, get_pipeline

__all__ = [
    "PreprocessConfig",
    "log_device_info",
    "get_device_info",
    "FacePipeline",
    "get_pipeline",
]
