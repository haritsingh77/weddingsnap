"""
Google Drive service — lists, fetches, and streams files
from the wedding folder structure.
"""

import io
import json
import logging
import threading
from pathlib import Path
from functools import lru_cache
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

from app.config import settings

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]

PHOTO_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic"}
VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-matroska",
}


# ── Auth ──────────────────────────────────────────────────────────────────────

# Thread-local storage: each thread gets its own Drive service instance.
# This prevents SSL corruption when multiple threads share one HTTP connection.
_thread_local = threading.local()


def get_drive_service(fresh: bool = False):
    """Return a Drive API client local to the current thread, or a fresh one if requested."""
    import httplib2
    if fresh or not hasattr(_thread_local, "service"):
        import os
        import json
        google_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT")
        if google_json:
            try:
                info = json.loads(google_json.strip())
                creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            except Exception as e:
                log.error(f"Failed to load service account credentials from GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT: {e}")
                creds = service_account.Credentials.from_service_account_file(
                    settings.GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES
                )
        else:
            creds = service_account.Credentials.from_service_account_file(
                settings.GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES
            )
        
        # Guarantee a fresh connection pool to avoid SSL EOF/reset errors
        service = build(
            "drive", "v3", credentials=creds, cache_discovery=False
        )
        if fresh:
            return service
        _thread_local.service = service
    return _thread_local.service


def execute_with_retry(api_call_func, max_retries=3):
    """
    Executes a Google Drive API call. If it fails due to connection/SSL errors,
    it clears the cached thread-local drive service, gets a fresh one, 
    re-creates the request by calling api_call_func with the new service, and retries.
    """
    import time
    for attempt in range(max_retries):
        try:
            service = get_drive_service()
            request = api_call_func(service)
            return request.execute()
        except Exception as e:
            err_str = str(e).lower()
            is_conn_error = any(
                term in err_str 
                for term in ["eof occurred in violation of protocol", "broken pipe", "connection reset", "ssl", "timeout", "socket"]
            )
            if is_conn_error and attempt < max_retries - 1:
                log.warning(f"Google Drive API call failed (SSL/connection error): {e}. Retrying with fresh service (attempt {attempt + 1}/{max_retries})...")
                if hasattr(_thread_local, "service"):
                    delattr(_thread_local, "service")
                time.sleep(0.5)
                continue
            else:
                raise e


# ── Folder traversal ──────────────────────────────────────────────────────────


def list_files_in_folder(folder_id: str, media_type: str = "all") -> list[dict]:
    """
    Recursively list all files under a folder.
    media_type: 'photos' | 'videos' | 'all'
    """
    results = []

    def _recurse(fid: str):
        page_token = None
        while True:
            resp = execute_with_retry(
                lambda service: service.files().list(
                    q=f"'{fid}' in parents and trashed = false",
                    spaces="drive",
                    fields="nextPageToken, files(id, name, mimeType, parents, size)",
                    pageToken=page_token,
                    pageSize=1000,
                )
            )

            for f in resp.get("files", []):
                mime = f.get("mimeType", "")
                if mime == "application/vnd.google-apps.folder":
                    _recurse(f["id"])  # go deeper
                elif media_type == "photos" and mime in PHOTO_MIME_TYPES:
                    results.append(f)
                elif media_type == "videos" and mime in VIDEO_MIME_TYPES:
                    results.append(f)
                elif media_type == "all" and (
                    mime in PHOTO_MIME_TYPES or mime in VIDEO_MIME_TYPES
                ):
                    results.append(f)

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    _recurse(folder_id)
    log.info(f"Found {len(results)} files under folder {folder_id}")
    return results


def get_all_photos() -> list[dict]:
    return list_files_in_folder(settings.GOOGLE_DRIVE_FOLDER_ID, media_type="photos")


def get_file_metadata(file_id: str) -> Optional[dict]:
    """Fetch metadata for a single file."""
    try:
        return execute_with_retry(
            lambda service: service.files().get(fileId=file_id, fields="id, name, mimeType, size")
        )
    except Exception as e:
        log.error(f"Could not fetch metadata for {file_id}: {e}")
        return None


