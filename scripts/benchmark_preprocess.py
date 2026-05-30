#!/usr/bin/env python3
"""
Benchmark face preprocessing speed on a small sample folder.

Usage:
    python scripts/benchmark_preprocess.py --input D:\sample_photos --count 20
    python scripts/benchmark_preprocess.py --input D:\sample_photos --backend insightface
"""

import argparse
import gc
import sys
import time
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from scripts.face_engine.config import PreprocessConfig
from scripts.face_engine.gpu import log_device_info
from scripts.face_engine.pipeline import FacePipeline, SUPPORTED_EXTENSIONS, sort_media_priority


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", "-i", required=True)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--backend", choices=["auto", "insightface", "dlib"], default="auto")
    parser.add_argument("--ssd-root", type=str, default=None)
    args = parser.parse_args()

    config = PreprocessConfig()
    if args.backend:
        config.backend = args.backend
    if args.ssd_root:
        config.ssd_root = Path(args.ssd_root).resolve()
    config.ensure_dirs()

    log_device_info()

    folder = Path(args.input).resolve()
    files = sort_media_priority([
        f for f in folder.rglob("*") if f.suffix.lower() in SUPPORTED_EXTENSIONS
    ])[: args.count]

    if not files:
        print("No media files found.")
        return 1

    pipeline = FacePipeline(config)
    print(f"\nBenchmarking {len(files)} files | backend={pipeline.backend_name}\n")

    times = []
    faces = 0
    for i, path in enumerate(files, 1):
        t0 = time.time()
        if path.suffix.lower() in {".mp4", ".mov", ".avi", ".mkv", ".webm"}:
            r = pipeline.encode_video(path)
        else:
            r = pipeline.encode_photo(path)
        elapsed = time.time() - t0
        times.append(elapsed)
        fc = r.get("face_count", 0) if r else 0
        faces += fc
        print(f"  [{i}/{len(files)}] {elapsed:.2f}s | faces={fc} | {path.name}")
        gc.collect()

    avg = sum(times) / len(times)
    print(f"\n--- Results ---")
    print(f"Backend     : {pipeline.backend_name}")
    print(f"Avg sec/file: {avg:.2f}")
    print(f"Total faces : {faces}")
    print(f"Est 13k imgs: {avg * 13000 / 3600:.1f} hours (images only, no Drive I/O)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
