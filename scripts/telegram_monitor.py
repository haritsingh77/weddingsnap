import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

# Add project root and backend to path
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root / "backend"))
sys.path.append(str(project_root))

from scripts.telegram_bot_listener import get_preprocessor_stats, send_reply

def main():
    print("Telegram 2-hour monitor script started.")
    
    # Load env variables
    env_path = project_root / "backend" / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
    
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    
    if not chat_id:
        print("Error: TELEGRAM_CHAT_ID is not set in environment or backend/.env.")
        return

    # Send initial status report on startup
    try:
        stats = get_preprocessor_stats()
        send_reply(chat_id, stats)
        print("Initial monitor report sent to Telegram.")
    except Exception as e:
        print(f"Error sending initial report: {e}")

    # Loop indefinitely, sending reports every 2 hours
    while True:
        try:
            print("Sleeping for 2 hours before the next status update...")
            time.sleep(7200)  # 2 hours
            stats = get_preprocessor_stats()
            send_reply(chat_id, stats)
            print("Periodic status report sent to Telegram.")
        except Exception as e:
            print(f"Error in monitor loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
