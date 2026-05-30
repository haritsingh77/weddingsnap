import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "backend"))

from app.database import supabase

def clear_local_cache():
    local_cache_roots = [Path("/tmp/weddingsnap_cache")]
    ssd_root = os.getenv("WEDDINGSNAP_SSD_ROOT")
    if ssd_root:
        local_cache_roots.extend(
            [
                Path(ssd_root),
                Path(ssd_root) / "api_cache",
                Path(ssd_root) / "temp_preprocess",
            ]
        )

    print("Clearing local ephemeral caches...")
    deleted = 0
    for cache_root in local_cache_roots:
        if not cache_root.exists():
            continue
        for f in cache_root.rglob("*"):
            try:
                if f.is_file():
                    f.unlink()
                    deleted += 1
            except Exception as e:
                print(f"  WARN: Could not delete local file {f}: {e}")
    print(f"  OK: Cleared local ephemeral cache files ({deleted} deleted)")

    # Also clear local workspace cache files to prevent resume from reloading old encodings
    workspace_cache_paths = [
        Path(__file__).resolve().parent.parent / "backend" / "encodings" / "face_encodings.pkl",
        Path(__file__).resolve().parent.parent / "backend" / "encodings" / "processed_files.txt",
        Path(__file__).resolve().parent.parent / "encodings" / "face_encodings.pkl",
        Path(__file__).resolve().parent.parent / "encodings" / "processed_files.txt",
    ]
    
    print("Clearing local workspace cache files...")
    deleted_ws = 0
    for path in workspace_cache_paths:
        if path.exists():
            try:
                path.unlink()
                deleted_ws += 1
                print(f"  Deleted: {path.relative_to(Path(__file__).resolve().parent.parent)}")
            except Exception as e:
                print(f"  WARN: Could not delete workspace cache file {path}: {e}")
    print(f"  OK: Cleared local workspace cache files ({deleted_ws} deleted)")

def clear_bucket(bucket_name: str):
    print(f"Clearing Supabase Storage bucket '{bucket_name}'...")
    try:
        deleted_total = 0
        while True:
            # List files at root (Supabase storage lists files up to limit)
            files = supabase.storage.from_(bucket_name).list(options={"limit": 1000})
            filenames = [f["name"] for f in files if f["name"] != ".emptyFolderPlaceholder"]
            
            if not filenames:
                break
            
            print(f"  Deleting batch of {len(filenames)} files...")
            supabase.storage.from_(bucket_name).remove(filenames)
            deleted_total += len(filenames)
            
        print(f"OK: Bucket '{bucket_name}' cleared. Deleted {deleted_total} files.")
    except Exception as e:
        print(f"ERROR clearing bucket: {e}")

def clear_database_tables():
    print("Clearing Supabase PostgreSQL tables...")
    try:
        # guest_photos has a foreign key to photos, so we clear guest_photos first
        print("  - Clearing 'guest_photos' mapping table...")
        gp_res = supabase.table("guest_photos").delete().neq("photo_id", 0).execute()
        print(f"  OK: Deleted {len(gp_res.data) if gp_res.data else 0} rows from 'guest_photos'.")

        print("  - Clearing 'photos' table...")
        p_res = supabase.table("photos").delete().neq("id", 0).execute()
        print(f"  OK: Deleted {len(p_res.data) if p_res.data else 0} rows from 'photos'.")
        
        print("OK: Database tables cleared successfully.")
    except Exception as e:
        print(f"ERROR clearing database tables: {e}")

if __name__ == "__main__":
    # Clear local cache first so it doesn't get re-uploaded
    clear_local_cache()
    
    # Clear the storage bucket
    clear_bucket("weddingsnap-cache")
    
    # Clear the database tables
    clear_database_tables()
