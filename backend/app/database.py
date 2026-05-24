import logging
from supabase import create_client, Client
from app.config import settings

log = logging.getLogger(__name__)

if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
    log.error("SUPABASE_URL or SUPABASE_KEY environment variables are missing.")
    raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in the environment.")

supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
