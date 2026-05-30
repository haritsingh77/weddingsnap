import logging
from typing import Optional
from datetime import datetime
from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Header, Response
from pydantic import BaseModel

from app.database import supabase
from app.services.face_service import match_guest_selfie, resolve_drive_ids, load_encodings
from app.config import settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
def admin_login(body: LoginRequest):
    """Validate admin password."""
    if body.password == settings.ADMIN_PASSWORD:
        return {"success": True, "token": settings.ADMIN_PASSWORD}
    raise HTTPException(status_code=401, detail="Invalid admin password")


@router.get("/guests")
def get_guests(x_admin_password: str = Header(..., alias="x-admin-password")):
    """Get all guests with matching photo counts."""
    if x_admin_password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        guests_res = supabase.table("guests").select("*").order("name").execute()
        guests = guests_res.data or []

        # Count matched photos per guest
        gp_res = supabase.table("guest_photos").select("guest_id").execute()
        counts = {}
        for row in gp_res.data:
            gid = row["guest_id"]
            counts[gid] = counts.get(gid, 0) + 1

        for guest in guests:
            guest["photo_count"] = counts.get(guest["id"], 0)

        return guests
    except Exception as e:
        log.error(f"Error listing guests: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/guests")
async def create_guest(
    name: str = Form(...),
    phone: str = Form(""),
    selfie: UploadFile = File(None),
    tolerance: Optional[float] = Form(None),
    x_admin_password: str = Header(..., alias="x-admin-password")
):
    """Register a new guest, cache their selfie to Google Drive, and run initial matching."""
    if x_admin_password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        # Resolve active invite code dynamically
        invite_code = settings.INVITE_CODE
        code_check = supabase.table("invite_codes").select("code").eq("code", invite_code).execute()
        if not code_check.data:
            invite_res = supabase.table("invite_codes").select("code").eq("active", True).execute()
            if invite_res.data:
                invite_code = invite_res.data[0]["code"]

        # Create guest record
        guest_payload = {
            "name": name.strip(),
            "phone": phone.strip(),
            "invite_code": invite_code
        }
        res = supabase.table("guests").insert(guest_payload).execute()
        if not res.data:
            raise HTTPException(status_code=500, detail="Failed to create guest record in database.")

        new_guest = res.data[0]
        guest_id = new_guest["id"]

        # Process selfie if provided
        if selfie:
            selfie_bytes = await selfie.read()
            if len(selfie_bytes) > 0:
                # Save to persistent Google Drive cache
                from app.services.drive_cache import save_cached_file
                save_cached_file(f"selfie_{guest_id}.jpg", selfie_bytes)

                # Auto-name cluster
                try:
                    from app.routes.faces import auto_name_cluster_for_guest
                    auto_name_cluster_for_guest(name.strip(), selfie_bytes)
                except Exception as auto_name_err:
                    log.warning(f"Could not auto-name cluster on create: {auto_name_err}")

                # Run matching
                match_result = match_guest_selfie(selfie_bytes, tolerance=tolerance)
                if match_result.get("success", True):
                    personal_ids = resolve_drive_ids(match_result["personal_photos"])
                    common_ids = resolve_drive_ids(match_result["common_photos"])

                    # Save matches
                    unique_photos = {}
                    for drive_id in personal_ids:
                        if drive_id:
                            unique_photos[drive_id] = {"drive_path": drive_id, "is_common": False, "face_count": 1}
                    for drive_id in common_ids:
                        if drive_id:
                            unique_photos[drive_id] = {"drive_path": drive_id, "is_common": True, "face_count": 4}

                    photos_to_upsert = list(unique_photos.values())
                    if photos_to_upsert:
                        upserted = supabase.table("photos").upsert(photos_to_upsert, on_conflict="drive_path").execute()
                        if upserted.data:
                            drive_to_id = {p["drive_path"]: p["id"] for p in upserted.data}
                            photo_rows = []
                            seen_photo_ids = set()
                            for drive_id in list(personal_ids) + list(common_ids):
                                pid = drive_to_id.get(drive_id)
                                if pid and pid not in seen_photo_ids:
                                    seen_photo_ids.add(pid)
                                    photo_rows.append({"guest_id": guest_id, "photo_id": pid})
                            if photo_rows:
                                supabase.table("guest_photos").upsert(photo_rows, on_conflict="guest_id,photo_id").execute()

        # Re-fetch guest to return fresh details
        fresh_res = supabase.table("guests").select("*").eq("id", guest_id).execute()
        fresh_guest = fresh_res.data[0]
        
        # Get count
        gp_res = supabase.table("guest_photos").select("photo_id").eq("guest_id", guest_id).execute()
        fresh_guest["photo_count"] = len(gp_res.data) if gp_res.data else 0

        return fresh_guest
    except Exception as e:
        log.error(f"Error creating guest: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/guests/{guest_id}")
