"""
Setup script — run ONCE before preprocessing.
1. Creates thumbnails/ and encodings/ subfolders in the Drive cache folder.
2. Prints the folder IDs — copy them into .env.
3. Wipes all local cache/encodings directories.
4. Clears Supabase guests, photos, guest_photos tables.
"""
import sys
import shutil
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "backend"))

from app.config import settings
from app.services.drive_service import get_drive_service
from app.database import supabase

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"

def create_drive_subfolder(service, parent_id: str, name: str) -> str:
    """Create a subfolder inside parent_id. Returns the new folder's ID."""
    # Check if it already exists
    resp = service.files().list(
        q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and name='{name}' and trashed=false",
        fields="files(id, name)",
    ).execute()
    existing = resp.get("files", [])
    if existing:
        fid = existing[0]["id"]
        print(f"  ✅ Folder '{name}' already exists → {fid}")
        return fid

    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=meta, fields="id").execute()
    fid = folder["id"]
    print(f"  ✅ Created folder '{name}' → {fid}")
    return fid


def clear_local():
    dirs_to_clear = [
        BACKEND_DIR / "cache",
        BACKEND_DIR / "encodings",
        BACKEND_DIR / "temp_preprocess",
        Path("/tmp/weddingsnap_cache"),
    ]
    for d in dirs_to_clear:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            print(f"  🗑  Cleared local: {d}")
        else:
            print(f"  ⏭  Not found (skipped): {d}")


def clear_supabase():
    print("\n── Clearing Supabase tables ──────────────────────────────────────")
    try:
        supabase.table("guest_photos").delete().neq("guest_id", "00000000-0000-0000-0000-000000000000").execute()
        print("  🗑  guest_photos cleared")
    except Exception as e:
        print(f"  ⚠  guest_photos: {e}")
    try:
        supabase.table("photos").delete().neq("id", 0).execute()
        print("  🗑  photos cleared")
    except Exception as e:
        print(f"  ⚠  photos: {e}")
    try:
        supabase.table("guests").delete().neq("id", "00000000-0000-0000-0000-000000000000").execute()
        print("  🗑  guests cleared")
    except Exception as e:
        print(f"  ⚠  guests: {e}")


def main():
    cache_folder_id = settings.GOOGLE_DRIVE_CACHE_FOLDER_ID
    if not cache_folder_id:
        print("❌ GOOGLE_DRIVE_CACHE_FOLDER_ID is not set in .env — aborting.")
        sys.exit(1)

    print(f"\n── Creating Drive subfolders inside {cache_folder_id} ────────────")
    service = get_drive_service()
    thumbnails_id = create_drive_subfolder(service, cache_folder_id, "thumbnails")
    encodings_id  = create_drive_subfolder(service, cache_folder_id, "encodings")

    print(f"\n── Copy these into backend/.env ──────────────────────────────────")
    print(f"  GOOGLE_DRIVE_THUMBNAILS_FOLDER_ID={thumbnails_id}")
    print(f"  GOOGLE_DRIVE_ENCODINGS_FOLDER_ID={encodings_id}")

    print(f"\n── Clearing local cache directories ──────────────────────────────")
    clear_local()

    clear_supabase()

    print("\n✅ Setup complete. Update .env with the folder IDs above, then run:")
    print("   cd backend && python ../scripts/preprocess_drive.py")


if __name__ == "__main__":
    main()
