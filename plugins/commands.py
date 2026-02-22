import asyncio
import os
import re
import time
import urllib.parse
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from plugins.config import Config
from plugins.helper.database import add_user, get_user, update_user, is_banned
from plugins.helper.upload import (
    download_url, upload_file, humanbytes,
    smart_output_name, is_ytdlp_url, fetch_ytdlp_info,
)

# ─────────────────────────────────────────────────────────────────────────────
# State dicts
#   PENDING_RENAMES: waiting for user to provide new filename
#   PENDING_MODE:    filename resolved, waiting for Media vs Document choice
# ─────────────────────────────────────────────────────────────────────────────
PENDING_RENAMES: dict[int, dict] = {}   # {user_id: {"url": str, "orig": str}}
PENDING_MODE: dict[int, dict] = {}      # {user_id: {"url": str, "filename": str}}
PENDING_QUALITY: dict[int, dict] = {}   # {user_id: {"url": str, "filename": str}}


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_filename(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = os.path.basename(parsed.path.rstrip("/"))
    name = urllib.parse.unquote(name) if name else ""
    # For YouTube-style URLs the path segment is useless (e.g. 'watch', 'reel', 'shorts')
    # Fall back to the video ID from the query string or last path segment
    USELESS_NAMES = {"watch", "reel", "shorts", "video", "embed", "v", "e"}
    if not name or name.lower() in USELESS_NAMES:
        # Try query param 'v' (YouTube) or 'id'
        qs = urllib.parse.parse_qs(parsed.query)
        vid_id = qs.get("v", qs.get("id", [""]))[0]
        if vid_id:
            return f"{vid_id}.mp4"
        # Last non-empty path segment
        parts = [p for p in parsed.path.split("/") if p]
        name = parts[-1] if parts else "download"
    return name if name else "downloaded_file"


HELP_TEXT = """
📋 **Bot Commands**

➤ /start – Check if the bot is alive 🔔
➤ /help – Show this help message ❓
➤ /about – Info about the bot ℹ️
➤ /upload `<url>` – Upload a file from a direct URL 📤
➤ /skip – Keep original filename (use after /upload)

**Caption:**
➤ /caption `<text>` – Set a custom caption for uploads 📝
➤ /showcaption – View your current caption
➤ /clearcaption – Remove your custom caption

**Thumbnail:**
➤ /setthumb – Reply to a photo to set thumbnail 🖼️
➤ /showthumb – View your current thumbnail
➤ /delthumb – Delete your saved thumbnail

**Admin only:**
➤ /broadcast `<msg>` – Broadcast to all users 📢
➤ /total – Total registered users 👥
➤ /ban `<id>` – Ban a user ⛔
➤ /unban `<id>` – Unban a user ✅
➤ /status – Bot resource usage 🚀

**Supported platforms (via yt-dlp):**
YouTube · Instagram · Twitter/X · TikTok · Facebook · Reddit
Vimeo · Dailymotion · Twitch · SoundCloud · Bilibili + more
"""

ABOUT_TEXT = """
🤖 **URL Uploader Bot**

Upload files up to **2 GB** directly to Telegram from any direct URL.

**Features:**
• ✏️ Rename files before upload
• 🎬 Choose Media or Document upload mode
• 🖼️ Permanent thumbnails (saved to your account)
• 📝 Custom captions
• 📊 Live progress bars

**Tech:** Pyrogram MTProto · MongoDB · Docker · Koyeb
"""


# ─────────────────────────────────────────────────────────────────────────────
#  Build the Mode-selection keyboard
# ─────────────────────────────────────────────────────────────────────────────

# Standard quality breakpoints shown in the selector
_STD_QUALITIES = [("360p", 360), ("480p", 480), ("720p 📺", 720), ("1080p ⭐", 1080)]


def quality_keyboard_from_heights(user_id: int, heights: list) -> InlineKeyboardMarkup:
    """
    Build quality selector from the video's real available heights.
    - If heights is non-empty: video platform → show height buttons + Best + MP3.
    - If heights is empty: audio-only platform → show Best Audio + MP3 only.
    """
    max_h = max(heights) if heights else 0

    if max_h > 0:
        # Video platform: show applicable height buttons
        buttons = [
            InlineKeyboardButton(label, callback_data=f"quality:{user_id}:{h}p")
            for label, h in _STD_QUALITIES
            if max_h >= h
        ]
        buttons += [
            InlineKeyboardButton("🏆 Best",  callback_data=f"quality:{user_id}:best"),
            InlineKeyboardButton("🎧 MP3",   callback_data=f"quality:{user_id}:mp3"),
        ]
    else:
        # Audio-only platform (SoundCloud, Bandcamp, Mixcloud, etc.)
        buttons = [
            InlineKeyboardButton("🎧 Best Audio", callback_data=f"quality:{user_id}:audio"),
            InlineKeyboardButton("🎧 MP3 (192k)", callback_data=f"quality:{user_id}:mp3"),
        ]

    rows = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(rows)


def mode_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 Media",    callback_data=f"mode:{user_id}:media"),
            InlineKeyboardButton("📄 Document", callback_data=f"mode:{user_id}:doc"),
        ]
    ])


