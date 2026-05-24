"""
Supabase-backed cache service (previously named drive_cache).

Architecture:
  - Supabase Storage bucket ('weddingsnap-cache') = persistent store for thumbnails, face encodings, and cluster names
  - Local /tmp/weddingsnap_cache = ephemeral L1 speed layer (rebuilt from Supabase on cold start)

This is required for hosted deployments (Railway, Render, Fly.io etc.) where disk
is ephemeral and wiped on every restart.
"""

import io
import json
import logging
from pathlib import Path
from typing import Optional

from app.database import supabase

log = logging.getLogger(__name__)

# ── Local L1 ephemeral cache ──────────────────────────────────────────────────
LOCAL_CACHE_DIR = Path("/tmp/weddingsnap_cache")
LOCAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

BUCKET_NAME = "weddingsnap-cache"


def reload_indexes():
    """Dummy function to preserve compatibility with existing routes."""
    pass


# ── Public API ────────────────────────────────────────────────────────────────

def get_cached_file(filename: str) -> Optional[bytes]:
    """
    Retrieve a cached file.
    1. Check local L1 (/tmp) first (fast, ephemeral).
    2. Fall back to Supabase Storage bucket.
    Returns raw bytes or None if not found anywhere.
    """
    local_path = LOCAL_CACHE_DIR / filename
    if local_path.exists():
        try:
            return local_path.read_bytes()
        except Exception:
            pass

    try:
        data = supabase.storage.from_(BUCKET_NAME).download(filename)
        # Populate L1 cache
        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(data)
        except Exception:
            pass
        return data
    except Exception as e:
        log.debug(f"File '{filename}' not found or failed download from Supabase Storage: {e}")
        return None


def save_cached_file(filename: str, data: bytes, mime_type: str = "image/jpeg"):
    """
    Save data to Supabase Storage (persistent) and local L1 (fast).
    Creates a new file or updates existing one if already present.
    """
    # Always write to L1
    local_path = LOCAL_CACHE_DIR / filename
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)
    except Exception as e:
        log.warning(f"Failed to write L1 cache for '{filename}': {e}")

    try:
        supabase.storage.from_(BUCKET_NAME).upload(
            path=filename,
            file=data,
            file_options={
                "cache-control": "3600",
                "upsert": "true",
                "content-type": mime_type
            }
        )
        log.debug(f"Saved '{filename}' to Supabase Storage")
    except Exception as e:
        log.error(f"Failed to upload '{filename}' to Supabase Storage: {e}")


def delete_cached_file(filename: str):
    """Remove a file from both L1 and Supabase Storage."""
    local_path = LOCAL_CACHE_DIR / filename
    if local_path.exists():
        try:
            local_path.unlink()
        except Exception:
            pass

    try:
        supabase.storage.from_(BUCKET_NAME).remove([filename])
        log.debug(f"Deleted '{filename}' from Supabase Storage")
    except Exception as e:
        log.error(f"Failed to delete '{filename}' from Supabase Storage: {e}")


# ── JSON helpers (for cluster_names.json etc.) ────────────────────────────────

def get_cached_json(filename: str) -> Optional[dict]:
    """Load a JSON file from Supabase Storage. Returns dict or None."""
    data = get_cached_file(filename)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        log.error(f"Failed to parse cached JSON '{filename}': {e}")
        return None


def save_cached_json(filename: str, obj: dict):
    """Save a dict as JSON to Supabase Storage."""
    data = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
    save_cached_file(filename, data, mime_type="application/json")
