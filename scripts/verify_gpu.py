#!/usr/bin/env python3
"""
Verify GPU / CUDA setup for WeddingSnap preprocessing.
Does not run face detection on a full library.

Usage:
    python scripts/verify_gpu.py
"""

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from scripts.face_engine.gpu import log_device_info, get_device_info
from scripts.face_engine.config import PreprocessConfig


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print("\n=== WeddingSnap GPU Verification ===\n")
    info = log_device_info()

    print("\n--- ONNX Runtime ---")
    try:
        import onnxruntime as ort
        print("Version:", ort.__version__)
        print("Providers:", ort.get_available_providers())
    except ImportError as e:
        print("MISSING:", e)
        print("Fix: pip install onnxruntime-gpu")

    print("\n--- InsightFace ---")
    try:
        import insightface
        print("Version:", insightface.__version__)
    except ImportError as e:
        print("MISSING:", e)
        print("Fix: pip install insightface")

    print("\n--- dlib (legacy fallback) ---")
    try:
        import dlib
        print("Version:", dlib.__version__)
        print("DLIB_USE_CUDA:", getattr(dlib, "DLIB_USE_CUDA", False))
    except ImportError:
        print("Not installed (optional if using InsightFace)")

    config = PreprocessConfig()
    print("\n--- Paths ---")
    print("SSD root:", config.ssd_root)
    print("Temp dir:", config.temp_dir)
    print("Model cache:", config.model_cache_dir)

    if info.insightface_gpu or info.dlib_cuda:
        print("\n✅ GPU acceleration appears AVAILABLE")
        return 0

    print("\n⚠️  GPU not fully configured — preprocessing will use CPU")
    print("See docs/PREPROCESS_GPU_SETUP.md")
    return 1


if __name__ == "__main__":
    sys.exit(main())
