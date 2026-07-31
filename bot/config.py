import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
FORCE_JOIN_CHANNEL = (os.getenv("FORCE_JOIN_CHANNEL", "") or "").lstrip("@").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
PORT = int(os.getenv("PORT", "10000") or 10000)
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
# Optional MongoDB persistence (free tier 512MB). When set, owner-managed
# data (settings, custom providers, users, groups, grants) is mirrored to
# MongoDB so re-deploys / restarts preserve all customization.
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB = os.getenv("MONGODB_DB", "xenex_bot").strip() or "xenex_bot"

# --- Rich Messages (Bot API 10.1 via Telethon MTProto) ---------------------
# Optional. Get api_id / api_hash from https://my.telegram.org.
# Without them the bot keeps working with classic HTML formatting.
TELEGRAM_API_ID = (os.getenv("TELEGRAM_API_ID", "") or os.getenv("API_ID", "")).strip()
TELEGRAM_API_HASH = (os.getenv("TELEGRAM_API_HASH", "") or os.getenv("API_HASH", "")).strip()
# Optional StringSession (useful on read-only / ephemeral filesystems).
TELETHON_SESSION = os.getenv("TELETHON_SESSION", "").strip()
TELETHON_SESSION_NAME = os.getenv("TELETHON_SESSION_NAME", "rich_bot").strip() or "rich_bot"
RICH_MESSAGES = (os.getenv("RICH_MESSAGES", "on").strip().lower() != "off")


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing. Set it in .env or environment.")
if not OWNER_ID:
    raise RuntimeError("OWNER_ID missing. Set it in .env or environment.")
