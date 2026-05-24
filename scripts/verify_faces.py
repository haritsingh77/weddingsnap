import os
import sys
import logging
from pathlib import Path

# Add project root and backend to path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root / "backend"))
sys.path.append(str(project_root))

from app.routes.faces import get_face_clusters, get_cluster_thumbnail
from app.services.drive_cache import get_cached_file
from app.services.face_service import get_filename_map

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

def export_clusters_for_verification():
    verify_dir = project_root / "temp_preprocess" / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"🔍 Fetching face clusters from database...")
    clusters = get_face_clusters()
    
    if not clusters:
        print("⚠️ No face clusters found. Preprocessor may not have run yet or found no faces.")
        return
        
    print(f"📊 Found {len(clusters)} face clusters. Exporting top 5 for verification...")
    
    # Get filename mapping
    mapping = get_filename_map()
    
    # Export top 5 clusters
    for rank, (cid, cdata) in enumerate(list(clusters.items())[:5], 1):
        print(f"\n👤 [Cluster #{cid}] Rank: {rank}, Matches count: {cdata['count']}")
        
        # 1. Download and save the cropped face representative
        try:
            resp = get_cluster_thumbnail(cid)
            rep_path = verify_dir / f"cluster_{cid}_representative.jpg"
            rep_path.write_bytes(resp.body)
            print(f"  ✅ Saved cropped face representative: {rep_path.relative_to(project_root)}")
        except Exception as e:
            print(f"  ❌ Failed to save representative thumbnail for cluster {cid}: {e}")
            
        # 2. Download and save up to 3 member photo thumbnails
        member_paths = cdata["photos"][:3]
        for idx, path_str in enumerate(member_paths, 1):
            filename = Path(path_str).name
            drive_id = mapping.get(filename)
            if not drive_id:
                continue
                
            try:
                thumb_key = f"thumb_{drive_id}_400.jpg"
                thumb_data = get_cached_file(thumb_key)
                if thumb_data:
                    member_path = verify_dir / f"cluster_{cid}_member_{idx}_{filename}.jpg"
                    member_path.write_bytes(thumb_data)
                    print(f"  ✅ Saved member photo {idx}: {member_path.relative_to(project_root)}")
            except Exception as e:
                print(f"  ❌ Failed to save member photo {filename}: {e}")
                
    print(f"\n🎉 Verification export complete! Files are saved in: {verify_dir.relative_to(project_root)}")
    print("You can view these files to verify the correctness of the CNN face matching.")
    
    # Send notification via Telegram/WhatsApp
    try:
        from scripts.whatsapp_notifier import send_whatsapp
        msg = (
            f"🔍 Face Matching Verification Check:\n"
            f"Found {len(clusters)} face clusters.\n"
            f"Successfully exported thumbnails for the top {min(5, len(clusters))} clusters to local folder for verification."
        )
        send_whatsapp(msg)
    except Exception as e:
        print(f"Failed to send verification alert: {e}")


if __name__ == "__main__":
    export_clusters_for_verification()
