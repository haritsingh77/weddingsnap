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
    ENCODINGS_CACHE_PATH = os.getenv(
        "ENCODINGS_CACHE_PATH",
        str(Path(__file__).resolve().parent.parent / "encodings" / "face_encodings.pkl")
    )

settings = Config()
