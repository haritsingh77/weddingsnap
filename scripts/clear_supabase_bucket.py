import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "backend"))

from app.database import supabase

def clear_bucket(bucket_name: str):
    print(f"🧹 Clearing Supabase Storage bucket '{bucket_name}'...")
    try:
        # List all files at root
        files = supabase.storage.from_(bucket_name).list(options={"limit": 1000})
        filenames = [f["name"] for f in files if f["name"] != ".emptyFolderPlaceholder"]
        if filenames:
            print(f"  🗑  Deleting {len(filenames)} files: {filenames}")
            supabase.storage.from_(bucket_name).remove(filenames)
        else:
            print("  Bucket is already empty.")
        print("✅ Bucket cleared!")
    except Exception as e:
        print(f"❌ Error clearing bucket: {e}")

if __name__ == "__main__":
    clear_bucket("weddingsnap-cache")