async def ask_quality(target_msg: Message, user_id: int, filename: str,
                      heights: list):
    """Show the quality/format selector, adapting to audio-only vs video sources."""
    has_video = bool(heights)
    if has_video:
        avail = ", ".join(str(h) + "p" for h in heights if h >= 240)
        subtitle = "📺 Select **video quality** or **🎧 MP3** for audio only:"
        top = f"📊 Available: {avail}" if avail else ""
    else:
        subtitle = "🎧 Select **audio format** (audio-only source):"
        top = "📳 Audio-only source — no video streams detected"
    text = (
        f"📁 **File:** `{filename}`\n"
        + (f"{top}\n" if top else "")
        + f"\n{subtitle}"
    )
    kb = quality_keyboard_from_heights(user_id, heights)
    try:
        await target_msg.edit_text(text, reply_markup=kb)
    except Exception:
        await target_msg.reply_text(text, reply_markup=kb, quote=True)


async def ask_mode(target_msg: Message, user_id: int, filename: str):
    """Edit or reply with the upload-mode selection prompt."""
    text = (
        f"📁 **File:** `{filename}`\n\n"
        "How should this file be uploaded?"
    )
    try:
        await target_msg.edit_text(text, reply_markup=mode_keyboard(user_id))
    except Exception:
        await target_msg.reply_text(text, reply_markup=mode_keyboard(user_id), quote=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Core upload executor
# ─────────────────────────────────────────────────────────────────────────────

async def do_upload(
    client: Client,
    reply_to: Message,
    user_id: int,
    url: str,
    filename: str,
    force_document: bool = False,
    quality: str = "1080p",
):
    status_msg = await reply_to.reply_text(
        f"📥 Starting download…\n`{filename}`", quote=True
    )
    start_time = [time.time()]
    file_path = None
    try:
        file_path, mime = await download_url(url, filename, status_msg, start_time,
                                              quality=quality)
        file_size = os.path.getsize(file_path)

        # ── User settings ──────────────────────────────────────────────────
        user_data = await get_user(user_id) or {}
        custom_caption = user_data.get("caption") or ""
        thumb_file_id = user_data.get("thumb") or None

        caption = custom_caption or os.path.basename(file_path)

        await status_msg.edit_text("📤 Uploading to Telegram…")
        await upload_file(
            client, reply_to.chat.id, file_path, mime,
            caption, thumb_file_id, status_msg, start_time,
            force_document=force_document,
        )
        await status_msg.edit_text("✅ Upload complete!")

        # ── Log ────────────────────────────────────────────────────────────
        if Config.LOG_CHANNEL:
            elapsed = time.time() - start_time[0]
            try:
                await client.send_message(
                    Config.LOG_CHANNEL,
                    f"📤 **Upload log**\n"
                    f"👤 `{user_id}`\n"
                    f"🔗 `{url}`\n"
                    f"📁 `{os.path.basename(file_path)}`\n"
                    f"💾 {humanbytes(file_size)} · ⏱ {elapsed:.1f}s\n"
                    f"📦 Mode: {'Document' if force_document else quality if quality == 'mp3' else f'Media ({quality})'}",
                )
            except Exception:
                pass

    except ValueError as e:
        await status_msg.edit_text(f"❌ {e}")
    except Exception as e:
        Config.LOGGER.exception("Upload error")
        await status_msg.edit_text(f"❌ Error: `{e}`")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
#  Shared rename resolver — called after filename is decided
# ─────────────────────────────────────────────────────────────────────────────

async def resolve_rename(
    client: Client,
    prompt_msg: Message,
    user_id: int,
    url: str,
    filename: str,
    heights: list | None = None,
):
    """Route to quality selector (yt-dlp) or Media/Document selector (direct)."""
    if is_ytdlp_url(url):
        h = heights or []
        PENDING_QUALITY[user_id] = {"url": url, "filename": filename, "heights": h}
        await ask_quality(prompt_msg, user_id, filename, h)
    else:
        PENDING_MODE[user_id] = {"url": url, "filename": filename}
        await ask_mode(prompt_msg, user_id, filename)


# ─────────────────────────────────────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user = message.from_user
    await add_user(user.id, user.username)
    if await is_banned(user.id):
        return await message.reply_text("🚫 You are banned from using this bot.")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Updates", url=f"https://t.me/{Config.UPDATES_CHANNEL}")],
        [InlineKeyboardButton("❓ Help", callback_data="help"),
         InlineKeyboardButton("ℹ️ About", callback_data="about")],
    ])
    await message.reply_text(
        f"👋 Hello **{user.first_name}**!\n\n"
        "I can upload files up to **2 GB** to Telegram from any direct URL.\n\n"
        "📤 Send a URL or use `/upload <url>` to get started!\n"
        "✏️ I'll ask you to rename and choose upload mode before uploading.",
        reply_markup=kb,
        quote=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /help  /about
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("help") & filters.private)
async def help_handler(client: Client, message: Message):
    await message.reply_text(HELP_TEXT, quote=True)