async def update_guest_profile(
    guest_id: str,
    name: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    selfie: UploadFile = File(None),
    tolerance: Optional[float] = Form(None),
    x_admin_password: str = Header(..., alias="x-admin-password")
):
    """Update a guest's profile details (name, phone) and/or reference face photo."""
    if x_admin_password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        # 1. Fetch guest record
        guest_res = supabase.table("guests").select("*").eq("id", guest_id).execute()
        if not guest_res.data:
            raise HTTPException(status_code=404, detail="Guest not found")
        old_guest = guest_res.data[0]
        old_name = old_guest["name"]

        # 2. Update database fields
        update_payload = {}
        if name is not None:
            update_payload["name"] = name.strip()
        if phone is not None:
            update_payload["phone"] = phone.strip()

        if update_payload:
            supabase.table("guests").update(update_payload).eq("id", guest_id).execute()

        # Sync renaming in cluster_names.json
        if name is not None and old_name.strip().lower() != name.strip().lower():
            try:
                from app.services.drive_cache import get_cached_json, save_cached_json
                names_data = get_cached_json("cluster_names.json") or {}
                updated_any = False
                for k, v in list(names_data.items()):
                    if v.strip().lower() == old_name.strip().lower():
                        names_data[k] = name.strip()
                        updated_any = True
                if updated_any:
                    save_cached_json("cluster_names.json", names_data)
            except Exception as sync_err:
                log.error(f"Failed to sync guest renaming in cluster_names.json: {sync_err}")

        # 3. Handle new reference photo (selfie)
        if selfie:
            selfie_bytes = await selfie.read()
            if len(selfie_bytes) > 0:
                from app.services.drive_cache import save_cached_file, get_cached_json
                # Save to persistent cache
                save_cached_file(f"selfie_{guest_id}.jpg", selfie_bytes)

                # Auto-name cluster
                try:
                    from app.routes.faces import auto_name_cluster_for_guest
                    final_name = name.strip() if name is not None else old_name.strip()
                    auto_name_cluster_for_guest(final_name, selfie_bytes)
                except Exception as auto_name_err:
                    log.warning(f"Could not auto-name cluster on edit selfie: {auto_name_err}")

                # Run matching
                match_result = match_guest_selfie(selfie_bytes, tolerance=tolerance)
                if match_result.get("success", True):
                    personal_ids = resolve_drive_ids(match_result["personal_photos"])
                    common_ids = resolve_drive_ids(match_result["common_photos"])

                    # Load disassociated photos
                    disassociated = (get_cached_json("disassociated_photos.json") or {}).get(guest_id, [])
                    disassociated_set = set(disassociated)

                    unique_photos = {}
                    for drive_id in personal_ids:
                        if drive_id:
                            unique_photos[drive_id] = {"drive_path": drive_id, "is_common": False, "face_count": 1}
                    for drive_id in common_ids:
                        if drive_id:
                            unique_photos[drive_id] = {"drive_path": drive_id, "is_common": True, "face_count": 4}

                    photos_to_upsert = list(unique_photos.values())
                    if photos_to_upsert:
                        upserted = supabase.table("photos").upsert(photos_to_upsert, on_conflict="drive_path").execute()
                        if upserted.data:
                            drive_to_id = {p["drive_path"]: p["id"] for p in upserted.data}
                            photo_rows = []
                            seen_photo_ids = set()
                            for drive_id in list(personal_ids) + list(common_ids):
                                pid = drive_to_id.get(drive_id)
                                if pid and pid not in seen_photo_ids and pid not in disassociated_set:
                                    seen_photo_ids.add(pid)
                                    photo_rows.append({"guest_id": guest_id, "photo_id": pid})
                            if photo_rows:
                                supabase.table("guest_photos").upsert(photo_rows, on_conflict="guest_id,photo_id").execute()

        # Re-fetch guest to return fresh details
        fresh_res = supabase.table("guests").select("*").eq("id", guest_id).execute()
        fresh_guest = fresh_res.data[0]
        
        # Get count
        gp_res = supabase.table("guest_photos").select("photo_id").eq("guest_id", guest_id).execute()
        fresh_guest["photo_count"] = len(gp_res.data) if gp_res.data else 0

        return fresh_guest
    except Exception as e:
        log.error(f"Error updating guest profile: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/guests/{guest_id}/photos")
