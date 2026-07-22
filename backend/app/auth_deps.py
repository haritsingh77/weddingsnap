"""
Request authentication.

Before this existed, photos.py and faces.py had no auth at all: /photos/all
listed every photo in the wedding, /photos/stream/{id} served the original file,
and DELETE /photos/{id} was open to anyone. The backend URL is in the frontend
bundle, so "nobody knows the URL" was never protection. The invite code gated
the login screen only — every endpoint behind it was directly reachable.

Two credentials:

  guest token   X-Guest-Token, or ?t= for links a browser follows directly
                (image src, video src, download). Identifies one guest and
                grants READ access to that guest's own photos.

  admin         X-Admin-Password, as admin.py already used. Required for
                anything that changes or deletes.

Guests deliberately cannot reach mutations. The frontend used to decide this
itself — isAdmin was computed in the browser from the name the guest typed, so
anyone registering as "saurav" got rename, merge and delete controls.
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import Header, HTTPException, Query

from app.config import settings
from app.database import supabase

log = logging.getLogger(__name__)

TOKEN_BYTES = 24  # ~32 url-safe chars


def new_access_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def _lookup(token: str) -> Optional[dict]:
    try:
        rows = (
            supabase.table("guests")
            .select("id, name, is_household, access_revoked")
            .eq("access_token", token)
            .limit(1)
            .execute()
        ).data or []
    except Exception as e:
        log.error("Guest token lookup failed: %s", e)
        raise HTTPException(status_code=503, detail="Auth backend unavailable")

    if not rows:
        return None
    guest = rows[0]
    if guest.get("access_revoked"):
        return None
    return guest


def require_guest(
    x_guest_token: str | None = Header(None, alias="X-Guest-Token"),
    t: str | None = Query(None, description="token, for URLs the browser loads directly"),
) -> dict:
    """Resolve the caller to a guest, or 401.

    The query-string form exists because <img>, <video> and download links
    cannot carry custom headers. It is the same token either way; anything
    sensitive enough to matter is behind an admin check instead.
    """
    token = (x_guest_token or t or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="This link is missing its access code.")

    guest = _lookup(token)
    if not guest:
        # Deliberately identical for unknown and revoked, so the response does
        # not confirm which tokens ever existed.
        raise HTTPException(status_code=401, detail="This link is no longer valid.")
    return guest


def require_admin(
    x_admin_password: str | None = Header(None, alias="x-admin-password"),
    password: str | None = Query(None),
) -> bool:
    supplied = (x_admin_password or password or "").strip()
    expected = (settings.ADMIN_PASSWORD or "").strip()
    if not expected:
        # Refuse rather than fall open — an unset password must not mean
        # "everyone is admin".
        log.error("ADMIN_PASSWORD is not set; refusing admin request")
        raise HTTPException(status_code=503, detail="Admin access is not configured.")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return True


def guest_or_admin(
    x_guest_token: str | None = Header(None, alias="X-Guest-Token"),
    t: str | None = Query(None),
    x_admin_password: str | None = Header(None, alias="x-admin-password"),
    password: str | None = Query(None),
) -> dict:
    """Read access for a guest, with admins allowed through as well.

    Used by endpoints an admin needs while browsing the gallery, where they hold
    no guest token of their own.
    """
    admin_supplied = (x_admin_password or password or "").strip()
    expected = (settings.ADMIN_PASSWORD or "").strip()
    if admin_supplied and expected and secrets.compare_digest(admin_supplied, expected):
        return {"id": None, "name": "admin", "is_admin": True}

    guest = require_guest(x_guest_token, t)
    guest["is_admin"] = False
    return guest
