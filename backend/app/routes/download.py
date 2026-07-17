"""
ZIP generation and download routes.

ZIPs are built on disk (LOCAL_CACHE_DIR/zips), not in RAM: the old in-memory
_zip_store held every ZIP forever (OOM risk at GB scale) and evaporated on
restart while download_sessions still said 'ready'. Disk files survive process
restarts, are streamed via FileResponse without loading into memory, and are
pruned after ZIP_TTL_HOURS.
"""

import logging
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse

from app.database import supabase
from app.services.drive_service import download_file_to_memory
from app.services.drive_cache import LOCAL_CACHE_DIR

log = logging.getLogger(__name__)
router = APIRouter(prefix="/download", tags=["download"])

ZIP_DIR = LOCAL_CACHE_DIR / "zips"
ZIP_DIR.mkdir(parents=True, exist_ok=True)
ZIP_TTL_HOURS = 24


def _zip_path(session_id: str) -> Path:
    return ZIP_DIR / f"{session_id}.zip"


def _prune_old_zips() -> None:
    cutoff = time.time() - ZIP_TTL_HOURS * 3600
    for f in ZIP_DIR.glob("*.zip"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass

# Extension → correct file extension for download filename
_MIME_EXT = {
    "video/mp4": ".mp4", "video/quicktime": ".mov",
    "video/x-msvideo": ".avi", "video/x-matroska": ".mkv",
    "video/webm": ".webm",
}


def _download_one(drive_id: str, is_common: bool, index: int):
    """Download a single file from Drive. Returns (zip_path, bytes) or None."""
    data = download_file_to_memory(drive_id)
    if not data:
        return None

    from app.routes.photos import get_drive_id_to_mime_map
    mime_map = get_drive_id_to_mime_map()
    mime_type = mime_map.get(drive_id, "image/jpeg")

    # Resolve correct extension
    ext = ".jpg"
    if mime_type in _MIME_EXT:
        ext = _MIME_EXT[mime_type]
    elif mime_type.startswith("video/"):
        ext = ".mp4"

    folder = "Common Photos" if is_common else "My Photos"
    filename = f"{folder}/{drive_id}_{index}{ext}"
    return filename, data


def _collect_rows(guest_id: str) -> list[tuple]:
    """(drive_id, is_common, index) for every photo in the guest's download."""
    gp_res = supabase.table("guest_photos").select("photo_id").eq("guest_id", guest_id).execute()
    personal_ids = [str(row["photo_id"]) for row in gp_res.data] if (gp_res.data and len(gp_res.data) > 0) else []

    if personal_ids:
        or_filter = f"is_common.eq.true,id.in.({','.join(personal_ids)})"
        result = supabase.table("photos").select("drive_path, is_common").or_(or_filter).execute()
    else:
        result = supabase.table("photos").select("drive_path, is_common").eq("is_common", True).execute()

    return [
        (row["drive_path"], row.get("is_common", False), i)
        for i, row in enumerate(result.data)
        if row.get("drive_path")
    ]


def _build_zip_file(guest_id: str, session_id: str) -> tuple[Path, int]:
    """
    Download all guest photos in parallel and zip them straight to disk.
    Writes to a .part file, then renames — a crash never leaves a ZIP that
    looks complete. Returns (path, file_count). Blocking: call from a
    background task or a threadpool, never the event loop.
    """
    rows = _collect_rows(guest_id)
    total = len(rows)
    log.info(f"ZIP: starting parallel download of {total} files for guest {guest_id}")

    final_path = _zip_path(session_id)
    part_path = final_path.parent / (final_path.name + ".part")
    success_count = 0

    try:
        with open(part_path, "wb") as fh:
            with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    futures = {
                        executor.submit(_download_one, drive_id, is_common, idx): idx
                        for drive_id, is_common, idx in rows
                    }
                    for future in as_completed(futures):
                        result_item = future.result()
                        if result_item:
                            filename, data = result_item
                            zf.writestr(filename, data)
                            success_count += 1
                            if success_count % 50 == 0:
                                log.info(f"ZIP progress: {success_count}/{total} files")
        part_path.rename(final_path)
    except Exception:
        part_path.unlink(missing_ok=True)
        raise

    log.info(
        f"ZIP ready for guest {guest_id}: {success_count} files, "
        f"{final_path.stat().st_size:,} bytes on disk"
    )
    return final_path, success_count


def build_zip(guest_id: str, session_id: str):
    """Background task wrapper — regular def so FastAPI runs it in a thread
    pool and Drive I/O never blocks the event loop."""
    try:
        supabase.table("download_sessions").update({
            "status": "processing"
        }).eq("id", session_id).execute()

        _, success_count = _build_zip_file(guest_id, session_id)

        supabase.table("download_sessions").update({
            "status": "ready",
            "photo_count": success_count,
        }).eq("id", session_id).execute()

        _prune_old_zips()

    except Exception as e:
        log.error(f"ZIP build failed for {guest_id}: {e}")
        supabase.table("download_sessions").update({
            "status": "failed"
        }).eq("id", session_id).execute()


@router.post("/{guest_id}/prepare")
async def prepare_download(guest_id: str, background_tasks: BackgroundTasks):
    """Kick off ZIP generation in the background."""
    guest = supabase.table("guests").select("id").eq("id", guest_id).execute()
    if not guest.data:
        raise HTTPException(status_code=404, detail="Guest not found")

    session = supabase.table("download_sessions").insert({
        "guest_id": guest_id,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
    }).execute()

    session_id = session.data[0]["id"]
    background_tasks.add_task(build_zip, guest_id, session_id)

    return {
        "session_id": session_id,
        "status": "pending",
        "message": "Your ZIP is being prepared. Poll /download/status/{session_id} to check progress."
    }


@router.get("/status/{session_id}")
async def download_status(session_id: str):
    """Poll this to check if ZIP is ready."""
    result = supabase.table("download_sessions").select("*").eq(
        "id", session_id
    ).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Session not found")

    return result.data[0]


@router.get("/{guest_id}/stream/{session_id}")
async def stream_zip(guest_id: str, session_id: str):
    """Stream the pre-built ZIP file to the guest. No re-download needed."""
    session = supabase.table("download_sessions").select("*").eq(
        "id", session_id
    ).eq("guest_id", guest_id).eq("status", "ready").execute()

    if not session.data:
        raise HTTPException(status_code=404, detail="ZIP not ready yet")

    zip_file = _zip_path(session_id)

    if not zip_file.exists():
        # Server restarted since the ZIP was built (ephemeral disk wiped).
        # Rebuild in the threadpool so the event loop stays free — the old code
        # rebuilt inline in this async handler and froze the whole server.
        log.info(f"ZIP for session {session_id} missing on disk — rebuilding in threadpool")
        from starlette.concurrency import run_in_threadpool
        try:
            await run_in_threadpool(_build_zip_file, guest_id, session_id)
        except Exception as e:
            log.error(f"ZIP rebuild failed for session {session_id}: {e}")
            raise HTTPException(status_code=500, detail="ZIP rebuild failed. Please try again.")

    # Fetch guest name to customize zip filename
    guest_name = "Guest"
    try:
        guest_res = supabase.table("guests").select("name").eq("id", guest_id).execute()
        if guest_res.data:
            raw_name = guest_res.data[0].get("name", "Guest")
            guest_name = "".join(c for c in raw_name if c.isalnum() or c in (' ', '_', '-')).strip().replace(" ", "_")
            if not guest_name:
                guest_name = "Guest"
    except Exception as guest_err:
        log.warning(f"Failed to query guest name for ZIP naming: {guest_err}")

    return FileResponse(
        str(zip_file),
        media_type="application/zip",
        filename=f"{guest_name}_wedding_photos.zip",
    )