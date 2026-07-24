"""Drive record-path helpers.

Deliberately duplicated from scripts/face_engine/matching.py rather than
imported: `scripts/` is NOT in the deployed image (the Dockerfile ships the
light backend only), so a `from scripts...` inside a request handler raises at
runtime in production. That is exactly what made the People tab return 500 —
/faces/clusters imported drive_record_path from scripts on every call.

These are two lines each; a backend-local copy is far cheaper than shipping the
whole preprocessing package. Keep them behaviourally identical to the originals.
"""

from typing import Optional


def drive_record_path(file_id: str, file_name: str) -> str:
    """Build the record `path` for a Drive file.

    Filenames are NOT unique on Drive — thousands of files share a basename
    (two camera bodies both emitted DSC0xxxx.JPG), so keying on the basename
    alone resolved faces to the wrong photo. The id goes in the middle so
    Path(path).name still returns the bare filename while the path stays unique.
    """
    return f"GoogleDrive/{file_id}/{file_name}"


def drive_id_from_path(path: str) -> Optional[str]:
    """Recover the Drive file id from a record path, or None for legacy
    'GoogleDrive/<name>' records written before the id was carried through."""
    parts = str(path).split("/")
    if len(parts) >= 3 and parts[0] == "GoogleDrive":
        return parts[-2]
    return None
