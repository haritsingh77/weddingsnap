"""
Postgres-backed face/guest state (Phase 1).

Replaces disassociated_photos.json (a whole-file read-modify-write blob in
Supabase Storage) with the guest_photo_disassociations table. The blob lost
concurrent updates and mixed string/int photo ids, so admin disassociations
could silently fail to survive re-matching.

Reads merge in the legacy JSON so history recorded before the migration still
counts; writes go to the table only. Run scripts/sync_encodings_to_db.py
--migrate-disassociations once to fold old JSON entries into the table.
"""

import logging

from app.database import supabase

log = logging.getLogger(__name__)


def get_disassociated_photo_ids(guest_id: str) -> set[int]:
    """All photo ids this guest has disassociated ('Not Me'), table + legacy JSON."""
    ids: set[int] = set()

    try:
        res = (
            supabase.table("guest_photo_disassociations")
            .select("photo_id")
            .eq("guest_id", guest_id)
            .execute()
        )
        ids.update(int(row["photo_id"]) for row in (res.data or []))
    except Exception as e:
        # Table may not exist yet (migration not run) — legacy JSON still covers us.
        log.debug(f"Disassociation table read failed for {guest_id}: {e}")

    try:
        from app.services.drive_cache import get_cached_json

        legacy = (get_cached_json("disassociated_photos.json") or {}).get(guest_id, [])
        for raw in legacy:
            try:
                ids.add(int(raw))
            except (TypeError, ValueError):
                log.warning(f"Skipping non-numeric legacy disassociation entry: {raw!r}")
    except Exception as e:
        log.debug(f"Legacy disassociation JSON read failed for {guest_id}: {e}")

    return ids


def add_disassociation(guest_id: str, photo_id: int) -> None:
    """Record that this photo must never be re-associated with this guest."""
    supabase.table("guest_photo_disassociations").upsert(
        {"guest_id": guest_id, "photo_id": int(photo_id)},
        on_conflict="guest_id,photo_id",
    ).execute()
