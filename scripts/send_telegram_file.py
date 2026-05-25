import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv
import httpx

# Add project root and backend to path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root / "backend"))
sys.path.append(str(project_root))

# Setup logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Load env variables
env_path = project_root / "backend" / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def send_telegram_photo(photo_path: Path, caption: str = "") -> bool:
    """Send a photo to the Telegram chat."""
    if not (token and chat_id):
        log.error("Telegram credentials missing in environment.")
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        with open(photo_path, "rb") as f:
            files = {"photo": f}
            data = {"chat_id": chat_id, "caption": caption}
            with httpx.Client() as client:
                resp = client.post(url, data=data, files=files, timeout=30.0)
        if resp.status_code == 200:
            log.info(f"Photo {photo_path.name} sent successfully to Telegram.")
            return True
        else:
            log.error(f"Failed to send photo: Status {resp.status_code}, Body: {resp.text}")
            return False
    except Exception as e:
        log.error(f"Error sending photo to Telegram: {e}")
        return False

def send_telegram_document(doc_path: Path, caption: str = "") -> bool:
    """Send a document (e.g. ZIP file) to the Telegram chat."""
    if not (token and chat_id):
        log.error("Telegram credentials missing in environment.")
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendDocument"
        with open(doc_path, "rb") as f:
            files = {"document": f}
            data = {"chat_id": chat_id, "caption": caption}
            with httpx.Client() as client:
                resp = client.post(url, data=data, files=files, timeout=60.0)
        if resp.status_code == 200:
            log.info(f"Document {doc_path.name} sent successfully to Telegram.")
            return True
        else:
            log.error(f"Failed to send document: Status {resp.status_code}, Body: {resp.text}")
            return False
    except Exception as e:
        log.error(f"Error sending document to Telegram: {e}")
        return False

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Send file or photo to Telegram")
    parser.add_argument("type", choices=["photo", "doc"], help="File type to send")
    parser.add_argument("path", type=str, help="Path to the file")
    parser.add_argument("--caption", type=str, default="", help="Optional caption")
    args = parser.parse_args()
    
    file_path = Path(args.path)
    if not file_path.exists():
        print(f"Error: File {file_path} does not exist.")
        sys.exit(1)
        
    if args.type == "photo":
        send_telegram_photo(file_path, args.caption)
    else:
        send_telegram_document(file_path, args.caption)