@Client.on_message(filters.command("about") & filters.private)
async def about_handler(client: Client, message: Message):
    await message.reply_text(ABOUT_TEXT, quote=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Inline keyboard callbacks  — MUST use specific filters to avoid conflicts
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^(help|about)$"))
async def cb_help_about(client: Client, callback_query: CallbackQuery):
    data = callback_query.data
    if data == "help":
        await callback_query.message.edit_text(HELP_TEXT)
    elif data == "about":
        await callback_query.message.edit_text(ABOUT_TEXT)
    await callback_query.answer()


@Client.on_callback_query(filters.regex(r"^skip_rename:(\d+)$"))
async def skip_rename_cb(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    target_id = int(callback_query.data.split(":")[1])
    if user_id != target_id:
        return await callback_query.answer("Not your upload!", show_alert=True)

    pending = PENDING_RENAMES.pop(user_id, None)
    if not pending:
        return await callback_query.answer("Already processed or expired.", show_alert=True)

    await callback_query.answer()
    # Move to mode selection
    await resolve_rename(
        client,
        callback_query.message,   # the rename prompt message to edit in place
        user_id,
        pending["url"],
        pending["orig"],
    )


@Client.on_callback_query(filters.regex(r"^mode:(\d+):(media|doc)$"))
async def mode_cb(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    parts = callback_query.data.split(":")
    target_id = int(parts[1])
    choice = parts[2]   # "media" or "doc"

    if user_id != target_id:
        return await callback_query.answer("Not your upload!", show_alert=True)

    pending = PENDING_MODE.pop(user_id, None)
    if not pending:
        return await callback_query.answer("Already processed or expired.", show_alert=True)

    await callback_query.answer()
    mode_label = "📄 Document" if choice == "doc" else "🎬 Media"
    try:
        await callback_query.message.edit_text(
            f"✅ Uploading as **{mode_label}**…\n`{pending['filename']}`"
        )
    except Exception:
        pass

    await do_upload(
        client,
        callback_query.message,
        user_id,
        pending["url"],
        pending["filename"],
        force_document=(choice == "doc"),
    )


@Client.on_callback_query(filters.regex(r"^quality:(\d+):(360p|480p|720p|1080p|best|mp3|audio)$"))
async def quality_cb(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    parts = callback_query.data.split(":")
    target_id = int(parts[1])
    quality = parts[2]   # "360p" | "480p" | "720p" | "1080p" | "best" | "mp3"

    if user_id != target_id:
        return await callback_query.answer("Not your upload!", show_alert=True)

    pending = PENDING_QUALITY.pop(user_id, None)
    if not pending:
        return await callback_query.answer("Already processed or expired.", show_alert=True)

    await callback_query.answer()

    # Adjust filename extension for audio-only downloads
    filename = pending["filename"]
    if quality == "mp3":
        filename = os.path.splitext(filename)[0] + ".mp3"
    elif quality == "audio":
        filename = os.path.splitext(filename)[0] + ".m4a"

    label_map = {
        "360p": "360p", "480p": "480p", "720p": "720p 📺",
        "1080p": "1080p ⭐", "best": "🏆 Best Quality",
        "mp3": "🎧 MP3 192k", "audio": "🎧 Best Audio",
    }
    try:
        await callback_query.message.edit_text(
            f"⬇️ Downloading **{label_map.get(quality, quality)}**…\n`{filename}`"
        )
    except Exception:
        pass

    await do_upload(
        client,
        callback_query.message,
        user_id,
        pending["url"],
        filename,
        quality=quality,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /upload <url>  — step 1: ask for rename
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("upload") & filters.private)
async def upload_handler(client: Client, message: Message):
    user = message.from_user
    await add_user(user.id, user.username)

    if await is_banned(user.id):
        return await message.reply_text("🚫 You are banned.")

    args = message.command
    url = None
    if len(args) > 1:
        url = args[1].strip()
    elif message.reply_to_message and message.reply_to_message.text:
        url = message.reply_to_message.text.strip()

    if not url or not url.startswith(("http://", "https://")):
        return await message.reply_text(
            "❌ Please provide a valid direct URL.\n\nUsage: `/upload https://example.com/file.mp4`",
            quote=True,
        )

    # For yt-dlp URLs, fetch the video title and available heights
    heights = []
    if is_ytdlp_url(url):
        status_info = await message.reply_text("🔍 Fetching video info…", quote=True)
        info = await fetch_ytdlp_info(url)
        try:
            await status_info.delete()
        except Exception:
            pass
        orig_filename = info["title"] or smart_output_name(extract_filename(url))
        heights = info["heights"]
    else:
        orig_filename = smart_output_name(extract_filename(url))
    PENDING_RENAMES[user.id] = {"url": url, "orig": orig_filename, "heights": heights}

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Skip (keep original)", callback_data=f"skip_rename:{user.id}")]
    ])
    await message.reply_text(
        f"✏️ **Rename file?**\n\n"
        f"📁 Original: `{orig_filename}`\n\n"
        "Send the **new filename** (with extension) or press **Skip**:",
        reply_markup=kb,
        quote=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  /skip — keep original filename via command
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("skip") & filters.private)
async def skip_handler(client: Client, message: Message):
    user_id = message.from_user.id
    pending = PENDING_RENAMES.pop(user_id, None)
    if not pending:
        return await message.reply_text("❌ No pending upload. Send a URL first.", quote=True)

    prompt = await message.reply_text("⏭ Keeping original filename…", quote=True)
    await resolve_rename(client, prompt, user_id, pending["url"], pending["orig"],
                         heights=pending.get("heights", []))


# ─────────────────────────────────────────────────────────────────────────────
#  Text handler — rename input OR bare URL
# ─────────────────────────────────────────────────────────────────────────────

_ALL_COMMANDS = [
    "start", "help", "about", "upload", "skip", "caption", "showcaption",
    "clearcaption", "setthumb", "showthumb", "delthumb",
    "broadcast", "total", "ban", "unban", "status",
]


@Client.on_message(filters.private & filters.text & ~filters.command(_ALL_COMMANDS))
async def text_handler(client: Client, message: Message):
    user = message.from_user
    text = (message.text or "").strip()

    # ── Pending rename input ──────────────────────────────────────────────────
    if user.id in PENDING_RENAMES:
        pending = PENDING_RENAMES.pop(user.id)
        new_name = text.strip()
        # Preserve original extension if user didn't include one
        orig_ext = os.path.splitext(pending["orig"])[1]
        new_ext = os.path.splitext(new_name)[1]
        if not new_ext and orig_ext:
            new_name = new_name + orig_ext

        prompt = await message.reply_text(f"✏️ Renamed to: `{new_name}`", quote=True)
        await resolve_rename(client, prompt, user.id, pending["url"], new_name,
                             heights=pending.get("heights", []))
        return

    # ── Bare URL ──────────────────────────────────────────────────────────────
    if text.startswith(("http://", "https://")):
        await add_user(user.id, user.username)
        if await is_banned(user.id):
            return await message.reply_text("🚫 You are banned.")
        # For yt-dlp URLs, fetch video title as suggested filename
        if is_ytdlp_url(text):
            status_info = await message.reply_text("🔍 Fetching video info…", quote=True)
            info = await fetch_ytdlp_info(text)
            try:
                await status_info.delete()
            except Exception:
                pass
            orig_filename = info["title"] or smart_output_name(extract_filename(text))
            heights = info["heights"]
        else:
            orig_filename = smart_output_name(extract_filename(text))
            heights = []
        PENDING_RENAMES[user.id] = {"url": text, "orig": orig_filename, "heights": heights}
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭ Skip (keep original)", callback_data=f"skip_rename:{user.id}")]
        ])
        await message.reply_text(
            f"✏️ **Rename file?**\n\n"
            f"📁 Original: `{orig_filename}`\n\n"
            "Send the **new filename** (with extension) or press **Skip**:",
            reply_markup=kb,
            quote=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Caption management
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("caption") & filters.private)
async def set_caption(client: Client, message: Message):
    args = message.command
    if len(args) < 2:
        return await message.reply_text("Usage: `/caption Your caption text here`", quote=True)
    caption = " ".join(args[1:])
    await update_user(message.from_user.id, {"caption": caption})
    await message.reply_text(f"✅ Caption saved:\n\n{caption}", quote=True)


