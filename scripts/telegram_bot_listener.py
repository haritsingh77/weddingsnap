import os
import sys
import time
import pickle
import subprocess
from pathlib import Path
from dotenv import load_dotenv
import httpx

# Add project root and backend to path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root / "backend"))
sys.path.append(str(project_root))

from app.config import settings

# Load env variables
env_path = project_root / "backend" / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def is_preprocessor_running() -> bool:
    """Check if the preprocess_drive.py script is currently running."""
    try:
        res = subprocess.run(["ps", "aux"], capture_output=True, text=True)
        return "preprocess_drive.py" in res.stdout
    except Exception:
        return False

def format_seconds(seconds) -> str:
    """Format seconds to a human-readable duration."""
    if seconds is None:
        return "Calculating..."
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m {seconds % 60}s"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h {minutes % 60}m"
    days = hours // 24
    return f"{days}d {hours % 24}h {minutes % 60}m"

def get_preprocessor_stats() -> str:
    """Calculate and return stats on current face preprocessing progress."""
    cache_path = Path(settings.ENCODINGS_CACHE_PATH)
    progress_log = cache_path.parent / "processed_files.txt"
    state_file = cache_path.parent / "preprocessor_state.json"
    
    processed_count = 0
    if progress_log.exists():
        try:
            lines = progress_log.read_text(encoding="utf-8").splitlines()
            processed_count = len(lines) // 2
        except Exception:
            pass
            
    faces_count = 0
    if cache_path.exists():
        try:
            with open(cache_path, "rb") as f:
                data = pickle.load(f)
                faces_count = sum(r.get("face_count", 0) for r in data if isinstance(r, dict))
        except Exception:
            pass
            
    running = is_preprocessor_running()
    status_str = "🟢 Running" if running else "🔴 Stopped"
    
    # Try reading the real-time preprocessor_state.json if available
    state_data = None
    if state_file.exists():
        try:
            import json
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    # If running and we have fresh state data (updated in the last 15 minutes)
    if running and state_data and state_data.get("status") == "running" and (time.time() - state_data.get("last_update", 0) < 900):
        model_str = state_data.get("model", "cnn").upper()
        total_files = state_data.get("total_files", 12823)
        processed_all = state_data.get("processed_files_all_time", processed_count)
        pct = (processed_all / total_files) * 100 if total_files else 0
        speed = state_data.get("speed_seconds_per_file")
        current_file = state_data.get("current_file_name", "N/A")
        est_seconds = state_data.get("estimated_remaining_seconds")
        
        est_str = format_seconds(est_seconds)
        speed_str = f"{speed}s/file" if speed else "Calculating..."
        
        return (
            f"🤖 *WeddingSnap Status Update*\n\n"
            f"Status: {status_str} ({model_str} Model)\n"
            f"Progress: *{processed_all:,} / {total_files:,}* files ({pct:.1f}%)\n"
            f"Speed: {speed_str}\n"
            f"Estimated Time Remaining: *{est_str}*\n"
            f"Current File: `{current_file}`\n"
            f"Faces Registered: {faces_count:,} faces\n"
            f"Local Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
    
    # Static fallback (when stopped or state file is missing/stale)
    total_estimated_files = 12823 # default wedding drive count
    pct = (processed_count / total_estimated_files) * 100 if total_estimated_files else 0
    return (
        f"🤖 *WeddingSnap Status Update*\n\n"
        f"Status: {status_str}\n"
        f"Progress: *{processed_count:,} / {total_estimated_files:,}* files ({pct:.1f}%)\n"
        f"Faces Registered: {faces_count:,} faces\n"
        f"Local Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )

def send_reply(chat_id_to_reply: str, text: str):
    """Send message back to Telegram chat."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id_to_reply, "text": text, "parse_mode": "Markdown"}
    try:
        httpx.post(url, data=data, timeout=10.0)
    except Exception as e:
        print(f"Error sending reply: {e}")

def main():
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN is not set.")
        return
        
    print("Telegram bot listener started. Polling for updates...")
    offset = 0
    
    # Clear old messages on start
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        resp = httpx.get(url, params={"timeout": 0}, timeout=5.0)
        if resp.status_code == 200:
            results = resp.json().get("result", [])
            if results:
                offset = results[-1]["update_id"] + 1
    except Exception as e:
        print(f"Error clearing offset: {e}")

    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"offset": offset, "timeout": 15}
            resp = httpx.get(url, params=params, timeout=20.0)
            
            if resp.status_code != 200:
                time.sleep(5)
                continue
                
            updates = resp.json().get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                
                message = update.get("message")
                if not message:
                    continue
                    
                text = message.get("text", "").strip()
                from_chat_id = message.get("chat", {}).get("id")
                
                # Check if sender is authorized (configured chat_id)
                if str(from_chat_id) != chat_id:
                    continue
                    
                if text.lower() in ("/status", "status", "/ping", "ping"):
                    stats = get_preprocessor_stats()
                    send_reply(from_chat_id, stats)
                elif text.lower() in ("/help", "help"):
                    help_text = (
                        "Available commands:\n"
                        "/status - Get preprocessor progress\n"
                        "/ping - Check if bot is active"
                    )
                    send_reply(from_chat_id, help_text)
                    
        except KeyboardInterrupt:
            print("Stopping listener.")
            break
        except Exception as e:
            print(f"Error in polling loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
