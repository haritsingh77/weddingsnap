import sys
from pathlib import Path

# Setup paths to import backend
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root / "backend"))

from app.database import supabase
from app.services.drive_cache import BUCKET_NAME

def cleanup_db_face_clusters():
    print("=== 1. CLEARING DATABASE TABLES ===")
    try:
        # Clear guest_photos mapping table
        print("Clearing 'guest_photos' mapping table (removing all face/photo assignments)...")
        gp_res = supabase.table("guest_photos").delete().neq("photo_id", 0).execute()
        print(f"  ✅ Deleted {len(gp_res.data) if gp_res.data else 0} rows from 'guest_photos'.")
        
        # Clear photos table
        print("Clearing 'photos' index table...")
        p_res = supabase.table("photos").delete().neq("id", 0).execute()
        print(f"  ✅ Deleted {len(p_res.data) if p_res.data else 0} rows from 'photos'.")
        
    except Exception as e:
        print(f"  ❌ Error clearing database tables: {e}")

    print("\n=== 2. DELETING FACE-RELATED METADATA JSONs FROM STORAGE ===")
    face_jsons = [
        "cluster_names.json",
        "cluster_merges.json",
        "cluster_representatives.json",
        "disassociated_photos.json",
        "encodings_meta.json"
    ]
    
    try:
        # Check bucket files first
        bucket_files = supabase.storage.from_(BUCKET_NAME).list()
        existing_names = {f["name"] for f in bucket_files}
        
        to_delete = [f for f in face_jsons if f in existing_names]
        
        if to_delete:
            print(f"Deleting files from Supabase Storage: {to_delete}...")
            supabase.storage.from_(BUCKET_NAME).remove(to_delete)
            print("  ✅ Metadata files successfully deleted.")
        else:
            print("  ✅ No face-related JSON files found in storage.")
            
    except Exception as e:
        print(f"  ❌ Error deleting metadata files: {e}")

    print("\n=== 3. VERIFYING RETAINED CORE FILES ===")
    try:
        bucket_files = supabase.storage.from_(BUCKET_NAME).list(options={"limit": 10})
        print("Current top files remaining in your bucket:")
        for f in bucket_files:
            print(f"  - {f['name']}")
    except Exception as e:
        print(f"  ❌ Error listing bucket: {e}")

if __name__ == "__main__":
    cleanup_db_face_clusters()
