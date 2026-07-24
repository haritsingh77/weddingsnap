"""
Auth routes — invite code verification and guest session management.
"""

import uuid
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth_deps import guest_or_admin
from app.database import supabase
from app.config import settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class InviteRequest(BaseModel):
    code: str
    name: str
    phone: str = ""          # optional


class InviteResponse(BaseModel):
    valid: bool
    guest_id: str
    event_name: str
    message: str
    has_selfie: bool


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_phone(raw: str) -> str:
    """Comparable form of a phone number.

    Guests type the same number many ways — "+91 98765 43210", "098765 43210",
    "9876543210". Comparing raw strings would treat those as different people,
    which is the failure this whole change exists to prevent. Keep digits only
    and compare the last 10, which is the subscriber number in India and
    ignores country code and trunk prefix.
    """
    digits = "".join(c for c in (raw or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/verify-invite", response_model=InviteResponse)
async def verify_invite(body: InviteRequest):
    """
    Step 1 of guest flow.
    Guest enters invite code + their name.
    Creates or reuses a guest record and returns guest_id for subsequent requests.
    """
    # Check invite code
    result = supabase.table("invite_codes").select("*").eq(
        "code", body.code.upper().strip()
    ).eq("active", True).execute()

    if not result.data:
        raise HTTPException(
            status_code=403,
            detail="Invalid invite code. Please check the link you received."
        )

    event = result.data[0]

    # Everyone shares one invite code, so name alone cannot identify a guest.
    # Two people called "Ravi Singh" previously collapsed onto the same record,
    # and the second to register was logged into the first one's account and
    # shown their photos. Phone number is the only distinguishing field we
    # collect, so it decides between same-named guests; where it can't, we ask
    # rather than guess.
    name = body.name.strip()
    phone = _normalize_phone(body.phone)
    code = body.code.upper().strip()

    # ilike with no wildcards is case-insensitive equality — but % and _ in a
    # name would be read as wildcards, so escape them.
    escaped = name.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    matches = (
        supabase.table("guests").select("*")
        .ilike("name", escaped)
        .eq("invite_code", code)
        .execute()
    ).data or []

    guest_id = None
    if matches:
        if phone:
            same_phone = [g for g in matches if _normalize_phone(g.get("phone") or "") == phone]
            if same_phone:
                guest_id = same_phone[0]["id"]
                log.info("Existing guest logged in by phone: %s (%s)", name, guest_id)
            else:
                unclaimed = [g for g in matches if not (g.get("phone") or "").strip()]
                if len(unclaimed) == 1:
                    # The one account for this name has never been claimed —
                    # this is that person adding their number.
                    guest_id = unclaimed[0]["id"]
                    supabase.table("guests").update({"phone": body.phone.strip()}).eq(
                        "id", guest_id
                    ).execute()
                    log.info("Claimed unclaimed guest record: %s (%s)", name, guest_id)
                else:
                    # Same name, different number -> a different person.
                    log.info("New guest sharing the name %r (%d existing)", name, len(matches))
        else:
            unclaimed = [g for g in matches if not (g.get("phone") or "").strip()]
            if len(matches) == 1 and len(unclaimed) == 1:
                guest_id = matches[0]["id"]
                log.info("Existing guest logged in: %s (%s)", name, guest_id)
            else:
                # Cannot tell which of the same-named guests this is. Refusing is
                # the only safe answer — picking one exposes someone's photos.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "More than one guest is registered under this name. "
                        "Please enter your phone number so we can find your photos."
                    ),
                )

    if guest_id is None:
        # Create guest record
        guest_id = str(uuid.uuid4())
        supabase.table("guests").insert({
            "id": guest_id,
            "name": body.name.strip(),
            "phone": body.phone.strip(),
            "invite_code": body.code.upper().strip(),
            "registered_at": datetime.utcnow().isoformat(),
        }).execute()
        log.info(f"New guest registered: {body.name} ({guest_id})")

    # Auto-associate face clusters with same name if any
    try:
        from app.services.face_service import associate_guest_by_name
        associate_guest_by_name(guest_id, body.name)
    except Exception as e:
        log.warning(f"Could not auto-associate name for guest: {e}")

    # Check if this guest already has a cached reference selfie in Drive
    from app.services.drive_cache import get_cached_file
    has_selfie = False
    try:
        selfie_data = get_cached_file(f"selfie_{guest_id}.jpg")
        has_selfie = selfie_data is not None
    except Exception as selfie_err:
        log.warning(f"Error checking selfie file existence: {selfie_err}")

    return InviteResponse(
        valid=True,
        guest_id=guest_id,
        event_name=event["event_name"],
        message=f"Welcome {body.name.split()[0]}! Now let's find your photos.",
        has_selfie=has_selfie
    )


@router.get("/guest/{guest_id}")
async def get_guest(guest_id: str, caller: dict = Depends(guest_or_admin)):
    """Fetch guest details — used by frontend to restore session.

    Was open, and selected "*", so anyone could read any guest's row — name,
    phone number and access token — just by knowing their id. Now it requires a
    token, only returns your own record, and never returns the credential
    columns.
    """
    if not caller.get("is_admin") and caller.get("id") != guest_id:
        raise HTTPException(status_code=403, detail="This link cannot open that profile.")

    result = (
        supabase.table("guests")
        .select("id, name, is_household, registered_at, last_login")
        .eq("id", guest_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Guest not found")
    return result.data[0]

# ── Per-guest access links ────────────────────────────────────────────────────

class LinkResponse(BaseModel):
    valid: bool
    guest_id: str
    name: str
    is_household: bool
    is_admin: bool = False
    event_name: str
    members: list = []


@router.get("/link/{token}", response_model=LinkResponse)
async def open_link(token: str):
    """Resolve a per-guest link.

    Replaces "type the invite code and your name", which could not tell two
    guests with the same name apart and was not a credential — the code gated
    this screen while every other endpoint was reachable without it. The token
    both identifies the guest and is what the API checks from here on.
    """
    from app.auth_deps import _lookup

    guest = _lookup(token.strip())
    if not guest:
        # Same response for unknown and revoked, so a caller cannot probe which
        # tokens exist.
        raise HTTPException(
            status_code=404,
            detail="This link is not valid. Please check the message you were sent.",
        )

    event_name = "Wedding"
    try:
        ev = supabase.table("invite_codes").select("event_name").eq("active", True).limit(1).execute()
        if ev.data:
            event_name = ev.data[0].get("event_name") or event_name
    except Exception:
        pass

    members = []
    try:
        rows = (
            supabase.table("guest_clusters")
            .select("cluster_id, label")
            .eq("guest_id", guest["id"])
            .execute()
        ).data or []
        members = [
            {"cluster_id": r["cluster_id"], "label": r.get("label") or "Someone"}
            for r in rows
        ]
    except Exception as e:
        log.debug("No guest_clusters for %s: %s", guest["id"], e)

    try:
        supabase.table("guests").update(
            {"last_login": datetime.utcnow().isoformat()}
        ).eq("id", guest["id"]).execute()
    except Exception:
        pass

    log.info("Link opened: %s (%s), %d member(s)", guest["name"], guest["id"], len(members))
    from app.auth_deps import ADMIN_GUEST_IDS
    return LinkResponse(
        valid=True,
        guest_id=guest["id"],
        name=guest["name"],
        is_household=bool(guest.get("is_household")),
        is_admin=guest["id"] in ADMIN_GUEST_IDS,
        event_name=event_name,
        members=members,
    )