def get_guest_personal_photos(guest_id: str, x_admin_password: str = Header(..., alias="x-admin-password")):
    """Get only the personal matching photos for the guest (excludes common photos for disassociation)."""
    if x_admin_password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

    gp_res = supabase.table("guest_photos").select("photo_id").eq("guest_id", guest_id).execute()
    if not gp_res.data:
        return []

    photo_ids = [row["photo_id"] for row in gp_res.data]
    res = supabase.table("photos").select("id, drive_path, is_common, face_count").in_("id", photo_ids).execute()

    from app.routes.photos import get_drive_id_to_mime_map
    mime_map = get_drive_id_to_mime_map()

    photos_list = []
    for photo in res.data:
        # Exclude common photos from matches review to prevent admin from removing people from group album
        if photo.get("is_common", False):
            continue
        drive_id = photo.get("drive_path")
        if not drive_id:
            continue

        mime_type = mime_map.get(drive_id, "image/jpeg")
        is_video = mime_type.startswith("video/")

        photos_list.append({
            "id": photo["id"],
            "drive_id": drive_id,
            "is_common": photo.get("is_common", False),
            "face_count": photo.get("face_count", 1),
            "is_video": is_video,
            "thumb_url": f"/photos/thumb/{drive_id}",
            "stream_url": f"/photos/stream/{drive_id}"
        })
    return photos_list


@router.delete("/guests/{guest_id}/photos/{photo_id}")
def remove_guest_photo(guest_id: str, photo_id: str, x_admin_password: str = Header(..., alias="x-admin-password")):
    """Disassociate a photo from a guest's album permanently (adds to disassociated_photos.json cache)."""
    if x_admin_password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Add to disassociated list to prevent future re-matching
    from app.services.drive_cache import get_cached_json, save_cached_json
    disassociated_data = get_cached_json("disassociated_photos.json") or {}
    if guest_id not in disassociated_data:
        disassociated_data[guest_id] = []
    if photo_id not in disassociated_data[guest_id]:
        disassociated_data[guest_id].append(photo_id)
    save_cached_json("disassociated_photos.json", disassociated_data)

    # Delete row from guest_photos
    supabase.table("guest_photos").delete().eq("guest_id", guest_id).eq("photo_id", photo_id).execute()

    return {"success": True, "message": "Photo removed from guest's personal album."}


