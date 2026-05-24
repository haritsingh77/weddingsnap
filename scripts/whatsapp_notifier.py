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

from app.config import settings

# Setup logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Load env variables if not already loaded
env_path = project_root / "backend" / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

def send_telegram(message: str) -> bool:
    """Send a notification to Telegram chat using bot token."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    
    if not (token and chat_id):
        return False
        
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": message
        }
        with httpx.Client() as client:
            resp = client.post(url, data=data)
            
        if resp.status_code == 200:
            log.info("Telegram notification sent successfully.")
            return True
        else:
            log.error(f"Failed to send Telegram message: Status {resp.status_code}, Body: {resp.text}")
            return False
    except Exception as e:
        log.error(f"Error sending Telegram message: {e}")
        return False

def send_whatsapp(message: str) -> bool:
    """Send a notification using Telegram (first choice) or WhatsApp (second choice)."""
    # 1. Try Telegram first if credentials are set
    if send_telegram(message):
        return True
        
    # 2. Otherwise fall back to WhatsApp configuration
    provider = os.getenv("WHATSAPP_PROVIDER", "").strip().lower()
    phone = os.getenv("USER_WHATSAPP_PHONE", "").strip()
    
    if not phone:
        log.warning("Neither Telegram nor WhatsApp configurations are active. Notification skipped.")
        print(f"[Mock Notification] {message}")
        return False
        
    if provider == "twilio":
        account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
        auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
        sender = os.getenv("TWILIO_SENDER_PHONE", "whatsapp:+14155238886").strip()
        
        if not (account_sid and auth_token):
            log.error("Twilio credentials missing (TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN)")
            return False
            
        try:
            url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
            auth = (account_sid, auth_token)
            
            to_number = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"
            from_number = sender if sender.startswith("whatsapp:") else f"whatsapp:{sender}"
            
            data = {
                "To": to_number,
                "From": from_number,
                "Body": message
            }
            
            with httpx.Client() as client:
                resp = client.post(url, auth=auth, data=data)
                
            if resp.status_code in (200, 201):
                log.info("Twilio WhatsApp message sent successfully.")
                return True
            else:
                log.error(f"Failed to send Twilio message: Status {resp.status_code}, Body: {resp.text}")
                return False
        except Exception as e:
            log.error(f"Error sending Twilio WhatsApp message: {e}")
            return False
            
    else:
        # Default to CallMeBot
        apikey = os.getenv("CALLMEBOT_API_KEY", "").strip()
        if not apikey:
            log.warning("Neither Telegram nor CallMeBot is configured. Notification skipped.")
            print(f"[Mock Notification] {message}")
            return False
            
        try:
            url = "https://api.callmebot.com/whatsapp.php"
            params = {
                "phone": phone,
                "text": message,
                "apikey": apikey
            }
            with httpx.Client() as client:
                resp = client.get(url, params=params)
                
            if resp.status_code == 200:
                log.info("CallMeBot WhatsApp message sent successfully.")
                return True
            else:
                log.error(f"Failed to send CallMeBot message: Status {resp.status_code}, Body: {resp.text}")
                return False
        except Exception as e:
            log.error(f"Error sending CallMeBot WhatsApp message: {e}")
            return False

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Send progress notification")
    parser.add_argument("message", type=str, nargs="?", default="Test message from WeddingSnap notifier!", help="Message content")
    args = parser.parse_args()
    
    send_whatsapp(args.message)
