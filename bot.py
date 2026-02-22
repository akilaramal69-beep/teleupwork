import os
import threading
from plugins.config import Config
from pyrogram import Client

def run_health_server():
    import app  # noqa: F401 – registers routes
    from app import app as flask_app
    flask_app.run(host="0.0.0.0", port=8080, use_reloader=False)

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("🚀  URL Uploader Bot — Starting…")
    print("=" * 60 + "\n")

    # Ensure download folder exists
    os.makedirs(Config.DOWNLOAD_LOCATION, exist_ok=True)

    # Start Flask health server in background thread (required by Koyeb)
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    print("🌐 Health server started on port 8080")

    # Start bot
    plugins = dict(root="plugins")
    bot = Client(
        Config.SESSION_NAME if hasattr(Config, "SESSION_NAME") else "url_uploader_bot",
        bot_token=Config.BOT_TOKEN,
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        plugins=plugins,
        sleep_threshold=300,
        workers=8,                      # more concurrent handler coroutines
        upload_boost=True,              # pyroblack: parallel MTProto upload connections
        max_concurrent_transmissions=5, # pyroblack: 5 parallel chunk streams
    )

    print("🎊 BOT IS ALIVE 🎊")
    bot.run()
