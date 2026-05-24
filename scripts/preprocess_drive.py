"""
WeddingSnap Google Drive Preprocessor
Runs face matching and encoding directly against your Google Drive files.
Downloads each photo/video temporarily, runs detection, updates the cache,
and deletes the temp file to save disk space.
Generates size-400 thumbnails side by side and uploads them to Supabase Storage.

Usage:
    python scripts/preprocess_drive.py
"""

import os
import sys
import pickle
import logging
import argparse
import io
from typing import Optional
from pathlib import Path
from tqdm import tqdm
from PIL import Image, ImageOps
import cv2

# Add backend and project root directories to sys.path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root / "backend"))
sys.path.append(str(project_root))

# Load config and Drive service
from app.config import settings
from app.services.drive_service import (
    get_drive_service,
    list_files_in_folder
)
from app.services.drive_cache import get_cached_file, save_cached_file
from googleapiclient.http import MediaIoBaseDownload

# Import preprocessor functions
from scripts.preprocess import (
    encode_photo,
    encode_video,
    SUPPORTED_VIDEO_EXTENSIONS,
    SUPPORTED_EXTENSIONS
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("preprocess_drive.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

TEMP_DIR = Path(__file__).resolve().parent.parent / "temp_preprocess"


def download_file_from_drive(file_id: str, dest_path: Path):
    """Stream download a file from Google Drive to a local path."""
    try:
        service = get_drive_service()
        request = service.files().get_media(fileId=file_id)
        
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(dest_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request, chunksize=1024*1024*5) # 5MB chunks
            done = False
            while not done:
                status, done = downloader.next_chunk()
        return True
    except Exception as e:
        log.error(f"Failed to download file {file_id}: {e}")
        if dest_path.exists():
            dest_path.unlink()
        return False


def create_media_thumbnail(file_path: Path, is_video: bool, size: int = 400) -> Optional[bytes]:
    """Generate high-quality JPEG thumbnail for an image or video frame."""
    try:
        if is_video:
            cap = cv2.VideoCapture(str(file_path))
            if not cap.isOpened():
                return None
            ret, frame = cap.read()
            cap.release()
            if not ret:
                return None
            
            # Convert BGR (OpenCV) to RGB
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb_frame)
        else:
            img = Image.open(file_path)
            img = ImageOps.exif_transpose(img)
        
        w, h = img.size
        if w > h:
            new_w = size
            new_h = int(h * (size / w))
        else:
            new_h = size
            new_w = int(w * (size / h))
            
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception as e:
        log.warning(f"Could not generate thumbnail for {file_path.name}: {e}")
        return None


def run_drive_preprocess(output_folder: Path, resume: bool, limit_photos: int = None, limit_videos: int = None):
    output_folder.mkdir(parents=True, exist_ok=True)
    cache_path   = output_folder / "face_encodings.pkl"
    progress_log = output_folder / "processed_files.txt"

    # Try downloading existing cache files from Supabase Storage if resuming
    if resume:
        log.info("Checking for cloud cache on Supabase Storage...")
        enc_data = get_cached_file("face_encodings.pkl")
        if enc_data:
            cache_path.write_bytes(enc_data)
            log.info("Loaded face_encodings.pkl from Supabase")
            
        prog_data = get_cached_file("processed_files.txt")
        if prog_data:
            progress_log.write_bytes(prog_data)
            log.info("Loaded processed_files.txt from Supabase")

    # Load already processed Google Drive IDs/filenames
    processed_ids = set()
    if resume and progress_log.exists():
        processed_ids = set(progress_log.read_text(encoding="utf-8").splitlines())
        log.info(f"Resuming — {len(processed_ids):,} files already processed")

    # Load existing encodings list if resuming
    all_results = []
    if resume and cache_path.exists():
        try:
            with open(cache_path, "rb") as f:
                all_results = pickle.load(f)
            log.info(f"Loaded {len(all_results):,} existing face encodings")
        except Exception as e:
            log.warning(f"Failed to load existing cache, starting fresh: {e}")

    log.info("Fetching folder structure & file list from Google Drive...")
    drive_files = list_files_in_folder(settings.GOOGLE_DRIVE_FOLDER_ID, media_type="all")
    log.info(f"Found {len(drive_files):,} total media files on Google Drive")

    # Upload filename-to-ID mapping to Supabase Storage so the API doesn't have to list files
    try:
        from app.services.drive_cache import save_cached_json
        mapping = {f["name"]: f["id"] for f in drive_files}
        save_cached_json("drive_filename_map.json", mapping)
        log.info("Uploaded Google Drive filename mapping to Supabase Storage")
    except Exception as map_err:
        log.warning(f"Failed to upload Drive mapping cache: {map_err}")

    # Filter out files that don't match supported extensions or are hidden macOS metadata files
    filtered_files = [
        f for f in drive_files
        if Path(f["name"]).suffix.lower() in SUPPORTED_EXTENSIONS and not f["name"].startswith("._")
    ]
    log.info(f"Filtered to {len(filtered_files):,} supported photos and videos")

    skipped = failed = success = 0
    success_photos = 0
    success_videos = 0
    
    # Clear any residual temp files from previous interrupted runs to free up disk space immediately
    if TEMP_DIR.exists():
        for f in TEMP_DIR.glob("*"):
            try:
                if f.is_file():
                    f.unlink()
            except Exception as e:
                log.warning(f"Could not delete stale temp file {f.name}: {e}")
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    with open(progress_log, "a", encoding="utf-8") as log_file:
        for file in tqdm(filtered_files, desc="Processing Drive Files", unit="file"):
            file_id = file["id"]
            file_name = file["name"]

            # Check if both limits are met to stop processing early
            if (limit_photos is not None and success_photos >= limit_photos) and \
               (limit_videos is not None and success_videos >= limit_videos):
                log.info("Reached specified batch limits for both photos and videos. Stopping run.")
                break

            # Skip if already processed
            if file_id in processed_ids or file_name in processed_ids:
                skipped += 1
                continue

            # Check file-type specific limit constraints
            suffix = Path(file_name).suffix.lower()
            is_vid = suffix in SUPPORTED_VIDEO_EXTENSIONS
            if is_vid and limit_videos is not None and success_videos >= limit_videos:
                continue
            if not is_vid and limit_photos is not None and success_photos >= limit_photos:
                continue

            # Temporary download path
            temp_path = TEMP_DIR / file_name

            # Stream download from Google Drive
            download_success = download_file_from_drive(file_id, temp_path)
            if not download_success:
                failed += 1
                continue

            # Process the downloaded file
            try:
                # ── 1. Generate size-400 thumbnail side by side ──
                thumb_bytes = create_media_thumbnail(temp_path, is_video=is_vid, size=400)
                if thumb_bytes:
                    thumb_key = f"thumb_{file_id}_400.jpg"
                    save_cached_file(thumb_key, thumb_bytes, mime_type="image/jpeg")
                
                # ── 2. Run Face Matching & Encodings ──
                if is_vid:
                    result = encode_video(temp_path)
                else:
                    result = encode_photo(temp_path)

                if result:
                    # Clean up local path prefix and replace with a clean virtual path
                    result["path"] = f"GoogleDrive/{file_name}"
                    all_results.append(result)
                    success += 1
                    if is_vid:
                        success_videos += 1
                    else:
                        success_photos += 1
                else:
                    # File was successfully downloaded and thumbnail generated, but contains no faces
                    # We still count it as a success for tracking/resuming
                    success += 1
                    if is_vid:
                        success_videos += 1
                    else:
                        success_photos += 1

                # Log as processed using both ID and Name to ensure resume works perfectly
                log_file.write(f"{file_id}\n")
                log_file.write(f"{file_name}\n")
                processed_ids.add(file_id)
                processed_ids.add(file_name)

            except Exception as e:
                log.error(f"Error processing {file_name}: {e}")
                failed += 1
            finally:
                # Clean up local temp file immediately to save disk space
                if temp_path.exists():
                    temp_path.unlink()

            # Save checkpoint every 25 files
            if (success + failed) % 25 == 0:
                with open(cache_path, "wb") as f:
                    pickle.dump(all_results, f)
                log_file.flush()
                log.info("Uploading checkpoint to Supabase Storage...")
                save_cached_file("face_encodings.pkl", cache_path.read_bytes(), mime_type="application/octet-stream")
                save_cached_file("processed_files.txt", progress_log.read_bytes(), mime_type="text/plain")

    # Save final results
    with open(cache_path, "wb") as f:
        pickle.dump(all_results, f)
    
    log.info("Uploading final cache to Supabase Storage...")
    save_cached_file("face_encodings.pkl", cache_path.read_bytes(), mime_type="application/octet-stream")
    save_cached_file("processed_files.txt", progress_log.read_bytes(), mime_type="text/plain")

    # Cleanup temp directory
    try:
        if TEMP_DIR.exists():
            TEMP_DIR.rmdir()
    except Exception:
        pass

    log.info("=" * 40)
    log.info("✅ Google Drive Preprocessing Complete!")
    log.info(f"   Processed  : {success:,} new files")
    log.info(f"   Skipped    : {skipped:,} files")
    log.info(f"   Failed     : {failed:,} files")
    log.info(f"   Saved to   : {cache_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WeddingSnap Google Drive Preprocessor")
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Start preprocessing from scratch (do not resume)",
    )
    parser.add_argument(
        "--limit-photos",
        type=int,
        default=None,
        help="Max number of photos to process during this run",
    )
    parser.add_argument(
        "--limit-videos",
        type=int,
        default=None,
        help="Max number of videos to process during this run",
    )
    args = parser.parse_args()

    output_dir = Path(settings.ENCODINGS_CACHE_PATH).parent

    log.info("Starting face preprocessing directly from Google Drive...")
    log.info(f"Drive Folder ID : {settings.GOOGLE_DRIVE_FOLDER_ID}")
    log.info(f"Output Cache    : {settings.ENCODINGS_CACHE_PATH}")
    log.info(f"Resume          : {not args.no_resume}")
    if args.limit_photos is not None:
        log.info(f"Photo Limit     : {args.limit_photos}")
    if args.limit_videos is not None:
        log.info(f"Video Limit     : {args.limit_videos}")

    run_drive_preprocess(
        output_dir, 
        resume=not args.no_resume,
        limit_photos=args.limit_photos,
        limit_videos=args.limit_videos
    )
