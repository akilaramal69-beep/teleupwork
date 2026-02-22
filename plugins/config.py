import os
import logging
import base64

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("log.txt"), logging.StreamHandler()],
    level=logging.INFO,
)


def _resolve_cookies() -> str | None:
    """
    Resolve yt-dlp cookies file path at startup.
    Priority:
      1. YT_COOKIES_B64  – base64-encoded cookies.txt (for Koyeb env vars)
      2. COOKIES_FILE    – direct path to an existing cookies.txt file
    Returns the file path, or None if not configured.
    """
    b64 = os.environ.get("YT_COOKIES_B64", "").strip()
    if b64:
        try:
            path = "/tmp/yt_cookies.txt"
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
            logging.info("yt-dlp: cookies loaded from YT_COOKIES_B64")
            return path
        except Exception as e:
            logging.warning(f"yt-dlp: failed to decode YT_COOKIES_B64: {e}")

    direct = os.environ.get("COOKIES_FILE", "").strip()
    if direct and os.path.exists(direct):
        logging.info(f"yt-dlp: cookies loaded from COOKIES_FILE={direct}")
        return direct

    return None


class Config:
    # ── Telegram ──────────────────────────────────────
    BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
    API_ID: int = int(os.environ.get("API_ID", 0))
    API_HASH: str = os.environ.get("API_HASH", "")
    BOT_USERNAME: str = os.environ.get("BOT_USERNAME", "UrlUploaderBot")

    # ── Owner / Admins ────────────────────────────────
    OWNER_ID: int = int(os.environ.get("OWNER_ID", 0))
    ADMIN: set = set(
        int(x) for x in os.environ.get("ADMIN", "").split() if x.isdigit()
    )
    BANNED_USERS: set = set(
        int(x) for x in os.environ.get("BANNED_USERS", "").split() if x.isdigit()
    )

    # ── Channels ──────────────────────────────────────
    LOG_CHANNEL: int = int(os.environ.get("LOG_CHANNEL", 0))
    UPDATES_CHANNEL: str = os.environ.get("UPDATES_CHANNEL", "")

    # ── Database ──────────────────────────────────────
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

    # ── File handling ─────────────────────────────────
    DOWNLOAD_LOCATION: str = "./DOWNLOADS"
    MAX_FILE_SIZE: int = 2_097_152_000          # ~2 GB (Pyrogram MTProto limit)
    CHUNK_SIZE: int = int(os.environ.get("CHUNK_SIZE", 512)) * 1024  # KB → bytes

    # ── yt-dlp cookies & tokens ───────────────────────
    # Decoded once at import time; reused for every yt-dlp call
    YT_COOKIES_FILE: str | None = _resolve_cookies()
    # Proof-of-Origin token for YouTube (bypasses server-IP bot detection)
    # Generate via: https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
    YT_POTOKEN: str = os.environ.get("YT_POTOKEN", "").strip()

    # ── Misc ──────────────────────────────────────────
    LOGGER = logging
    DEF_WATER_MARK_FILE: str = "@" + BOT_USERNAME
    PROCESS_MAX_TIMEOUT: int = 3600
    SESSION_STRING: str = os.environ.get("SESSION_STRING", "")
