"""
Per-media cache: skip reprocessing unchanged files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pickle
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


def file_content_hash(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """SHA256 of file bytes (for local files)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def drive_file_key(file_id: str, file_name: str, md5_checksum: Optional[str] = None) -> str:
    """Stable cache key for Google Drive files."""
    if md5_checksum:
        return f"drive:{file_id}:{md5_checksum}"
    return f"drive:{file_id}:{file_name}"


class MediaCache:
    """JSON index + optional per-file pickle payloads on SSD."""

    def __init__(self, cache_dir: Path, index_name: str = "media_index.json"):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = cache_dir / index_name
        self._index: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.index_path.exists():
            try:
                self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("Could not load media cache index: %s", e)
                self._index = {}

    def save_index(self) -> None:
        self.index_path.write_text(
            json.dumps(self._index, indent=2),
            encoding="utf-8",
        )

    def get(self, key: str) -> Optional[dict]:
        return self._index.get(key)

    def is_unchanged(self, key: str, content_hash: str) -> bool:
        entry = self._index.get(key)
        if not entry:
            return False
        return entry.get("content_hash") == content_hash and entry.get("result_file")

    def load_result(self, key: str) -> Optional[dict]:
        entry = self._index.get(key)
        if not entry:
            return None
        result_file = entry.get("result_file")
        if not result_file:
            return None
        path = self.cache_dir / result_file
        if not path.exists():
            return None
        try:
            with open(path, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            log.warning("Failed to load cached result for %s: %s", key, e)
            return None

    def store_result(self, key: str, content_hash: str, result: dict) -> None:
        safe_name = hashlib.md5(key.encode()).hexdigest() + ".pkl"
        path = self.cache_dir / safe_name
        with open(path, "wb") as f:
            pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
        self._index[key] = {
            "content_hash": content_hash,
            "result_file": safe_name,
        }
