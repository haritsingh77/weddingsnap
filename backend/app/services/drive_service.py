"""
Google Drive service — lists, fetches, and streams files
from the wedding folder structure.
"""

import io
import logging
from pathlib import Path
from functools import lru_cache
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from app.config import settings

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

PHOTO_MIME_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/heic"
}
VIDEO_MIME_TYPES = {
    "video/mp4", "video/quicktime", "video/x-msvideo", "video/x-matroska"
}


# ── Auth ──────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_drive_service():
    """Build and cache the Drive API client."""
    creds = service_account.Credentials.from_service_account_file(
        settings.GOOGLE_SERVICE_ACCOUNT_JSON,
        scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


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
            resp = service.files().list(
                q=f"'{fid}' in parents and trashed = false",
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, parents, size)",
                pageToken=page_token,
                pageSize=1000,
            ).execute()

            for f in resp.get("files", []):
                mime = f.get("mimeType", "")
                if mime == "application/vnd.google-apps.folder":
                    _recurse(f["id"])  # go deeper
                elif media_type == "photos" and mime in PHOTO_MIME_TYPES:
                    results.append(f)
                elif media_type == "videos" and mime in VIDEO_MIME_TYPES:
                    results.append(f)
                elif media_type == "all" and (mime in PHOTO_MIME_TYPES or mime in VIDEO_MIME_TYPES):
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
        return service.files().get(
            fileId=file_id,
            fields="id, name, mimeType, size"
        ).execute()
    except Exception as e:
        log.error(f"Could not fetch metadata for {file_id}: {e}")
        return None


def download_file_to_memory(file_id: str) -> Optional[bytes]:
    """
    Download a file from Drive into memory.
    Used during face matching for guest selfie comparison.
    """
    try:
        service = get_drive_service()
        request = service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()
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
    top = service.files().list(
        q=f"'{root_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)"
    ).execute().get("files", [])

    for folder in top:
        name = folder["name"]  # Photos or Videos
        if name not in structure:
            continue

        # Get camera subfolders
        subs = service.files().list(
            q=f"'{folder['id']}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id, name)"
        ).execute().get("files", [])

        for sub in subs:
            folder_info = {"folder_id": sub["id"]}
            if include_counts:
                folder_info["file_count"] = len(list_files_in_folder(sub["id"]))
            
            structure[name][sub["name"]] = folder_info

    return structure

def build_filename_to_id_map() -> dict[str, str]:
    """
    Returns a dict mapping filename → Drive file ID
    for all photos and videos.
    Used to bridge local preprocessor paths to Drive IDs.
    """
    all_files = (
        list_files_in_folder(settings.GOOGLE_DRIVE_PHOTOS_FOLDER_ID) +
        list_files_in_folder(settings.GOOGLE_DRIVE_VIDEOS_FOLDER_ID)
    )
    return {f["name"]: f["id"] for f in all_files}