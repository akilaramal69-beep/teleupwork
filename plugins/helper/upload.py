import asyncio
import time
import os
import json
import mimetypes
import re
import urllib.parse
import aiohttp
import aiofiles
from pyrogram import Client
from plugins.config import Config

PROGRESS_UPDATE_DELAY = 5  # seconds between progress edits

# ── Streaming / HLS detection ─────────────────────────────────────────────────

# Extensions that indicate a playlist / stream, not a direct media file
STREAMING_EXTENSIONS: dict[str, str] = {
    ".m3u8": ".mp4",
    ".m3u":  ".mp4",
    ".mpd":  ".mp4",   # DASH manifest
    ".ts":   ".mp4",   # raw MPEG-TS segment
}

# Quality label → max height (None = no limit = absolute best)
QUALITY_HEIGHT_MAP: dict[str, int | None] = {
    "360p":  360,
    "480p":  480,
    "720p":  720,
    "1080p": 1080,
    "best":  None,
}

HLS_MIME_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "video/mp2t",
}


def needs_ffmpeg_download(url: str, mime: str) -> bool:
    """Return True if this URL must be downloaded with ffmpeg instead of aiohttp."""
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    return ext in STREAMING_EXTENSIONS or (mime or "").lower() in HLS_MIME_TYPES


def smart_output_name(filename: str) -> str:
    """
    Remap known streaming extensions to the proper container extension.
    e.g. 'stream.m3u8' → 'stream.mp4'
    """
    stem, ext = os.path.splitext(filename)
    return stem + STREAMING_EXTENSIONS.get(ext.lower(), ext)

# ── yt-dlp integration ───────────────────────────────────────────────────────

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False

# Domains where yt-dlp should be used instead of direct HTTP download
YTDLP_DOMAINS = {
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "instagram.com",
    "twitter.com", "x.com", "t.co",
    "tiktok.com", "vm.tiktok.com",
    "facebook.com", "fb.watch", "fb.com",
    "reddit.com", "v.redd.it", "redd.it",
    "dailymotion.com", "dai.ly",
    "vimeo.com",
    "twitch.tv", "clips.twitch.tv",
    "soundcloud.com",
    "bilibili.com", "b23.tv",
    "pinterest.com",
    "streamable.com",
    "rumble.com",
    "odysee.com",
    "bitchute.com",
    "mixcloud.com",
}


def is_ytdlp_url(url: str) -> bool:
    """Return True if the URL belongs to a yt-dlp-supported platform."""
    if not YTDLP_AVAILABLE:
        return False
    try:
        host = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
        return any(host == d or host.endswith("." + d) for d in YTDLP_DOMAINS)
    except Exception:
        return False


async def _safe_edit(msg, text: str):
    """Edit a Telegram message, silently ignoring all errors."""
    try:
        await msg.edit_text(text)
    except Exception:
        pass


async def fetch_ytdlp_info(url: str) -> dict:
    """
    Fetch video title and available heights from yt-dlp without downloading.
    Returns: {"title": str | None, "heights": list[int]}
    """
    if not YTDLP_AVAILABLE:
        return {"title": None, "heights": []}
    loop = asyncio.get_running_loop()

    def _fetch():
        try:
            opts = {"quiet": True, "no_warnings": True, "skip_download": True}
            if Config.YT_COOKIES_FILE:
                opts["cookiefile"] = Config.YT_COOKIES_FILE
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # Title
                raw = info.get("title") or info.get("id") or "video"
                title = re.sub(r'[\\/*?"<>|:\n\r\t]', "_", raw).strip()
                title = f"{title[:180]}.mp4"
                # Available video-only heights (deduplicated, sorted)
                heights = sorted(set(
                    int(f["height"])
                    for f in info.get("formats", [])
                    if f.get("height") and f.get("vcodec", "none") != "none"
                    and int(f.get("height", 0)) > 0
                ))
                return {"title": title, "heights": heights}
        except Exception:
            return {"title": None, "heights": []}

    return await loop.run_in_executor(None, _fetch)