@router.post("/guests/{guest_id}/run-matching")
def run_guest_matching(guest_id: str, tolerance: Optional[float] = None, x_admin_password: str = Header(..., alias="x-admin-password")):
    """Re-runs matching for a specific guest using their stored reference selfie."""
    if x_admin_password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

    from app.services.drive_cache import get_cached_file, get_cached_json
    selfie_data = get_cached_file(f"selfie_{guest_id}.jpg")
    if not selfie_data:
        raise HTTPException(status_code=404, detail="Selfie reference photo not found.")

    # Match selfie
    match_result = match_guest_selfie(selfie_data, tolerance=tolerance)
    if not match_result.get("success", True):
        raise HTTPException(status_code=422, detail=match_result.get("message", "No face detected in reference photo."))

    # Auto-name cluster
    try:
        guest = supabase.table("guests").select("name").eq("id", guest_id).execute()
        if guest.data:
            from app.routes.faces import auto_name_cluster_for_guest
            auto_name_cluster_for_guest(guest.data[0]["name"], selfie_data)
    except Exception as auto_name_err:
        log.warning(f"Could not auto-name cluster on re-match: {auto_name_err}")

    # Resolve Drive IDs
    personal_ids = resolve_drive_ids(match_result["personal_photos"])
    common_ids = resolve_drive_ids(match_result["common_photos"])

    # Load disassociated photos
    disassociated = (get_cached_json("disassociated_photos.json") or {}).get(guest_id, [])
    disassociated_set = set(disassociated)

    # Save matches
    unique_photos = {}
    for drive_id in personal_ids:
        if drive_id:
            unique_photos[drive_id] = {"drive_path": drive_id, "is_common": False, "face_count": 1}
    for drive_id in common_ids:
        if drive_id:
            unique_photos[drive_id] = {"drive_path": drive_id, "is_common": True, "face_count": 4}

    photos_to_upsert = list(unique_photos.values())
    if photos_to_upsert:
        upserted = supabase.table("photos").upsert(photos_to_upsert, on_conflict="drive_path").execute()
        if upserted.data:
            drive_to_id = {p["drive_path"]: p["id"] for p in upserted.data}
            photo_rows = []
            seen_photo_ids = set()
            for drive_id in list(personal_ids) + list(common_ids):
                pid = drive_to_id.get(drive_id)
                if pid and pid not in seen_photo_ids and pid not in disassociated_set:
                    seen_photo_ids.add(pid)
                    photo_rows.append({"guest_id": guest_id, "photo_id": pid})
            if photo_rows:
                supabase.table("guest_photos").upsert(photo_rows, on_conflict="guest_id,photo_id").execute()

    # Get fresh count
    gp_res = supabase.table("guest_photos").select("photo_id").eq("guest_id", guest_id).execute()
    photo_count = len(gp_res.data) if gp_res.data else 0

    return {
        "success": True,
        "photo_count": photo_count,
        "message": f"Matching re-run completed. Guest now has {photo_count} matches."
    }


