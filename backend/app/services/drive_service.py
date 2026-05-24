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


def get_drive_service():
    """Return a Drive API client local to the current thread."""
    if not hasattr(_thread_local, "service"):
        creds = service_account.Credentials.from_service_account_file(
            settings.GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES
        )
        _thread_local.service = build(
            "drive", "v3", credentials=creds, cache_discovery=False
        )
    return _thread_local.service


# ── Folder traversal ──────────────────────────────────────────────────────────


def list_files_in_folder(folder_id: str, media_type: str = "all") -> list[dict]:
    """
    Recursively list all files under a folder.
    media_type: 'photos' | 'videos' | 'all'
    """
    service = get_drive_service()
    results = []

    def _recurse(fid: str):
        page_token = None
        while True:
            resp = (
                service.files()
                .list(
                    q=f"'{fid}' in parents and trashed = false",
                    spaces="drive",
                    fields="nextPageToken, files(id, name, mimeType, parents, size)",
                    pageToken=page_token,
                    pageSize=1000,
                )
                .execute()
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


def get_all_videos() -> list[dict]:
    return list_files_in_folder(settings.GOOGLE_DRIVE_FOLDER_ID, media_type="videos")


# ── File access ───────────────────────────────────────────────────────────────


def get_file_metadata(file_id: str) -> Optional[dict]:
    """Fetch metadata for a single file."""
    try:
        service = get_drive_service()
        return (
            service.files()
            .get(fileId=file_id, fields="id, name, mimeType, size")
            .execute()
        )
    except Exception as e:
        log.error(f"Could not fetch metadata for {file_id}: {e}")
        return None


def get_or_create_temp_delete_folder() -> str:
    """
    Finds or creates a 'temp_delete' folder inside the main event folder.
    Returns the Google Drive folder ID of this temp_delete folder.
    """
    service = get_drive_service()
    parent_id = settings.GOOGLE_DRIVE_FOLDER_ID

    # 1. Search for existing 'temp_delete' folder inside parent_id
    query = f"name = 'temp_delete' and mimeType = 'application/vnd.google-apps.folder' and '{parent_id}' in parents and trashed = false"
    try:
        results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
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
        folder = service.files().create(body=folder_metadata, fields='id').execute()
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
        log.error(f"Could not download file {file_id}: {e}")
        return None


def generate_signed_url(file_id: str) -> str:
    """
    Returns a direct viewable link for a Drive file.
    Note: works because service account has viewer access.
    For extra security we serve files through our backend proxy instead.
    """
    return f"https://drive.google.com/uc?export=download&id={file_id}"


# ── Sync utility ──────────────────────────────────────────────────────────────


def get_folder_structure(include_counts: bool = False) -> dict:
    """
    Returns the full folder tree for debugging.
    Maps camera folder names to their file lists.
    """
    service = get_drive_service()
    root_id = settings.GOOGLE_DRIVE_FOLDER_ID

    structure = {"PHOTOS": {}, "VIDEOS": {}}

    # Get top-level subfolders (Photos, Videos)
    top = (
        service.files()
        .list(
            q=f"'{root_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id, name)",
        )
        .execute()
        .get("files", [])
    )

    for folder in top:
        name = folder["name"]  # Photos or Videos
        if name not in structure:
            continue

        # Get camera subfolders
        subs = (
            service.files()
            .list(
                q=f"'{folder['id']}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields="files(id, name)",
            )
            .execute()
            .get("files", [])
        )

        for sub in subs:
            folder_info = {"folder_id": sub["id"]}
            if include_counts:
                folder_info["file_count"] = len(list_files_in_folder(sub["id"]))

            structure[name][sub["name"]] = folder_info

    return structure


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
        service = get_drive_service()
        # Search for folder
        q = "mimeType='application/vnd.google-apps.folder' and name='WeddingSnap Cache' and trashed=false"
        resp = service.files().list(q=q, fields="files(id)").execute()
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
            folder = service.files().create(body=file_metadata, fields="id").execute()
            _cache_folder_id = folder.get("id")
            log.info(
                f"Created new WeddingSnap Cache folder on Google Drive (ID: {_cache_folder_id})"
            )

        return _cache_folder_id
    except Exception as e:
        log.error(f"Failed to get or create WeddingSnap Cache folder: {e}")
        # Fall back to root folder if cache folder creation fails
        return settings.GOOGLE_DRIVE_FOLDER_ID


def upload_file_to_drive(file_path: Path, filename: str, folder_id: str = None) -> bool:
    """Upload or update a local file in the Google Drive cache folder."""
    if folder_id is None:
        folder_id = get_cache_folder_id()

    try:
        service = get_drive_service()

        # 1. Search if the file already exists in the target folder
        q = f"'{folder_id}' in parents and name='{filename}' and trashed = false"
        resp = service.files().list(q=q, fields="files(id)").execute()
        files = resp.get("files", [])

        media = MediaFileUpload(
            str(file_path), mimetype="application/octet-stream", resumable=True
        )

        if files:
            # File exists, update content
            file_id = files[0]["id"]
            service.files().update(fileId=file_id, media_body=media).execute()
            log.info(
                f"Uploaded and updated Google Drive file: {filename} (ID: {file_id})"
            )
        else:
            # File does not exist, create it
            file_metadata = {"name": filename, "parents": [folder_id]}
            new_file = (
                service.files()
                .create(body=file_metadata, media_body=media, fields="id")
                .execute()
            )
            log.info(
                f"Uploaded new Google Drive file: {filename} (ID: {new_file['id']})"
            )
        return True
    except Exception as e:
        log.error(f"Failed to upload {filename} to Google Drive: {e}")
        return False


def download_file_by_name(
    filename: str, dest_path: Path, folder_id: str = None
) -> bool:
    """Download a file by name from the Google Drive cache folder."""
    if folder_id is None:
        folder_id = get_cache_folder_id()

    try:
        service = get_drive_service()
        q = f"'{folder_id}' in parents and name='{filename}' and trashed = false"
        resp = service.files().list(q=q, fields="files(id)").execute()
        files = resp.get("files", [])

        if not files:
            log.info(
                f"File {filename} not found on Google Drive (this is normal for a fresh run)."
            )
            return False

        file_id = files[0]["id"]
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        log.info(f"Downloading {filename} from Google Drive (ID: {file_id})...")
        request = service.files().get_media(fileId=file_id)
        with open(dest_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request, chunksize=1024 * 1024 * 5)
            done = False
            while not done:
                _, done = downloader.next_chunk()

        log.info(f"Successfully downloaded {filename} to {dest_path}")
        return True
    except Exception as e:
        log.error(f"Failed to download {filename} from Google Drive: {e}")
        return False
