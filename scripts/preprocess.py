"""
WeddingSnap Preprocessor
Run once locally to scan all photos and build face encodings database.

Usage:
    python scripts/preprocess.py --input /path/to/photos --output backend/encodings

    OR just run without arguments for interactive mode:
    python scripts/preprocess.py

GPU-optimized (InsightFace + ONNX CUDA on Windows):
    set WEDDINGSNAP_SSD_ROOT=D:\weddingsnap_cache
    set FACE_BACKEND=insightface
    python scripts/preprocess.py --input D:\photos --output backend/encodings --resume
"""

import os
import sys
import pickle
import argparse
import logging
import gc
import time
from pathlib import Path
from typing import Optional

from tqdm import tqdm

# Project root on path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from scripts.face_engine.config import PreprocessConfig
from scripts.face_engine.pipeline import (
    FacePipeline,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_VIDEO_EXTENSIONS,
    get_pipeline,
    sort_media_priority,
)
from scripts.face_engine.metrics import RunMetrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("preprocess.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WeddingSnap Face Preprocessor")

    parser.add_argument("--input", "-i", type=str, help="Path to folder containing wedding photos and videos")
    parser.add_argument("--output", "-o", type=str, default="backend/encodings", help="Where to save encodings")
    parser.add_argument("--resume", action="store_true", help="Resume from previous run")
    parser.add_argument(
        "--model",
        type=str,
        choices=["hog", "cnn"],
        default=None,
        help="dlib model only (hog/cnn). Ignored when FACE_BACKEND=insightface",
    )
    parser.add_argument(
        "--backend",
        type=str,
        choices=["auto", "insightface", "dlib"],
        default=None,
        help="Face backend (default: env FACE_BACKEND or auto)",
    )
    parser.add_argument("--batch-size", type=int, default=None, help="Image batch size (default: 4)")
    parser.add_argument("--ssd-root", type=str, default=None, help="SSD root for temp/model cache")
    return parser.parse_args()


def prompt_for_folder() -> Path:
    print("\n📁 WeddingSnap Preprocessor")
    print("=" * 40)
    print("Drag and drop your photos/videos folder into this terminal window,")
    print("or type the full path manually.\n")

    while True:
        raw = input("📂 Target folder path: ").strip().strip("'\"")
        path = Path(raw).expanduser().resolve()

        if not path.exists():
            print(f"  ❌ Folder not found: {path}")
            continue
        if not path.is_dir():
            print(f"  ❌ That's a file, not a folder: {path}")
            continue

        photo_count = sum(1 for f in path.rglob("*") if f.suffix.lower() in SUPPORTED_EXTENSIONS)
        print(f"\n  ✅ Found {photo_count:,} media files in: {path}")
        confirm = input("  Proceed with this folder? (y/n): ").strip().lower()
        if confirm == "y":
            return path
        print()


def get_all_media(folder: Path) -> list[Path]:
    files = [f for f in folder.rglob("*") if f.suffix.lower() in SUPPORTED_EXTENSIONS]
    return sort_media_priority(files)


