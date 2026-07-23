"""
Photo gallery routes — fetch and stream photos for a guest.
"""

import logging
import io
import json
import pickle
from functools import lru_cache
from pathlib import Path
from typing import Optional
from pydantic import BaseModel

from fastapi import APIRouter, HTTPException, Request, BackgroundTasks, UploadFile, File
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

from fastapi import Depends

from app.auth_deps import guest_or_admin, require_admin

log = logging.getLogger(__name__)

# Auth is applied at the ROUTER, not per endpoint. Every route here was open:
# /all listed the whole wedding, /stream/{id} served originals, and
# DELETE /{drive_id} was reachable by anyone. Declaring it once means a new
# endpoint is protected by default — with 14 routes in this file, opting each
# one in individually is a matter of time before one is missed.
#
# Mutations additionally depend on require_admin below.
router = APIRouter(
    prefix="/photos",
    tags=["photos"],
    dependencies=[Depends(guest_or_admin)],
)

# Track which guests have already had their named-cluster association run this
# server session. Running it on every page load triggers Agglomerative Clustering
# on 28k+ faces and hangs the endpoint for minutes.
_associated_guests: set[str] = set()


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
def thumb_photo(file_id: str, size: int = 400):
    """
    Returns thumbnail of photo/video.
    Cache hierarchy:
      1. Local L1 disk cache (fast, ephemeral)
      2. Supabase CDN redirect (if pre-cached)
      3. Google Drive thumbnailLink redirect (instant, no download)
      4. Last resort: full-res download from Drive + PIL resize (images only)

    Deliberately a sync `def`: FastAPI runs it in a threadpool, keeping the
    blocking I/O below (CDN HEAD check, Drive metadata call, last-resort
    download) off the event loop.
    """
    from app.services.drive_cache import LOCAL_CACHE_DIR, save_cached_file
    from fastapi.responses import RedirectResponse, Response
    size = min(size, 2048)
    cache_key = f"thumb_{file_id}_{size}.jpg"

    # ── 1. Check local L1 disk cache first (instant) ──────────────────────────
    local_path = LOCAL_CACHE_DIR / cache_key
    if local_path.exists():
        try:
            return Response(
                content=local_path.read_bytes(),
                media_type="image/jpeg",
                headers={"Cache-Control": "private, max-age=86400"},
            )
        except Exception:
            pass

    # ── 2. Redirect to the Supabase CDN ───────────────────────────────────────
    #
    # Deliberately no HEAD check first. It was there to confirm the object
    # exists, but preprocessing uploads a thumbnail for every file it handles,
    # so the answer is always yes — measured 25/25 on a random sample of the
    # 11,034 photos. On a hosted backend that check cost ~2s per thumbnail
    # (Cloud Run and Supabase are in different regions), and browsers open only
    # ~6 connections per host, so a 50-photo page took ~17s to fill.
    #
    # Redirecting blind means a genuinely missing thumbnail shows as a broken
    # image rather than falling back to Drive. That is the right trade at 20x
    # the speed — and set THUMB_VERIFY_CDN=1 to restore the check if a batch of
    # photos is ever synced without its thumbnails.
    cdn_url = f"{settings.SUPABASE_URL}/storage/v1/object/public/weddingsnap-cache/{cache_key}"
    import os

    if os.getenv("THUMB_VERIFY_CDN", "").strip() not in ("1", "true", "yes"):
        return RedirectResponse(url=cdn_url, status_code=307)

    try:
        import httpx
        head = httpx.head(cdn_url, timeout=2.0)
        if head.status_code == 200:
            return RedirectResponse(url=cdn_url, status_code=307)
    except Exception:
        pass  # Supabase check failed — fall through to Drive

    # ── 3. Use Google Drive thumbnailLink (instant redirect, no download) ──────
    try:
        from app.services.drive_service import execute_with_retry
        meta = execute_with_retry(lambda svc: svc.files().get(
            fileId=file_id,
            fields='thumbnailLink,imageMediaMetadata,mimeType'
        ))
        link = meta.get('thumbnailLink')

        if link:
            # Adjust thumbnail size in the Drive CDN URL
            if '=s220' in link:
                adjusted_link = link.replace('=s220', f'=s{size}')
            elif '=' in link:
                adjusted_link = link.split('=')[0] + f'=s{size}'
            else:
                adjusted_link = f"{link}=s{size}"

            # Redirect browser directly to Drive CDN — zero server bandwidth
            # Also kick off a background thread to generate + cache a proper thumbnail
            import threading
            def _cache_thumb():
                try:
                    import httpx as _httpx
                    import io as _io2
                    # follow_redirects: unlike requests, httpx doesn't follow by default
                    r2 = _httpx.get(adjusted_link, timeout=15.0, follow_redirects=True)
                    if r2.status_code != 200:
                        return
                    from PIL import Image, ImageOps
                    rot = 0
                    if 'imageMediaMetadata' in meta:
                        rot = meta['imageMediaMetadata'].get('rotation', 0)
                    img = Image.open(_io2.BytesIO(r2.content))
                    if rot:
                        img = img.rotate(rot if rot > 4 else rot * 90, expand=True)
                    else:
                        img = ImageOps.exif_transpose(img)
                    if img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")
                    buf = _io2.BytesIO()
                    img.save(buf, format="JPEG", quality=85, optimize=True)
                    from app.services.drive_cache import save_cached_file
                    save_cached_file(cache_key, buf.getvalue(), mime_type="image/jpeg")
                except Exception as bg_err:
                    log.debug(f"Background thumb cache failed for {file_id}: {bg_err}")
            threading.Thread(target=_cache_thumb, daemon=True).start()

            return RedirectResponse(url=adjusted_link, status_code=307)

    except Exception as e:
        log.warning(f"Could not fetch Drive thumbnailLink for {file_id}: {e}")

    # ── 4. Last resort: download full-res from Drive, resize with Pillow ───────
    mime_map = get_drive_id_to_mime_map()
    mime_type = mime_map.get(file_id, "image/jpeg")

    if mime_type.startswith("video/"):
        raise HTTPException(status_code=404, detail="Video thumbnail unavailable")

    try:
        data = download_file_to_memory(file_id)
        if not data:
            raise Exception("Failed to download file from Google Drive")
        import io as _io

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

        return Response(
            content=thumb_data,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )
    except Exception as e:
        log.error(f"Thumbnail fallback failed for {file_id}: {e}")
        raise HTTPException(status_code=404, detail="Thumbnail unavailable")


