"""
WeddingSnap Preprocessor
Run once locally to scan all photos and build face encodings database.

Usage:
    python scripts/preprocess.py --input /path/to/photos --output backend/encodings

    OR just run without arguments for interactive mode:
    python scripts/preprocess.py
"""

import os
import sys
import pickle
import argparse
import logging
from pathlib import Path
from typing import Optional, Union, List
from tqdm import tqdm

import face_recognition
import numpy as np
from PIL import Image, ImageOps
from sklearn.cluster import DBSCAN

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("preprocess.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

import cv2

# ── Constants ─────────────────────────────────────────────────────────────────
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
SUPPORTED_EXTENSIONS       = SUPPORTED_IMAGE_EXTENSIONS | SUPPORTED_VIDEO_EXTENSIONS
GROUP_PHOTO_THRESHOLD      = 4      # 4+ faces → common photo
MAX_IMAGE_SIZE             = 1200   # px — resize before processing for speed
DBSCAN_EPS                 = 0.5    # face similarity tolerance
DBSCAN_MIN_SAMPLES         = 2      # min photos to form a person cluster


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WeddingSnap Face Preprocessor")

    parser.add_argument(
        "--input", "-i",
        type=str,
        help="Path to folder containing wedding photos and videos",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default="backend/encodings",
        help="Where to save encodings (default: backend/encodings)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previous run (skip already-processed files)",
    )

    return parser.parse_args()


def prompt_for_folder() -> Path:
    """Interactive fallback if --input not provided."""
    print("\n📁 WeddingSnap Preprocessor")
    print("=" * 40)
    print("Drag and drop your photos/videos folder into this terminal window,")
    print("or type the full path manually.\n")

    while True:
        raw = input("📂 Target folder path: ").strip().strip("'\"")  # strip quotes from drag-drop
        path = Path(raw).expanduser().resolve()

        if not path.exists():
            print(f"  ❌ Folder not found: {path}")
            continue
        if not path.is_dir():
            print(f"  ❌ That's a file, not a folder: {path}")
            continue

        # Show a quick preview
        photo_count = sum(
            1 for f in path.rglob("*")
            if f.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        print(f"\n  ✅ Found {photo_count:,} media files in: {path}")
        confirm = input("  Proceed with this folder? (y/n): ").strip().lower()
        if confirm == "y":
            return path
        print()


# ── Image helpers ─────────────────────────────────────────────────────────────

def get_all_photos(folder: Path) -> list[Path]:
    photos = sorted(
        f for f in folder.rglob("*")
        if f.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    return photos


def load_and_resize(path: Path) -> Optional[np.ndarray]:
    try:
        img = Image.open(path).convert("RGB")
        img = ImageOps.exif_transpose(img)  # fix phone rotation
        w, h = img.size
        if max(w, h) > MAX_IMAGE_SIZE:
            scale = MAX_IMAGE_SIZE / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.BILINEAR)
        return np.array(img)
    except Exception as e:
        log.warning(f"Skipped {path.name}: {e}")
        return None


def encode_photo(path: Path) -> Optional[dict]:
    """Returns encoding data for one photo, or None if no faces found."""
    img = load_and_resize(path)
    if img is None:
        return None

    locations = face_recognition.face_locations(img, model="hog")
    if not locations:
        return None

    encodings = face_recognition.face_encodings(img, locations)
    face_count = len(locations)

    common_keywords = {"venue", "decor", "ceremony", "stage", "mandap", "common"}
    is_common = (
        face_count >= GROUP_PHOTO_THRESHOLD or
        any(kw in path.parent.name.lower() for kw in common_keywords)
    )

    return {
        "path": str(path),
        "locations": locations,       # one per face found (top, right, bottom, left)
        "encodings": encodings,       # one per face found
        "face_count": face_count,
        "is_common": is_common,
    }


def encode_video(path: Path) -> Optional[dict]:
    """Extract face encodings from a video by sampling frames."""
    try:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            log.warning(f"Could not open video file: {path.name}")
            return None

        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if fps <= 0 or total_frames <= 0:
            cap.release()
            return None

        # Sample 1 frame per 3 seconds of video (more than enough for face recognition)
        sample_interval = max(1, int(fps * 3))
        unique_samples = []  # List of tuples: (location, encoding, frame_idx)
        max_faces_in_frame = 0

        frame_idx = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            # Only process every sample_interval frames
            if frame_idx % sample_interval == 0:
                # Convert BGR (OpenCV) to RGB
                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                
                # Resize for speed
                h, w = rgb_frame.shape[:2]
                if max(h, w) > MAX_IMAGE_SIZE:
                    scale = MAX_IMAGE_SIZE / max(h, w)
                    rgb_frame = cv2.resize(rgb_frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

                # Detect faces in frame
                locations = face_recognition.face_locations(rgb_frame, model="hog")
                if locations:
                    max_faces_in_frame = max(max_faces_in_frame, len(locations))
                    encodings = face_recognition.face_encodings(rgb_frame, locations)
                    for loc, enc in zip(locations, encodings):
                        # Avoid adding duplicate encodings of the same person
                        if not unique_samples:
                            unique_samples.append((loc, enc, frame_idx))
                        else:
                            existing_encs = [s[1] for s in unique_samples]
                            distances = face_recognition.face_distance(existing_encs, enc)
                            if not any(d < 0.5 for d in distances):
                                unique_samples.append((loc, enc, frame_idx))

            frame_idx += 1

        cap.release()

        if not unique_samples:
            return None

        common_keywords = {"venue", "decor", "ceremony", "stage", "mandap", "common"}
        is_common = (
            max_faces_in_frame >= GROUP_PHOTO_THRESHOLD or
            any(kw in path.parent.name.lower() for kw in common_keywords)
        )

        return {
            "path": str(path),
            "locations": [s[0] for s in unique_samples],
            "encodings": [s[1] for s in unique_samples],
            "frame_indices": [s[2] for s in unique_samples],
            "face_count": max_faces_in_frame,
            "is_common": is_common,
        }

    except Exception as e:
        log.error(f"Failed to encode video {path.name}: {e}")
        return None


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(input_folder: Path, output_folder: Path, resume: bool):
    output_folder.mkdir(parents=True, exist_ok=True)
    cache_path   = output_folder / "face_encodings.pkl"
    progress_log = output_folder / "processed_files.txt"

    # Load already-processed files if resuming
    processed = set()
    if resume and progress_log.exists():
        processed = set(progress_log.read_text().splitlines())
        log.info(f"Resuming — {len(processed):,} files already done")

    photos = get_all_photos(input_folder)
    log.info(f"Found {len(photos):,} media files in {input_folder}")

    # Load existing encodings if resuming
    all_results = []
    if resume and cache_path.exists():
        with open(cache_path, "rb") as f:
            all_results = pickle.load(f)
        log.info(f"Loaded {len(all_results):,} existing encodings")

    skipped = failed = 0

    with open(progress_log, "a") as log_file:
        for media_file in tqdm(photos, desc="Scanning media files", unit="file"):
            if str(media_file) in processed:
                skipped += 1
                continue

            suffix = media_file.suffix.lower()
            if suffix in SUPPORTED_VIDEO_EXTENSIONS:
                result = encode_video(media_file)
            else:
                result = encode_photo(media_file)

            if result:
                all_results.append(result)
            else:
                failed += 1

            # Mark as processed
            log_file.write(str(media_file) + "\n")

            # Save checkpoint every 500 photos
            if len(all_results) % 500 == 0:
                with open(cache_path, "wb") as f:
                    pickle.dump(all_results, f)

    # Final save
    with open(cache_path, "wb") as f:
        pickle.dump(all_results, f)

    log.info("=" * 40)
    log.info(f"✅ Done!")
    log.info(f"   Photos scanned : {len(photos):,}")
    log.info(f"   Faces found    : {sum(r['face_count'] for r in all_results):,}")
    log.info(f"   Common photos  : {sum(1 for r in all_results if r['is_common']):,}")
    log.info(f"   No face / skip : {failed + skipped:,}")
    log.info(f"   Saved to       : {cache_path}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = parse_args()

    # Resolve input folder — CLI arg or interactive prompt
    if args.input:
        input_folder = Path(args.input).expanduser().resolve()
        if not input_folder.exists():
            print(f"❌ Folder not found: {input_folder}")
            sys.exit(1)
    else:
        input_folder = prompt_for_folder()

    output_folder = Path(args.output).expanduser().resolve()

    log.info(f"Input  : {input_folder}")
    log.info(f"Output : {output_folder}")
    log.info(f"Resume : {args.resume}")

    run(input_folder, output_folder, resume=args.resume)