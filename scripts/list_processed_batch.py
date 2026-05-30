#!/usr/bin/env python3
"""
List and optionally export thumbnails for files processed by the preprocessor.

Usage:
  cd backend
  ..\.venv\Scripts\python ..\scripts\list_processed_batch.py
  ..\.venv\Scripts\python ..\scripts\list_processed_batch.py --export
  ..\.venv\Scripts\python ..\scripts\list_processed_batch.py --export --only-insightface
"""

import argparse
import pickle
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "backend"))

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def main():
    parser = argparse.ArgumentParser(description="List/export processed wedding media")
    parser.add_argument("--export", action="store_true", help="Download thumbnails to review folder")
    parser.add_argument("--only-insightface", action="store_true", help="Only GPU/InsightFace batch (this laptop)")
    parser.add_argument("--output", type=str, default=str(project_root / "batch_review"))
    args = parser.parse_args()

    enc_path = project_root / "backend" / "encodings" / "face_encodings.pkl"
    map_path = project_root / "backend" / "encodings" / "drive_filename_map.json"

    if not enc_path.exists():
        print(f"No encodings file at {enc_path}")
        return 1

    records = pickle.loads(enc_path.read_bytes())
    if args.only_insightface:
        records = [r for r in records if r.get("backend") == "insightface"]

    photos, videos, no_face = [], [], []
    for r in records:
        path = r.get("path", "")
        name = Path(path).name
        fc = r.get("face_count", 0)
        backend = r.get("backend", "legacy")
        ext = Path(name).suffix.lower()
        entry = {"name": name, "path": path, "faces": fc, "backend": backend}
        if fc == 0:
            no_face.append(entry)
        elif ext in VIDEO_EXT:
            videos.append(entry)
        else:
            photos.append(entry)

    print("=" * 60)
    print(f"Encoded records: {len(records)}  (photos: {len(photos)}, videos: {len(videos)})")
    if args.only_insightface:
        print("(filtered: InsightFace / Windows batch only)")
    print("=" * 60)

    def print_section(title, items, limit=200):
        print(f"\n--- {title} ({len(items)}) ---")
        for e in items[:limit]:
            print(f"  {e['name']:<40} faces={e['faces']:<3} backend={e['backend']}")
        if len(items) > limit:
            print(f"  ... and {len(items) - limit} more")

    print_section("Photos with faces", photos)
    print_section("Videos with faces", videos)

    # Build name -> drive id map (Supabase or local)
    id_by_name = {}
    try:
        from app.services.drive_cache import get_cached_json
        id_by_name = get_cached_json("drive_filename_map.json") or {}
    except Exception:
        pass
    if not id_by_name and map_path.exists():
        import json
        id_by_name = json.loads(map_path.read_text(encoding="utf-8"))

    if args.export:
        from app.services.drive_cache import get_cached_file

        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        exported = 0
        for e in photos + videos:
            name = e["name"]
            fid = id_by_name.get(name)
            if not fid:
                continue
            thumb_key = f"thumb_{fid}_400.jpg"
            data = get_cached_file(thumb_key)
            if not data:
                continue
            dest = out / f"{Path(name).stem}_{fid[:8]}.jpg"
            dest.write_bytes(data)
            exported += 1
        print(f"\nExported {exported} thumbnails to: {out.resolve()}")
        print("Open that folder in File Explorer to visually review each item.")

    print("\n--- How to view originals on Google Drive ---")
    print("1. Open your wedding Drive folder in the browser.")
    print("2. Search for any filename from the list above (e.g. 1G7A6145.JPG).")
    print("3. Or use Supabase Dashboard > Storage > weddingsnap-cache > thumb_<file_id>_400.jpg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