def download_to_local_cache_task(file_id: str, dest_path: Path):
    if dest_path.exists():
        return
    try:
        log.info(f"Background task: caching file {file_id} to local disk...")
        download_file_to_memory(file_id)
        log.info(f"Background task: finished caching file {file_id} locally.")
    except Exception as e:
        log.error(f"Background task: failed to cache file {file_id}: {e}")


@router.get("/stream/{file_id}")
async def stream_photo(
    file_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    download: bool = False
):
    """
    Proxy route — streams full-res photo/video from Drive.
    Supports Range Requests via FileResponse or direct Range-forwarding.
    Can trigger direct download with download=true.
    """
    original_path = ORIGINALS_DIR / file_id
    mime_map = get_drive_id_to_mime_map()
    mime_type = mime_map.get(file_id, "image/jpeg")

    headers = {"Cache-Control": "private, max-age=86400"}
    if download:
        filename = f"wedding_media_{file_id}"
        try:
            # May hit Supabase Storage / the Drive API — keep it off the event loop
            from starlette.concurrency import run_in_threadpool
            name_to_id = await run_in_threadpool(build_filename_to_id_map)
            for name, fid in name_to_id.items():
                if fid == file_id:
                    filename = name
                    break
        except Exception:
            pass
        headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    if not original_path.exists():
        # Cache the original to local disk for next time — but ONLY where there is
        # real disk. On Cloud Run CACHE_DIR is /tmp, which is RAM: this task calls
        # download_file_to_memory(), pulling the whole file (a video can be
        # hundreds of MB) into the small container and OOM-killing it mid-request.
        # That is exactly what made the full-size preview "load forever" — the
        # container died before the stream finished. Skip caching unless a real
        # SSD root is configured (local dev / an Oracle VM), where it's a win.
        import os
        if os.getenv("WEDDINGSNAP_SSD_ROOT", "").strip():
            background_tasks.add_task(download_to_local_cache_task, file_id, original_path)

        # Immediately stream directly from Google Drive — fully async so a
        # large video stream never blocks the event loop for other requests.
        log.info(f"Streaming {file_id} directly from Google Drive (not cached yet)...")
        try:
            import httpx
            from starlette.concurrency import run_in_threadpool

            def _get_drive_token() -> str:
                """Blocking credential load + refresh — runs in the threadpool."""
                from google.oauth2 import service_account
                import google_auth_httplib2
                import httplib2
                import os
                import json

                google_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT")
                if google_json:
                    info = json.loads(google_json.strip())
                    creds = service_account.Credentials.from_service_account_info(
                        info,
                        scopes=["https://www.googleapis.com/auth/drive"]
                    )
                else:
                    creds = service_account.Credentials.from_service_account_file(
                        settings.GOOGLE_SERVICE_ACCOUNT_JSON,
                        scopes=["https://www.googleapis.com/auth/drive"]
                    )
                # google_auth_httplib2 keeps this off the transitive-only
                # `requests` package (google-auth-httplib2 is a declared dep).
                creds.refresh(google_auth_httplib2.Request(httplib2.Http()))
                return creds.token

            token = await run_in_threadpool(_get_drive_token)

            drive_headers = {"Authorization": f"Bearer {token}"}

            # Forward the Range header if present
            range_header = request.headers.get("range")
            if range_header:
                drive_headers["Range"] = range_header

            drive_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

            # Opened manually (not as a context manager) so it outlives this
            # scope for the StreamingResponse; the generator closes it. If the
            # send itself fails the client would leak, so close it explicitly.
            client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
            try:
                req = client.build_request("GET", drive_url, headers=drive_headers)
                resp = await client.send(req, stream=True)
            except BaseException:
                await client.aclose()
                raise

            response_headers = headers.copy()
            for h in ["Content-Range", "Content-Length", "Accept-Ranges"]:
                if h in resp.headers:
                    response_headers[h] = resp.headers[h]

            async def _iter_and_close():
                try:
                    async for chunk in resp.aiter_bytes(chunk_size=1024 * 64):
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                _iter_and_close(),
                status_code=resp.status_code,
                media_type=mime_type,
                headers=response_headers
            )
        except Exception as e:
            log.error(f"Failed to stream from Google Drive on-the-fly: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to stream media: {e}")

    return FileResponse(
        str(original_path),
        media_type=mime_type,
        headers=headers
    )