def get_or_create_temp_delete_folder() -> str:
    """
    Finds or creates a 'temp_delete' folder inside the main event folder.
    Returns the Google Drive folder ID of this temp_delete folder.
    """
    if settings.GOOGLE_DRIVE_TEMP_DELETE_FOLDER_ID:
        log.info(f"Using configured temp_delete folder ID: {settings.GOOGLE_DRIVE_TEMP_DELETE_FOLDER_ID}")
        return settings.GOOGLE_DRIVE_TEMP_DELETE_FOLDER_ID

    parent_id = settings.GOOGLE_DRIVE_FOLDER_ID

    # 1. Search for existing 'temp_delete' folder inside parent_id
    query = f"name = 'temp_delete' and mimeType = 'application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed = false"
    try:
        results = execute_with_retry(
            lambda service: service.files().list(q=query, spaces='drive', fields='files(id, name)')
        )
        files = results.get('files', [])
        if files:
            log.info(f"Found existing temp_delete folder: {files[0]['id']}")
            return files[0]['id']
    except Exception as search_err:
        log.warning(f"Error searching for temp_delete folder: {search_err}")

    # 2. Create the folder if not found
    try:
        folder_metadata = {
            'name': 'temp_delete',
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        folder = execute_with_retry(
            lambda service: service.files().create(body=folder_metadata, fields='id')
        )
        log.info(f"Created new temp_delete folder with ID: {folder['id']}")
        return folder['id']
    except Exception as create_err:
        log.error(f"Failed to create temp_delete folder: {create_err}")
        raise create_err


CACHE_DIR = Path("/tmp/weddingsnap_cache")
ORIGINALS_DIR = CACHE_DIR / "originals"
THUMBNAILS_DIR = CACHE_DIR / "thumbnails"

ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)


def download_file_to_memory(file_id: str) -> Optional[bytes]:
    """
    Download a file from Drive into memory, caching it on disk.
    Used during face matching for guest selfie comparison and streaming.
    """
    cache_path = ORIGINALS_DIR / file_id
    if cache_path.exists():
        try:
            return cache_path.read_bytes()
        except Exception as e:
            log.error(f"Failed to read cached original {file_id}: {e}")

    max_retries = 3
    import time
    for attempt in range(max_retries):
        try:
            service = get_drive_service()
            request = service.files().get_media(fileId=file_id)
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

            data = buffer.getvalue()
            try:
                cache_path.write_bytes(data)
            except Exception as e:
                log.error(f"Failed to write cache for original {file_id}: {e}")

            return data
        except Exception as e:
            err_str = str(e).lower()
            is_conn_error = any(
                term in err_str 
                for term in ["eof occurred in violation of protocol", "broken pipe", "connection reset", "ssl", "timeout", "socket"]
            )
            if is_conn_error and attempt < max_retries - 1:
                log.warning(f"Download for file {file_id} failed (SSL/connection error): {e}. Retrying with fresh service (attempt {attempt + 1}/{max_retries})...")
                if hasattr(_thread_local, "service"):
                    delattr(_thread_local, "service")
                time.sleep(0.5)
                continue
            else:
                log.error(f"Could not download file {file_id} after {attempt + 1} attempts: {e}")
                return None


def download_file_from_drive(file_id: str, dest_path: Path) -> bool:
    """Download a file from Google Drive directly to a local disk path, with retry on SSL/connection errors."""
    max_retries = 3
    import time
    for attempt in range(max_retries):
        try:
            service = get_drive_service()
            request = service.files().get_media(fileId=file_id)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                downloader = MediaIoBaseDownload(f, request, chunksize=1024 * 1024 * 5)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            return True
        except Exception as e:
            err_str = str(e).lower()
            is_conn_error = any(
                term in err_str 
                for term in ["eof occurred in violation of protocol", "broken pipe", "connection reset", "ssl", "timeout", "socket"]
            )
            if is_conn_error and attempt < max_retries - 1:
                log.warning(f"Download of file {file_id} to disk failed (SSL/connection error): {e}. Retrying with fresh service (attempt {attempt + 1}/{max_retries})...")
                if hasattr(_thread_local, "service"):
                    delattr(_thread_local, "service")
                time.sleep(0.5)
                continue
            else:
                log.error(f"Failed to download {file_id} to {dest_path} after {attempt + 1} attempts: {e}")
                if dest_path.exists():
                    try:
                        dest_path.unlink()
                    except Exception:
                        pass
                return False