async def download_ytdlp(
    url: str,
    filename: str,
    progress_msg,
    start_time_ref: list,
    quality: str = "1080p",
) -> tuple[str, str]:
    """
    Download content using yt-dlp with live progress.
    quality: '360p' | '480p' | '720p' | '1080p' | 'best' | 'mp3'
    Returns (file_path, mime_type).
    """
    start_time_ref[0] = time.time()
    loop = asyncio.get_running_loop()
    last_edit = [start_time_ref[0]]

    out_dir = Config.DOWNLOAD_LOCATION
    os.makedirs(out_dir, exist_ok=True)

    is_mp3 = quality == "mp3"

    # Build a safe output stem from the user-chosen filename
    safe_stem = re.sub(r'[\\/*?"<>|:]', "_", os.path.splitext(filename)[0])[:190]
    outtmpl = os.path.join(out_dir, f"{safe_stem}.%(ext)s")

    def _progress_hook(d: dict):
        now = time.time()
        if d["status"] == "downloading" and now - last_edit[0] >= PROGRESS_UPDATE_DELAY:
            last_edit[0] = now
            done = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            bar = progress_bar(done, total) if total else "░" * 12
            pct = f"{done / total * 100:.1f}%" if total else "…"
            text = (
                f"📥 **Downloading via yt-dlp…**\n\n"
                f"[{bar}] {pct}\n"
                f"**Done:** {humanbytes(done)}"
                + (f" / {humanbytes(total)}" if total else "")
                + (f"\n**Speed:** {humanbytes(speed)}/s" if speed else "")
                + (f"\n**ETA:** {time_formatter(eta)}" if eta else "")
            )
            asyncio.run_coroutine_threadsafe(_safe_edit(progress_msg, text), loop)

    # Build format string — 3-level fallback so something always matches
    is_mp3 = quality == "mp3"
    is_audio_only = quality == "audio"

    if is_mp3:
        format_str = "bestaudio/best"
        postprocessors = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    elif is_audio_only:
        # Download best audio in its native codec — no re-encoding, no merge needed
        format_str = "bestaudio/best"
        postprocessors = []
    else:
        height = QUALITY_HEIGHT_MAP.get(quality, 1080)
        if height:
            # 1. DASH video at requested height + best audio
            # 2. best DASH video+audio at any height
            # 3. best combined single-stream as last resort
            format_str = f"bv[height<={height}]+ba/bv+ba/b"
        else:  # "best" — no height cap
            format_str = "bv+ba/b"
        postprocessors = []

    ydl_opts: dict = {
        "format": format_str,
        "outtmpl": outtmpl,
        "progress_hooks": [_progress_hook],
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
        "noplaylist": True,
        "max_filesize": Config.MAX_FILE_SIZE,
    }
    # Only set merge_output_format for video downloads
    if not is_mp3 and not is_audio_only:
        ydl_opts["merge_output_format"] = "mp4"
    if postprocessors:
        ydl_opts["postprocessors"] = postprocessors
    if Config.YT_COOKIES_FILE:
        ydl_opts["cookiefile"] = Config.YT_COOKIES_FILE

    def _run() -> str:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
            # Determine expected extension
            if is_mp3:
                expected_ext = ".mp3"
            elif is_audio_only:
                expected_ext = None   # yt-dlp chooses (m4a, opus, etc.)
            else:
                expected_ext = ".mp4"

            if expected_ext:
                expected_path = os.path.join(out_dir, f"{safe_stem}{expected_ext}")
                if os.path.exists(expected_path):
                    return expected_path
            # Fallback: largest file starting with the stem
            candidates = sorted(
                [f for f in os.listdir(out_dir) if f.startswith(safe_stem)],
                key=lambda f: os.path.getsize(os.path.join(out_dir, f)),
                reverse=True,
            )
            if candidates:
                return os.path.join(out_dir, candidates[0])
            raise FileNotFoundError("yt-dlp: output file not found after download")

    file_path = await loop.run_in_executor(None, _run)
    if is_mp3:
        mime = "audio/mpeg"
    elif is_audio_only:
        mime = mimetypes.guess_type(file_path)[0] or "audio/mp4"
    else:
        mime = mimetypes.guess_type(file_path)[0] or "video/mp4"
    return file_path, mime




def humanbytes(size: int) -> str:
    if not size:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def time_formatter(seconds: float) -> str:
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    elif minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def progress_bar(current: int, total: int, length: int = 12) -> str:
    filled = int(length * current / total) if total else 0
    bar = "█" * filled + "░" * (length - filled)
    percent = current / total * 100 if total else 0
    return f"[{bar}] {percent:.1f}%"


# ── FFprobe / FFmpeg helpers ──────────────────────────────────────────────────

