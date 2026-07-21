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
    import argparse

    ap = argparse.ArgumentParser(description="Periodic Telegram status reporter")
    ap.add_argument(
        "--interval-hours",
        type=float,
        default=float(os.getenv("TELEGRAM_REPORT_INTERVAL_HOURS", "3")),
        help="Hours between reports (default 3, or TELEGRAM_REPORT_INTERVAL_HOURS)",
    )
    args = ap.parse_args()
    interval = max(60.0, args.interval_hours * 3600)

    print(f"Telegram monitor started — reporting every {args.interval_hours}h.")

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

    while True:
        try:
            print(f"Sleeping {args.interval_hours}h before the next status update...")
            time.sleep(interval)
            stats = get_preprocessor_stats()
            send_reply(chat_id, stats)
            print("Periodic status report sent to Telegram.")
        except Exception as e:
            print(f"Error in monitor loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
