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

# ── Local L1 ephemeral cache (prefer SSD on Windows) ─────────────────────────
def _local_cache_dir() -> Path:
    import os
    import platform
    ssd = os.getenv("WEDDINGSNAP_SSD_ROOT", "").strip()
    if ssd:
        return Path(ssd) / "api_cache"
    if platform.system() == "Windows":
        return Path(os.getenv("LOCALAPPDATA", ".")) / "weddingsnap" / "api_cache"
    return Path("/tmp/weddingsnap_cache")


LOCAL_CACHE_DIR = _local_cache_dir()
LOCAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

BUCKET_NAME = "weddingsnap-cache"


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

    is_thumbnail = filename.startswith("thumb_") or filename.startswith("face_cluster_")
    # Config JSON (categories.json, household_names.json, cluster_names.json…)
    # must persist too. Only thumbnails used to be uploaded, so everything else
    # lived in LOCAL_CACHE_DIR — which is /tmp on Cloud Run, wiped on every cold
    # start. Creating an album or naming a family looked like it worked and then
    # quietly vanished.
    is_config = filename.endswith(".json")

    if is_thumbnail:
        # Thumbnails are immutable and read-through-cached, so a background
        # upload is fine — losing one just re-generates on the next request.
        import threading
        def _upload():
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
                log.debug(f"Saved '{filename}' to Supabase Storage in background")
            except Exception as upload_err:
                log.error(f"Failed to upload '{filename}' to Supabase Storage in background: {upload_err}")
        threading.Thread(target=_upload, daemon=True).start()
    else:
        # Config JSON and everything else upload SYNCHRONOUSLY: the admin action
        # that triggered the write (delete, rename, album edit) must be durable
        # and visible to the other instances before the request returns. A
        # background thread could be killed on instance shutdown and silently
        # lose the change, or lose the race against another instance's re-pull.
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
            if is_config:
                _mark_json_fresh(filename)  # our L1 copy is now authoritative
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

# Config JSON (drive_filename_map, categories, household/cluster names,
# photo_people…) is MUTATED at runtime and lives on several Cloud Run instances
# at once. get_cached_file serves each instance's own /tmp copy first and never
# refreshes it, so a delete / rename / album edit on one instance stayed
# invisible to the others — most visibly, a batch-deleted photo reappeared in
# "All Moments" on the next page load because /photos/all read a stale map.
# Re-pull config JSON from Storage (authoritative) at most once per TTL, which
# bounds cross-instance staleness to a few seconds without hammering Storage.
_JSON_TTL_SECONDS = 15
_json_fetched_at: dict[str, float] = {}


def _mark_json_fresh(filename: str) -> None:
    """A local save just wrote the authoritative copy — no need to re-pull yet."""
    import time
    _json_fetched_at[filename] = time.time()


def get_cached_json(filename: str) -> Optional[dict]:
    """Load a JSON file, refreshing from Supabase Storage on a short TTL so
    runtime edits on other instances become visible. Returns dict or None."""
    import time
    now = time.time()
    if now - _json_fetched_at.get(filename, 0.0) >= _JSON_TTL_SECONDS:
        try:
            data = supabase.storage.from_(BUCKET_NAME).download(filename)
            _json_fetched_at[filename] = now
            try:
                (LOCAL_CACHE_DIR / filename).write_bytes(data)  # keep L1 consistent
            except Exception:
                pass
            return json.loads(data.decode("utf-8"))
        except Exception as e:
            # Not in Storage yet (or transient) — fall back to the L1 copy.
            log.debug(f"JSON '{filename}' Storage refresh failed, using L1: {e}")

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