class CreateCategoryBody(BaseModel):
    name: str


@router.get("/categories")
def get_categories():
    """Fetch the list of custom albums/categories and their counts."""
    from app.services.drive_cache import get_cached_json
    categories = get_cached_json("categories.json") or {}
    
    result = []
    for name, drive_ids in categories.items():
        thumb_url = f"/photos/thumb/{drive_ids[0]}" if drive_ids else None
        result.append({
            "name": name,
            "count": len(drive_ids),
            "thumbnail_url": thumb_url
        })
    return result


@router.post("/categories", dependencies=[Depends(require_admin)])
def create_category(body: CreateCategoryBody):
    """Create a new dynamic category and prepare its Google Drive subfolder."""
    from app.services.drive_cache import get_cached_json, save_cached_json
    from app.services.drive_service import get_or_create_drive_folder
    
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Category name cannot be empty")
        
    categories = get_cached_json("categories.json") or {}
    if name not in categories:
        categories[name] = []
        save_cached_json("categories.json", categories)
        
    # Get or create subfolder in Google Drive
    try:
        get_or_create_drive_folder(name)
    except Exception as err:
        log.error(f"Failed to create Google Drive folder for category {name}: {err}")
        
    return {"success": True, "category": name}


@router.get("/categories/{category_name}/photos")
def get_category_photos(category_name: str):
    """Fetch all photos/videos under a dynamic category."""
    from app.services.drive_cache import get_cached_json
    categories = get_cached_json("categories.json") or {}
    
    if category_name not in categories:
        return []
        
    drive_ids = categories[category_name]
    mime_map = get_drive_id_to_mime_map()
    
    photos = []
    for drive_id in drive_ids:
        mime_type = mime_map.get(drive_id, "image/jpeg")
        is_video = mime_type.startswith("video/")
        photos.append({
            "drive_id": drive_id,
            "is_common": False,
            "thumb_url": f"/photos/thumb/{drive_id}",
            "stream_url": f"/photos/stream/{drive_id}",
            "is_video": is_video,
            "mime_type": mime_type
        })
    return photos