async def get_video_metadata(file_path: str) -> dict:
    """
    Use ffprobe (async subprocess) to extract duration, width, height from a video.
    Returns a dict with keys: duration (int seconds), width (int), height (int).
    Falls back to zeros if ffprobe is unavailable or fails.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        data = json.loads(stdout)
        video_stream = next(
            (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
            None,
        )
        duration = int(float(data.get("format", {}).get("duration", 0)))
        width = int(video_stream.get("width", 0)) if video_stream else 0
        height = int(video_stream.get("height", 0)) if video_stream else 0
        return {"duration": duration, "width": width, "height": height}
    except Exception:
        return {"duration": 0, "width": 0, "height": 0}


async def generate_video_thumbnail(file_path: str, chat_id: int, duration: int = 0) -> str | None:
    """
    Extract a single frame from the video at 10% of its duration (or 1 s if unknown),
    scaled to max width 320 px, saved as JPEG.  Returns the path or None on failure.
    """
    thumb_path = os.path.join(Config.DOWNLOAD_LOCATION, f"thumb_auto_{chat_id}.jpg")
    # Pick a timestamp: 10% into the video, minimum 1 s
    seek = max(1, int(duration * 0.1)) if duration else 1
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-ss", str(seek),
            "-i", file_path,
            "-vframes", "1",
            "-vf", "scale=320:-1",
            "-q:v", "2",          # JPEG quality (2 = very high, 31 = worst)
            thumb_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=60)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception:
        pass
    return None


# ── Download helpers ──────────────────────────────────────────────────────────

async def _download_hls(url: str, out_path: str, progress_msg, start_time_ref: list) -> str:
    """
    Use ffmpeg to download an HLS/DASH/TS stream and remux it to mp4.
    Shows elapsed-time progress (no size info available for streams).
    """
    start_time_ref[0] = time.time()
    last_edit = start_time_ref[0]

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-i", url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",   # fix AAC bitstream for mp4 container
        out_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    # Poll until ffmpeg finishes, editing progress every PROGRESS_UPDATE_DELAY s
    while proc.returncode is None:
        await asyncio.sleep(1)
        now = time.time()
        if now - last_edit >= PROGRESS_UPDATE_DELAY:
            elapsed = now - start_time_ref[0]
            try:
                await progress_msg.edit_text(
                    f"📥 **Downloading stream via ffmpeg…**\n"
                    f"⏱ Elapsed: {time_formatter(elapsed)}"
                )
            except Exception:
                pass
            last_edit = now

    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="replace")[-600:]
        raise RuntimeError(f"ffmpeg stream download failed:\n{err}")

    return out_path


async def download_url(url: str, filename: str, progress_msg, start_time_ref: list,
                       quality: str = "1080p"):
    """
    Stream-download a URL to disk, editing progress_msg periodically.
    Returns (path, mime_type) on success or raises.
    quality only applies to yt-dlp URLs.
    """
    download_dir = Config.DOWNLOAD_LOCATION
    os.makedirs(download_dir, exist_ok=True)

    # Remap streaming extensions to proper container (e.g. .m3u8 → .mp4)
    filename = smart_output_name(filename)
    safe_name = re.sub(r'[\\/*?"<>|]', "_", filename)[:200]
    file_path = os.path.join(download_dir, safe_name)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    # ── Route yt-dlp-supported platforms ─────────────────────────────────────
    if is_ytdlp_url(url):
        try:
            await progress_msg.edit_text(
                "📥 **Fetching from yt-dlp…**\n_(connecting to platform…)_"
            )
        except Exception:
            pass
        return await download_ytdlp(url, filename, progress_msg, start_time_ref, quality=quality)

    # ── Probe the URL to detect content type ─────────────────────────────────
    async with aiohttp.ClientSession(headers=headers) as probe_session:
        async with probe_session.head(
            url, allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as head:
            mime = head.headers.get("Content-Type", "").split(";")[0].strip()
            total_str = head.headers.get("Content-Length", "0")
            total = int(total_str) if total_str.isdigit() else 0

    # ── Route HLS / DASH / TS streams through ffmpeg ──────────────────────────
    if needs_ffmpeg_download(url, mime):
        # Force mp4 output path
        mp4_path = os.path.splitext(file_path)[0] + ".mp4"
        try:
            await progress_msg.edit_text(
                "📥 **Downloading stream…**\n"
                "_(HLS/DASH stream detected — using ffmpeg)_"
            )
        except Exception:
            pass
        await _download_hls(url, mp4_path, progress_msg, start_time_ref)
        return mp4_path, "video/mp4"

    # ── Standard aiohttp streaming download ──────────────────────────────────
    if total > Config.MAX_FILE_SIZE:
        raise ValueError(
            f"File too large: {humanbytes(total)} (max {humanbytes(Config.MAX_FILE_SIZE)})"
        )

    start_time_ref[0] = time.time()
    last_edit = start_time_ref[0]
    downloaded = 0

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(
            url,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=Config.PROCESS_MAX_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            # Refine content-length/mime from GET response
            if not total:
                total = int(resp.headers.get("Content-Length", 0))
            mime = resp.content_type or mime or "application/octet-stream"

            async with aiofiles.open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(Config.CHUNK_SIZE):
                    await f.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if now - last_edit >= PROGRESS_UPDATE_DELAY:
                        elapsed = now - start_time_ref[0]
                        speed = downloaded / elapsed if elapsed else 0
                        eta = (total - downloaded) / speed if speed and total else 0
                        bar = progress_bar(downloaded, total)
                        text = (
                            "📥 **Downloading…**\n\n"
                            f"{bar}\n"
                            f"**Done:** {humanbytes(downloaded)}"
                            + (f" / {humanbytes(total)}" if total else "")
                            + f"\n**Speed:** {humanbytes(speed)}/s\n"
                            f"**ETA:** {time_formatter(eta)}"
                        )
                        try:
                            await progress_msg.edit_text(text)
                        except Exception:
                            pass
                        last_edit = now

    mime_from_ext = mimetypes.guess_type(file_path)[0]
    final_mime = mime_from_ext or mime
    return file_path, final_mime



# ── Upload helper ─────────────────────────────────────────────────────────────

async def upload_file(
    client: Client,
    chat_id: int,
    file_path: str,
    mime: str,
    caption: str,
    thumb_file_id: str | None,
    progress_msg,
    start_time_ref: list,
    force_document: bool = False,
):
    """
    Upload a local file to Telegram with:
    - Live progress bar
    - Correct duration / width / height for videos (extracted via ffprobe)
    - Auto-generated thumbnail from the video frame if no custom thumb is set
    - Custom thumbnail (downloaded from Telegram by file_id) if set by user
    """

    last_edit = [time.time()]
    start_time_ref[0] = time.time()

    async def _progress(current: int, total: int):
        now = time.time()
        if now - last_edit[0] < PROGRESS_UPDATE_DELAY:
            return
        elapsed = now - start_time_ref[0]
        speed = current / elapsed if elapsed else 0
        eta = (total - current) / speed if speed else 0
        bar = progress_bar(current, total)
        text = (
            "📤 **Uploading…**\n\n"
            f"{bar}\n"
            f"**Done:** {humanbytes(current)} / {humanbytes(total)}\n"
            f"**Speed:** {humanbytes(speed)}/s\n"
            f"**ETA:** {time_formatter(eta)}"
        )
        try:
            await progress_msg.edit_text(text)
        except Exception:
            pass
        last_edit[0] = now

    os.makedirs(Config.DOWNLOAD_LOCATION, exist_ok=True)
    is_video = not force_document and bool(mime and mime.startswith("video/"))
    is_audio = not force_document and bool(mime and mime.startswith("audio/"))
    is_image = not force_document and bool(mime and mime.startswith("image/"))

    # ── 1. Get video metadata (duration, width, height) ───────────────────────
    meta = {"duration": 0, "width": 0, "height": 0}
    if is_video:
        try:
            await progress_msg.edit_text("🔍 Reading video metadata…")
        except Exception:
            pass
        meta = await get_video_metadata(file_path)

    # ── 2. Resolve thumbnail ───────────────────────────────────────────────────
    thumb_local = None
    auto_thumb = False

    if thumb_file_id:
        # User has a saved thumbnail — download it from Telegram
        try:
            thumb_local = await client.download_media(
                thumb_file_id,
                file_name=os.path.join(Config.DOWNLOAD_LOCATION, f"thumb_user_{chat_id}.jpg"),
            )
        except Exception:
            thumb_local = None

    if not thumb_local and is_video:
        # No custom thumb → auto-generate from video frame
        try:
            await progress_msg.edit_text("🖼️ Generating thumbnail…")
        except Exception:
            pass
        thumb_local = await generate_video_thumbnail(file_path, chat_id, meta["duration"])

    # ── 3. Build kwargs (chat_id and file passed as positional args) ───────────
    kwargs = dict(
        caption=caption,
        parse_mode=None,
        progress=_progress,
    )
    if thumb_local:
        kwargs["thumb"] = thumb_local

    # ── 4. Send to Telegram ───────────────────────────────────────────────────
    try:
        if force_document:
            await client.send_document(chat_id, file_path, **kwargs)
        elif is_video:
            await client.send_video(
                chat_id,
                file_path,
                duration=meta["duration"],
                width=meta["width"],
                height=meta["height"],
                supports_streaming=True,
                **kwargs,
            )
        elif is_audio:
            await client.send_audio(chat_id, file_path, **kwargs)
        elif is_image:
            await client.send_photo(chat_id, file_path,
                                    caption=caption, progress=_progress)
        else:
            await client.send_document(chat_id, file_path, **kwargs)
    finally:
        # Clean up any temp thumbnail files
        if thumb_local and os.path.exists(thumb_local):
            try:
                os.remove(thumb_local)
            except Exception:
                pass