@Client.on_message(filters.command("showcaption") & filters.private)
async def show_caption(client: Client, message: Message):
    user_data = await get_user(message.from_user.id) or {}
    cap = user_data.get("caption") or "_(none set)_"
    await message.reply_text(f"📝 Your caption:\n\n{cap}", quote=True)


@Client.on_message(filters.command("clearcaption") & filters.private)
async def clear_caption(client: Client, message: Message):
    await update_user(message.from_user.id, {"caption": ""})
    await message.reply_text("✅ Caption cleared.", quote=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Thumbnail management — stored as Telegram file_id (permanent)
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("setthumb") & filters.private)
async def set_thumb(client: Client, message: Message):
    reply = message.reply_to_message
    if not reply or not reply.photo:
        return await message.reply_text(
            "❌ Reply to a **photo** with /setthumb to save it as your thumbnail.",
            quote=True,
        )
    file_id = reply.photo.file_id
    await update_user(message.from_user.id, {"thumb": file_id})
    await message.reply_text(
        "✅ Thumbnail saved permanently!\n"
        "It will be applied to all your future uploads.",
        quote=True,
    )


@Client.on_message(filters.command("showthumb") & filters.private)
async def show_thumb(client: Client, message: Message):
    user_data = await get_user(message.from_user.id) or {}
    thumb_id = user_data.get("thumb")
    if not thumb_id:
        return await message.reply_text("❌ No thumbnail set. Reply to a photo with /setthumb.", quote=True)
    try:
        await message.reply_photo(photo=thumb_id, caption="🖼️ Your current thumbnail", quote=True)
    except Exception as e:
        await message.reply_text(f"❌ Could not show thumbnail: `{e}`", quote=True)


@Client.on_message(filters.command("delthumb") & filters.private)
async def del_thumb(client: Client, message: Message):
    await update_user(message.from_user.id, {"thumb": None})
    await message.reply_text("✅ Thumbnail deleted.", quote=True)
