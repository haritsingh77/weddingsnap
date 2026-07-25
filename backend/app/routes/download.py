"""
Download route — streams the whole album as a ZIP, generated on the fly.

Each file goes from Drive straight into a ZIP entry and out to the client, so
memory stays flat regardless of album size. The earlier prepare/poll flow built
the entire archive in LOCAL_CACHE_DIR first — which is /tmp (RAM) on Cloud Run —
and OOM-killed the container on large albums; it has been removed.
"""

import logging
import zipfile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.auth_deps import guest_or_admin
from app.database import supabase

log = logging.getLogger(__name__)
# This router serves the actual photo archive, so it carries the same auth as
# photos.py and faces.py — the whole album in one file must not be reachable
# without a credential.
router = APIRouter(
    prefix="/download",
    tags=["download"],
    dependencies=[Depends(guest_or_admin)],
)

# Extension → correct file extension for download filename
_MIME_EXT = {
    "video/mp4": ".mp4", "video/quicktime": ".mov",
    "video/x-msvideo": ".avi", "video/x-matroska": ".mkv",
    "video/webm": ".webm",
}


def _collect_rows(guest_id: str) -> list[tuple]:
    """(drive_id, is_common, index) for every photo in the guest's download.

    Paged throughout. Unpaged, PostgREST capped this at 1000 rows without error,
    so a guest's zip quietly stopped at 1000 files — and a zip that downloads
    successfully but is missing most of the photos is worse than one that fails,
    because nobody notices.

    The personal ids also go through an .in_() filter, which lands in the URL, so
    a guest with thousands of photos would otherwise blow the URL length limit.
    """
    from app.services.db_paging import chunked, fetch_all

    personal_ids = [
        str(r["photo_id"])
        for r in fetch_all(
            lambda a, b: supabase.table("guest_photos")
            .select("photo_id")
            .eq("guest_id", guest_id)
            .range(a, b)
        )
    ]

    rows: list[dict] = []
    seen: set[str] = set()

    # Everything flagged common, for everyone.
    for r in fetch_all(
        lambda a, b: supabase.table("photos")
        .select("drive_path, is_common")
        .eq("is_common", True)
        .range(a, b)
    ):
        if r.get("drive_path") and r["drive_path"] not in seen:
            seen.add(r["drive_path"])
            rows.append(r)

    # Plus this guest's own photos, in id batches.
    for batch in chunked(personal_ids, 200):
        for r in (
            supabase.table("photos")
            .select("drive_path, is_common")
            .in_("id", batch)
            .execute()
        ).data or []:
            if r.get("drive_path") and r["drive_path"] not in seen:
                seen.add(r["drive_path"])
                rows.append(r)

    log.info(
        "Download for %s: %d file(s) (%d personal)",
        guest_id, len(rows), len(personal_ids),
    )
    return [
        (r["drive_path"], r.get("is_common", False), i)
        for i, r in enumerate(rows)
    ]


def _drive_bearer_token() -> str:
    """Service-account access token for Drive media (same source as the stream route)."""
    import os
    import json
    from google.oauth2 import service_account
    import google_auth_httplib2
    import httplib2

    content = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT")
    if content:
        info = json.loads(content.strip())
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"]
        )
    else:
        from app.config import settings
        creds = service_account.Credentials.from_service_account_file(
            settings.GOOGLE_SERVICE_ACCOUNT_JSON,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
    creds.refresh(google_auth_httplib2.Request(httplib2.Http()))
    return creds.token


class _ZipSink:
    """Unseekable sink ZipFile writes into; we hand the bytes to the client and
    drop them, so neither the whole archive nor a whole file is ever retained."""

    def __init__(self):
        self._parts = []

    def write(self, data) -> int:
        self._parts.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        if not self._parts:
            return b""
        out = b"".join(self._parts)
        self._parts.clear()
        return out


@router.get("/{guest_id}/all")
def download_all_streaming(guest_id: str, caller: dict = Depends(guest_or_admin)):
    """Stream the guest's whole album as a ZIP, generated on the fly.

    ZIP_STORED (no deflate): photos and videos are already compressed, so
    re-compressing only burns CPU. Files stream from Drive in 256 KB chunks
    straight into the archive, which streams to the client — flat memory.
    """
    if not caller.get("is_admin") and caller.get("id") != guest_id:
        raise HTTPException(status_code=403, detail="This link cannot download that album.")
    guest = supabase.table("guests").select("name").eq("id", guest_id).execute()
    if not guest.data:
        raise HTTPException(status_code=404, detail="Guest not found")

    rows = _collect_rows(guest_id)
    from app.routes.photos import get_drive_id_to_mime_map
    mime_map = get_drive_id_to_mime_map()

    def _entry_name(drive_id, is_common, index):
        mime = mime_map.get(drive_id, "image/jpeg")
        ext = _MIME_EXT.get(mime, ".mp4" if mime.startswith("video/") else ".jpg")
        folder = "Common Photos" if is_common else "My Photos"
        return f"{folder}/{drive_id}_{index}{ext}"

    def generate():
        import httpx
        token = _drive_bearer_token()
        sink = _ZipSink()
        ok = 0
        with httpx.Client(timeout=httpx.Timeout(30.0, read=300.0)) as client, \
                zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for drive_id, is_common, index in rows:
                url = f"https://www.googleapis.com/drive/v3/files/{drive_id}?alt=media"
                try:
                    with client.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as r:
                        if r.status_code != 200:
                            log.warning(f"download-all: skip {drive_id}, drive {r.status_code}")
                            continue
                        with zf.open(_entry_name(drive_id, is_common, index), "w") as entry:
                            for chunk in r.iter_bytes(256 * 1024):
                                entry.write(chunk)
                                out = sink.drain()
                                if out:
                                    yield out
                    ok += 1
                except Exception as e:
                    log.error(f"download-all: failed {drive_id}: {e}")
                out = sink.drain()
                if out:
                    yield out
        tail = sink.drain()
        if tail:
            yield tail
        log.info(f"download-all {guest_id}: streamed {ok}/{len(rows)} files")

    safe = "".join(c for c in guest.data[0]["name"] if c.isalnum() or c in " _-").strip().replace(" ", "_") or "wedding"
    return StreamingResponse(
        generate(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}_wedding_photos.zip"'},
    )
