import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent / "backend"))

from app.database import supabase

def main():
    try:
        bucket = "weddingsnap-cache"
        
        # Test upload
        test_data = b"hello world from weddingsnap!"
        print("Uploading test file...")
        res = supabase.storage.from_(bucket).upload(
            path="test/hello.txt",
            file=test_data,
            file_options={"cache-control": "3600", "upsert": "true"}
        )
        print("Upload result:", res)
        
        # Test download
        print("Downloading test file...")
        downloaded = supabase.storage.from_(bucket).download("test/hello.txt")
        print("Downloaded content:", downloaded)
        
        # Test list
        print("Listing files in 'test' folder...")
        files = supabase.storage.from_(bucket).list("test")
        print("Files in test:", [f["name"] for f in files])
        
        # Test delete
        print("Deleting test file...")
        del_res = supabase.storage.from_(bucket).remove(["test/hello.txt"])
        print("Delete result:", del_res)
        
        print("All tests passed!")
    except Exception as e:
        print("Error with Supabase storage operations:", e)

if __name__ == "__main__":
    main()
