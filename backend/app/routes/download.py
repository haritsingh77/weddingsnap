"""
ZIP generation and download routes.
"""

import io
import logging
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse

from app.database import supabase
from app.services.drive_service import download_file_to_memory

log = logging.getLogger(__name__)
router = APIRouter(prefix="/download", tags=["download"])

# In-memory ZIP store keyed by session_id.
# Avoids rebuilding the ZIP on stream after background task finishes.
_zip_store: dict[str, bytes] = {}

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


def build_zip(guest_id: str, session_id: str):
    """Background task — downloads all guest photos in parallel and zips them.
    Must be regular def (not async) so FastAPI runs it in a thread pool
    and does NOT block the main event loop with Drive I/O."""
    try:
        supabase.table("download_sessions").update({
            "status": "processing"
        }).eq("id", session_id).execute()

        # 1. Fetch personal photo IDs from guest_photos mapping table
        gp_res = supabase.table("guest_photos").select("photo_id").eq("guest_id", guest_id).execute()
        personal_ids = [str(row["photo_id"]) for row in gp_res.data] if (gp_res.data and len(gp_res.data) > 0) else []

        # 2. Query photos table where is_common is true OR photo_id is in personal_ids
        if personal_ids:
            or_filter = f"is_common.eq.true,id.in.({','.join(personal_ids)})"
            result = supabase.table("photos").select("drive_path, is_common").or_(or_filter).execute()
        else:
            result = supabase.table("photos").select("drive_path, is_common").eq("is_common", True).execute()

        rows = [
            (row["drive_path"], row.get("is_common", False), i)
            for i, row in enumerate(result.data)
            if row.get("drive_path")
        ]

        zip_buffer = io.BytesIO()
        success_count = 0
        total = len(rows)
        log.info(f"ZIP: starting parallel download of {total} files for guest {guest_id}")

        # Download all files in parallel (8 concurrent threads)
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
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

        # Cache ZIP in-memory so stream endpoint serves it without rebuilding
        zip_bytes = zip_buffer.getvalue()
        _zip_store[session_id] = zip_bytes

        supabase.table("download_sessions").update({
            "status": "ready",
            "photo_count": success_count,
        }).eq("id", session_id).execute()

        log.info(f"ZIP ready for guest {guest_id}: {success_count} files, {len(zip_bytes):,} bytes")

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

    # Serve from in-memory cache if available
    zip_bytes = _zip_store.get(session_id)

    if not zip_bytes:
        # Fallback: rebuild if server restarted and in-memory cache was cleared
        # 1. Fetch personal photo IDs from guest_photos mapping table
        gp_res = supabase.table("guest_photos").select("photo_id").eq("guest_id", guest_id).execute()
        personal_ids = [str(row["photo_id"]) for row in gp_res.data] if (gp_res.data and len(gp_res.data) > 0) else []

        # 2. Query photos table where is_common is true OR photo_id is in personal_ids
        if personal_ids:
            or_filter = f"is_common.eq.true,id.in.({','.join(personal_ids)})"
            result = supabase.table("photos").select("drive_path, is_common").or_(or_filter).execute()
        else:
            result = supabase.table("photos").select("drive_path, is_common").eq("is_common", True).execute()

        rows = [
            (row["drive_path"], row.get("is_common", False), i)
            for i, row in enumerate(result.data)
            if row.get("drive_path")
        ]

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {
                    executor.submit(_download_one, drive_id, is_common, idx): idx
                    for drive_id, is_common, idx in rows
                }
                for future in as_completed(futures):
                    item = future.result()
                    if item:
                        zf.writestr(item[0], item[1])
        zip_bytes = buf.getvalue()

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

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename={guest_name}_wedding_photos.zip",
            "Content-Length": str(len(zip_bytes)),
        }
    )