@router.post("/run-matching-all")
def run_matching_all(tolerance: Optional[float] = None, x_admin_password: str = Header(..., alias="x-admin-password")):
    """Re-runs face matching for all registered guests against precomputed encodings."""
    if x_admin_password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

    from app.services.drive_cache import get_cached_file, get_cached_json
    
    guests_res = supabase.table("guests").select("id, name").execute()
    guests = guests_res.data or []

    matched_count = 0
    errors = []

    # Clear face matching caches to ensure reload of fresh face encodings
    load_encodings.cache_clear()

    disassociated_data = get_cached_json("disassociated_photos.json") or {}

    for guest in guests:
        guest_id = guest["id"]
        selfie_data = get_cached_file(f"selfie_{guest_id}.jpg")
        if not selfie_data:
            continue

        try:
            match_result = match_guest_selfie(selfie_data, tolerance=tolerance)
            if not match_result.get("success", True):
                errors.append(f"Guest {guest['name']}: {match_result.get('message', 'Failed')}")
                continue

            personal_ids = resolve_drive_ids(match_result["personal_photos"])
            common_ids = resolve_drive_ids(match_result["common_photos"])
            disassociated_set = set(disassociated_data.get(guest_id, []))

            unique_photos = {}
            for drive_id in personal_ids:
                if drive_id:
                    unique_photos[drive_id] = {"drive_path": drive_id, "is_common": False, "face_count": 1}
            for drive_id in common_ids:
                if drive_id:
                    unique_photos[drive_id] = {"drive_path": drive_id, "is_common": True, "face_count": 4}

            photos_to_upsert = list(unique_photos.values())
            if photos_to_upsert:
                upserted = supabase.table("photos").upsert(photos_to_upsert, on_conflict="drive_path").execute()
                if upserted.data:
                    drive_to_id = {p["drive_path"]: p["id"] for p in upserted.data}
                    photo_rows = []
                    seen_photo_ids = set()
                    for drive_id in list(personal_ids) + list(common_ids):
                        pid = drive_to_id.get(drive_id)
                        if pid and pid not in seen_photo_ids and pid not in disassociated_set:
                            seen_photo_ids.add(pid)
                            photo_rows.append({"guest_id": guest_id, "photo_id": pid})
                    if photo_rows:
                        supabase.table("guest_photos").upsert(photo_rows, on_conflict="guest_id,photo_id").execute()

            matched_count += 1
        except Exception as e:
            errors.append(f"Guest {guest['name']}: {str(e)}")

    return {
        "success": True,
        "matched_count": matched_count,
        "errors": errors,
        "message": f"Successfully re-matched {matched_count} guests."
    }


@router.delete("/guests/{guest_id}")
def delete_guest(guest_id: str, x_admin_password: str = Header(..., alias="x-admin-password")):
    """Delete a guest entirely from database."""
    if x_admin_password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        supabase.table("guest_photos").delete().eq("guest_id", guest_id).execute()
        supabase.table("guests").delete().eq("id", guest_id).execute()
        return {"success": True, "message": "Guest deleted successfully."}
    except Exception as e:
        log.error(f"Error deleting guest: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/guests/{guest_id}/selfie")
def get_guest_selfie(
    guest_id: str,
    x_admin_password: str = Header(None, alias="x-admin-password"),
    password: str = None
):
    """Get the reference selfie image for a guest."""
    token = x_admin_password or password
    if token != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

    from app.services.drive_cache import get_cached_file
    selfie_data = get_cached_file(f"selfie_{guest_id}.jpg")
    if not selfie_data:
        raise HTTPException(status_code=404, detail="Selfie not found")

    return Response(content=selfie_data, media_type="image/jpeg")


# ── Family Members Management Endpoints ─────────────────────────────────────

@router.get("/guests/{guest_id}/members")
def get_family_members(guest_id: str, x_admin_password: str = Header(..., alias="x-admin-password")):
    """Get all family members for a specific guest/household."""
    if x_admin_password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        res = supabase.table("family_members").select("*").eq("guest_id", guest_id).order("name").execute()
        return res.data or []
    except Exception as e:
        log.error(f"Error fetching family members for guest {guest_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/guests/{guest_id}/members")