@router.post("/categories/{category_name}/upload", dependencies=[Depends(require_admin)])
async def upload_category_photo(
    category_name: str,
    file: UploadFile = File(...)
):
    """Upload a file/folder item to a category folder (no face recognition)."""
    from app.services.drive_service import get_or_create_drive_folder, get_drive_service
    from app.services.drive_cache import LOCAL_CACHE_DIR, save_cached_file, get_cached_json, save_cached_json
    from googleapiclient.http import MediaFileUpload
    
    try:
        # 1. Resolve folder ID on Google Drive
        folder_id = get_or_create_drive_folder(category_name)
        
        # 2. Write file content to a temporary location
        temp_dir = LOCAL_CACHE_DIR / "uploads"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_path = temp_dir / file.filename
        
        contents = await file.read()
        temp_path.write_bytes(contents)
        
        # 3. Upload to Google Drive
        from app.services.drive_service import execute_with_retry
        file_metadata = {
            "name": file.filename,
            "parents": [folder_id]
        }
        media = MediaFileUpload(str(temp_path), mimetype=file.content_type, resumable=True)
        drive_file = execute_with_retry(lambda svc: svc.files().create(body=file_metadata, media_body=media, fields="id"))
        drive_id = drive_file.get("id")
        
        # 4. Generate and save size-400 thumbnail
        is_video = file.content_type.startswith("video/") if file.content_type else False
        if not file.content_type:
            ext = Path(file.filename).suffix.lower()
            is_video = ext in (".mp4", ".mov", ".avi", ".mkv", ".webm")
            
        thumb_bytes = create_media_thumbnail(temp_path, is_video=is_video, size=400)
        if thumb_bytes:
            save_cached_file(f"thumb_{drive_id}_400.jpg", thumb_bytes, mime_type="image/jpeg")
            
        # Cleanup temp file
        if temp_path.exists():
            temp_path.unlink()
            
        # 5. Insert photo record in Supabase database
        upsert_res = supabase.table("photos").upsert({
            "drive_path": drive_id,
            "is_common": False,
            "face_count": 0
        }, on_conflict="drive_path").execute()
        
        # 6. Add drive_id to categories index JSON
        categories = get_cached_json("categories.json") or {}
        if category_name not in categories:
            categories[category_name] = []
        if drive_id not in categories[category_name]:
            categories[category_name].append(drive_id)
        save_cached_json("categories.json", categories)
        
        # 7. Update filename mapping
        try:
            filename_map = get_cached_json("drive_filename_map.json") or {}
            filename_map[file.filename] = drive_id
            save_cached_json("drive_filename_map.json", filename_map)
            get_drive_id_to_mime_map.cache_clear()
        except Exception as map_err:
            log.warning(f"Failed to update filename map: {map_err}")
            
        return {"success": True, "drive_id": drive_id}
        
    except Exception as e:
        log.error(f"Error uploading category photo: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class SharePhotoRequest(BaseModel):
    drive_id: str
    guest_id: str


@router.post("/share", dependencies=[Depends(require_admin)])
def share_photo_with_guest(body: SharePhotoRequest):
    """Manually share/associate a photo (by drive_id) with a guest's album."""
    guest_id = body.guest_id.replace("guest_", "")
    drive_id = body.drive_id
    
    try:
        # 1. Verify guest exists
        guest_res = supabase.table("guests").select("id").eq("id", guest_id).execute()
        if not guest_res.data:
            raise HTTPException(status_code=404, detail="Guest not found")
            
        # 2. Get or create photo in database
        photo_res = supabase.table("photos").select("id").eq("drive_path", drive_id).execute()
        if not photo_res.data:
            # Register dynamically
            mime_map = get_drive_id_to_mime_map()
            mime_type = mime_map.get(drive_id, "image/jpeg")
            
            insert_res = supabase.table("photos").insert({
                "drive_path": drive_id,
                "is_common": False,
                "face_count": 1
            }).execute()
            if not insert_res.data:
                raise HTTPException(status_code=500, detail="Failed to register photo in database")
            photo_id = insert_res.data[0]["id"]
        else:
            photo_id = photo_res.data[0]["id"]
            
        # 3. Associate photo with guest in guest_photos table
        supabase.table("guest_photos").upsert({
            "guest_id": guest_id,
            "photo_id": photo_id
        }, on_conflict="guest_id,photo_id").execute()
        
        return {"success": True, "message": "Photo shared successfully"}
    except Exception as e:
        log.error(f"Error sharing photo: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/all", dependencies=[Depends(require_admin)])
def get_all_photos(page: int = 1, limit: int = 50):
    """
    Return ALL media from the Drive (drive_filename_map.json), paginated.
    Used for the 'All Moments' tab so every guest can browse the full gallery.
    """
    from app.services.drive_cache import get_cached_json
    name_map: dict = get_cached_json("drive_filename_map.json") or {}

    all_items = list(name_map.items())           # [(filename, drive_id), ...]
    total = len(all_items)
    offset = (page - 1) * limit
    page_items = all_items[offset: offset + limit]

    mime_map = get_drive_id_to_mime_map()
    photos = []
    for filename, drive_id in page_items:
        ext = Path(filename).suffix.lower()
        mime_type = mime_map.get(drive_id) or _EXT_MIME.get(ext, "image/jpeg")
        is_video = mime_type.startswith("video/")
        photos.append({
            "drive_id":   drive_id,
            "filename":   filename,
            "is_common":  False,
            "thumb_url":  f"/photos/thumb/{drive_id}",
            "stream_url": f"/photos/stream/{drive_id}",
            "is_video":   is_video,
            "mime_type":  mime_type,
        })

    return {
        "photos":   photos,
        "page":     page,
        "limit":    limit,
        "total":    total,
        "has_more": offset + limit < total,
    }


@router.get("/{drive_id}/people")
def get_people_in_photo(drive_id: str):
    """
    Return which guests / face clusters appear in a given photo.
    Looks up the file path in face_encodings.pkl clusters, then resolves names.
    Returns a list of {id, name, thumbnail_url, is_guest} objects.
    """
    from app.services.drive_cache import get_cached_json
    from app.routes.faces import get_face_clusters

    # Map drive_id → filename
    name_map: dict = get_cached_json("drive_filename_map.json") or {}
    id_to_name = {fid: fname for fname, fid in name_map.items()}
    filename = id_to_name.get(drive_id)
    if not filename:
        return []

    target_path = f"GoogleDrive/{filename}"

    try:
        clusters = get_face_clusters()
    except Exception as e:
        log.warning("Could not load clusters for people-in-photo: %s", e)
        return []

    names_data: dict = get_cached_json("cluster_names.json") or {}

    # Also fetch registered guests for name lookup
    guest_name_map: dict[str, str] = {}   # guest_id → name
    try:
        guests_res = supabase.table("guests").select("id, name").execute()
        guest_name_map = {g["id"]: g["name"] for g in (guests_res.data or [])}
    except Exception:
        pass

    results = []
    seen_ids: set = set()

    for cid, cdata in clusters.items():
        if target_path not in cdata.get("photos", []):
            continue
        if cid in seen_ids:
            continue
        seen_ids.add(cid)

        is_guest = cid.startswith("guest_")
        if is_guest:
            guest_id = cid.replace("guest_", "")
            name = guest_name_map.get(guest_id, f"Guest #{guest_id}")
            thumbnail_url = f"/faces/guests/{guest_id}/selfie"
        else:
            name = names_data.get(cid, f"Person #{cid}")
            thumbnail_url = f"/faces/clusters/{cid}/thumbnail"

        results.append({
            "id":            cid,
            "name":          name,
            "thumbnail_url": thumbnail_url,
            "is_guest":      is_guest,
        })

    # Sort: named persons first, then by id
    results.sort(key=lambda x: (x["name"].startswith("Person #"), x["name"]))
    return results


@router.get("/{guest_id}")
async def get_guest_photos(
    guest_id: str,
    page: int = 1,
    limit: int = 50,
    filter: str = "all",
    caller: dict = Depends(guest_or_admin),
):
    """
    Returns paginated list of Drive file IDs for a guest household, with video indicators.
    Includes personal matching photos for all family members and common/group photos.
    Supports nested family member metadata for custom gallery views.
    """
    # A token unlocks its OWN album only. Guest ids are uuids so they are not
    # guessable in practice, but "hard to guess" is not access control — without
    # this, any valid guest token could read every other guest's album by id.
    if not caller.get("is_admin") and caller.get("id") != guest_id:
        raise HTTPException(status_code=403, detail="This link cannot open that album.")
    guest = supabase.table("guests").select("id, name").eq("id", guest_id).execute()
    if not guest.data:
        raise HTTPException(status_code=404, detail="Guest not found")

    # Dynamically associate named face clusters with guest if name matches.
    if guest_id not in _associated_guests:
        try:
            from app.services.face_service import associate_guest_by_name
            associated = associate_guest_by_name(guest_id, guest.data[0].get("name", ""))
            if associated >= 0:
                _associated_guests.add(guest_id)
                if associated > 0:
                    log.info(f"Auto-associated {associated} photos for guest '{guest.data[0].get('name')}' via name match.")
        except Exception as association_err:
            log.error(f"Failed to dynamically associate guest name: {association_err}")
            _associated_guests.add(guest_id)

    offset = (page - 1) * limit

    # `filter` decides which tab is being shown, and it MUST be applied here
    # rather than in the browser. The gallery fetched a page of 50 mixed photos
    # and filtered them client-side, so "Just Me" showed only the personal
    # photos that happened to fall in that page — for a guest with 1,316
    # personal photos the newest 50 were all group shots, so the tab looked
    # completely empty.
    from app.services.db_paging import fetch_all

    # How many photos this guest is in. A cheap count (no rows returned) that
    # replaces the old "fetch every personal id on every request" — that pulled
    # thousands of ids even for the Group Moments tab, which never needs them,
    # and was a big part of why the gallery felt slow.
    personal_count = (
        supabase.table("guest_photos")
        .select("photo_id", count="exact")
        .eq("guest_id", guest_id)
        .limit(1)
        .execute()
        .count
    ) or 0

    want = (filter or "all").lower()

    class _Rows:
        def __init__(self, data):
            self.data = data

    if want == "common":
        # Everyone sees these, so guest_photos need not be involved at all.
        count_res = supabase.table("photos").select("id", count="exact").eq("is_common", True).execute()
        total_count = count_res.count or 0
        result = supabase.table("photos").select("drive_path, is_common, face_count")            .eq("is_common", True)            .order("created_at", desc=True).order("id", desc=True)            .range(offset, offset + limit - 1)            .execute()

    elif want == "mine" and personal_count:
        # "Just Me" = every photo this guest is IN — solo shots AND group shots
        # they were matched or manually assigned to. A group photo she is in
        # shows here and in Group Moments both, which is correct: it is a photo
        # of her and a group moment. (Filtering these to is_common=False, an
        # earlier attempt, hid the group photos she is actually in.)
        #
        # Paginate the guest_photos -> photos join directly, newest first, so a
        # page is 50 rows instead of the whole album — this used to fetch every
        # one of her thousands of photos just to return one page. The ids stay
        # inside the join (never in the URL), and ordering is by the embedded
        # photos.created_at.
        # Order by the photo date (newest first), then by guest_photos.photo_id
        # as a tiebreaker. The tiebreaker is essential, not cosmetic: thousands
        # of photos share an identical imported created_at, and ordering by a
        # non-unique column alone makes each page's range non-deterministic, so
        # pages overlap and drop rows. (created_at + photo_id) is a total order,
        # so pagination is stable.
        total_count = personal_count
        rows = (
            supabase.table("guest_photos")
            .select("photo_id, photos!inner(drive_path, is_common, face_count, created_at)")
            .eq("guest_id", guest_id)
            .order("created_at", desc=True, foreign_table="photos")
            .order("photo_id", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        result = _Rows([r["photos"] for r in rows.data if r.get("photos")])

    elif personal_count:
        # "All Moments" (admin-only view) = every group photo plus every photo
        # this guest is in.
        # The obvious query — .or_("is_common.eq.true,id.in.(<personal ids>)") —
        # puts every personal id in the URL, and past ~4,000 of them (the bride)
        # that URL exceeds the length limit and the whole request 500s. So read
        # both sets paged (fetch_all also dodges the 1,000-row cap) and merge
        # them here. Personal photos come through the guest_photos join, exactly
        # like the "mine" branch, so their ids never touch the URL.
        common_rows = fetch_all(
            lambda a, b: supabase.table("photos")
            .select("drive_path, is_common, face_count, created_at")
            .eq("is_common", True)
            .range(a, b)
        )
        personal_rows = [
            r["photos"]
            for r in fetch_all(
                lambda a, b: supabase.table("guest_photos")
                .select("photos!inner(drive_path, is_common, face_count, created_at)")
                .eq("guest_id", guest_id)
                .range(a, b)
            )
            if r.get("photos")
        ]
        # Dedupe by drive_path — a group photo the guest is in is in both sets.
        merged = {}
        for p in common_rows + personal_rows:
            dp = p.get("drive_path")
            if dp and dp not in merged:
                merged[dp] = p
        # Newest first, matching the created_at desc ordering this branch used
        # back when it was a single query.
        # created_at is non-unique (bulk import shares timestamps), so add
        # drive_path as a tiebreaker — otherwise the slice boundary between
        # pages is non-deterministic and pages overlap.
        ordered = sorted(
            merged.values(),
            key=lambda p: (p.get("created_at") or "", p.get("drive_path") or ""),
            reverse=True,
        )
        total_count = len(ordered)
        result = _Rows(ordered[offset : offset + limit])

    else:
        count_res = supabase.table("photos").select("id", count="exact").eq("is_common", True).execute()
        total_count = count_res.count or 0
        
        result = supabase.table("photos").select("drive_path, is_common, face_count")\
            .eq("is_common", True)\
            .order("created_at", desc=True)\
            .order("id", desc=True)\
            .range(offset, offset + limit - 1)\
            .execute()

    # 3. Fetch family members registered under this guest/household
    try:
        members_res = supabase.table("family_members").select("id, name").eq("guest_id", guest_id).order("name").execute()
        family_members = members_res.data or []
    except Exception as e:
        log.error(f"Error fetching family members in get_guest_photos: {e}")
        family_members = []

    # 4. Map which photo belongs to which family member(s)
    photo_to_members = {}
    if family_members:
        member_ids = [m["id"] for m in family_members]
        try:
            m_photos = supabase.table("member_photos").select("member_id, photo_id, photos(drive_path)").in_("member_id", member_ids).execute()
            if m_photos.data:
                for row in m_photos.data:
                    m_id = row["member_id"]
                    photo_data = row.get("photos", {})
                    if photo_data:
                        drive_path = photo_data.get("drive_path")
                        if drive_path:
                            if drive_path not in photo_to_members:
                                photo_to_members[drive_path] = []
                            if m_id not in photo_to_members[drive_path]:
                                photo_to_members[drive_path].append(m_id)
        except Exception as e:
            log.error(f"Error mapping member photos in get_guest_photos: {e}")

    mime_map = get_drive_id_to_mime_map()
    photos = []

    for photo in result.data:
        drive_id = photo.get("drive_path")
        if not drive_id:
            continue

        mime_type = mime_map.get(drive_id, "image/jpeg")
        is_video = mime_type.startswith("video/")

        photos.append({
            "drive_id": drive_id,
            "is_common": photo.get("is_common", False),
            "thumb_url": f"/photos/thumb/{drive_id}",
            "stream_url": f"/photos/stream/{drive_id}",
            "is_video": is_video,
            "mime_type": mime_type,
            "member_ids": photo_to_members.get(drive_id, [])
        })

    return {
        "photos": photos,
        "page": page,
        "limit": limit,
        "total": total_count,
        "has_more": offset + limit < total_count,
        "family_members": family_members
    }


class NotMeBody(BaseModel):
    guest_id: str


@router.post("/{drive_id}/not-me")
async def guest_not_me(
    drive_id: str,
    body: NotMeBody,
    caller: dict = Depends(guest_or_admin),
):
    """
    Disassociate a photo from a guest's album permanently (guest-level 'Not Me' action).
    Guests may only do this to their own album.
    """
    guest_id = body.guest_id
    if not caller.get("is_admin") and caller.get("id") != guest_id:
        raise HTTPException(status_code=403, detail="This link cannot change that album.")
    try:
        # 1. Resolve database photo ID from drive_path
        photo_res = supabase.table("photos").select("id").eq("drive_path", drive_id).execute()
        if not photo_res.data:
            raise HTTPException(status_code=404, detail="Photo not found in registry.")
        
        photo_id = photo_res.data[0]["id"]
        
        # 2. Record disassociation (typed row, safe under concurrency)
        from app.services.face_state import add_disassociation
        add_disassociation(guest_id, photo_id)

        # 3. Delete row from guest_photos mapping table
        supabase.table("guest_photos").delete().eq("guest_id", guest_id).eq("photo_id", photo_id).execute()

        log.info(f"Guest {guest_id} marked photo {drive_id} (DB: {photo_id}) as 'Not Me'. Removed mapping.")
        return {"success": True, "message": "Photo disassociated from your gallery."}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error in guest_not_me: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{drive_id}", dependencies=[Depends(require_admin)])
async def delete_photo(drive_id: str):
    """
    Delete a photo/video:
    1. Moves it in Google Drive to the temp_delete folder.
    2. Deletes local cached originals and thumbnails.
    3. Removes face encodings and processed log entries.
    4. Deletes from Supabase photos/guest_photos databases.
    """
    try:
        from app.services.drive_service import execute_with_retry
        
        # 1. Retrieve current parents and filename from Google Drive
        try:
            file_meta = execute_with_retry(lambda svc: svc.files().get(fileId=drive_id, fields='parents, name'))
            previous_parents = ",".join(file_meta.get('parents', []))
            filename = file_meta.get('name')
        except Exception as drive_err:
            log.error(f"Failed to fetch file metadata from Drive for {drive_id}: {drive_err}")
            # Raise an exception so that we do not delete from database if the Drive operation failed
            raise HTTPException(status_code=500, detail=f"Failed to fetch Drive file metadata: {drive_err}")

        # 2. Get or create temp_delete folder ID and move file
        if previous_parents:
            try:
                from app.services.drive_service import get_or_create_temp_delete_folder
                temp_delete_id = get_or_create_temp_delete_folder()
                execute_with_retry(lambda svc: svc.files().update(
                    fileId=drive_id,
                    addParents=temp_delete_id,
                    removeParents=previous_parents,
                    fields='id, parents'
                ))
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

        # Remove from drive_filename_map.json
        try:
            from app.services.drive_cache import get_cached_json, save_cached_json
            name_map = get_cached_json("drive_filename_map.json") or {}
            map_modified = False
            if filename and filename in name_map:
                del name_map[filename]
                map_modified = True
            
            # Value check fallback
            keys_to_del = [k for k, v in name_map.items() if v == drive_id]
            if keys_to_del:
                for k in keys_to_del:
                    del name_map[k]
                map_modified = True
                
            if map_modified:
                save_cached_json("drive_filename_map.json", name_map)
                log.info(f"Removed {drive_id} (filename: {filename}) from drive_filename_map.json cache")
        except Exception as cache_err:
            log.error(f"Failed to remove from drive_filename_map.json: {cache_err}")

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


def create_media_thumbnail(file_path: Path, is_video: bool, size: int = 400) -> Optional[bytes]:
    try:
        from PIL import Image, ImageOps
        import cv2

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


class DownloadBatchRequest(BaseModel):
    drive_ids: list[str]


class DeleteBatchRequest(BaseModel):
    drive_ids: list[str]


@router.post("/download-batch")
def download_batch(body: DownloadBatchRequest):
    """
    Download multiple photos as a single ZIP file.
    """
    import zipfile
    from app.services.drive_service import download_file_to_memory
    from app.services.drive_cache import get_cached_json
    
    # 1. Fetch file names for the drive_ids to name files in zip
    id_to_name = {}
    try:
        name_map = get_cached_json("drive_filename_map.json")
        if name_map:
            id_to_name = {fid: name for name, fid in name_map.items()}
    except Exception as e:
        log.warning(f"Could not build filename map for batch download: {e}")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for fid in body.drive_ids:
            # Try to get from ORIGINALS_DIR first
            original_path = ORIGINALS_DIR / fid
            file_data = None
            if original_path.exists():
                try:
                    file_data = original_path.read_bytes()
                except Exception:
                    pass
            if not file_data:
                # Fall back to downloading from Drive
                file_data = download_file_to_memory(fid)
            
            if file_data:
                filename = id_to_name.get(fid, f"photo_{fid}.jpg")
                zip_file.writestr(filename, file_data)
                
    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=wedding_photos.zip"}
    )


@router.post("/delete-batch", dependencies=[Depends(require_admin)])
async def delete_photos_batch(body: DeleteBatchRequest):
    """
    Delete multiple photos/videos in batch:
    1. Moves them in Google Drive to the temp_delete folder.
    2. Deletes local cached originals and thumbnails.
    3. Removes face encodings and processed log entries in a single bulk update.
    4. Deletes from Supabase photos/guest_photos databases.
    """
    success_count = 0
    errors = []
    
    from app.services.drive_service import execute_with_retry, get_or_create_temp_delete_folder
    from app.services.drive_cache import get_cached_file, save_cached_file, delete_cached_file
    from app.services.face_service import get_filename_map
    
    try:
        temp_delete_id = get_or_create_temp_delete_folder()
    except Exception as e:
        log.error(f"Failed to fetch/create temp_delete folder: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to set up deletion folder: {e}")

    # Load face_encodings.pkl and processed_files.txt once
    all_encodings = []
    encodings_modified = False
    try:
        encodings_data = get_cached_file("face_encodings.pkl")
        if encodings_data:
            all_encodings = pickle.loads(encodings_data)
    except Exception as pkl_err:
        log.error(f"Failed to load face encodings pickle: {pkl_err}")

    processed_lines = []
    processed_modified = False
    try:
        progress_data = get_cached_file("processed_files.txt")
        if progress_data:
            processed_lines = progress_data.decode("utf-8").splitlines()
    except Exception as log_err:
        log.error(f"Failed to load processed log: {log_err}")

    mapping = get_filename_map()

    # Get DB ids for all drive_ids to do a batch DB delete
    try:
        db_photos_res = supabase.table("photos").select("id, drive_path").in_("drive_path", body.drive_ids).execute()
        db_photo_map = {row["drive_path"]: row["id"] for row in db_photos_res.data} if db_photos_res.data else {}
    except Exception as db_err:
        log.error(f"Failed to fetch database photo IDs: {db_err}")
        db_photo_map = {}

    mapping_modified = False
    for drive_id in body.drive_ids:
        try:
            # 1. Retrieve current parents and filename from Google Drive
            file_meta = execute_with_retry(lambda svc: svc.files().get(fileId=drive_id, fields='parents, name'))
            previous_parents = ",".join(file_meta.get('parents', []))
            filename = file_meta.get('name')
            
            # Move file on Drive
            if previous_parents:
                execute_with_retry(lambda svc: svc.files().update(
                    fileId=drive_id,
                    addParents=temp_delete_id,
                    removeParents=previous_parents,
                    fields='id, parents'
                ))
            
            # 2. Delete local cached files
            orig_file = ORIGINALS_DIR / drive_id
            if orig_file.exists():
                try:
                    orig_file.unlink()
                except Exception:
                    pass
            
            delete_cached_file(f"thumb_{drive_id}_400.jpg")
            
            # 3. Filter face encodings — match on Drive id, never on basename.
            # Over half the corpus shares a basename with a different photo, so
            # filtering by name would delete unrelated photos' encodings too.
            if filename:
                from scripts.face_engine.matching import drive_id_from_path

                def _record_drive_id(item):
                    return drive_id_from_path(item["path"]) or mapping.get(
                        Path(item["path"]).name
                    )

                initial_enc_len = len(all_encodings)
                all_encodings = [
                    item for item in all_encodings if _record_drive_id(item) != drive_id
                ]
                if len(all_encodings) < initial_enc_len:
                    encodings_modified = True
                
                # Filter processed_files.txt
                initial_lines_len = len(processed_lines)
                processed_lines = [line for line in processed_lines if line.strip() not in (drive_id, filename)]
                if len(processed_lines) < initial_lines_len:
                    processed_modified = True

            # Remove from filename mapping
            if filename and filename in mapping:
                del mapping[filename]
                mapping_modified = True
            else:
                keys_to_del = [k for k, v in mapping.items() if v == drive_id]
                if keys_to_del:
                    for k in keys_to_del:
                        del mapping[k]
                    mapping_modified = True

            success_count += 1
        except Exception as file_err:
            log.error(f"Error deleting file {drive_id} in batch: {file_err}")
            errors.append({"drive_id": drive_id, "error": str(file_err)})

    # Save modified caches if anything was deleted
    if encodings_modified:
        try:
            save_cached_file("face_encodings.pkl", pickle.dumps(all_encodings), mime_type="application/octet-stream")
            from app.services.face_service import load_encodings
            load_encodings.cache_clear()
        except Exception as pkl_save_err:
            log.error(f"Failed to save updated face encodings pickle: {pkl_save_err}")

    if processed_modified:
        try:
            save_cached_file("processed_files.txt", ("\n".join(processed_lines) + "\n").encode("utf-8"), mime_type="text/plain")
        except Exception as log_save_err:
            log.error(f"Failed to save updated progress log: {log_save_err}")

    if mapping_modified:
        try:
            from app.services.drive_cache import save_cached_json
            save_cached_json("drive_filename_map.json", mapping)
            log.info("Saved updated drive_filename_map.json cache")
        except Exception as map_save_err:
            log.error(f"Failed to save updated drive_filename_map.json: {map_save_err}")

    # Batch delete from database
    db_ids_to_delete = [db_photo_map[did] for did in body.drive_ids if did in db_photo_map]
    if db_ids_to_delete:
        try:
            # Delete references from guest_photos
            supabase.table("guest_photos").delete().in_("photo_id", db_ids_to_delete).execute()
            # Delete rows from photos
            supabase.table("photos").delete().in_("id", db_ids_to_delete).execute()
            log.info(f"Batch deleted photo records {db_ids_to_delete} from Supabase")
        except Exception as db_del_err:
            log.error(f"Failed to batch delete from database: {db_del_err}")
            errors.append({"database": str(db_del_err)})

    return {"success": True, "deleted_count": success_count, "errors": errors}