def run(input_folder: Path, output_folder: Path, resume: bool, config: PreprocessConfig):
    pipeline = get_pipeline(config)
    output_folder.mkdir(parents=True, exist_ok=True)
    cache_path = output_folder / "face_encodings.pkl"
    progress_log = output_folder / "processed_files.txt"

    processed = set()
    if resume and progress_log.exists():
        processed = set(progress_log.read_text(encoding="utf-8").splitlines())
        log.info("Resuming — %s files already done", f"{len(processed):,}")

    media_files = get_all_media(input_folder)
    log.info("Found %s media files | backend=%s | batch=%d", f"{len(media_files):,}", pipeline.backend_name, config.batch_size)

    all_results = []
    if resume and cache_path.exists():
        with open(cache_path, "rb") as f:
            all_results = pickle.load(f)
        log.info("Loaded %s existing encodings", f"{len(all_results):,}")

    metrics = RunMetrics(
        total_files=len(media_files),
        model=pipeline.model_label,
        backend=pipeline.backend_name,
    )

    skipped = failed = 0
    batch_paths: list[Path] = []
    file_counter = 0

    with open(progress_log, "a", encoding="utf-8") as log_file:
        for media_file in tqdm(media_files, desc="Scanning media", unit="file"):
            if str(media_file) in processed:
                skipped += 1
                metrics.skipped = skipped
                continue

            metrics.current_file = media_file.name
            is_video = media_file.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS

            if is_video:
                if batch_paths:
                    _flush_image_batch(pipeline, batch_paths, all_results, log_file, metrics, config)
                    batch_paths = []
                t0 = time.time()
                result = pipeline.encode_video(media_file)
                metrics.record_batch(1, time.time() - t0, result["face_count"] if result else 0)
                if result:
                    all_results.append(result)
                    metrics.success += 1
                else:
                    failed += 1
                    metrics.failed = failed
                log_file.write(str(media_file) + "\n")
                file_counter += 1
                pipeline.maybe_gc(file_counter)
            else:
                batch_paths.append(media_file)
                if len(batch_paths) >= config.batch_size:
                    _flush_image_batch(pipeline, batch_paths, all_results, log_file, metrics, config)
                    batch_paths = []
                    file_counter += config.batch_size
                    pipeline.maybe_gc(file_counter)

            if len(all_results) > 0 and len(all_results) % 500 == 0:
                with open(cache_path, "wb") as f:
                    pickle.dump(all_results, f)
            metrics.faces_found = sum(r.get("face_count", 0) for r in all_results)
            metrics.write_state(output_folder)

        if batch_paths:
            _flush_image_batch(pipeline, batch_paths, all_results, log_file, metrics, config)

    with open(cache_path, "wb") as f:
        pickle.dump(all_results, f)

    _write_metadata(output_folder, pipeline)

    log.info("=" * 40)
    log.info("✅ Done!")
    log.info("   Media scanned  : %s", f"{len(media_files):,}")
    log.info("   Faces found    : %s", f"{sum(r['face_count'] for r in all_results):,}")
    log.info("   Backend        : %s", pipeline.backend_name)
    log.info("   Avg sec/file   : %s", metrics.sec_per_file())
    log.info("   Saved to       : %s", cache_path)
    gc.collect()


def _flush_image_batch(
    pipeline: FacePipeline,
    paths: list[Path],
    all_results: list,
    log_file,
    metrics: RunMetrics,
    config: PreprocessConfig,
):
    t0 = time.time()
    batch_results = pipeline.encode_photo_batch(paths)
    faces = 0
    for p, result in zip(paths, batch_results):
        log_file.write(str(p) + "\n")
        if result:
            all_results.append(result)
            faces += result.get("face_count", 0)
            metrics.success += 1
        else:
            metrics.failed += 1
    metrics.record_batch(len(paths), time.time() - t0, faces)
    gc.collect()


def _write_metadata(output_folder: Path, pipeline: FacePipeline) -> None:
    import json
    meta = {
        "backend": pipeline.backend_name,
        "embedding_dim": pipeline.backend.embedding_dim,
        "model": pipeline.model_label,
    }
    (output_folder / "encodings_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


if __name__ == "__main__":
    args = parse_args()
    config = PreprocessConfig()

    if args.ssd_root:
        config.ssd_root = Path(args.ssd_root).resolve()
    if args.backend:
        config.backend = args.backend
    if args.model:
        config.dlib_model = args.model
    if args.batch_size:
        config.batch_size = args.batch_size

    config.ensure_dirs()
    log.info("SSD root: %s", config.ssd_root)

    if args.input:
        input_folder = Path(args.input).expanduser().resolve()
        if not input_folder.exists():
            print(f"❌ Folder not found: {input_folder}")
            sys.exit(1)
    else:
        input_folder = prompt_for_folder()

    output_folder = Path(args.output).expanduser().resolve()
    log.info("Input  : %s", input_folder)
    log.info("Output : %s", output_folder)
    log.info("Resume : %s", args.resume)

    run(input_folder, output_folder, resume=args.resume, config=config)
