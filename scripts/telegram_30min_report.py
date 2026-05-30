import os
import sys
import time
import json
from pathlib import Path
from dotenv import load_dotenv

project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root / "backend"))
sys.path.append(str(project_root))

from scripts.telegram_bot_listener import get_preprocessor_stats, send_reply

def main():
    # Load env variables
    env_path = project_root / "backend" / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
    
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    
    if not chat_id:
        print("Error: TELEGRAM_CHAT_ID not configured.")
        return

    # Fetch stats
    stats = get_preprocessor_stats()
    
    # Analyze if performance matches GPU expectation
    cache_path = Path(project_root / "backend" / "encodings" / "face_encodings.pkl")
    state_file = cache_path.parent / "preprocessor_state.json"
    
    perf_status = "Unknown"
    speed = None
    
    if state_file.exists():
        try:
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            speed = state_data.get("speed_seconds_per_file")
        except Exception:
            pass
            
    if speed is not None:
        # GPU speed target is under 5 seconds per image
        if speed < 5.0:
            perf_status = f"✅ Running as expected on GPU ({speed:.2f}s/file). Matches the targeted 1–5s/image range!"
        else:
            perf_status = f"⚠️ Slower than expected ({speed:.2f}s/file). Expected under 5s/image on GPU."
    else:
        perf_status = "ℹ️ Speed details not yet registered in the state file. It may still be listing files or downloading the first batches."

    report = (
        f"📊 *30-Minute Preprocessor Report*\n\n"
        f"{stats}\n\n"
        f"*Performance Comparison*:\n"
        f"{perf_status}"
    )

    send_reply(chat_id, report)
    print("30-minute status report sent to Telegram.")

if __name__ == "__main__":
    main()
