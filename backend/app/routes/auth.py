"""
Auth routes — invite code verification and guest session management.
"""

import uuid
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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

    # Check if guest with this name and invite code already exists
    existing = supabase.table("guests").select("*").eq(
        "name", body.name.strip()
    ).eq("invite_code", body.code.upper().strip()).execute()

    if existing.data:
        guest_id = existing.data[0]["id"]
        # Update phone number if provided and not set
        if body.phone.strip() and not existing.data[0].get("phone"):
            supabase.table("guests").update({"phone": body.phone.strip()}).eq("id", guest_id).execute()
        log.info(f"Existing guest logged in: {body.name} ({guest_id})")
    else:
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
async def get_guest(guest_id: str):
    """Fetch guest details — used by frontend to restore session."""
    result = supabase.table("guests").select("*").eq("id", guest_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Guest not found")
    return result.data[0]