async def add_family_member(
    guest_id: str,
    name: str = Form(...),
    selfie: UploadFile = File(None),
    x_admin_password: str = Header(..., alias="x-admin-password")
):
    """Add a new family member to a household, cache their portrait, and run face matching."""
    if x_admin_password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        # 1. Verify guest exists
        guest_check = supabase.table("guests").select("id").eq("id", guest_id).execute()
        if not guest_check.data:
            raise HTTPException(status_code=404, detail="Guest household not found.")

        # 2. Create family member record
        member_payload = {
            "guest_id": guest_id,
            "name": name.strip()
        }
        res = supabase.table("family_members").insert(member_payload).execute()
        if not res.data:
            raise HTTPException(status_code=500, detail="Failed to create family member in database.")
        
        new_member = res.data[0]
        member_id = new_member["id"]

        # 3. Process selfie if provided
        if selfie:
            selfie_bytes = await selfie.read()
            if len(selfie_bytes) > 0:
                # Save to persistent Supabase Storage cache as selfie_member_{member_id}.jpg
                from app.services.drive_cache import save_cached_file
                save_cached_file(f"selfie_member_{member_id}.jpg", selfie_bytes)

                # Auto-name cluster using the member name
                try:
                    from app.routes.faces import auto_name_cluster_for_guest
                    auto_name_cluster_for_guest(name.strip(), selfie_bytes)
                except Exception as auto_name_err:
                    log.warning(f"Could not auto-name cluster for member: {auto_name_err}")

                # Run face matching
                match_result = match_guest_selfie(selfie_bytes)
                if match_result.get("success", True):
                    personal_ids = resolve_drive_ids(match_result["personal_photos"])
                    common_ids = resolve_drive_ids(match_result["common_photos"])

                    # Save matches to photos
                    unique_photos = {}
                    for drive_id in personal_ids:
                        if drive_id:
                            unique_photos[drive_id] = {"drive_path": drive_id, "is_common": False, "face_count": 1}
                    for drive_id in common_ids:
                        if drive_id:
                            unique_photos[drive_id] = {"drive_path": drive_id, "is_common": True, "face_count": 4}

                    photos_to_upsert = list(unique_photos.values())
                    if photos_to_upsert:
                        upserted = supabase.table("photos").upsert(photos_to_upsert, on_conflict="drive_path").execute()
                        if upserted.data:
                            drive_to_id = {p["drive_path"]: p["id"] for p in upserted.data}
                            
                            # Build mappings for member_photos and guest_photos
                            member_photo_rows = []
                            guest_photo_rows = []
                            seen_photo_ids = set()
                            
                            for drive_id in list(personal_ids) + list(common_ids):
                                pid = drive_to_id.get(drive_id)
                                if pid and pid not in seen_photo_ids:
                                    seen_photo_ids.add(pid)
                                    member_photo_rows.append({"member_id": member_id, "photo_id": pid})
                                    guest_photo_rows.append({"guest_id": guest_id, "photo_id": pid})
                            
                            if member_photo_rows:
                                supabase.table("member_photos").upsert(member_photo_rows, on_conflict="member_id,photo_id").execute()
                            if guest_photo_rows:
                                supabase.table("guest_photos").upsert(guest_photo_rows, on_conflict="guest_id,photo_id").execute()

        return new_member
    except Exception as e:
        log.error(f"Error adding family member: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/members/{member_id}")
def delete_family_member(member_id: str, x_admin_password: str = Header(..., alias="x-admin-password")):
    """Delete a family member, cascade-delete their photo mappings, and delete their cached selfie."""
    if x_admin_password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        # Delete from family_members (cascade deletes member_photos in database)
        supabase.table("family_members").delete().eq("id", member_id).execute()

        # Delete cached selfie file
        from app.services.drive_cache import delete_cached_file
        try:
            delete_cached_file(f"selfie_member_{member_id}.jpg")
        except Exception as e:
            log.warning(f"Could not delete cached selfie for member {member_id}: {e}")

        return {"success": True, "message": "Family member deleted successfully."}
    except Exception as e:
        log.error(f"Error deleting family member {member_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/members/{member_id}/selfie")
def get_family_member_selfie(
    member_id: str,
    x_admin_password: str = Header(None, alias="x-admin-password"),
    password: str = None
):
    """Retrieve the reference portrait image for a family member."""
    token = x_admin_password or password
    if token != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")

    from app.services.drive_cache import get_cached_file
    selfie_data = get_cached_file(f"selfie_member_{member_id}.jpg")
    if not selfie_data:
        raise HTTPException(status_code=404, detail="Selfie not found")

    return Response(content=selfie_data, media_type="image/jpeg")
