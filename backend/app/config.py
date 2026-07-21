import os
from dotenv import load_dotenv
from pathlib import Path

# Load .env from the parent directory of 'app'
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

class Config:
    APP_ENV = os.getenv("APP_ENV", "development")
    SECRET_KEY = os.getenv("SECRET_KEY")
    INVITE_CODE = os.getenv("INVITE_CODE", "WEDDING2024")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "wedding2026")
    
    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")
    
    GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    GOOGLE_DRIVE_CACHE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_CACHE_FOLDER_ID")
    GOOGLE_DRIVE_THUMBNAILS_FOLDER_ID = os.getenv("GOOGLE_DRIVE_THUMBNAILS_FOLDER_ID")
    GOOGLE_DRIVE_ENCODINGS_FOLDER_ID = os.getenv("GOOGLE_DRIVE_ENCODINGS_FOLDER_ID")
    GOOGLE_DRIVE_TEMP_DELETE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_TEMP_DELETE_FOLDER_ID")
    
    _service_account_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service-account.json")
    if not Path(_service_account_path).is_absolute():
        GOOGLE_SERVICE_ACCOUNT_JSON = str(Path(__file__).resolve().parent.parent / _service_account_path)
    else:
        GOOGLE_SERVICE_ACCOUNT_JSON = _service_account_path
    
    FACE_MATCH_TOLERANCE = float(os.getenv("FACE_MATCH_TOLERANCE", "0.5"))
    # Retrieval cut. Measured on 936 photos / 2,961 buffalo_l faces: the closest
    # two DIFFERENT people ever came was 0.540, so 0.40 was leaving recall on the
    # table — 0.50 yields ~53% more matches with zero false matches against
    # same-photo pairs (which are guaranteed different identities).
    ARCFACE_MATCH_TOLERANCE = float(os.getenv("ARCFACE_MATCH_THRESHOLD", "0.5"))
    # Clustering stays at 0.40 — see cluster_tolerance() in face_engine/matching.
    CLUSTER_THRESHOLD = float(os.getenv("CLUSTER_THRESHOLD", "0.4"))
    FACE_BACKEND = os.getenv("FACE_BACKEND", "auto")
    WEDDINGSNAP_SSD_ROOT = os.getenv("WEDDINGSNAP_SSD_ROOT", "")
    ENCODINGS_CACHE_PATH = os.getenv(
        "ENCODINGS_CACHE_PATH",
        str(Path(__file__).resolve().parent.parent / "encodings" / "face_encodings.pkl")
    )

settings = Config()
