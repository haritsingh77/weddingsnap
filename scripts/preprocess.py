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

# ── Constants ─────────────────────────────────────────────────────────────────
SUPPORTED_EXTENSIONS  = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
GROUP_PHOTO_THRESHOLD = 4      # 4+ faces → common photo
MAX_IMAGE_SIZE        = 1200   # px — resize before processing for speed
DBSCAN_EPS            = 0.5    # face similarity tolerance
DBSCAN_MIN_SAMPLES    = 2      # min photos to form a person cluster


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WeddingSnap Face Preprocessor")

    parser.add_argument(
        "--input", "-i",
        type=str,
        help="Path to folder containing wedding photos",
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
    print("Drag and drop your photos folder into this terminal window,")
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
        print(f"\n  ✅ Found {photo_count:,} photos in: {path}")
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
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
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
        "encodings": encodings,       # one per face found
        "face_count": face_count,
        "is_common": is_common,
    }


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
    log.info(f"Found {len(photos):,} photos in {input_folder}")

    # Load existing encodings if resuming
    all_results = []
    if resume and cache_path.exists():
        with open(cache_path, "rb") as f:
            all_results = pickle.load(f)
        log.info(f"Loaded {len(all_results):,} existing encodings")

    skipped = failed = 0

    with open(progress_log, "a") as log_file:
        for photo in tqdm(photos, desc="Scanning photos", unit="photo"):
            if str(photo) in processed:
                skipped += 1
                continue

            result = encode_photo(photo)

            if result:
                all_results.append(result)
            else:
                failed += 1

            # Mark as processed
            log_file.write(str(photo) + "\n")

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