import sys
from pathlib import Path

# Setup paths to import backend
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root / "backend"))

from app.database import supabase
from app.services.drive_cache import BUCKET_NAME

def cleanup_supabase_faces():
    print(f"Connecting to Supabase Storage bucket '{BUCKET_NAME}'...")
    try:
        bucket_files = []
        offset = 0
        limit = 1000
        
        # 1. Fetch all file metadata
        while True:
            files = supabase.storage.from_(BUCKET_NAME).list(options={"limit": limit, "offset": offset})
            if not files:
                break
            bucket_files.extend(files)
            if len(files) < limit:
                break
            offset += limit
            
        print(f"Found {len(bucket_files)} total files in bucket.")
        
        # 2. Filter target files to delete
        target_files = []
        for f in bucket_files:
            name = f["name"]
            if name.startswith("face_cluster_") or name == "face_encodings.pkl":
                target_files.append(name)
                
        print(f"Identified {len(target_files)} face-related files to delete.")
        
        if not target_files:
            print("No face-related files found to delete. Bucket is already clean!")
            return
            
        # 3. Perform batch deletion in chunks of 100 files
        chunk_size = 100
        deleted_count = 0
        for i in range(0, len(target_files), chunk_size):
            chunk = target_files[i:i + chunk_size]
            print(f"  Deleting batch {i // chunk_size + 1}: {len(chunk)} files...")
            supabase.storage.from_(BUCKET_NAME).remove(chunk)
            deleted_count += len(chunk)
            
        print(f"✅ Success! Deleted {deleted_count} face-related files from Supabase Storage.")
        
    except Exception as e:
        print(f"❌ Error during cleanup: {e}")

if __name__ == "__main__":
    cleanup_supabase_faces()
