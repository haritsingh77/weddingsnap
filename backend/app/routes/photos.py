"""
Photo gallery routes — fetch and stream photos for a guest.
"""

import logging
import io
import json
import pickle
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from app.database import supabase
from app.config import settings
from app.services.drive_service import (
    download_file_to_memory,
    build_filename_to_id_map,
    ORIGINALS_DIR,
    THUMBNAILS_DIR,
    get_drive_service,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/photos", tags=["photos"])


# Extension → MIME type fallback (instant, no network call)
_EXT_MIME = {
    ".mp4": "video/mp4", ".mov": "video/quicktime",
    ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp", ".heic": "image/heic",
}


@lru_cache(maxsize=1)
def get_drive_id_to_mime_map() -> dict[str, str]:
    """
    Build Drive file ID → MIME type mapping from the Supabase/local JSON cache.
    """
    from app.services.drive_cache import get_cached_json
    try:
        name_map = get_cached_json("drive_filename_map.json")
        if name_map:
            res = {}
            for name, fid in name_map.items():
                ext = Path(name).suffix.lower()
                res[fid] = _EXT_MIME.get(ext, "image/jpeg")
            return res
    except Exception as e:
        log.warning(f"Could not build MIME map from Supabase JSON cache: {e}")

    try:
        mapping_path = Path("encodings/drive_filename_map.json")
        if not mapping_path.exists():
            mapping_path = Path("../encodings/drive_filename_map.json")
        if mapping_path.exists():
            with open(mapping_path) as f:
                name_map = json.load(f)
            # Map ID to mime by suffix
            res = {}
            for name, fid in name_map.items():
                ext = Path(name).suffix.lower()
                res[fid] = _EXT_MIME.get(ext, "image/jpeg")
            return res
    except Exception as e:
        log.warning(f"Could not build MIME map from local fallback: {e}")
    return {}



# ── IMPORTANT: /stream/{file_id} MUST be defined BEFORE /{guest_id} ──────────
# FastAPI matches routes top-to-bottom. If /{guest_id} comes first, it will
# intercept all /photos/stream/... requests and treat "stream" as a guest ID.

@router.get("/thumb/{file_id}")
async def thumb_photo(file_id: str, size: int = 400):
    """
    Returns thumbnail of photo/video.
    Cache hierarchy:
      1. Local /tmp (L1, fast, ephemeral)
      2. Google Drive cache folder (persistent across deployments)
      3. Generate from Google Drive CDN thumbnail link → save to Drive cache
    """
    from app.services.drive_cache import get_cached_file, save_cached_file
    size = min(size, 2048)
    cache_key = f"thumb_{file_id}_{size}.jpg"

    # ── 1. L1 + Drive cache lookup ────────────────────────────────────────────
    cached = get_cached_file(cache_key)
    if cached:
        from fastapi.responses import Response
        return Response(
            content=cached,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    # ── 2. Generate from Google Drive CDN thumbnail link ──────────────────────
    try:
        service = get_drive_service()
        meta = service.files().get(
            fileId=file_id,
            fields='thumbnailLink,imageMediaMetadata,videoMediaMetadata'
        ).execute()
        link = meta.get('thumbnailLink')

        if link:
            if '=s220' in link:
                adjusted_link = link.replace('=s220', f'=s{size}')
            elif '=' in link:
                adjusted_link = link.split('=')[0] + f'=s{size}'
            else:
                adjusted_link = f"{link}=s{size}"

            import requests as _req
            res = _req.get(adjusted_link, timeout=10)
            if res.status_code == 200:
                from PIL import Image, ImageOps
                import io as _io

                rot = 0
                if 'imageMediaMetadata' in meta:
                    rot = meta['imageMediaMetadata'].get('rotation', 0)

                img = Image.open(_io.BytesIO(res.content))
                if rot:
                    rot_deg = rot if rot > 4 else rot * 90
                    img = img.rotate(rot_deg, expand=True)
                else:
                    img = ImageOps.exif_transpose(img)

                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")

                buf = _io.BytesIO()
                img.save(buf, format="JPEG", quality=85, optimize=True)
                thumb_data = buf.getvalue()

                # Save to Drive cache (persistent) + local L1
                save_cached_file(cache_key, thumb_data, mime_type="image/jpeg")

                from fastapi.responses import Response
                return Response(
                    content=thumb_data,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"},
                )
    except Exception as e:
        log.warning(f"Could not fetch Google CDN thumbnail for {file_id}: {e}")

    # ── 3. Last resort: download full-res from Drive, resize with Pillow ──────
    mime_map = get_drive_id_to_mime_map()
    mime_type = mime_map.get(file_id, "image/jpeg")

    if mime_type.startswith("video/"):
        raise HTTPException(status_code=404, detail="Video thumbnail unavailable")

    try:
        service = get_drive_service()
        request = service.files().get_media(fileId=file_id)
        import io as _io
        buf = _io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        data = buf.getvalue()

        from PIL import Image, ImageOps
        img = Image.open(_io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        ratio = size / img.width
        img = img.resize((size, int(img.height * ratio)), Image.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        out = _io.BytesIO()
        img.save(out, format="JPEG", quality=82, optimize=True)
        thumb_data = out.getvalue()

        save_cached_file(cache_key, thumb_data, mime_type="image/jpeg")

        from fastapi.responses import Response
        return Response(
            content=thumb_data,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )
    except Exception as e:
        log.error(f"Thumbnail fallback failed for {file_id}: {e}")
        raise HTTPException(status_code=404, detail="Thumbnail unavailable")


@router.get("/stream/{file_id}")
async def stream_photo(file_id: str, download: bool = False):
    """
    Proxy route — streams full-res photo/video from Drive.
    Supports Range Requests via FileResponse.
    Can trigger direct download with download=true.
    """
    original_path = ORIGINALS_DIR / file_id
    mime_map = get_drive_id_to_mime_map()
    mime_type = mime_map.get(file_id, "image/jpeg")

    headers = {"Cache-Control": "private, max-age=86400"}
    if download:
        filename = f"wedding_media_{file_id}"
        try:
            name_to_id = build_filename_to_id_map()
            for name, fid in name_to_id.items():
                if fid == file_id:
                    filename = name
                    break
        except Exception:
            pass
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    if not original_path.exists():
        log.info(f"Downloading original file {file_id} to L1 cache for smooth streaming...")
        try:
            service = get_drive_service()
            request = service.files().get_media(fileId=file_id)
            temp_path = original_path.with_suffix(".tmp")
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp_path, "wb") as f:
                downloader = MediaIoBaseDownload(f, request, chunksize=1024 * 1024 * 5)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            temp_path.rename(original_path)
            log.info(f"Finished caching original file {file_id} locally.")
        except Exception as e:
            log.error(f"Failed to download/cache original file {file_id}: {e}")
            # Fall back to on-the-fly request-based StreamingResponse if download fails
            try:
                from google.oauth2 import service_account
                import google.auth.transport.requests
                import requests
                
                creds = service_account.Credentials.from_service_account_file(
                    settings.GOOGLE_SERVICE_ACCOUNT_JSON,
                    scopes=["https://www.googleapis.com/auth/drive"]
                )
                auth_req = google.auth.transport.requests.Request()
                creds.refresh(auth_req)
                
                drive_headers = {"Authorization": f"Bearer {creds.token}"}
                drive_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
                resp = requests.get(drive_url, headers=drive_headers, stream=True)
                if resp.status_code == 200:
                    response_headers = headers.copy()
                    if "Content-Length" in resp.headers:
                        response_headers["Content-Length"] = resp.headers["Content-Length"]
                    return StreamingResponse(
                        resp.iter_content(chunk_size=1024 * 1024),
                        media_type=mime_type,
                        headers=response_headers
                    )
            except Exception as fallback_err:
                log.error(f"Fallback streaming also failed: {fallback_err}")
                raise HTTPException(status_code=500, detail=f"Failed to stream media: {e}")

    return FileResponse(
        str(original_path),
        media_type=mime_type,
        headers=headers
    )


@router.get("/{guest_id}")
async def get_guest_photos(guest_id: str, page: int = 1, limit: int = 50):
    """
    Returns paginated list of Drive file IDs for a guest, with video indicators.
    """
    guest = supabase.table("guests").select("id, name").eq("id", guest_id).execute()
    if not guest.data:
        raise HTTPException(status_code=404, detail="Guest not found")

    # Dynamically associate named face clusters with guest if name matches
    try:
        from app.services.face_service import associate_guest_by_name
        associate_guest_by_name(guest_id, guest.data[0].get("name", ""))
    except Exception as association_err:
        log.error(f"Failed to dynamically associate guest name with face clusters: {association_err}")

    offset = (page - 1) * limit

    result = supabase.table("guest_photos").select(
        "photo_id, photos(drive_path, is_common, face_count)"
    ).eq("guest_id", guest_id).range(offset, offset + limit - 1).execute()

    mime_map = get_drive_id_to_mime_map()
    photos = []

    for row in result.data:
        photo = row.get("photos", {})
        drive_id = photo.get("drive_path")
        if not drive_id:
            continue

        mime_type = mime_map.get(drive_id, "image/jpeg")
        is_video = mime_type.startswith("video/")

        # thumb_url → small resized JPEG (fast for grid)
        # stream_url → full-res original (used in lightbox)
        photos.append({
            "drive_id": drive_id,
            "is_common": photo.get("is_common", False),
            "thumb_url": f"/photos/thumb/{drive_id}",
            "stream_url": f"/photos/stream/{drive_id}",
            "is_video": is_video,
            "mime_type": mime_type
        })

    count_result = supabase.table("guest_photos").select(
        "photo_id", count="exact"
    ).eq("guest_id", guest_id).execute()

    return {
        "photos": photos,
        "page": page,
        "limit": limit,
        "total": count_result.count,
        "has_more": offset + limit < (count_result.count or 0)
    }


@router.delete("/{drive_id}")
async def delete_photo(drive_id: str):
    """
    Delete a photo/video:
    1. Moves it in Google Drive to the temp_delete folder.
    2. Deletes local cached originals and thumbnails.
    3. Removes face encodings and processed log entries.
    4. Deletes from Supabase photos/guest_photos databases.
    """
    try:
        service = get_drive_service()
        
        # 1. Retrieve current parents and filename from Google Drive
        try:
            file_meta = service.files().get(fileId=drive_id, fields='parents, name').execute()
            previous_parents = ",".join(file_meta.get('parents', []))
            filename = file_meta.get('name')
        except Exception as drive_err:
            log.error(f"Failed to fetch file metadata from Drive for {drive_id}: {drive_err}")
            filename = None
            previous_parents = ""

        # 2. Get or create temp_delete folder ID and move file
        if previous_parents:
            try:
                from app.services.drive_service import get_or_create_temp_delete_folder
                temp_delete_id = get_or_create_temp_delete_folder()
                service.files().update(
                    fileId=drive_id,
                    addParents=temp_delete_id,
                    removeParents=previous_parents,
                    fields='id, parents'
                ).execute()
                log.info(f"Moved Drive file {drive_id} to temp_delete folder {temp_delete_id}")
            except Exception as move_err:
                log.error(f"Failed to move Drive file {drive_id} to temp_delete folder: {move_err}")
                raise HTTPException(status_code=500, detail=f"Failed to move file to temp_delete folder: {move_err}")
        else:
            log.warning("Skipping Drive move: file metadata unavailable")

        # 3. Delete local cached files
        orig_file = ORIGINALS_DIR / drive_id
        if orig_file.exists():
            try:
                orig_file.unlink()
            except Exception:
                pass
                
        # 3. Delete cached thumbnails from L1/L2
        try:
            from app.services.drive_cache import delete_cached_file
            delete_cached_file(f"thumb_{drive_id}_400.jpg")
        except Exception as e:
            log.warning(f"Failed to delete cached thumbnail for {drive_id}: {e}")

        # 4. Remove entries from face_encodings pickle and processed log in Supabase cache
        if filename:
            from app.services.drive_cache import get_cached_file, save_cached_file
            try:
                encodings_data = get_cached_file("face_encodings.pkl")
                if encodings_data:
                    all_encodings = pickle.loads(encodings_data)
                    updated_encodings = [item for item in all_encodings if Path(item["path"]).name != filename]
                    save_cached_file("face_encodings.pkl", pickle.dumps(updated_encodings), mime_type="application/octet-stream")
                    
                    from app.services.face_service import load_encodings
                    load_encodings.cache_clear()
                    log.info(f"Removed face encodings for {filename} from pickle cache")
            except Exception as pkl_err:
                log.error(f"Failed to remove encoding from pickle for {filename}: {pkl_err}")

            try:
                progress_data = get_cached_file("processed_files.txt")
                if progress_data:
                    lines = progress_data.decode("utf-8").splitlines()
                    updated_lines = [line for line in lines if line.strip() not in (drive_id, filename)]
                    save_cached_file("processed_files.txt", ("\n".join(updated_lines) + "\n").encode("utf-8"), mime_type="text/plain")
            except Exception as log_err:
                log.error(f"Failed to remove from progress log for {filename}: {log_err}")

        # 5. Delete records from Supabase
        photo_res = supabase.table("photos").select("id").eq("drive_path", drive_id).execute()
        if photo_res.data:
            photo_db_id = photo_res.data[0]["id"]
            
            # Delete references from guest_photos
            supabase.table("guest_photos").delete().eq("photo_id", photo_db_id).execute()
            
            # Delete row from photos
            supabase.table("photos").delete().eq("id", photo_db_id).execute()
            log.info(f"Deleted photo record {photo_db_id} (Drive ID: {drive_id}) from Supabase")

        return {"success": True, "message": "Photo deleted and archived successfully"}
        
    except Exception as e:
        log.error(f"Error deleting photo {drive_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))