def build_filename_to_id_map() -> dict[str, str]:
    """
    Returns a dict mapping filename → Drive file ID.
    Caches the mapping in Supabase Storage to avoid expensive Google Drive API calls.
    """
    from app.services.drive_cache import get_cached_json, save_cached_json

    cached_mapping = get_cached_json("drive_filename_map.json")
    if cached_mapping is not None:
        log.info("Loading Google Drive filename mapping from Supabase Storage cache")
        return cached_mapping

    log.info("Fetching complete file list from Google Drive (first-time build)...")
    all_files = list_files_in_folder(settings.GOOGLE_DRIVE_FOLDER_ID, media_type="all")
    mapping = {f["name"]: f["id"] for f in all_files}

    try:
        save_cached_json("drive_filename_map.json", mapping)
        log.info("Saved Google Drive filename mapping to Supabase Storage cache")
    except Exception as e:
        log.error(f"Failed to save Drive cache file to Supabase: {e}")

    return mapping



# ── Cloud Cache Utilities (Google Drive-native storage) ────────────────────────

_cache_folder_id = None


def get_cache_folder_id() -> str:
    """Retrieve or create a dedicated 'WeddingSnap Cache' folder in the service account's Google Drive."""
    global _cache_folder_id
    if _cache_folder_id is not None:
        return _cache_folder_id

    if settings.GOOGLE_DRIVE_CACHE_FOLDER_ID:
        _cache_folder_id = settings.GOOGLE_DRIVE_CACHE_FOLDER_ID
        log.info(
            f"Using user-specified Google Drive cache folder (ID: {_cache_folder_id})"
        )
        return _cache_folder_id

    try:
        # Search for folder
        q = "mimeType='application/vnd.google-apps.folder' and name='WeddingSnap Cache' and trashed=false"
        resp = execute_with_retry(
            lambda service: service.files().list(q=q, fields="files(id)")
        )
        files = resp.get("files", [])

        if files:
            _cache_folder_id = files[0]["id"]
            log.info(
                f"Found existing WeddingSnap Cache folder on Google Drive (ID: {_cache_folder_id})"
            )
        else:
            # Create folder
            file_metadata = {
                "name": "WeddingSnap Cache",
                "mimeType": "application/vnd.google-apps.folder",
            }
            folder = execute_with_retry(
                lambda service: service.files().create(body=file_metadata, fields="id")
            )
            _cache_folder_id = folder.get("id")
            log.info(
                f"Created new WeddingSnap Cache folder on Google Drive (ID: {_cache_folder_id})"
            )

        return _cache_folder_id
    except Exception as e:
        log.error(f"Failed to get or create WeddingSnap Cache folder: {e}")
        # Fall back to root folder if cache folder creation fails
        return settings.GOOGLE_DRIVE_FOLDER_ID


def get_or_create_drive_folder(name: str, parent_id: str = None) -> str:
    """
    Finds or creates a subfolder by name inside the main wedding folder.
    Returns the Google Drive folder ID.
    """
    if parent_id is None:
        parent_id = settings.GOOGLE_DRIVE_FOLDER_ID

    query = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed = false"
    try:
        results = execute_with_retry(
            lambda service: service.files().list(q=query, spaces='drive', fields='files(id, name)')
        )
        files = results.get('files', [])
        if files:
            log.info(f"Found existing subfolder '{name}': {files[0]['id']}")
            return files[0]['id']
    except Exception as search_err:
        log.warning(f"Error searching for folder '{name}': {search_err}")

    try:
        folder_metadata = {
            'name': name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        folder = execute_with_retry(
            lambda service: service.files().create(body=folder_metadata, fields='id')
        )
        log.info(f"Created new subfolder '{name}' with ID: {folder['id']}")
        return folder['id']
    except Exception as create_err:
        log.error(f"Failed to create subfolder '{name}': {create_err}")
        raise create_err
