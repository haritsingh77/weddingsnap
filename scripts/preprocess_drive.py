"""
WeddingSnap Google Drive Preprocessor
Runs face matching and encoding directly against your Google Drive files.

Usage:
    cd backend
    python ../scripts/preprocess_drive.py

Optimized for Windows + NVIDIA GPU:
    set WEDDINGSNAP_SSD_ROOT=D:\weddingsnap_cache
    set FACE_BACKEND=insightface
    python ../scripts/preprocess_drive.py
"""

import sys
import pickle
import logging
import argparse
import io
import gc
import time
import json
import queue
import threading
from pathlib import Path
from typing import Optional

from tqdm import tqdm
from PIL import Image, ImageOps
import cv2

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "backend"))

from app.config import settings
from app.services.drive_service import get_drive_service, list_files_in_folder, download_file_from_drive
from app.services.drive_cache import get_cached_file, save_cached_file
from googleapiclient.http import MediaIoBaseDownload

from scripts.face_engine.config import PreprocessConfig
from scripts.face_engine.pipeline import (
    FacePipeline,
    SUPPORTED_VIDEO_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    get_pipeline,
)
from scripts.face_engine.cache import MediaCache, file_content_hash, drive_file_key
from scripts.face_engine.matching import drive_record_path
from scripts.face_engine.metrics import RunMetrics
from scripts.whatsapp_notifier import send_whatsapp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("preprocess_drive.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def create_media_thumbnail(file_path: Path, is_video: bool, size: int = 400) -> Optional[bytes]:
    try:
        if is_video:
            cap = cv2.VideoCapture(str(file_path))
            if not cap.isOpened():
                return None
            ret, frame = cap.read()
            cap.release()
            if not ret:
                return None
            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        else:
            img = Image.open(file_path)
            img = ImageOps.exif_transpose(img)

        w, h = img.size
        if w > h:
            new_w, new_h = size, int(h * (size / w))
        else:
            new_h, new_w = size, int(w * (size / h))
        img = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception as e:
        log.warning("Thumbnail failed for %s: %s", file_path.name, e)
        return None


def sort_drive_files_priority(files: list[dict]) -> list[dict]:
    """Images first, videos by ascending size (large last)."""
    images, videos = [], []
    for f in files:
        name = f.get("name", "")
        suffix = Path(name).suffix.lower()
        size = int(f.get("size") or 0)
        if suffix in SUPPORTED_VIDEO_EXTENSIONS:
            videos.append((size, f))
        else:
            images.append(f)
    images.sort(key=lambda x: x.get("name", "").lower())
    videos.sort(key=lambda x: x[0])
    return images + [f for _, f in videos]


def run_drive_preprocess(
    output_folder: Path,
    resume: bool,
    config: PreprocessConfig,
    limit_photos: int = None,
    limit_videos: int = None,
):
    pipeline = get_pipeline(config)
    temp_dir = config.temp_dir
    temp_dir.mkdir(parents=True, exist_ok=True)

    media_cache = MediaCache(config.per_file_cache_dir)
    start_time = time.time()
    last_whatsapp_time = start_time

    output_folder.mkdir(parents=True, exist_ok=True)
    cache_path = output_folder / "face_encodings.pkl"
    progress_log = output_folder / "processed_files.txt"

    if resume:
        log.info("Checking Supabase for cloud cache...")
        enc_data = get_cached_file("face_encodings.pkl")
        if enc_data:
            cache_path.write_bytes(enc_data)
            log.info("Loaded face_encodings.pkl from Supabase")
        prog_data = get_cached_file("processed_files.txt")
        if prog_data:
            progress_log.write_bytes(prog_data)
            log.info("Loaded processed_files.txt from Supabase")

    processed_ids = set()
    if resume and progress_log.exists():
        processed_ids = set(progress_log.read_text(encoding="utf-8").splitlines())
        log.info("Resuming — %s entries in progress log", f"{len(processed_ids):,}")

    all_results = []
    if resume and cache_path.exists():
        try:
            with open(cache_path, "rb") as f:
                all_results = pickle.load(f)
            log.info("Loaded %s face encodings", f"{len(all_results):,}")
        except Exception as e:
            log.warning("Cache load failed, starting fresh: %s", e)

    log.info("Fetching file list from Google Drive...")
    drive_files = list_files_in_folder(settings.GOOGLE_DRIVE_FOLDER_ID, media_type="all")
    log.info("Found %s total media files", f"{len(drive_files):,}")

    try:
        from app.services.drive_cache import save_cached_json
        mapping = {f["name"]: f["id"] for f in drive_files}
        save_cached_json("drive_filename_map.json", mapping)
    except Exception as map_err:
        log.warning("Drive mapping upload failed: %s", map_err)

    filtered_files = [
        f for f in drive_files
        if Path(f["name"]).suffix.lower() in SUPPORTED_EXTENSIONS and not f["name"].startswith("._")
    ]
    filtered_files = sort_drive_files_priority(filtered_files)
    log.info("Processing order: images first (%s), then videos by size", sum(1 for f in filtered_files if Path(f["name"]).suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS))

    send_whatsapp(
        f"🚀 WeddingSnap preprocessor started!\n"
        f"Backend: {pipeline.backend_name}\n"
        f"Model: {pipeline.model_label}\n"
        f"SSD: {config.ssd_root}\n"
        f"Files: {len(filtered_files):,}"
    )

    metrics = RunMetrics(
        total_files=len(filtered_files),
        model=pipeline.model_label,
        backend=pipeline.backend_name,
    )

    skipped = failed = success = 0
    success_photos = success_videos = 0
    image_batch: list[tuple] = []  # (file dict, temp path)
    file_counter = 0

    last_checkpoint_count = 0
    last_whatsapp_processed = 0

    # Clear stale temp on SSD
    if temp_dir.exists():
        for f in temp_dir.glob("*"):
            try:
                if f.is_file():
                    f.unlink()
            except Exception:
                pass

    def persist_checkpoint():
        with open(cache_path, "wb") as f:
            pickle.dump(all_results, f)
        log.info("Uploading checkpoint to Supabase...")
        pkl_size = cache_path.stat().st_size
        SUPABASE_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
        if pkl_size <= SUPABASE_MAX_BYTES:
            save_cached_file("face_encodings.pkl", cache_path.read_bytes(), mime_type="application/octet-stream")
        else:
            log.info("face_encodings.pkl is %.1f MB — skipping Supabase upload (too large). Saved locally only.", pkl_size / (1024*1024))
        save_cached_file("processed_files.txt", progress_log.read_bytes(), mime_type="text/plain")
        media_cache.save_index()

    def flush_image_batch():
        nonlocal success, success_photos, file_counter, image_batch
        if not image_batch:
            return
        t0 = time.time()
        paths = [t[1] for t in image_batch]
        batch_results = pipeline.encode_photo_batch(paths)
        faces = 0
        with open(progress_log, "a", encoding="utf-8") as log_file:
            for (file_meta, temp_path), result in zip(image_batch, batch_results):
                file_id, file_name = file_meta["id"], file_meta["name"]
                try:
                    content_hash = file_content_hash(temp_path)
                    cache_key = drive_file_key(file_id, file_name, file_meta.get("md5Checksum"))
                    if result:
                        result["path"] = drive_record_path(file_id, file_name)
                        result["drive_id"] = file_id
                        all_results.append(result)
                        media_cache.store_result(cache_key, content_hash, result)
                        faces += result.get("face_count", 0)
                    else:
                        media_cache.store_result(cache_key, content_hash, {"path": drive_record_path(file_id, file_name), "drive_id": file_id, "skipped": True})
                    log_file.write(f"{file_id}\n{file_name}\n")
                    processed_ids.add(file_id)
                    processed_ids.add(file_name)
                    success += 1
                    success_photos += 1
                except Exception as e:
                    log.error("Batch item %s: %s", file_name, e)
                    metrics.failed += 1
                finally:
                    if temp_path.exists():
                        temp_path.unlink()
        metrics.record_batch(len(image_batch), time.time() - t0, faces)
        file_counter += len(image_batch)
        image_batch = []
        pipeline.maybe_gc(file_counter)
        gc.collect()

    # 1. Pre-filter files to identify which ones actually need downloading/processing
    files_to_process = []
    temp_skipped = skipped
    temp_success_photos = success_photos
    temp_success_videos = success_videos
    
    for file in filtered_files:
        file_id = file["id"]
        file_name = file["name"]
        
        # Check if already processed
        if file_id in processed_ids or file_name in processed_ids:
            temp_skipped += 1
            continue
            
        suffix = Path(file_name).suffix.lower()
        is_vid = suffix in SUPPORTED_VIDEO_EXTENSIONS
        
        # Check limits
        if is_vid:
            if limit_videos is not None and temp_success_videos >= limit_videos:
                continue
            temp_success_videos += 1
        else:
            if limit_photos is not None and temp_success_photos >= limit_photos:
                continue
            temp_success_photos += 1
            
        files_to_process.append(file)
        
    skipped = temp_skipped
    log.info("Filtered: %s files to process, %s files already processed/skipped.", len(files_to_process), skipped)

    # 2. Setup bounded producer-consumer thread queue for downloading
    MAX_PREFETCH = 10
    NUM_DOWNLOAD_THREADS = 5
    
    task_queue = queue.Queue()
    result_queue = queue.Queue(maxsize=MAX_PREFETCH)
    shutdown_event = threading.Event()
    
    for file in files_to_process:
        task_queue.put(file)
        
    for _ in range(NUM_DOWNLOAD_THREADS):
        task_queue.put(None)
        
    def download_worker():
        while not shutdown_event.is_set():
            try:
                file = task_queue.get(timeout=0.2)
            except queue.Empty:
                continue
                
            if file is None:
                task_queue.task_done()
                break
                
            file_id = file["id"]
            file_name = file["name"]
            temp_path = temp_dir / f"{file_id}_{file_name}"
            
            success = False
            try:
                success = download_file_from_drive(file_id, temp_path)
            except Exception as e:
                log.error("Download failed for %s: %s", file_name, e)
                
            if shutdown_event.is_set():
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except Exception:
                        pass
                task_queue.task_done()
                break
                
            if success:
                result_queue.put((file, temp_path))
            else:
                result_queue.put((file, None))
                
            task_queue.task_done()

    # 3. Start downloader threads
    threads = []
    for _ in range(NUM_DOWNLOAD_THREADS):
        t = threading.Thread(target=download_worker)
        t.daemon = True
        t.start()
        threads.append(t)

    # 4. Consumer loop
    pbar = tqdm(total=len(filtered_files), desc="Drive preprocess", unit="file")
    pbar.update(skipped)
    metrics.skipped = skipped
    
    try:
        with open(progress_log, "a", encoding="utf-8") as log_file:
            processed_count = 0
            total_to_process = len(files_to_process)
            
            while processed_count < total_to_process and not shutdown_event.is_set():
                try:
                    file, temp_path = result_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                    
                processed_count += 1
                file_id = file["id"]
                file_name = file["name"]
                
                metrics.current_file = file_name
                metrics.queue_size = len(filtered_files) - (skipped + success + failed)
                
                if temp_path is None or not temp_path.exists():
                    failed += 1
                    metrics.failed = failed
                    pbar.update(1)
                    result_queue.task_done()
                    continue
                    
                suffix = Path(file_name).suffix.lower()
                is_vid = suffix in SUPPORTED_VIDEO_EXTENSIONS
                
                # Check limits dynamically during processing too (safety)
                if is_vid and limit_videos is not None and success_videos >= limit_videos:
                    if temp_path.exists():
                        try:
                            temp_path.unlink()
                        except Exception:
                            pass
                    pbar.update(1)
                    result_queue.task_done()
                    continue
                if not is_vid and limit_photos is not None and success_photos >= limit_photos:
                    if temp_path.exists():
                        try:
                            temp_path.unlink()
                        except Exception:
                            pass
                    pbar.update(1)
                    result_queue.task_done()
                    continue
                    
                cache_key = drive_file_key(file_id, file_name, file.get("md5Checksum"))
                
                try:
                    content_hash = file_content_hash(temp_path)
                    if media_cache.is_unchanged(cache_key, content_hash):
                        cached = media_cache.load_result(cache_key)
                        if cached and not cached.get("skipped"):
                            cached["path"] = drive_record_path(file_id, file_name)
                            cached["drive_id"] = file_id
                            all_results.append(cached)
                        log_file.write(f"{file_id}\n{file_name}\n")
                        processed_ids.add(file_id)
                        processed_ids.add(file_name)
                        skipped += 1
                        metrics.skipped = skipped
                        # NOTE: do NOT call task_done() here — the finally block always runs
                        # even after continue, so it would be called twice (ValueError)
                        pbar.update(1)
                        continue
                        
                    thumb_bytes = create_media_thumbnail(temp_path, is_video=is_vid, size=400)
                    if thumb_bytes:
                        save_cached_file(f"thumb_{file_id}_400.jpg", thumb_bytes, mime_type="image/jpeg")
                        
                    if is_vid:
                        flush_image_batch()
                        t0 = time.time()
                        result = pipeline.encode_video(temp_path)
                        metrics.record_batch(1, time.time() - t0, result["face_count"] if result else 0)
                        if result:
                            result["path"] = drive_record_path(file_id, file_name)
                            result["drive_id"] = file_id
                            all_results.append(result)
                            media_cache.store_result(cache_key, content_hash, result)
                        else:
                            media_cache.store_result(cache_key, content_hash, {"path": drive_record_path(file_id, file_name), "drive_id": file_id, "skipped": True})
                        log_file.write(f"{file_id}\n{file_name}\n")
                        processed_ids.add(file_id)
                        processed_ids.add(file_name)
                        success += 1
                        success_videos += 1
                        file_counter += 1
                    else:
                        image_batch.append((file, temp_path))
                        if len(image_batch) >= config.batch_size:
                            flush_image_batch()
                            
                except Exception as e:
                    log.error("Error processing %s: %s", file_name, e)
                    failed += 1
                    metrics.failed = failed
                    retries = config.max_retries
                    if retries > 0:
                        log.info("Will retry on next run (resume enabled)")
                finally:
                    if temp_path.exists() and not any(t[1] == temp_path for t in image_batch):
                        try:
                            temp_path.unlink()
                        except Exception:
                            pass
                    pbar.update(1)
                    result_queue.task_done()
                    
                metrics.success = success
                metrics.faces_found = sum(r.get("face_count", 0) for r in all_results if isinstance(r, dict))
                metrics.write_state(output_folder)
                
                total_current = success + failed
                if total_current % config.checkpoint_every == 0 and total_current > last_checkpoint_count:
                    persist_checkpoint()
                    last_checkpoint_count = total_current
                    current_time = time.time()
                    total_processed = metrics.skipped + metrics.success + metrics.failed
                    time_elapsed = current_time - last_whatsapp_time
                    crossed_500_boundary = (total_processed // 500) > (last_whatsapp_processed // 500)
                    
                    if (time_elapsed >= 3600) or crossed_500_boundary:
                        speed = metrics.sec_per_file()
                        speed_line = f"Speed: {speed:.1f}s/file\n" if speed else ""
                        eta_h = (metrics.eta_seconds() or 0) // 3600
                        msg = (
                            f"📈 Preprocess update:\n"
                            f"{total_processed}/{len(filtered_files)} ({100*total_processed/len(filtered_files):.1f}%)\n"
                            f"{speed_line}"
                            f"ETA: ~{eta_h}h\n"
                            f"Backend: {pipeline.backend_name}\n"
                            f"Faces: {metrics.faces_found:,}"
                        )
                        send_whatsapp(msg)
                        last_whatsapp_time = current_time
                        last_whatsapp_processed = total_processed
                        
        flush_image_batch()
    finally:
        shutdown_event.set()
        # Drain task_queue
        while not task_queue.empty():
            try:
                task_queue.get_nowait()
                task_queue.task_done()
            except queue.Empty:
                break
        # Drain result_queue and unlink files
        while not result_queue.empty():
            try:
                file, temp_path = result_queue.get_nowait()
                if temp_path and temp_path.exists():
                    try:
                        temp_path.unlink()
                    except Exception:
                        pass
                result_queue.task_done()
            except queue.Empty:
                break
        # Wait for threads
        for t in threads:
            t.join(timeout=2.0)
        pbar.close()

    with open(cache_path, "wb") as f:
        pickle.dump(all_results, f)
    persist_checkpoint()

    meta = {
        "backend": pipeline.backend_name,
        "embedding_dim": pipeline.backend.embedding_dim,
        "model": pipeline.model_label,
    }
    (output_folder / "encodings_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    save_cached_file("encodings_meta.json", json.dumps(meta).encode(), mime_type="application/json")

    try:
        if temp_dir.exists() and not any(temp_dir.iterdir()):
            temp_dir.rmdir()
    except Exception:
        pass

    log.info("✅ Google Drive preprocessing complete")
    log.info("   Processed: %s | Skipped: %s | Failed: %s", success, skipped, failed)
    log.info("   Avg sec/file: %s", metrics.sec_per_file())

    send_whatsapp(
        f"✅ Preprocessing complete!\nBackend: {pipeline.backend_name}\n"
        f"Processed: {success}\nFailed: {failed}"
    )

    state_file = output_folder / "preprocessor_state.json"
    if state_file.exists():
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["status"] = "stopped"
        state["last_update"] = time.time()
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WeddingSnap Google Drive Preprocessor")
    parser.add_argument("--no-resume", action="store_true", help="Start from scratch")
    parser.add_argument("--limit-photos", type=int, default=None)
    parser.add_argument("--limit-videos", type=int, default=None)
    parser.add_argument("--model", type=str, choices=["hog", "cnn"], default=None, help="dlib model only")
    parser.add_argument("--backend", type=str, choices=["auto", "insightface", "dlib"], default=None)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--ssd-root", type=str, default=None, help="SSD path for temp/cache/models")
    args = parser.parse_args()

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

    output_dir = Path(settings.ENCODINGS_CACHE_PATH).parent

    log.info("SSD root          : %s", config.ssd_root)
    log.info("Temp dir          : %s", config.temp_dir)
    log.info("Drive Folder ID   : %s", settings.GOOGLE_DRIVE_FOLDER_ID)
    log.info("Output cache      : %s", settings.ENCODINGS_CACHE_PATH)
    log.info("Resume            : %s", not args.no_resume)
    log.info("Backend (env)     : %s", config.backend)
    log.info("Batch size        : %s", config.batch_size)

    run_drive_preprocess(
        output_dir,
        resume=not args.no_resume,
        config=config,
        limit_photos=args.limit_photos,
        limit_videos=args.limit_videos,
    )
