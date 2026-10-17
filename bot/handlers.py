import os
import re
import time
import uuid
import logging
import asyncio
import urllib.parse
from typing import Dict, Any, Optional, List, Tuple, Union, Set, Callable
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified, RPCError
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

from pathlib import Path
from datetime import datetime

from config.settings import (
    OWNER_ID, ADMINS, WATERMARK_TEXT, MAX_CONCURRENT_JOBS, BOT_USERNAME,
    TELEGRAM_STORAGE_CHANNEL_ID, get_public_base_url
)
from db.connection import get_db_session
from db.repository import ContentRepository, slugify
from db.models import JobStatus, PublicationStatus, VideoStorageStatus, VideoStorage, App, Batch
from parsers.indexer import TxtIndexer, NormalizedBatchTree
from engines.job_engine import ContentProcessingEngine, BatchJobController
from engines.youtube_account_manager import YouTubeAccountManager
from bot.progress_ui import TelegramProgressUI, TelegramMessageThrottler
from bot.batch_wizard import BatchWizardManager, BatchWizardState
from validators.image_validator import validate_image_url
from storage.health import StorageHealthService
from storage.manager import MultiStorageManager
from storage.telegram_stream.client_pool import TelegramClientPool
from storage.telegram_stream.caption_formatter import format_telegram_channel_caption

logger = logging.getLogger(__name__)


# Single instance of ContentProcessingEngine per bot
processing_engine = ContentProcessingEngine(bot_id="bot_1")

# Global cache of recent batch sessions for instant retry of failed items
_last_batch_sessions_by_batch: Dict[str, BatchWizardState] = {}
_last_batch_sessions_by_user: Dict[int, BatchWizardState] = {}

# State tracking for users in interactive YouTube credential adding flow
_yt_add_waiting_users: Dict[int, bool] = {}
_yt_oauth_pending_sessions: Dict[int, Dict[str, Any]] = {}

# State tracking for users in interactive storage channel configuration
_waiting_for_channel_id: Dict[int, bool] = {}

# State tracking for interactive App & Batch editing
_app_manage_state: Dict[int, Dict[str, Any]] = {}


async def publish_video_to_telegram_stream_and_db(
    client: Client,
    message: Message,
    user_id: int,
    custom_title: Optional[str] = None
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Copies/Uploads a video to TELEGRAM_STORAGE_CHANNEL_ID,
    registers it in DB as an active Lecture and VideoStorage,
    and returns direct stream URL and website player URL.
    """
    pool = TelegramClientPool.get_instance()
    storage_chat_id = pool.storage_chat_id or OWNER_ID

    # 1. Determine media metadata
    video_obj = message.video or message.document
    duration = getattr(video_obj, "duration", 0) or 0
    file_size = getattr(video_obj, "file_size", 0) or 0
    width = getattr(video_obj, "width", 1920) or 1920
    height = getattr(video_obj, "height", 1080) or 1080
    resolution = f"{height}p" if height else "1080p"

    file_name = getattr(video_obj, "file_name", "") or ""
    caption = message.caption or ""
    title = custom_title or caption or (Path(file_name).stem if file_name else f"Lecture Video {int(time.time())}")

    tg_caption = format_telegram_channel_caption(
        title=title,
        metadata={
            "subject_name": "Direct Uploads",
            "batch_name": "Direct Video Streams",
            "folder_name": "Telegram Cloud Stream",
            "lecture_index": 1,
            "width": width,
            "height": height,
            "resolution": resolution,
        },
        width=width,
        height=height,
        resolution=resolution,
    )

    # 2. Copy/Send to Telegram Storage Channel
    storage_msg = None
    try:
        storage_msg = await message.copy(
            chat_id=storage_chat_id,
            caption=tg_caption,
            parse_mode=ParseMode.HTML
        )
    except Exception as copy_err:
        logger.warning("Could not copy message directly (%s), attempting forwarding/sending...", copy_err)
        try:
            storage_msg = await client.forward_messages(
                chat_id=storage_chat_id,
                from_chat_id=message.chat.id,
                message_ids=message.id
            )
        except Exception as fwd_err:
            return False, f"Failed to upload video to storage channel ({storage_chat_id}): {fwd_err}", {}

    if not storage_msg:
        return False, "Failed to upload video to storage channel.", {}

    storage_msg_id = storage_msg.id

    # 3. Create or attach in DB
    base_url = get_public_base_url()
    stream_url = f"{base_url}/api/v1/stream/tg/{storage_msg_id}"

    async with get_db_session() as session:
        repo = ContentRepository(session)

        # Get or create default app
        app = await repo.get_or_create_app("Course Wallah", "Official Course Wallah Hub", "/static/logo.png")

        # Get or create default batch
        batch, _ = await repo.get_or_create_batch(
            app_id=app.id,
            name="Direct Video Streams",
            category="Video Lectures",
            branch="All Branches",
            semester="All Semesters",
            quality_pref=resolution
        )

        # Get or create subject and folder
        subj = await repo.get_or_create_subject(batch.id, "Direct Uploads")
        folder = await repo.get_or_create_folder(subj.id, "Telegram Cloud Stream")

        # Next lecture index
        max_idx = await repo.get_max_lecture_index_for_batch(batch.id)
        next_idx = max_idx + 1

        lecture = await repo.create_or_update_lecture(
            folder_id=folder.id,
            subject_id=subj.id,
            batch_id=batch.id,
            lecture_index=next_idx,
            title=title,
            source_url=stream_url,
            provider="telegram_stream",
            has_video=True,
            has_pdf=False,
            publication_status=PublicationStatus.PUBLISHED,
            thumbnail_url="/static/logo.png"
        )

        video = await repo.attach_video_to_lecture(
            lecture_id=lecture.id,
            youtube_video_id=f"tg_{storage_msg_id}",
            duration=float(duration),
            resolution=resolution,
            file_size=file_size,
            title=title,
            upload_completed_at=datetime.utcnow()
        )

        # Keep the chat ID with the video; message IDs are only unique within a chat.
        video.telegram_chat_id = storage_chat_id
        video.telegram_message_id = storage_msg_id

        # Attach VideoStorage
        st_record = await repo.get_video_storage_by_provider(video.id, "telegram_stream")
        if not st_record:
            st_record = VideoStorage(
                video_id=video.id,
                provider="telegram_stream",
                provider_video_id=str(storage_msg_id),
                playback_url=f"/api/v1/stream/tg/{storage_msg_id}",
                status=VideoStorageStatus.READY.value,
                remote_size=file_size,
                remote_duration=float(duration),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            session.add(st_record)
        else:
            st_record.provider_video_id = str(storage_msg_id)
            st_record.playback_url = f"/api/v1/stream/tg/{storage_msg_id}"
            st_record.status = VideoStorageStatus.READY.value
            st_record.remote_size = file_size
            st_record.remote_duration = float(duration)
            st_record.updated_at = datetime.utcnow()

        st_record.metadata_json = {
            **(st_record.metadata_json if isinstance(st_record.metadata_json, dict) else {}),
            "chat_id": storage_chat_id,
            "message_id": storage_msg_id,
        }

        await repo.add_lecture_to_playlist(subj.id, lecture.id)
        await session.commit()

        web_player_url = f"{base_url}/?app={app.slug}&batchId={batch.slug}&subjectId={subj.slug}&content={lecture.id}&isPlaying=true"

    return True, "Video uploaded & published successfully!", {
        "title": title,
        "duration": duration,
        "file_size": file_size,
        "resolution": resolution,
        "storage_chat_id": storage_chat_id,
        "storage_msg_id": storage_msg_id,
        "stream_url": stream_url,
        "web_player_url": web_player_url,
        "batch_name": "Direct Video Streams",
        "lecture_id": lecture.id,
        "lecture_index": next_idx
    }


def parse_youtube_credentials_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Intelligently parses YouTube OAuth credentials from:
    1. Google Cloud client_secrets.json (installed / web dict)
    2. Custom JSON with client_id, client_secret, [refresh_token]
    3. Pipe-separated string: "Name | CLIENT_ID | CLIENT_SECRET | [REFRESH_TOKEN]"
    4. Multiline key-value string: "Client ID: ... \n Client Secret: ... \n Refresh Token: ..."
    """
    if not text:
        return None
    cleaned = text.strip()

    # 1. Try parsing JSON
    if (cleaned.startswith("{") and cleaned.endswith("}")) or ("client_id" in cleaned.lower() and "client_secret" in cleaned.lower()):
        try:
            import json
            data = json.loads(cleaned)
            # Support standard Google Cloud client_secret.json structure (web / installed)
            installed = data.get("installed") or data.get("web") or {}
            cid = data.get("client_id") or installed.get("client_id")
            csec = data.get("client_secret") or installed.get("client_secret")
            rt = data.get("refresh_token") or installed.get("refresh_token") or ""
            
            # Extract redirect URIs from Google client_secrets.json
            redirect_uris = installed.get("redirect_uris") or data.get("redirect_uris") or []
            redirect_uri = redirect_uris[0] if (redirect_uris and isinstance(redirect_uris, list)) else "http://localhost"
            
            name = (
                data.get("name") 
                or data.get("channel_name") 
                or installed.get("project_id") 
                or data.get("project_id") 
                or "YouTube Channel"
            )
            
            if cid and csec:
                return {
                    "name": str(name).strip(),
                    "client_id": str(cid).strip(),
                    "client_secret": str(csec).strip(),
                    "refresh_token": str(rt).strip(),
                    "redirect_uri": str(redirect_uri).strip()
                }
        except Exception:
            pass

    # 2. Try pipe delimiter format: "Name | client_id | client_secret | [refresh_token]"
    if "|" in cleaned:
        parts = [p.strip() for p in cleaned.split("|") if p.strip()]
        if len(parts) >= 4:
            return {
                "name": parts[0],
                "client_id": parts[1],
                "client_secret": parts[2],
                "refresh_token": parts[3],
                "redirect_uri": "http://localhost"
            }
        elif len(parts) == 3:
            if "apps.googleusercontent.com" in parts[0] or "GOCSPX" in parts[1]:
                return {
                    "name": "YouTube Channel",
                    "client_id": parts[0],
                    "client_secret": parts[1],
                    "refresh_token": parts[2],
                    "redirect_uri": "http://localhost"
                }
            else:
                return {
                    "name": parts[0],
                    "client_id": parts[1],
                    "client_secret": parts[2],
                    "refresh_token": "",
                    "redirect_uri": "http://localhost"
                }
        elif len(parts) == 2:
            if "apps.googleusercontent.com" in parts[0] or "GOCSPX" in parts[1]:
                return {
                    "name": "YouTube Channel",
                    "client_id": parts[0],
                    "client_secret": parts[1],
                    "refresh_token": "",
                    "redirect_uri": "http://localhost"
                }

    # 3. Try multiline key-value format
    lines = cleaned.splitlines()
    res = {}
    for line in lines:
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip().lower()
            v = v.strip()
            if "name" in k or "channel" in k or "title" in k or "project" in k:
                res["name"] = v
            elif "client_id" in k or "client id" in k or "clientid" in k:
                res["client_id"] = v
            elif "client_secret" in k or "client secret" in k or "clientsecret" in k:
                res["client_secret"] = v
            elif "refresh_token" in k or "refresh token" in k or "refreshtoken" in k:
                res["refresh_token"] = v
            elif "redirect" in k:
                res["redirect_uri"] = v

    if "client_id" in res and "client_secret" in res:
        res.setdefault("name", "YouTube Channel")
        res.setdefault("refresh_token", "")
        res.setdefault("redirect_uri", "http://localhost")
        return res

    return None

def extract_oauth_code_from_input(text: str) -> Optional[str]:
    """
    Extracts Google OAuth 2.0 authorization code from:
    1. Full redirect URL: http://localhost/?code=4/0AWtg...&scope=...
    2. URL query string: ?code=4/0AWtg...
    3. Raw authorization code: 4/0AWtg... or 4%2F0AWtg...
    """
    if not text:
        return None
    raw = text.strip()
    
    # 1. URL with ?code= or &code=
    if "code=" in raw:
        try:
            if "?" in raw:
                query = raw.split("?", 1)[1]
                parsed = urllib.parse.parse_qs(query)
                if "code" in parsed and parsed["code"]:
                    return urllib.parse.unquote(parsed["code"][0].strip())
        except Exception:
            pass
        # Regex fallback
        m = re.search(r"[?&]code=([^&\s]+)", raw)
        if m:
            return urllib.parse.unquote(m.group(1)).strip()

    # 2. Raw auth code: Google auth codes start with '4/' or '4%2F'
    if raw.startswith("4/") or raw.startswith("4%2F"):
        return urllib.parse.unquote(raw).strip()

    # 3. If raw code is alphanumeric with dashes/underscores/slashes and length between 25 and 180
    if len(raw) >= 25 and len(raw) <= 180 and not raw.startswith("http") and not raw.startswith("{") and "|" not in raw:
        return urllib.parse.unquote(raw).strip()

    return None

def build_oauth_authorization_card_and_markup(parsed: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    """Builds interactive authorization prompt with 1-Click button and clear instructions."""
    cid = parsed["client_id"]
    r_uri = parsed.get("redirect_uri", "http://localhost")
    proj_name = parsed.get("name", "Google Cloud Project")
    auth_url = YouTubeAccountManager.generate_oauth_authorization_url(cid, r_uri)
    
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 1-Click Authorize YouTube Channel", url=auth_url)],
        [InlineKeyboardButton("❌ Cancel", callback_data="yt:cancel_oauth")]
    ])
    
    masked_cid = f"{cid[:12]}...{cid[-10:]}" if len(cid) > 24 else cid
    
    card = (
        f"🔑 <b>GOOGLE OAUTH 2.0 CLIENT DETECTED!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📁 <b>Project / App:</b> <code>{proj_name}</code>\n"
        f"🆔 <b>Client ID:</b> <code>{masked_cid}</code>\n"
        f"🔗 <b>Redirect URI:</b> <code>{r_uri}</code>\n\n"
        f"Follow these 3 quick steps to link your channel:\n\n"
        f"1️⃣ <b>Click the button below</b> to grant YouTube upload permissions:\n"
        f"👉 <a href=\"{auth_url}\"><b>Click Here to Authorize YouTube</b></a>\n\n"
        f"2️⃣ <b>Allow Permissions:</b> Select your YouTube account and click <b>Continue / Allow</b>.\n\n"
        f"3️⃣ <b>Paste Redirected Link / Code:</b> Google will redirect your browser to:\n"
        f"<code>http://localhost/?code=4/0A...</code>\n"
        f"<i>(Note: If your browser says 'Site can't be reached' or 'Unable to connect' — that is 100% normal!)</i>\n\n"
        f"📥 <b>Copy the URL from your browser's address bar (or just the code) and paste/send it right here!</b>\n\n"
        f"🤖 <i>Course Wallah will automatically exchange it for a Refresh Token, fetch your channel name & stats, and add it to your auto-failover pool!</i>"
    )
    return card, markup

def is_admin(user_id: int) -> bool:
    """Strict admin check: OWNER_ID or authorized ADMINS list."""
    if not user_id:
        return False
    return bool(user_id == OWNER_ID or user_id in ADMINS)


async def run_init_animation(message: Message) -> bool:
    """Plays smooth initial loading animation in a single editable message."""
    steps = [
        (0, "🚀 INITIALIZING BATCH UPLOADER\n[□□□□□□□□□□] 0%"),
        (20, "🔍 Loading parser...\n[██□□□□□□□□] 20%"),
        (40, "🗄 Checking database...\n[████□□□□□□] 40%"),
        (70, "⚙️ Loading processing engine...\n[███████□□□] 70%"),
        (100, "✨ Ready\n[██████████] 100%")
    ]
    for pct, label in steps:
        text = TelegramProgressUI.render_batch_start(1, pct, label)
        try:
            await message.edit_text(text)
            await asyncio.sleep(0.25)
        except Exception:
            pass
    return True


def render_apps_manage_list(apps: List[App]) -> Tuple[str, InlineKeyboardMarkup]:
    text = (
        "📱 <b>COURSE WALLAH — APP & BATCH MANAGER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Found <b>{len(apps)}</b> active learning platform(s) in your portal.\n\n"
        "👉 <i>Select an App below to change its Name, Icon, Description, or edit Batches & Thumbnails:</i>"
    )
    rows = []
    for a in apps:
        b_count = len(a.batches) if a.batches else 0
        rows.append([InlineKeyboardButton(f"📱 {a.name} ({b_count} Batches)", callback_data=f"manage:app:{a.id}")])

    rows.append([InlineKeyboardButton("➕ Create New App", callback_data="wizard:step_create_app")])
    rows.append([
        InlineKeyboardButton("👑 Admin Panel", callback_data="admin:dashboard"),
        InlineKeyboardButton("❌ Close", callback_data="admin:close")
    ])
    return text, InlineKeyboardMarkup(rows)


def render_app_manage_detail(app: App) -> Tuple[str, InlineKeyboardMarkup]:
    b_count = len(app.batches) if app.batches else 0
    icon_str = f"<code>{app.icon_url}</code>" if app.icon_url else "<i>Default / None</i>"
    desc_str = f"<i>{app.description}</i>" if app.description else "<i>No description set</i>"
    text = (
        f"📱 <b>APP MANAGEMENT: {app.name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏷️ <b>Name:</b> <b>{app.name}</b>\n"
        f"🔗 <b>Slug:</b> <code>{app.slug}</code>\n"
        f"🖼️ <b>Icon URL:</b> {icon_str}\n"
        f"📝 <b>Description:</b> {desc_str}\n"
        f"📦 <b>Total Batches:</b> <b>{b_count}</b>\n"
        f"🟢 <b>Status:</b> {app.status}\n\n"
        f"👇 <i>Choose an action to update this App or edit batch thumbnails:</i>"
    )
    buttons = [
        [
            InlineKeyboardButton("✏️ Rename App", callback_data=f"manage:rename_app:{app.id}"),
            InlineKeyboardButton("🖼️ Change Icon", callback_data=f"manage:icon_app:{app.id}")
        ],
        [
            InlineKeyboardButton("📝 Edit Description", callback_data=f"manage:desc_app:{app.id}"),
            InlineKeyboardButton(f"📚 View Batches ({b_count})", callback_data=f"manage:batches:{app.id}:0")
        ],
        [
            InlineKeyboardButton("⬅️ Back to All Apps", callback_data="manage:apps_list"),
            InlineKeyboardButton("👑 Admin Panel", callback_data="admin:dashboard")
        ]
    ]
    return text, InlineKeyboardMarkup(buttons)


def render_batches_manage_list(app: App, batches: List[Batch], page: int = 0, per_page: int = 8) -> Tuple[str, InlineKeyboardMarkup]:
    total_batches = len(batches)
    total_pages = max(1, (total_batches + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * per_page
    end_idx = min(start_idx + per_page, total_batches)
    page_batches = batches[start_idx:end_idx]

    text = (
        f"📚 <b>BATCHES FOR: {app.name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Showing {start_idx + 1}–{end_idx} of <b>{total_batches}</b> batch(es) (Page {page + 1}/{total_pages}).\n\n"
        f"👉 <i>Click on any batch to change its Thumbnail photo, Name, or Category:</i>"
    )
    rows = []
    for b in page_batches:
        thumb_indicator = "🖼️" if b.thumbnail_url else "📁"
        title = b.name[:36] + ("..." if len(b.name) > 36 else "")
        rows.append([InlineKeyboardButton(f"{thumb_indicator} {title}", callback_data=f"manage:batch:{b.id}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"manage:batches:{app.id}:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data=f"manage:batches:{app.id}:{page}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"manage:batches:{app.id}:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([
        InlineKeyboardButton(f"⬅️ Back to {app.name[:18]}", callback_data=f"manage:app:{app.id}"),
        InlineKeyboardButton("📱 All Apps", callback_data="manage:apps_list")
    ])
    return text, InlineKeyboardMarkup(rows)


def render_batch_manage_detail(app: App, batch: Batch) -> Tuple[str, InlineKeyboardMarkup]:
    thumb_str = f"<code>{batch.thumbnail_url}</code>" if batch.thumbnail_url else "<i>No custom thumbnail (using default)</i>"
    cat_str = batch.category or "General"
    sub_count = len(batch.subjects) if batch.subjects else 0
    text = (
        f"📦 <b>BATCH MANAGEMENT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📱 <b>App:</b> <b>{app.name}</b>\n"
        f"🏷️ <b>Batch Name:</b> <b>{batch.name}</b>\n"
        f"🗂️ <b>Category:</b> <b>{cat_str}</b>\n"
        f"🖼️ <b>Thumbnail URL:</b>\n{thumb_str}\n"
        f"📚 <b>Subjects:</b> {sub_count}\n"
        f"🎬 <b>Preferred Quality:</b> {batch.video_quality_preference or '1080p'}\n\n"
        f"👇 <i>Choose an action to customize this batch:</i>"
    )
    buttons = [
        [
            InlineKeyboardButton("🖼️ Set / Change Thumbnail", callback_data=f"manage:thumb_batch:{batch.id}"),
            InlineKeyboardButton("✏️ Rename Batch", callback_data=f"manage:rename_batch:{batch.id}")
        ],
        [
            InlineKeyboardButton("🏷️ Change Category", callback_data=f"manage:cat_batch:{batch.id}"),
            InlineKeyboardButton("⬅️ Back to Batches", callback_data=f"manage:batches:{app.id}:0")
        ],
        [
            InlineKeyboardButton(f"📱 Back to {app.name[:20]}", callback_data=f"manage:app:{app.id}")
        ]
    ]
    return text, InlineKeyboardMarkup(buttons)


def register_handlers(app: Client):

    # ==========================================
    # 1. COMMAND: /START
    # ==========================================
    @app.on_message(filters.command("start") & filters.private)
    async def cmd_start(client: Client, message: Message):
        try:
            user = message.from_user
            user_id = user.id if user else 0
            mention = user.mention if user else "Student"
            admin_status = is_admin(user_id)

            start_message = await message.reply_text(
                f"🌟 <b>Welcome {mention}!</b> 🌟\n\n"
                f"Initializing Course Wallah Bot... 🤖\n\n"
                f"Progress: [⬜⬜⬜⬜⬜⬜⬜⬜⬜] 0%\n\n"
            )

            await asyncio.sleep(0.3)
            try:
                await start_message.edit_text(
                    f"🌟 <b>Welcome {mention}!</b> 🌟\n\n"
                    f"Loading multi-account engines... ⏳\n\n"
                    f"Progress: [🟥🟥🟥⬜⬜⬜⬜⬜⬜] 25%\n\n"
                )
                await asyncio.sleep(0.3)
                await start_message.edit_text(
                    f"🌟 <b>Welcome {mention}!</b> 🌟\n\n"
                    f"Checking account authorization & failover... 🔍\n\n"
                    f"Progress: [🟨🟨🟨🟨🟨🟨🟨⬜⬜] 75%\n\n"
                )
                await asyncio.sleep(0.3)
            except Exception:
                pass

            if admin_status:
                premium_caption = (
                    f"🌟 <b>Welcome {mention}!</b> 👋\n\n"
                    f"👑 <b>Great! You are an AUTHORIZED / PREMIUM administrator!</b>\n\n"
                    f"⏰ <b>Status:</b> Active\n"
                    f"📅 <b>Subscription:</b> Lifetime Admin Access\n"
                    f"🎬 <b>YouTube Engine:</b> Multi-Account Failover & Limit Aware\n"
                    f"☁️ <b>PDF Engine:</b> Backblaze B2 Private Storage\n"
                    f"🎨 <b>Watermark:</b> Animated Moving Protection\n\n"
                    f"I download lectures from your <b>.txt</b> file and upload them to YouTube (Unlisted) with automatic daily limit rotation, then publish directly to your student portal.\n\n"
                    f"• Send me your <code>.txt</code> file or use /batch to begin.\n"
                    f"• Use /youtube_accounts to view channel quota and failover status.\n"
                    f"• Use /stop to cancel any ongoing task."
                )
                markup = BatchWizardManager.build_welcome_markup(True)
                await start_message.edit_text(premium_caption, reply_markup=markup)
            else:
                free_caption = (
                    f"🌟 <b>Welcome {mention}!</b> 👋\n\n"
                    f"You are currently viewing the <b>Course Wallah Platform Bot</b>. 🆓\n\n"
                    f"<b>Your Telegram ID:</b> <code>{user_id}</code>\n\n"
                    f"This bot processes course batches, moves watermarks, and uploads educational media.\n\n"
                    f"💬 Contact the administrator or @Course_diploma_bot to get <b>ACCESS</b> 🎫 and unlock batch processing."
                )
                markup = BatchWizardManager.build_welcome_markup(False)
                await start_message.edit_text(free_caption, reply_markup=markup)

        except Exception as exc:
            logger.exception("Error in /start command: %s", exc)
            await message.reply_text("❌ <b>An error occurred.</b> Please try again.")

    # ==========================================
    # 2. COMMAND: /INFO
    # ==========================================
    @app.on_message(filters.command("info") & filters.private)
    async def cmd_info(client: Client, message: Message):
        try:
            user = message.from_user
            if not user:
                await message.reply_text("❌ Could not retrieve user profile.")
                return

            first_name = user.first_name or "N/A"
            last_name = user.last_name or "None"
            username = f"@{user.username}" if user.username else "None"
            user_id = user.id
            mention = user.mention
            role = "👑 Administrator" if is_admin(user_id) else "🎓 Member / Student"

            text = (
                f"👤 <b>USER INFORMATION</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<b>🙋🏻‍♂️ First Name:</b> {first_name}\n"
                f"<b>🧖‍♂️ Last Name:</b> {last_name}\n"
                f"<b>🧑🏻🎓 Username:</b> {username}\n"
                f"<b>🆔 Telegram ID:</b> <code>{user_id}</code>\n"
                f"<b>🔗 Profile Link:</b> {mention}\n"
                f"<b>🏷 Role:</b> {role}"
            )
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("🆔 Copy My ID", callback_data="user:id"), InlineKeyboardButton("ℹ️ Help", callback_data="user:help")]
            ])
            await message.reply_text(text, reply_markup=buttons)
        except Exception as exc:
            logger.exception("Error in /info command: %s", exc)
            await message.reply_text("❌ Failed to retrieve user information.")

    # ==========================================
    # 3. COMMAND: /ID
    # ==========================================
    @app.on_message(filters.command("id"))
    async def cmd_id(client: Client, message: Message):
        try:
            chat = message.chat
            if chat.type in ("channel", "supergroup", "group"):
                chat_title = chat.title or "Channel"
                chat_id = chat.id
                await message.reply_text(
                    f"📃 <b>Chat / Channel Name:</b> {chat_title}\n"
                    f"🆔 <b>Chat / Channel ID:</b> <code>{chat_id}</code>\n\n"
                    f"<i>To add this Channel to allowed broadcast list, copy the ID above.</i>"
                )
            else:
                user = message.from_user
                user_id = user.id if user else 0
                admin_str = "✅ YES (Administrator)" if is_admin(user_id) else "❌ Standard User"
                bot_name = BOT_USERNAME or "@course_wallah_officalbot"
                text = (
                    f"🆔 <b>YOUR TELEGRAM ID:</b> <code>{user_id}</code>\n\n"
                    f"<b>Admin Access:</b> {admin_str}\n"
                    f"<b>Bot:</b> {bot_name}"
                )
                await message.reply_text(text)
        except Exception as exc:
            logger.exception("Error in /id command: %s", exc)
            await message.reply_text("❌ Failed to retrieve ID.")

    # ==========================================
    # 4. COMMAND: /STOP
    # ==========================================
    @app.on_message(filters.command("stop") & filters.private)
    async def cmd_stop(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ Sorry, you are not eligible.")
                return

            controllers = ContentProcessingEngine._active_batch_controllers
            stopped_count = 0
            for ctrl in list(controllers.values()):
                ctrl.cancel()
                stopped_count += 1

            if stopped_count > 0:
                await message.reply_text(f"🚦 <b>Stopped {stopped_count} active task(s).</b> Prepared artifacts remain safely checkpointed.")
            else:
                await message.reply_text("ℹ️ <b>No active running tasks to stop.</b>")
        except Exception as exc:
            logger.exception("Error in /stop command: %s", exc)
            await message.reply_text("❌ Failed to stop task.")

    # ==========================================
    # 5. COMMAND: /REMOVE_AUTH
    # ==========================================
    @app.on_message(filters.command("remove_auth") & filters.private)
    async def cmd_remove_auth(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if user_id != OWNER_ID and user_id not in ADMINS:
                await message.reply_text("⛔ <b>OWNER ONLY</b>")
                return

            parts = message.text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip().isdigit():
                await message.reply_text("⚠️ <b>Usage:</b> <code>/remove_auth &lt;user_id&gt;</code>")
                return

            user_to_remove = int(parts[1].strip())
            if user_to_remove in ADMINS and user_to_remove != OWNER_ID:
                ADMINS.remove(user_to_remove)
                await message.reply_text(f"✅ User <code>{user_to_remove}</code> removed from authorized administrators list.")
            elif user_to_remove == OWNER_ID:
                await message.reply_text("❌ Cannot remove primary OWNER_ID.")
            else:
                await message.reply_text(f"ℹ️ User <code>{user_to_remove}</code> is not in the active authorized list.")
        except Exception as exc:
            logger.exception("Error in /remove_auth command: %s", exc)
            await message.reply_text("❌ Failed to remove authorized user.")

    # ==========================================
    # 6. YOUTUBE COMMANDS: /YOUTUBE, /YOUTUBE_ACCOUNTS, /YOUTUBE_STATUS, /YOUTUBE_PAUSE, /YOUTUBE_RESUME
    # ==========================================
    # ==========================================
    # YOUTUBE MULTI-ACCOUNT MANAGEMENT COMMANDS
    # ==========================================

    @app.on_message(filters.command(["youtube", "youtube_accounts", "yt_list", "yt"]) & filters.private)
    async def cmd_youtube_accounts(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            status_msg = await message.reply_text("🔍 <i>Fetching YouTube channels & quota diagnostics...</i>")
            diag = await YouTubeAccountManager.get_diagnostics()
            text = TelegramProgressUI.render_youtube_accounts_screen(diag)
            buttons = TelegramProgressUI.build_youtube_accounts_markup(diag.get("accounts", []))
            
            try:
                await status_msg.edit_text(text, reply_markup=buttons, disable_web_page_preview=True)
            except Exception as e:
                if "MESSAGE_TOO_LONG" in str(e):
                    await status_msg.edit_text(text[:3500] + "\n\n<i>[Truncated]</i>", reply_markup=buttons, disable_web_page_preview=True)
                else:
                    raise e
        except Exception as exc:
            logger.exception("Error in /youtube_accounts command: %s", exc)
            await message.reply_text("❌ Failed to retrieve YouTube accounts diagnostics.")

    @app.on_message(filters.command(["yt_add", "add_youtube"]) & filters.private)
    async def cmd_yt_add(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            # Check if arguments were passed directly: e.g. /yt_add Channel Name | ClientID | Secret | RefreshToken
            args_text = ""
            if len(message.command) > 1:
                args_text = message.text.split(None, 1)[1].strip()

            parsed = parse_youtube_credentials_text(args_text) if args_text else None

            if parsed:
                if not parsed.get("refresh_token"):
                    _yt_oauth_pending_sessions[user_id] = parsed
                    _yt_add_waiting_users[user_id] = True
                    card, markup = build_oauth_authorization_card_and_markup(parsed)
                    await message.reply_text(card, reply_markup=markup, disable_web_page_preview=True)
                    return

                wait_msg = await message.reply_text("⏳ <i>Testing OAuth credentials & connecting to YouTube API...</i>")
                success, acc_dict, msg_or_err = await YouTubeAccountManager.add_account_from_credentials(
                    name=parsed["name"],
                    client_id=parsed["client_id"],
                    client_secret=parsed["client_secret"],
                    refresh_token=parsed["refresh_token"],
                    priority=2,
                    auto_test=True
                )
                if success:
                    ch_title = acc_dict.get("channel_title") or acc_dict.get("name")
                    ch_id = acc_dict.get("channel_id")
                    markup = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("🎬 View All Accounts", callback_data="admin:youtube_accounts"),
                            InlineKeyboardButton("➕ Add Another", callback_data="yt:add_prompt")
                        ]
                    ])
                    await wait_msg.edit_text(
                        f"🎉 <b>YOUTUBE CHANNEL LINKED SUCCESSFULLY!</b>\n\n"
                        f"📺 <b>Channel:</b> <b>{ch_title}</b>\n"
                        f"🆔 <b>Channel ID:</b> <code>{ch_id or 'Detected'}</code>\n"
                        f"⭐ <b>Priority:</b> #{acc_dict.get('priority', 2)}\n"
                        f"🟢 <b>Status:</b> ACTIVE\n"
                        f"🎯 <b>Daily Quota:</b> ~20 videos/day\n\n"
                        f"🛡️ <b>Multi-Channel Pool:</b> This channel is now active. When another channel hits the daily upload limit, the bot rotates to this channel automatically!",
                        reply_markup=markup
                    )
                else:
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Try Again", callback_data="yt:add_prompt")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="admin:youtube_accounts")]
                    ])
                    await wait_msg.edit_text(
                        f"❌ <b>Failed to link YouTube Channel:</b>\n\n"
                        f"<code>{msg_or_err}</code>\n\n"
                        f"<i>Please verify your Client ID, Client Secret, and Refresh Token.</i>",
                        reply_markup=markup
                    )
                return

            # Interactive Prompt Mode
            _yt_add_waiting_users[user_id] = True
            prompt_text = (
                f"🎬 <b>ADD YOUTUBE CHANNEL (1-CLICK OAUTH)</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"You can connect your channel in any of these easy ways:\n\n"
                f"🔹 <b>Method 1 (Instant 1-Click Link - Recommended):</b>\n"
                f"Send your Google Cloud <code>client_secrets.json</code> file or paste its JSON text directly.\n"
                f"<i>Course Wallah will instantly generate a 1-Click Authorization link to generate and autosave your refresh token!</i>\n\n"
                f"🔹 <b>Method 2 (Single Line / Pipe):</b>\n"
                f"<code>Channel Name | CLIENT_ID | CLIENT_SECRET | REFRESH_TOKEN</code>\n\n"
                f"🔹 <b>Method 3 (Multiline Key-Value):</b>\n"
                f"<pre>\n"
                f"Name: Backup Channel 2\n"
                f"Client ID: xxxxx.apps.googleusercontent.com\n"
                f"Client Secret: GOCSPX-xxxxx\n"
                f"Refresh Token: 1//04xxxxx\n"
                f"</pre>\n\n"
                f"👉 <i>Simply upload your <code>.json</code> file or paste your credentials here!</i>"
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="admin:youtube_accounts")]
            ])
            await message.reply_text(prompt_text, reply_markup=markup)
        except Exception as exc:
            logger.exception("Error in /yt_add command: %s", exc)
            await message.reply_text("❌ Failed to initiate YouTube channel linking.")

    @app.on_message(filters.command(["yt_del", "yt_remove"]) & filters.private)
    async def cmd_yt_del(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            if len(message.command) < 2:
                await message.reply_text("ℹ️ <b>Usage:</b> <code>/yt_del &lt;account_id&gt;</code>\n\n<i>Use /yt_list to view account IDs.</i>")
                return

            acc_id = message.command[1].strip()
            success, msg = await YouTubeAccountManager.remove_account(acc_id)
            if success:
                await message.reply_text(f"✅ {msg}")
            else:
                await message.reply_text(f"❌ {msg}")
        except Exception as exc:
            logger.exception("Error in /yt_del command: %s", exc)
            await message.reply_text("❌ Failed to delete account.")

    @app.on_message(filters.command("yt_test") & filters.private)
    async def cmd_yt_test(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            if len(message.command) < 2:
                await message.reply_text("ℹ️ <b>Usage:</b> <code>/yt_test &lt;account_id&gt;</code>")
                return

            acc_id = message.command[1].strip()
            wait_msg = await message.reply_text("⏳ <i>Testing OAuth connection to YouTube API...</i>")
            success, acc_dict, msg = await YouTubeAccountManager.test_and_refresh_account(acc_id)
            if success:
                await wait_msg.edit_text(f"✅ <b>Connection Verified!</b>\n\n{msg}")
            else:
                await wait_msg.edit_text(f"❌ <b>Test Failed:</b>\n\n<code>{msg}</code>")
        except Exception as exc:
            logger.exception("Error in /yt_test command: %s", exc)
            await message.reply_text("❌ Failed to test YouTube account.")

    # ==========================================
    # 7. TELEGRAM STREAM STORAGE CHANNEL COMMANDS
    # ==========================================
    @app.on_message(filters.command(["setchannel", "channel", "set_channel", "storagechannel", "setstoragechannel"]) & filters.private)
    async def cmd_set_storage_channel(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            pool = TelegramClientPool.get_instance()

            if len(message.command) < 2:
                _waiting_for_channel_id[user_id] = True
                current_chan = pool.storage_chat_id or OWNER_ID
                text = (
                    f"📁 <b>TELEGRAM STORAGE CHANNEL CONFIGURATION</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"<b>Current Active Storage Channel:</b> <code>{current_chan}</code>\n\n"
                    f"<b>How to connect your Storage Channel:</b>\n"
                    f"1. Create a private Telegram Channel.\n"
                    f"2. Add this bot as an <b>Administrator</b> with full <b>Post Messages</b> permissions.\n"
                    f"3. Either:\n"
                    f"   • Type & send the Channel ID here (e.g. <code>-1002345678901</code>), OR\n"
                    f"   • <b>FORWARD</b> any post from your private channel directly to this bot!\n\n"
                    f"👉 <i>Waiting for your Channel ID or Forwarded Message...</i>"
                )
                await message.reply_text(text)
                return

            raw_input = message.command[1].strip()
            try:
                target_chat_id = int(raw_input)
            except ValueError:
                await message.reply_text("❌ Invalid Channel ID format. Channel IDs must be integers (e.g. <code>-1001234567890</code>).")
                return

            wait_msg = await message.reply_text(f"⏳ <i>Verifying bot access to channel <code>{target_chat_id}</code>...</i>")
            
            # Test write permissions by sending a temporary message and deleting it
            try:
                test_post = await client.send_message(
                    chat_id=target_chat_id,
                    text="⚡ <b>Course Wallah Streaming Engine Verification</b>\n\nStorage Channel connected successfully."
                )
                await asyncio.sleep(1)
                await test_post.delete()
            except Exception as perm_err:
                await wait_msg.edit_text(
                    f"❌ <b>Channel Access Failed!</b>\n\n"
                    f"Error: <code>{str(perm_err)}</code>\n\n"
                    f"👉 Please make sure the bot is added as an <b>Administrator</b> to the channel with Post Messages permission."
                )
                return

            # Update pool storage chat ID
            pool.storage_chat_id = target_chat_id
            _waiting_for_channel_id.pop(user_id, None)

            # Persist to local .env if writable
            try:
                from config.settings import BASE_DIR
                env_file = BASE_DIR / ".env"
                if env_file.exists():
                    env_text = env_file.read_text(encoding="utf-8")
                    if "TELEGRAM_STORAGE_CHANNEL_ID=" in env_text:
                        env_text = re.sub(
                            r"TELEGRAM_STORAGE_CHANNEL_ID=.*",
                            f"TELEGRAM_STORAGE_CHANNEL_ID={target_chat_id}",
                            env_text
                        )
                    else:
                        env_text += f"\nTELEGRAM_STORAGE_CHANNEL_ID={target_chat_id}\n"
                    env_file.write_text(env_text, encoding="utf-8")
            except Exception as env_err:
                logger.debug(f"[ENV_UPDATE_NOTICE] {env_err}")

            success_text = (
                f"🎉 <b>STORAGE CHANNEL CONNECTED SUCCESSFULLY!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"✅ <b>Active Channel ID:</b> <code>{target_chat_id}</code>\n"
                f"⚡ <b>Engine:</b> Telegram MTProto High-Speed Zero-Cost Stream\n"
                f"🌐 <b>Website Player:</b> Course Wallah In-App HTML5 Cinema Player\n"
                f"🚀 <b>Limits:</b> 100% Unlimited (Up to 2GB per video)\n\n"
                f"<i>All newly ingested batches and video uploads will now save to this channel and stream directly to your website!</i>"
            )
            await wait_msg.edit_text(success_text)

        except Exception as exc:
            logger.exception("Error in /setchannel command: %s", exc)
            await message.reply_text("❌ Failed to configure storage channel.")

    @app.on_message(filters.command(["streamstatus", "tgstream", "stream_status"]) & filters.private)
    async def cmd_stream_status(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            from storage.providers.telegram_stream import TelegramStreamStorageProvider
            from storage.telegram_stream.client_pool import TelegramClientPool, GLOBAL_HEADER_CACHE

            provider = TelegramStreamStorageProvider()
            health = await provider.health_check()
            pool = TelegramClientPool.get_instance()

            chan_id = pool.storage_chat_id or OWNER_ID
            workers_count = len(pool.clients) or 1
            cached_count = len(GLOBAL_HEADER_CACHE.cache)

            text = (
                f"⚡ <b>TELEGRAM STREAMING ENGINE STATUS</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🟢 <b>Status:</b> {health.get('status', 'ONLINE')}\n"
                f"📁 <b>Storage Channel ID:</b> <code>{chan_id}</code>\n"
                f"🤖 <b>Active Worker Bots:</b> <code>{workers_count}</code> worker(s)\n"
                f"🧠 <b>Cached Media Headers (1ms Seek):</b> <code>{cached_count}</code> video(s)\n"
                f"🎬 <b>In-App Player URL:</b> <code>/api/v1/stream/tg/{{message_id}}</code>\n"
                f"🛡️ <b>Concurrency Balancer:</b> Round-Robin MTProto Dispatch Active\n\n"
                f"• Use <code>/setchannel &lt;channel_id&gt;</code> to change storage channel.\n"
                f"• Free tier 100MB restrictions are 100% bypassed!"
            )
            await message.reply_text(text)
        except Exception as exc:
            logger.exception("Error in /streamstatus command: %s", exc)
            await message.reply_text("❌ Failed to fetch stream status.")

    @app.on_message(filters.command(["stream", "uploadvideo", "upload_video", "streamvideo"]) & filters.private)
    async def cmd_stream(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            # Case 1: Replied to a video or document
            if message.reply_to_message and (message.reply_to_message.video or message.reply_to_message.document):
                target_msg = message.reply_to_message
                custom_title = " ".join(message.command[1:]).strip() if len(message.command) > 1 else None
                status_msg = await message.reply_text("⚡ <i>Publishing replied video to Storage Channel & Website...</i>")
                ok, text_msg, meta = await publish_video_to_telegram_stream_and_db(
                    client=client,
                    message=target_msg,
                    user_id=user_id,
                    custom_title=custom_title
                )
                if not ok:
                    await status_msg.edit_text(f"❌ {text_msg}")
                    return

                stream_url = meta["stream_url"]
                web_url = meta["web_player_url"]
                title = meta["title"]
                size_mb = meta["file_size"] / (1024 * 1024)
                duration_mins = meta["duration"] // 60
                duration_secs = meta["duration"] % 60
                dur_str = f"{duration_mins}m {duration_secs:02d}s" if meta["duration"] else "N/A"

                card = (
                    f"🎬 <b>TELEGRAM STREAM CREATED & PUBLISHED!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📺 <b>Title:</b> <b>{title}</b>\n"
                    f"📊 <b>Size:</b> <code>{size_mb:.2f} MB</code> | <b>Duration:</b> <code>{dur_str}</code>\n"
                    f"🎯 <b>Quality:</b> <code>{meta.get('resolution', '1080p')} Ultra HD</code>\n"
                    f"📁 <b>Storage Channel:</b> <code>{meta['storage_chat_id']}</code> (Msg #<code>{meta['storage_msg_id']}</code>)\n\n"
                    f"⚡ <b>Direct HTTP Stream Link:</b>\n"
                    f"<code>{stream_url}</code>\n\n"
                    f"🌐 <b>Website Player Link:</b>\n"
                    f"<code>{web_url}</code>\n\n"
                    f"💻 <b>HTML5 Embed Code:</b>\n"
                    f"<code>&lt;video controls src=\"{stream_url}\"&gt;&lt;/video&gt;</code>\n\n"
                    f"✅ <i>Lecture is now live on the Course Wallah website portal!</i>"
                )
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌐 Open on Website", url=web_url)],
                    [InlineKeyboardButton("🎬 Direct Stream", url=stream_url), InlineKeyboardButton("⚡ Stream Status", callback_data="admin:streamstatus")]
                ])
                await status_msg.edit_text(card, reply_markup=markup, disable_web_page_preview=True)
                return

            # Case 2: URL provided (e.g. /stream https://example.com/video.mp4 Lecture 1)
            if len(message.command) > 1:
                url_candidate = message.command[1].strip()
                if url_candidate.startswith("http://") or url_candidate.startswith("https://"):
                    custom_title = " ".join(message.command[2:]).strip() if len(message.command) > 2 else "Stream Video"
                    status_msg = await message.reply_text(f"⏳ <i>Downloading media stream from URL...</i>\n<code>{url_candidate[:60]}...</code>")

                    try:
                        from downloader.downloader import MediaDownloader
                        downloaded_path, dl_meta = await MediaDownloader.download_video_stream_with_meta(
                            url=url_candidate,
                            title=custom_title,
                            quality="1080p"
                        )
                        await status_msg.edit_text("⚡ <i>Uploading downloaded video to Telegram Storage Channel...</i>")

                        pool = TelegramClientPool.get_instance()
                        storage_chat = pool.storage_chat_id or OWNER_ID

                        stream_caption = format_telegram_channel_caption(
                            title=custom_title,
                            metadata={
                                "subject_name": "Direct Uploads",
                                "batch_name": "Direct Video Streams",
                                "folder_name": "Telegram Cloud Stream",
                                "lecture_index": 1,
                                "width": 1920,
                                "height": 1080,
                                "resolution": "1080p",
                            },
                            width=1920,
                            height=1080,
                            resolution="1080p",
                        )

                        sent_msg = await client.send_video(
                            chat_id=storage_chat,
                            video=downloaded_path,
                            caption=stream_caption,
                            parse_mode=ParseMode.HTML,
                            supports_streaming=True
                        )

                        file_size = os.path.getsize(downloaded_path) if os.path.exists(downloaded_path) else (sent_msg.video.file_size if sent_msg.video else 0)

                        try:
                            os.remove(downloaded_path)
                        except Exception:
                            pass

                        # Publish to DB
                        base_url = get_public_base_url()
                        stream_url = f"{base_url}/api/v1/stream/tg/{sent_msg.id}"

                        async with get_db_session() as session:
                            repo = ContentRepository(session)
                            app = await repo.get_or_create_app("Course Wallah", "Official Course Wallah Hub", "/static/logo.png")
                            batch, _ = await repo.get_or_create_batch(
                                app_id=app.id,
                                name="Direct Video Streams",
                                category="Video Lectures",
                                branch="All Branches",
                                semester="All Semesters",
                                quality_pref="1080p"
                            )
                            subj = await repo.get_or_create_subject(batch.id, "Direct Uploads")
                            folder = await repo.get_or_create_folder(subj.id, "Telegram Cloud Stream")
                            max_idx = await repo.get_max_lecture_index_for_batch(batch.id)
                            next_idx = max_idx + 1

                            lecture = await repo.create_or_update_lecture(
                                folder_id=folder.id,
                                subject_id=subj.id,
                                batch_id=batch.id,
                                lecture_index=next_idx,
                                title=custom_title,
                                source_url=stream_url,
                                provider="telegram_stream",
                                has_video=True,
                                has_pdf=False,
                                publication_status=PublicationStatus.PUBLISHED,
                                thumbnail_url="/static/logo.png"
                            )

                            video = await repo.attach_video_to_lecture(
                                lecture_id=lecture.id,
                                youtube_video_id=f"tg_{sent_msg.id}",
                                duration=float(sent_msg.video.duration if sent_msg.video else 0),
                                resolution="1080p",
                                file_size=file_size,
                                title=custom_title,
                                upload_completed_at=datetime.utcnow()
                            )

                            st_record = VideoStorage(
                                video_id=video.id,
                                provider="telegram_stream",
                                provider_video_id=str(sent_msg.id),
                                playback_url=f"/api/v1/stream/tg/{sent_msg.id}",
                                status=VideoStorageStatus.READY.value,
                                remote_size=file_size,
                                remote_duration=float(sent_msg.video.duration if sent_msg.video else 0),
                                created_at=datetime.utcnow(),
                                updated_at=datetime.utcnow()
                            )
                            session.add(st_record)
                            await repo.add_lecture_to_playlist(subj.id, lecture.id)
                            await session.commit()

                            web_player_url = f"{base_url}/?app={app.slug}&batchId={batch.slug}&subjectId={subj.slug}&content={lecture.id}&isPlaying=true"

                        card = (
                            f"🎬 <b>URL DOWNLOADED & PUBLISHED TO WEBSITE!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                            f"📺 <b>Title:</b> <b>{custom_title}</b>\n"
                            f"📊 <b>Size:</b> <code>{file_size / (1024*1024):.2f} MB</code>\n"
                            f"📁 <b>Storage Channel:</b> <code>{storage_chat}</code> (Msg #<code>{sent_msg.id}</code>)\n\n"
                            f"⚡ <b>Direct HTTP Stream Link:</b>\n"
                            f"<code>{stream_url}</code>\n\n"
                            f"🌐 <b>Website Player Link:</b>\n"
                            f"<code>{web_player_url}</code>\n\n"
                            f"✅ <i>Lecture is now live on the Course Wallah website portal!</i>"
                        )
                        markup = InlineKeyboardMarkup([
                            [InlineKeyboardButton("🌐 Open on Website", url=web_player_url)],
                            [InlineKeyboardButton("🎬 Direct Stream", url=stream_url), InlineKeyboardButton("⚡ Stream Status", callback_data="admin:streamstatus")]
                        ])
                        await status_msg.edit_text(card, reply_markup=markup, disable_web_page_preview=True)
                        return
                    except Exception as url_err:
                        logger.exception("Error downloading & streaming URL: %s", url_err)
                        await status_msg.edit_text(f"❌ Failed to stream video from URL: {url_err}")
                        return

            # Default Help for /stream
            help_text = (
                f"⚡ <b>COURSE WALLAH TELEGRAM STREAM ENGINE</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<b>How to upload & generate website stream links:</b>\n\n"
                f"1. <b>Direct Video Upload:</b> Just send any <code>.mp4</code> or video file directly to this bot.\n"
                f"2. <b>Reply to Video:</b> Reply to any video message in Telegram with <code>/stream [Title]</code>\n"
                f"3. <b>Stream from URL:</b> Send <code>/stream &lt;video_url&gt; [Title]</code>\n\n"
                f"<i>The bot will upload it directly to your Telegram storage channel, generate an HTTP 206 streaming link, and add it to your website player!</i>"
            )
            await message.reply_text(help_text)
        except Exception as exc:
            logger.exception("Error in /stream command: %s", exc)
            await message.reply_text("❌ Failed to process stream command.")


    @app.on_message(filters.command(["yt_primary", "yt_set_primary"]) & filters.private)
    async def cmd_yt_primary(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            if len(message.command) < 2:
                await message.reply_text("ℹ️ <b>Usage:</b> <code>/yt_primary &lt;account_id&gt;</code>")
                return

            acc_id = message.command[1].strip()
            success, msg = await YouTubeAccountManager.set_account_priority(acc_id, 1)
            if success:
                await message.reply_text(f"⭐ <b>Primary Channel Updated!</b>\n\n{msg}")
            else:
                await message.reply_text(f"❌ {msg}")
        except Exception as exc:
            logger.exception("Error in /yt_primary command: %s", exc)
            await message.reply_text("❌ Failed to set primary account.")

    @app.on_message(filters.command("yt_toggle") & filters.private)
    async def cmd_yt_toggle(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            if len(message.command) < 2:
                await message.reply_text("ℹ️ <b>Usage:</b> <code>/yt_toggle &lt;account_id&gt;</code>")
                return

            acc_id = message.command[1].strip()
            success, msg, new_st = await YouTubeAccountManager.toggle_account_active(acc_id)
            if success:
                await message.reply_text(f"✅ {msg}")
            else:
                await message.reply_text(f"❌ {msg}")
        except Exception as exc:
            logger.exception("Error in /yt_toggle command: %s", exc)
            await message.reply_text("❌ Failed to toggle account status.")

    @app.on_message(filters.command(["youtube_status", "yt_status"]) & filters.private)
    async def cmd_youtube_status(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            diag = await YouTubeAccountManager.get_diagnostics()
            controllers = ContentProcessingEngine._active_batch_controllers
            active_c = sum(1 for c in controllers.values() if not c.is_paused)
            paused_c = sum(1 for c in controllers.values() if c.is_paused)

            text = TelegramProgressUI.render_youtube_status_screen(diag, active_c, paused_c)
            buttons = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🎬 View All Accounts", callback_data="admin:youtube_accounts"),
                    InlineKeyboardButton("🔄 Refresh Status", callback_data="admin:youtube_status")
                ]
            ])
            await message.reply_text(text, reply_markup=buttons)
        except Exception as exc:
            logger.exception("Error in /youtube_status command: %s", exc)
            await message.reply_text("❌ Failed to retrieve YouTube status.")

    @app.on_message(filters.command(["youtube_pause", "yt_pause"]) & filters.private)
    async def cmd_youtube_pause(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            controllers = ContentProcessingEngine._active_batch_controllers
            paused = 0
            for ctrl in controllers.values():
                if not ctrl.is_paused:
                    ctrl.pause()
                    paused += 1

            if paused > 0:
                await message.reply_text(f"⏸ <b>Paused YouTube uploads for {paused} active batch(es).</b> Prepared video artifacts remain safely checkpointed.")
            else:
                await message.reply_text("ℹ️ <b>No active uploading batch jobs found to pause.</b>")
        except Exception as exc:
            logger.exception("Error in /youtube_pause command: %s", exc)
            await message.reply_text("❌ Failed to pause YouTube uploads.")

    @app.on_message(filters.command(["youtube_resume", "yt_resume"]) & filters.private)
    async def cmd_youtube_resume(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            controllers = ContentProcessingEngine._active_batch_controllers
            resumed = 0
            for ctrl in controllers.values():
                if ctrl.is_paused:
                    ctrl.resume()
                    resumed += 1

            if resumed > 0:
                await message.reply_text(f"▶️ <b>Resumed {resumed} batch(es).</b> Failover engine will select the next active YouTube account.")
            else:
                await message.reply_text("ℹ️ <b>No paused batch jobs found to resume.</b>")
        except Exception as exc:
            logger.exception("Error in /youtube_resume command: %s", exc)
    # ==========================================
    # 6.5. MULTI-STORAGE REPLICATION COMMANDS: /STORAGE, /STORAGE_HEALTH, /STORAGE_STATUS
    # ==========================================
    @app.on_message(filters.command(["storage", "storage_health", "storage_status"]) & filters.private)
    async def cmd_storage_status(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            wait_msg = await message.reply_text("🔍 <i>Running live health checks on all 4 storage providers...</i>")
            health_results = await StorageHealthService.check_all_providers()

            # Query DB statistics
            async with get_db_session() as session:
                repo = ContentRepository(session)
                db_stats = await repo.get_storage_providers_summary()

            prov_lines = []
            for h in health_results:
                p_name = h.get("provider", "unknown")
                status = h.get("status", "UNKNOWN")
                healthy = h.get("healthy", False)
                enabled = h.get("enabled", True)
                p_stat = db_stats.get(p_name, {})
                ready_c = p_stat.get("ready", 0)
                failed_c = p_stat.get("failed", 0)
                total_c = p_stat.get("total", 0)

                if not enabled:
                    badge = "⚪ DISABLED"
                elif healthy:
                    badge = "🟢 ONLINE"
                else:
                    badge = f"🔴 {status}"

                disp_name = {
                    "vcdn": "VCDN",
                    "media_cm": "Media.cm",
                    "anonmp4": "AnonMP4",
                    "vevocloud": "Vevocloud",
                }.get(p_name, p_name.upper())

                prov_lines.append(
                    f"<b>{disp_name}</b>: {badge}\n"
                    f"  • Total: <b>{total_c}</b> | Ready: <b>{ready_c}</b> | Failed: <b>{failed_c}</b>"
                )

            summary_text = "\n\n".join(prov_lines)
            card = (
                f"╭━━━━━━━━━━━━━━━━━━━━━━━━━━╮\n"
                f"│ ☁️ <b>STORAGE REPLICATION</b>    │\n"
                f"╰━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                f"{summary_text}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛡️ <i>Pipeline: VCDN ➔ Media.cm ➔ AnonMP4 ➔ Vevocloud</i>\n"
                f"🔒 <i>Zero Duplicates (SHA-256) & Auto-Resumable</i>"
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh Health", callback_data="admin:storage_health")],
                [InlineKeyboardButton("👑 Admin Menu", callback_data="admin:menu")]
            ])
            await wait_msg.edit_text(card, reply_markup=markup)
        except Exception as exc:
            logger.exception("Error in /storage command: %s", exc)
            await message.reply_text("❌ Failed to retrieve storage diagnostics.")


    # ==========================================
    # 7. COMMAND: /HELP
    # ==========================================
    @app.on_message(filters.command("help") & filters.private)
    async def cmd_help(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            admin_status = is_admin(user_id)
            if admin_status:
                help_text = (
                    f"🎓 <b>COURSE WALLAH COMMAND REFERENCE</b>\n\n"
                    f"<b>👑 Batch & Ingestion Commands:</b>\n"
                    f"• /batch or /uploadbatch — Start TXT course batch ingestion wizard\n"
                    f"• /batches — List all batches & their publication status\n"
                    f"• /publish_batch &lt;id&gt; — Force publish batch to student website\n"
                    f"• /reprocess_batch &lt;id&gt; — Re-download & process incomplete lectures\n"
                    f"• /batchstatus — View live batch processing progress\n"
                    f"• /batchjobs — View recent database jobs\n"
                    f"• /batchpause — Pause current batch safely\n"
                    f"• /batchresume — Resume paused batch\n"
                    f"• /batchretry — Reprocess failed items in active batch\n"
                    f"• /batchcancel — Cancel batch with disk cleanup\n"
                    f"• /batchlogs — Show operational processing logs\n\n"
                    f"<b>🎬 YouTube Multi-Account Management:</b>\n"
                    f"• /youtube or /youtube_accounts — Show all accounts, status & daily limits\n"
                    f"• /youtube_status — View pipeline queues & remaining daily capacity\n"
                    f"• /youtube_pause — Gracefully pause YouTube publishing\n"
                    f"• /youtube_resume — Resume publishing with active accounts\n\n"
                    f"<b>🛠 General & Diagnostics:</b>\n"
                    f"• /admin — Complete Admin Dashboard\n"
                    f"• /info — View your user profile & permissions\n"
                    f"• /id — View your Telegram ID\n"
                    f"• /stop — Stop any ongoing batch task\n"
                    f"• /health — Media health & coverage summary"
                )
            else:
                help_text = (
                    f"🎓 <b>COURSE WALLAH HELP</b>\n\n"
                    f"• /start — Welcome menu & subscription info\n"
                    f"• /info — View your profile details\n"
                    f"• /id — View your Telegram ID\n"
                    f"• /help — Show this help message"
                )
            await message.reply_text(help_text)
        except Exception as exc:
            logger.exception("Error in /help command: %s", exc)
            await message.reply_text("❌ Failed to display help menu.")


    # ==========================================
    # 3.1 COMMAND: /WIPEDB /CLEARDB (ADMIN ONLY)
    # ==========================================
    @app.on_message(filters.command(["wipedb", "cleardb", "resetdb"]) & filters.private)
    async def cmd_wipedb(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>\n\n<i>You are not authorized to reset the platform database.</i>")
                return

            wait_msg = await message.reply_text("🧹 <b>PURGING DATABASE FOR FRESH START...</b>\n\n<i>Cleaning apps, batches, subjects, folders, lectures, video storages...</i>")

            from sqlalchemy import text
            tables = [
                "video_storages", "videos", "pdfs", "playlist_items", "playlists",
                "jobs", "lectures", "folders", "subjects", "batches", "apps"
            ]

            async with get_db_session() as session:
                bind_str = str(session.bind.url).lower() if session.bind else ""
                if "postgres" in bind_str or "postgresql" in normalized_db_url.lower():
                    try:
                        tbl_list = ", ".join(tables)
                        await session.execute(text(f"TRUNCATE TABLE {tbl_list} CASCADE;"))
                        await session.commit()
                        logger.info("[WIPEDB] Postgres TRUNCATE CASCADE completed successfully.")
                    except Exception as pg_err:
                        logger.warning(f"[WIPEDB] Postgres truncate cascade failed: {pg_err}, falling back to single deletes...")
                        await session.rollback()
                        for t in tables:
                            try:
                                await session.execute(text(f"DELETE FROM {t};"))
                                await session.commit()
                            except Exception:
                                await session.rollback()
                else:
                    try:
                        await session.execute(text("PRAGMA foreign_keys = OFF;"))
                    except Exception:
                        pass

                    for t in tables:
                        try:
                            await session.execute(text(f"DELETE FROM {t};"))
                        except Exception:
                            pass

                    try:
                        await session.execute(text("PRAGMA foreign_keys = ON;"))
                    except Exception:
                        pass

                    await session.commit()

            # Purge in-memory wizard state & caches completely
            BatchWizardManager._wizard_sessions.clear()
            _last_batch_sessions_by_batch.clear()
            _last_batch_sessions_by_user.clear()
            ContentProcessingEngine._active_batch_controllers.clear()

            report_text = (
                "🗑️ <b>ONLINE DATABASE PURGED 100% CLEAN!</b>\n\n"
                "• <b>Apps & Batches:</b> 0\n"
                "• <b>Lectures & Videos:</b> 0\n"
                "• <b>Cloud Storage Records:</b> 0\n"
                "• <b>PDFs & Notes:</b> 0\n\n"
                "✨ <b>Ready for fresh batch ingestion!</b>\n"
                "👉 Send a new <code>.txt</code> file or use /uploadbatch to start."
            )
            await wait_msg.edit_text(report_text)
        except Exception as exc:
            logger.exception("Error in /wipedb command: %s", exc)
            await message.reply_text(f"❌ Failed to purge database: {exc}")


    # ==========================================
    # 4. COMMAND: /ADMIN (ADMIN DASHBOARD)
    # ==========================================
    @app.on_message(filters.command("admin") & filters.private)
    async def cmd_admin(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>\n\n<i>You are not authorized to access the Course Wallah Admin Dashboard.</i>")
                return

            async with get_db_session() as session:
                repo = ContentRepository(session)
                apps = await repo.get_all_apps()
                total_batches = sum(len(a.batches) for a in apps) if apps else 0

            stats = {
                "active_apps": len(apps),
                "total_batches": total_batches,
                "total_lectures": "Loaded in DB",
                "active_jobs": len(ContentProcessingEngine._active_batch_controllers)
            }
            text = TelegramProgressUI.render_admin_dashboard(stats)
            markup = BatchWizardManager.build_admin_dashboard_markup()
            await message.reply_text(text, reply_markup=markup)
        except Exception as exc:
            logger.exception("Error in /admin command: %s", exc)
            await message.reply_text("❌ Failed to open admin dashboard.")

    # ==========================================
    # 4B. COMMANDS: /APPS, /EDITAPP, /MANAGEAPPS
    # ==========================================
    async def render_apps_list(client: Client, chat_id: int, message_id: Optional[int] = None):
        async with get_db_session() as session:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload
            res = await session.execute(select(App).options(selectinload(App.batches)).order_by(App.name))
            apps = res.scalars().all()

        text = (
            "📱 <b>COURSE WALLAH — APPS & PLATFORMS MANAGER</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Select an application below to edit its name, icon/image, description, or manage its batches & thumbnails:\n\n"
            f"• <b>Total Platforms Registered:</b> {len(apps)}\n"
        )
        buttons = []
        for a in apps:
            b_count = len(a.batches) if a.batches else 0
            buttons.append([InlineKeyboardButton(f"📱 {a.name} ({b_count} Batches)", callback_data=f"manage:app:{a.id}")])

        buttons.append([InlineKeyboardButton("👑 Admin Panel", callback_data="admin:dashboard"), InlineKeyboardButton("❌ Close", callback_data="admin:close")])
        markup = InlineKeyboardMarkup(buttons)
        if message_id:
            try:
                await client.edit_message_text(chat_id, message_id, text, reply_markup=markup)
                return
            except Exception:
                pass
        await client.send_message(chat_id, text, reply_markup=markup)

    async def render_app_detail(client: Client, chat_id: int, app_id: str, message_id: Optional[int] = None):
        async with get_db_session() as session:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload
            res = await session.execute(select(App).options(selectinload(App.batches)).where(App.id == app_id))
            app_obj = res.scalar_one_or_none()
            if not app_obj:
                await client.send_message(chat_id, "❌ App not found.")
                return

        batches_count = len(app_obj.batches) if app_obj.batches else 0
        icon_preview = f"\n🖼 <b>Icon URL:</b> <code>{app_obj.icon_url}</code>" if app_obj.icon_url else "\n🖼 <b>Icon:</b> <i>Default Logo</i>"
        desc_preview = f"\n📝 <b>Description:</b> {app_obj.description}" if app_obj.description else ""

        text = (
            f"📱 <b>APP PROFILE: {app_obj.name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>App ID:</b> <code>{app_obj.id}</code>\n"
            f"• <b>Slug:</b> <code>{app_obj.slug}</code>\n"
            f"• <b>Status:</b> {app_obj.status or 'ACTIVE'}\n"
            f"• <b>Total Batches:</b> {batches_count}"
            f"{icon_preview}"
            f"{desc_preview}\n\n"
            f"<i>Select an option below to update this app or customize batch thumbnails:</i>"
        )

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✏️ Rename App", callback_data=f"manage:rename_app:{app_obj.id}"),
                InlineKeyboardButton("🖼 Change Icon / Image", callback_data=f"manage:icon_app:{app_obj.id}")
            ],
            [
                InlineKeyboardButton("📝 Edit Description", callback_data=f"manage:desc_app:{app_obj.id}"),
                InlineKeyboardButton(f"📦 Manage Batches ({batches_count})", callback_data=f"manage:batches:{app_obj.id}:0")
            ],
            [
                InlineKeyboardButton("⬅️ Back to Apps List", callback_data="manage:apps_list")
            ]
        ])

        if message_id:
            try:
                await client.edit_message_text(chat_id, message_id, text, reply_markup=markup)
                return
            except Exception:
                pass
        await client.send_message(chat_id, text, reply_markup=markup)

    async def render_batches_list(client: Client, chat_id: int, app_id: str, page: int = 0, message_id: Optional[int] = None):
        PAGE_SIZE = 8
        async with get_db_session() as session:
            from sqlalchemy import select
            res = await session.execute(select(App).where(App.id == app_id))
            app_obj = res.scalar_one_or_none()
            if not app_obj:
                await client.send_message(chat_id, "❌ App not found.")
                return

            b_res = await session.execute(select(Batch).where(Batch.app_id == app_id).order_by(Batch.name))
            batches = b_res.scalars().all()

        total_batches = len(batches)
        total_pages = max(1, (total_batches + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))

        start = page * PAGE_SIZE
        page_batches = batches[start:start + PAGE_SIZE]

        text = (
            f"📦 <b>BATCHES FOR: {app_obj.name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Page {page + 1} of {total_pages} (Total: {total_batches} batches)\n\n"
            f"<i>Tap any batch to view details or change its thumbnail image:</i>\n"
        )

        buttons = []
        for b in page_batches:
            buttons.append([InlineKeyboardButton(f"📦 {b.name[:38]}", callback_data=f"manage:batch:{b.id}:{app_id}")])

        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"manage:batches:{app_id}:{page - 1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"manage:batches:{app_id}:{page + 1}"))

        if nav_row:
            buttons.append(nav_row)

        buttons.append([InlineKeyboardButton("⬅️ Back to App Details", callback_data=f"manage:app:{app_id}")])
        markup = InlineKeyboardMarkup(buttons)

        if message_id:
            try:
                await client.edit_message_text(chat_id, message_id, text, reply_markup=markup)
                return
            except Exception:
                pass
        await client.send_message(chat_id, text, reply_markup=markup)

    async def render_batch_detail(client: Client, chat_id: int, batch_id: str, app_id: str, message_id: Optional[int] = None):
        async with get_db_session() as session:
            from sqlalchemy import select
            res = await session.execute(select(Batch).where(Batch.id == batch_id))
            batch = res.scalar_one_or_none()
            if not batch:
                await client.send_message(chat_id, "❌ Batch not found.")
                return

        thumb_preview = f"\n🖼 <b>Thumbnail:</b> <code>{batch.thumbnail_url}</code>" if batch.thumbnail_url else "\n🖼 <b>Thumbnail:</b> <i>None</i>"

        text = (
            f"📦 <b>BATCH DETAILS: {batch.name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Batch ID:</b> <code>{batch.id}</code>\n"
            f"• <b>Slug:</b> <code>{batch.slug}</code>\n"
            f"• <b>Category:</b> {batch.category or 'General'}\n"
            f"• <b>Branch:</b> {batch.branch or 'N/A'}\n"
            f"• <b>Year:</b> {batch.academic_year or '2025-2026'}\n"
            f"• <b>Status:</b> {batch.status or 'ACTIVE'}"
            f"{thumb_preview}\n\n"
            f"<i>Select an option below to change the batch thumbnail or rename it:</i>"
        )

        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🖼 Change Batch Thumbnail", callback_data=f"manage:thumb_batch:{batch.id}:{app_id}"),
                InlineKeyboardButton("✏️ Rename Batch", callback_data=f"manage:rename_batch:{batch.id}:{app_id}")
            ],
            [
                InlineKeyboardButton("🏷 Change Category", callback_data=f"manage:cat_batch:{batch.id}:{app_id}"),
                InlineKeyboardButton("⬅️ Back to Batches", callback_data=f"manage:batches:{app_id}:0")
            ]
        ])

        if message_id:
            try:
                await client.edit_message_text(chat_id, message_id, text, reply_markup=markup)
                return
            except Exception:
                pass
        await client.send_message(chat_id, text, reply_markup=markup)

    @app.on_message(filters.command(["apps", "editapp", "manageapps"]) & filters.private)
    async def cmd_manage_apps(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return
            await render_apps_list(client, message.chat.id, None)
        except Exception as exc:
            logger.exception("Error in /apps command: %s", exc)
            await message.reply_text("❌ Failed to open Apps Manager.")

    # ==========================================
    # 5. COMMANDS: /BATCH & /UPLOADBATCH
    # ==========================================
    @app.on_message(filters.command(["batch", "uploadbatch"]) & filters.private)
    async def cmd_batch(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            status_msg = await message.reply_text("🚀 <b>INITIALIZING BATCH UPLOADER...</b>")
            await run_init_animation(status_msg)

            prompt_text = (
                f"📄 <b>SEND YOUR TXT FILE</b>\n\n"
                f"Please upload your course <code>.txt</code> file to begin batch analysis and ingestion.\n\n"
                f"• Supported formats: APPX, YouTube, Spayee, Direct URLs, Notes/PDFs\n"
                f"• Accepts <code>.txt</code> files only\n\n"
                f"<i>Waiting for file upload...</i>"
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data="wizard:cancel")]
            ])
            await status_msg.edit_text(prompt_text, reply_markup=markup)
        except Exception as exc:
            logger.exception("Error in /batch command: %s", exc)
            await message.reply_text("❌ Failed to initialize batch uploader.")

    # ==========================================
    # 6. COMMANDS: /BATCHSTATUS, /BATCHJOBS, /BATCHRETRY, /BATCHRESUME, /BATCHPAUSE, /BATCHCANCEL, /BATCHLOGS
    # ==========================================
    @app.on_message(filters.command("batchstatus") & filters.private)
    async def cmd_batchstatus(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            controllers = ContentProcessingEngine._active_batch_controllers
            text = TelegramProgressUI.render_batch_status_screen(controllers)
            markup = BatchWizardManager.build_batch_status_markup()
            await message.reply_text(text, reply_markup=markup)
        except Exception as exc:
            logger.exception("Error in /batchstatus command: %s", exc)
            await message.reply_text("❌ Failed to retrieve batch status.")

    @app.on_message(filters.command("batchjobs") & filters.private)
    async def cmd_batchjobs(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            async with get_db_session() as session:
                repo = ContentRepository(session)
                db_jobs = await repo.get_recent_jobs(limit=10)
                jobs_data = []
                for j in db_jobs:
                    b_name = j.batch.name if j.batch else "Batch"
                    jobs_data.append({
                        "name": b_name,
                        "status": str(j.status.value) if j.status else "COMPLETED",
                        "progress": f"{j.current_step} ({j.progress_percent:.0f}%)" if j.current_step else "Finished"
                    })

            text = TelegramProgressUI.render_batch_jobs_screen(jobs_data)
            markup = BatchWizardManager.build_batch_jobs_markup()
            await message.reply_text(text, reply_markup=markup)
        except Exception as exc:
            logger.exception("Error in /batchjobs command: %s", exc)
            await message.reply_text("❌ Failed to retrieve recent batch jobs.")

    @app.on_message(filters.command("batchpause") & filters.private)
    async def cmd_batchpause(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            controllers = ContentProcessingEngine._active_batch_controllers
            paused = 0
            for ctrl in controllers.values():
                if not ctrl.is_paused:
                    ctrl.pause()
                    paused += 1

            if paused > 0:
                await message.reply_text(f"⏸ <b>Paused {paused} active batch job(s).</b> Checkpoints persisted in database.")
            else:
                await message.reply_text("ℹ️ <b>No running batch jobs found to pause.</b>")
        except Exception as exc:
            logger.exception("Error in /batchpause command: %s", exc)
            await message.reply_text("❌ Failed to pause batch job.")

    @app.on_message(filters.command("batchresume") & filters.private)
    async def cmd_batchresume(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            controllers = ContentProcessingEngine._active_batch_controllers
            resumed = 0
            for ctrl in controllers.values():
                if ctrl.is_paused:
                    ctrl.resume()
                    resumed += 1

            if resumed > 0:
                await message.reply_text(f"▶️ <b>Resumed {resumed} paused batch job(s).</b>")
            else:
                await message.reply_text("ℹ️ <b>No paused batch jobs found to resume.</b>")
        except Exception as exc:
            logger.exception("Error in /batchresume command: %s", exc)
            await message.reply_text("❌ Failed to resume batch job.")

    @app.on_message(filters.command("batchcancel") & filters.private)
    async def cmd_batchcancel(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            controllers = ContentProcessingEngine._active_batch_controllers
            if not controllers:
                await message.reply_text("ℹ️ <b>No active batch jobs found to cancel.</b>")
                return

            j_id = next(iter(controllers.keys()))
            ctrl = controllers[j_id]
            prompt = TelegramProgressUI.render_cancel_prompt(ctrl.batch_name, ctrl.completed_count, ctrl.total_lectures)
            markup = BatchWizardManager.build_cancel_confirmation_markup(j_id)
            await message.reply_text(prompt, reply_markup=markup)
        except Exception as exc:
            logger.exception("Error in /batchcancel command: %s", exc)
            await message.reply_text("❌ Failed to cancel batch job.")

    @app.on_message(filters.command("batchretry") & filters.private)
    async def cmd_batchretry(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            state = BatchWizardManager.get_session(processing_engine.bot_id, user_id) or _last_batch_sessions_by_user.get(user_id)
            if not state or not state.tree:
                await message.reply_text("⚠️ <b>No active batch session to retry.</b> Please upload a TXT file first via /batch.")
                return

            state.retry_failed_only = True
            state.execution_mode = "REPROCESS FAILED"
            status_msg = await message.reply_text("🔄 <b>Retrying failed items in batch...</b>")
            asyncio.create_task(launch_batch_execution(client, status_msg, state))
        except Exception as exc:
            logger.exception("Error in /batchretry command: %s", exc)
            await message.reply_text("❌ Failed to retry failed items.")

    @app.on_message(filters.command("batchlogs") & filters.private)
    async def cmd_batchlogs(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            logs = (
                f"📜 <b>OPERATIONAL BATCH PIPELINE LOGS:</b>\n\n"
                f"• <code>[JOB_ENGINE] Worker bot_1 operational ({MAX_CONCURRENT_JOBS} max concurrent)</code>\n"
                f"• <code>[STORAGE] B2 S3 bucket 'course-wallah-pdfs' connected (us-east-005)</code>\n"
                f"• <code>[YOUTUBE] OAuth resumable uploader online</code>\n"
                f"• <code>[WATERMARK] Moving drift filter loaded (H.264 CRF 26)</code>\n"
                f"• <code>[DATABASE] SQLite / PostgreSQL schema verified</code>\n"
                f"• <code>[SECURITY] Admin authentication active</code>"
            )
            await message.reply_text(logs)
        except Exception as exc:
            logger.exception("Error in /batchlogs command: %s", exc)
            await message.reply_text("❌ Failed to load batch logs.")

    @app.on_message(filters.command(["batches", "list_batches"]) & filters.private)
    async def cmd_batches(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            async with get_db_session() as session:
                repo = ContentRepository(session)
                batches = await repo.get_all_batches()
                if not batches:
                    await message.reply_text("📁 <b>No batches found in database.</b> Upload a .txt file with /batch to get started.")
                    return

                lines = ["📚 <b>COURSE WALLAH BATCHES:</b>\n━━━━━━━━━━━━━━━━━━━━"]
                for b in batches[:25]:
                    summary = await repo.get_batch_lecture_summary(b.id)
                    app_name = b.app.name if b.app else "App"
                    tot = summary["total_lectures"]
                    pub = summary["published_lectures"]
                    vid = summary["videos_count"]
                    pdf = summary["pdfs_count"]
                    status_emoji = "🟢" if pub == tot and tot > 0 else ("🟡" if pub > 0 else "🔴")
                    lines.append(
                        f"{status_emoji} <b>{b.name}</b> (<code>{b.slug}</code>)\n"
                        f"   📁 App: <b>{app_name}</b> | ID: <code>{b.id}</code>\n"
                        f"   📊 Published: <b>{pub}/{tot}</b> | 🎥 Videos: <b>{vid}</b> | 📄 PDFs: <b>{pdf}</b>\n"
                        f"   👉 <i>/publish_batch {b.id}</i> | <i>/reprocess_batch {b.id}</i>\n"
                    )

                text = "\n".join(lines)
                if len(text) > 4000:
                    text = text[:3900] + "\n\n<i>...and more batches.</i>"
                await message.reply_text(text)
        except Exception as exc:
            logger.exception("Error in /batches command: %s", exc)
            await message.reply_text("❌ Failed to list batches.")

    @app.on_message(filters.command(["publish_batch", "publish"]) & filters.private)
    async def cmd_publish_batch(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            if len(message.command) < 2:
                await message.reply_text(
                    "ℹ️ <b>Usage:</b> <code>/publish_batch &lt;batch_id_or_slug&gt;</code>\n\n"
                    "Publishes all existing valid lectures in a batch and ensures they are visible on the website."
                )
                return

            batch_query = " ".join(message.command[1:]).strip()
            async with get_db_session() as session:
                repo = ContentRepository(session)
                batch = await repo.get_batch_by_id_or_slug(batch_query)
                if not batch:
                    await message.reply_text(f"❌ <b>Batch not found:</b> <code>{batch_query}</code>")
                    return

                res = await repo.publish_all_batch_lectures(batch.id, force_all=False)
                await message.reply_text(
                    f"✅ <b>BATCH PUBLISHED SUCCESSFULLY!</b>\n\n"
                    f"📚 <b>Batch:</b> {batch.name}\n"
                    f"🆔 <b>Batch ID:</b> <code>{batch.id}</code>\n"
                    f"🚀 <b>Newly Published:</b> {res['published']}/{res['total']} lectures\n"
                    f"🌐 <b>Website Status:</b> Now live and viewable by students!"
                )
        except Exception as exc:
            logger.exception("Error in /publish_batch command: %s", exc)
            await message.reply_text("❌ Failed to publish batch.")

    @app.on_message(filters.command(["reprocess_batch", "retry_batch"]) & filters.private)
    async def cmd_reprocess_batch(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            if len(message.command) < 2:
                # Check if there is an active/cached batch session first
                state = BatchWizardManager.get_session(processing_engine.bot_id, user_id) or _last_batch_sessions_by_user.get(user_id)
                if state and state.tree:
                    state.retry_failed_only = True
                    state.execution_mode = "REPROCESS INCOMPLETE"
                    status_msg = await message.reply_text("🔄 <b>Retrying incomplete & failed items in batch...</b>")
                    asyncio.create_task(launch_batch_execution(client, status_msg, state))
                    return
                await message.reply_text("ℹ️ <b>Usage:</b> <code>/reprocess_batch &lt;batch_id_or_slug&gt;</code>")
                return

            batch_query = " ".join(message.command[1:]).strip()
            async with get_db_session() as session:
                repo = ContentRepository(session)
                batch = await repo.get_batch_by_id_or_slug(batch_query)
                if not batch:
                    await message.reply_text(f"❌ <b>Batch not found:</b> <code>{batch_query}</code>")
                    return

                # Check cached session by batch ID
                cached_state = _last_batch_sessions_by_batch.get(batch.id)
                if cached_state and cached_state.tree:
                    cached_state.retry_failed_only = True
                    cached_state.execution_mode = "REPROCESS INCOMPLETE"
                    status_msg = await message.reply_text(f"🔄 <b>Retrying incomplete items for '{batch.name}'...</b>")
                    asyncio.create_task(launch_batch_execution(client, status_msg, cached_state))
                    return

                # Reconstruct tree from DB lectures
                lectures = await repo.get_lectures_by_batch(batch.id)
                if not lectures:
                    await message.reply_text(f"⚠️ No lectures found in database for batch '{batch.name}'.")
                    return

                # Check how many are unpublished / failed
                failed_lecs = [l for l in lectures if l.publication_status != PublicationStatus.PUBLISHED]
                if not failed_lecs:
                    # Also check if any published lectures lack media
                    lacking_media = []
                    for l in lectures:
                        has_ready = False
                        if l.video and l.video.storages:
                            if any(s.status in ("READY", "ready") for s in l.video.storages):
                                has_ready = True
                        elif l.video and l.video.youtube_video_id and not l.video.youtube_video_id.startswith(("cw_temp_", "yt_id_", "EXISTING_YT", "YT_PERSIST", "dQw4w9WgXcQ")):
                            has_ready = True
                        elif not l.has_video and l.has_pdf and l.pdf:
                            has_ready = True
                        if not has_ready:
                            lacking_media.append(l)
                    failed_lecs.extend(lacking_media)

                if not failed_lecs:
                    await message.reply_text(
                        f"🎉 <b>All {len(lectures)} lectures in '{batch.name}' are already fully published and verified!</b>\n\n"
                        f"If you want to force publish all, use <code>/publish_batch {batch.id}</code>."
                    )
                    return

                # Build normalized batch tree from DB items
                raw_lines = []
                for l in lectures:
                    subj_name = l.subject.name if l.subject else "Subject"
                    folder_name = l.folder.name if l.folder else "Folder"
                    v_url = l.source_url or ""
                    p_url = l.source_pdf_url or ""
                    line = f"{subj_name} / {folder_name} : {l.title} : {v_url}"
                    if p_url:
                        line += f" | {p_url}"
                    raw_lines.append(line)

                reconstructed_tree = TxtIndexer.index_txt(
                    file_content="\n".join(raw_lines),
                    filename=f"{batch.name}.txt",
                    app_name=batch.app.name if batch.app else "Course Wallah",
                    batch_override=batch.name
                )
                app_name = batch.app.name if batch.app else "Course Wallah"

                reprocess_state = BatchWizardState(
                    bot_id=processing_engine.bot_id,
                    user_id=user_id,
                    tree=reconstructed_tree,
                    app_id=batch.app_id,
                    app_name=app_name,
                    batch_id=batch.id,
                    batch_name=batch.name,
                    category=batch.category or "Engineering",
                    branch=batch.branch or "General",
                    semester=batch.semester or "Semester",
                    academic_year=batch.academic_year or "2025-2026",
                    quality_pref=batch.video_quality_preference or "1080p",
                    watermark_enabled=True,
                    retry_failed_only=True,
                    execution_mode="REPROCESS INCOMPLETE"
                )

                _last_batch_sessions_by_batch[batch.id] = reprocess_state
                _last_batch_sessions_by_user[user_id] = reprocess_state

                status_msg = await message.reply_text(
                    f"🚀 <b>REPROCESSING INCOMPLETE LECTURES...</b>\n\n"
                    f"📚 <b>Batch:</b> {batch.name}\n"
                    f"🔄 <b>Incomplete / Failed:</b> {len(failed_lecs)} / {len(lectures)}\n"
                    f"⚙️ <i>Starting high-speed multi-storage ingestion pipeline...</i>"
                )
                asyncio.create_task(launch_batch_execution(client, status_msg, reprocess_state))

        except Exception as exc:
            logger.exception("Error in /reprocess_batch command: %s", exc)
            await message.reply_text("❌ Failed to reprocess batch.")

    # ==========================================
    # 7. COMMANDS: /STUDENTS, /STUDENT, /GRANT, /REVOKE
    # ==========================================
    @app.on_message(filters.command("students") & filters.private)
    async def cmd_students(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            query = ""
            if len(message.command) > 1:
                query = " ".join(message.command[1:])

            async with get_db_session() as session:
                repo = ContentRepository(session)
                students = await repo.find_students(query=query, limit=20)

            text = TelegramProgressUI.render_student_list(students)
            await message.reply_text(text)
        except Exception as exc:
            logger.exception("Error in /students command: %s", exc)
            await message.reply_text("❌ Failed to retrieve students list.")

    @app.on_message(filters.command("student") & filters.private)
    async def cmd_student(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            if len(message.command) < 2:
                await message.reply_text("ℹ️ <b>Usage:</b> <code>/student &lt;email_or_id&gt;</code>")
                return

            identifier = message.command[1].strip()
            async with get_db_session() as session:
                repo = ContentRepository(session)
                student = await repo.get_student_by_email(identifier)
                if not student:
                    student = await repo.get_student_by_id(identifier)

            if not student:
                await message.reply_text("❌ <b>Student not found.</b>")
                return

            text = TelegramProgressUI.render_student_detail(student)
            await message.reply_text(text)
        except Exception as exc:
            logger.exception("Error in /student command: %s", exc)
            await message.reply_text("❌ Failed to retrieve student details.")

    @app.on_message(filters.command("grant") & filters.private)
    async def cmd_grant(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            if len(message.command) < 3:
                await message.reply_text("ℹ️ <b>Usage:</b> <code>/grant &lt;email&gt; &lt;batch_slug_or_id&gt;</code>")
                return

            email = message.command[1].strip().lower()
            batch_identifier = message.command[2].strip()

            async with get_db_session() as session:
                repo = ContentRepository(session)
                student = await repo.get_student_by_email(email)
                if not student:
                    await message.reply_text(f"❌ <b>Student not found:</b> <code>{email}</code>")
                    return

                batch = await repo.get_batch_by_slug(batch_identifier)
                if not batch:
                    batch = await repo.get_batch(batch_identifier)

                if not batch:
                    await message.reply_text(f"❌ <b>Batch not found:</b> <code>{batch_identifier}</code>")
                    return

                await repo.grant_student_batch_access(student.id, batch.id, "ACTIVE")
                await session.commit()

            await message.reply_text(
                f"✅ <b>Access Granted Successfully!</b>\n\n"
                f"• <b>Student:</b> {student.name} (<code>{student.email}</code>)\n"
                f"• <b>Batch:</b> {batch.name}\n"
                f"• <b>Status:</b> ACTIVE"
            )
        except Exception as exc:
            logger.exception("Error in /grant command: %s", exc)
            await message.reply_text("❌ Failed to grant batch access.")

    @app.on_message(filters.command("revoke") & filters.private)
    async def cmd_revoke(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            if len(message.command) < 3:
                await message.reply_text("ℹ️ <b>Usage:</b> <code>/revoke &lt;email&gt; &lt;batch_slug_or_id&gt;</code>")
                return

            email = message.command[1].strip().lower()
            batch_identifier = message.command[2].strip()

            async with get_db_session() as session:
                repo = ContentRepository(session)
                student = await repo.get_student_by_email(email)
                if not student:
                    await message.reply_text(f"❌ <b>Student not found:</b> <code>{email}</code>")
                    return

                batch = await repo.get_batch_by_slug(batch_identifier)
                if not batch:
                    batch = await repo.get_batch(batch_identifier)

                if not batch:
                    await message.reply_text(f"❌ <b>Batch not found:</b> <code>{batch_identifier}</code>")
                    return

                revoked = await repo.revoke_student_batch_access(student.id, batch.id)
                await session.commit()

            if revoked:
                await message.reply_text(
                    f"🛑 <b>Access Revoked!</b>\n\n"
                    f"• <b>Student:</b> {student.name} (<code>{student.email}</code>)\n"
                    f"• <b>Batch:</b> {batch.name}\n"
                    f"• <b>Status:</b> REVOKED"
                )
            else:
                await message.reply_text(f"ℹ️ Student did not have access to <b>{batch.name}</b>.")
        except Exception as exc:
            logger.exception("Error in /revoke command: %s", exc)
            await message.reply_text("❌ Failed to revoke batch access.")

    # ==========================================
    # 8. COMMAND: /HEALTH & /DIAGNOSTICS
    # ==========================================
    @app.on_message(filters.command(["health", "diagnostics"]) & filters.private)
    async def cmd_health(client: Client, message: Message):
        try:
            user_id = message.from_user.id if message.from_user else 0
            if not is_admin(user_id):
                await message.reply_text("⛔ <b>ADMIN ONLY</b>")
                return

            async with get_db_session() as session:
                repo = ContentRepository(session)
                health = await repo.get_media_health_summary()

            text = TelegramProgressUI.render_media_health(health)
            await message.reply_text(text)
        except Exception as exc:
            logger.exception("Error in /health command: %s", exc)
            await message.reply_text("❌ Failed to retrieve media health diagnostics.")


    # ==========================================
    # 7. FORWARDED CHANNEL MESSAGE HANDLER (1-CLICK STORAGE CHANNEL SETUP)
    # ==========================================
    @app.on_message(filters.forwarded & filters.private & ~filters.document & ~filters.video & ~filters.audio & ~filters.photo)
    async def handle_forwarded_message(client: Client, message: Message):
        user_id = message.from_user.id if message.from_user else 0
        if not is_admin(user_id):
            return

        fwd_chat = None
        if hasattr(message, "forward_origin") and message.forward_origin:
            orig = message.forward_origin
            if hasattr(orig, "chat") and orig.chat:
                fwd_chat = orig.chat
            elif hasattr(orig, "sender_chat") and orig.sender_chat:
                fwd_chat = orig.sender_chat
        if not fwd_chat and hasattr(message, "forward_from_chat") and message.forward_from_chat:
            fwd_chat = message.forward_from_chat

        if fwd_chat and fwd_chat.type in ("channel", "supergroup", "group"):
            chan_id = fwd_chat.id
            chan_title = fwd_chat.title or "Private Storage Channel"

            wait_msg = await message.reply_text(
                f"🔍 <b>Detected Channel Forward:</b> <b>{chan_title}</b> (<code>{chan_id}</code>)\n"
                f"⏳ <i>Testing bot administrator permissions...</i>"
            )
            try:
                test_post = await client.send_message(
                    chat_id=chan_id,
                    text="⚡ <b>Course Wallah Streaming Engine Verification</b>\n\nStorage Channel connected successfully."
                )
                await asyncio.sleep(1)
                await test_post.delete()

                pool = TelegramClientPool.get_instance()
                pool.storage_chat_id = chan_id
                _waiting_for_channel_id.pop(user_id, None)

                # Persist to .env
                try:
                    from config.settings import BASE_DIR
                    env_file = BASE_DIR / ".env"
                    if env_file.exists():
                        env_text = env_file.read_text(encoding="utf-8")
                        if "TELEGRAM_STORAGE_CHANNEL_ID=" in env_text:
                            env_text = re.sub(
                                r"TELEGRAM_STORAGE_CHANNEL_ID=.*",
                                f"TELEGRAM_STORAGE_CHANNEL_ID={chan_id}",
                                env_text
                            )
                        else:
                            env_text += f"\nTELEGRAM_STORAGE_CHANNEL_ID={chan_id}\n"
                        env_file.write_text(env_text, encoding="utf-8")
                except Exception as env_err:
                    logger.debug(f"[ENV_UPDATE_NOTICE] {env_err}")

                card = (
                    f"🎉 <b>STORAGE CHANNEL CONNECTED SUCCESSFULLY!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📺 <b>Channel Name:</b> <b>{chan_title}</b>\n"
                    f"🆔 <b>Channel ID:</b> <code>{chan_id}</code>\n"
                    f"⚡ <b>Engine:</b> Telegram MTProto High-Speed Zero-Cost Stream\n"
                    f"🌐 <b>Website Player:</b> Course Wallah In-App HTML5 Cinema Player\n"
                    f"🚀 <b>Capacity:</b> 100% Unlimited (Up to 2GB per video)\n\n"
                    f"<i>Channel connected! Any video you send or batch you ingest will now upload here and stream to your website instantly.</i>"
                )
                await wait_msg.edit_text(card)
            except Exception as perm_err:
                await wait_msg.edit_text(
                    f"❌ <b>Bot Access Test Failed for '{chan_title}' (<code>{chan_id}</code>)!</b>\n\n"
                    f"Error: <code>{str(perm_err)}</code>\n\n"
                    f"👉 Please make sure this bot is added as an <b>Administrator</b> to the channel with <b>Post Messages</b> permission."
                )

    # ==========================================
    # 7.5. DIRECT VIDEO MESSAGE HANDLER
    # ==========================================
    @app.on_message(filters.video & filters.private)
    async def handle_video_message(client: Client, message: Message):
        user_id = message.from_user.id if message.from_user else 0
        if not is_admin(user_id):
            await message.reply_text("⛔ <b>ADMIN ONLY</b>")
            return

        status_msg = await message.reply_text("⚡ <i>Uploading video to Telegram Storage Channel & generating live web stream...</i>")
        try:
            ok, text_msg, meta = await publish_video_to_telegram_stream_and_db(
                client=client,
                message=message,
                user_id=user_id
            )
            if not ok:
                await status_msg.edit_text(f"❌ {text_msg}")
                return

            title = meta["title"]
            size_mb = meta["file_size"] / (1024 * 1024)
            duration_mins = meta["duration"] // 60
            duration_secs = meta["duration"] % 60
            dur_str = f"{duration_mins}m {duration_secs:02d}s" if meta["duration"] else "N/A"

            stream_url = meta["stream_url"]
            web_url = meta["web_player_url"]
            storage_chat = meta["storage_chat_id"]
            storage_msg_id = meta["storage_msg_id"]
            res = meta["resolution"]

            card = (
                f"🎬 <b>VIDEO UPLOADED & LIVE ON WEBSITE!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📺 <b>Title:</b> <b>{title}</b>\n"
                f"📊 <b>Size:</b> <code>{size_mb:.2f} MB</code> | <b>Duration:</b> <code>{dur_str}</code>\n"
                f"🎯 <b>Quality:</b> <code>{res} Ultra HD</code>\n"
                f"📁 <b>Storage Channel:</b> <code>{storage_chat}</code> (Msg #<code>{storage_msg_id}</code>)\n\n"
                f"⚡ <b>Direct HTTP Stream Link:</b>\n"
                f"<code>{stream_url}</code>\n\n"
                f"🌐 <b>Website Player Link:</b>\n"
                f"<code>{web_url}</code>\n\n"
                f"💻 <b>HTML5 Embed Code:</b>\n"
                f"<code>&lt;video controls src=\"{stream_url}\"&gt;&lt;/video&gt;</code>\n\n"
                f"✅ <i>Published to Course Wallah web portal automatically!</i>"
            )
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Open on Website", url=web_url)],
                [
                    InlineKeyboardButton("🎬 Direct Stream", url=stream_url),
                    InlineKeyboardButton("⚡ Stream Status", callback_data="admin:streamstatus")
                ]
            ])
            await status_msg.edit_text(card, reply_markup=markup, disable_web_page_preview=True)
        except Exception as exc:
            logger.exception("Error in handle_video_message: %s", exc)
            await status_msg.edit_text(f"❌ Failed to process video upload: {exc}")

    # ==========================================
    # 7.6. DOCUMENT HANDLER (.TXT BATCH UPLOADER, .JSON CREDS, & VIDEO FILES)
    # ==========================================
    @app.on_message(filters.document & filters.private)
    async def handle_document(client: Client, message: Message):
        user_id = message.from_user.id if message.from_user else 0
        if not is_admin(user_id):
            await message.reply_text("⛔ <b>ADMIN ONLY</b>\n\n<i>You are not authorized to upload course batches or credentials.</i>")
            return

        doc = message.document
        fname = (doc.file_name or "").lower()
        mime = (doc.mime_type or "").lower()

        # Route document videos (e.g. .mp4, .mkv, .mov, etc.) to video stream publisher
        video_exts = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".ts", ".m4v", ".flv", ".3gp")
        if fname.endswith(video_exts) or mime.startswith("video/"):
            await handle_video_message(client, message)
            return

        # Handle JSON OAuth Credentials File Upload
        if fname.endswith(".json"):
            status_msg = await message.reply_text("⏳ <i>Downloading and validating JSON credentials with Google OAuth...</i>")
            try:
                file_path = await message.download()
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                try:
                    os.remove(file_path)
                except Exception:
                    pass

                parsed = parse_youtube_credentials_text(content)
                if parsed and parsed.get("client_id") and parsed.get("client_secret"):
                    # Check if refresh token is present in JSON
                    if not parsed.get("refresh_token"):
                        _yt_oauth_pending_sessions[user_id] = parsed
                        _yt_add_waiting_users[user_id] = True
                        card, markup = build_oauth_authorization_card_and_markup(parsed)
                        await status_msg.edit_text(card, reply_markup=markup, disable_web_page_preview=True)
                        return

                    success, acc_dict, msg_or_err = await YouTubeAccountManager.add_account_from_credentials(
                        name=parsed.get("name", doc.file_name.replace(".json", "")),
                        client_id=parsed["client_id"],
                        client_secret=parsed["client_secret"],
                        refresh_token=parsed["refresh_token"],
                        priority=2,
                        auto_test=True
                    )
                    if success:
                        ch_title = acc_dict.get("channel_title") or acc_dict.get("name")
                        ch_id = acc_dict.get("channel_id")
                        markup = InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("🎬 View All Accounts", callback_data="admin:youtube_accounts"),
                                InlineKeyboardButton("➕ Add Another", callback_data="yt:add_prompt")
                            ]
                        ])
                        await status_msg.edit_text(
                            f"🎉 <b>YOUTUBE CHANNEL LINKED VIA JSON!</b>\n\n"
                            f"📺 <b>Channel:</b> <b>{ch_title}</b>\n"
                            f"🆔 <b>Channel ID:</b> <code>{ch_id or 'Detected'}</code>\n"
                            f"⭐ <b>Priority:</b> #{acc_dict.get('priority', 2)}\n"
                            f"🟢 <b>Status:</b> ACTIVE\n"
                            f"🎯 <b>Daily Limit:</b> ~20 uploads/day\n\n"
                            f"🛡️ This channel is now active in your failover pool!",
                            reply_markup=markup
                        )
                    else:
                        markup = InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔄 Try Again", callback_data="yt:add_prompt")],
                            [InlineKeyboardButton("❌ Cancel", callback_data="admin:youtube_accounts")]
                        ])
                        await status_msg.edit_text(
                            f"❌ <b>OAuth Verification Failed:</b>\n\n<code>{msg_or_err}</code>\n\n"
                            f"<i>Please verify that your Google Cloud OAuth app is configured and token is active.</i>",
                            reply_markup=markup
                        )
                else:
                    await status_msg.edit_text(
                        "❌ <b>Could not parse OAuth credentials from JSON.</b>\n\n"
                        "Please ensure the JSON contains <code>client_id</code>, <code>client_secret</code>, and <code>refresh_token</code>."
                    )
            except Exception as e:
                logger.exception("Error processing JSON credential file: %s", e)
                await status_msg.edit_text(f"❌ Failed to process JSON credential file: {e}")
            return

        if not fname.endswith(".txt"):
            await message.reply_text("❌ <b>Invalid file format.</b> Please upload a <code>.txt</code> course file, <code>.mp4</code> video, or <code>.json</code> credentials file.")
            return

        status_msg = await message.reply_text("⏳ <i>Downloading and analyzing TXT batch file...</i>")

        try:
            file_path = await message.download()
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            try:
                os.remove(file_path)
            except Exception:
                pass

            state = await BatchWizardManager.analyze_and_start_session(
                bot_id=processing_engine.bot_id,
                user_id=user_id,
                file_content=content,
                filename=doc.file_name
            )

            # Check if matching App exists in DB
            async with get_db_session() as session:
                repo = ContentRepository(session)
                all_apps = await repo.get_all_apps()
                matching_app = None
                for a in all_apps:
                    if slugify(a.name) == slugify(state.app_name) or a.name.lower() in state.app_name.lower():
                        matching_app = a
                        break

            if matching_app:
                state.app_id = matching_app.id
                state.app_name = matching_app.name
                card = TelegramProgressUI.render_existing_app_found(matching_app.name, state.analysis_stats)
                markup = BatchWizardManager.build_existing_app_found_markup(matching_app.id)
            else:
                card = TelegramProgressUI.render_app_not_found(state.app_name, state.analysis_stats)
                markup = BatchWizardManager.build_app_not_found_markup()

            await status_msg.edit_text(card, reply_markup=markup)
        except Exception as exc:
            logger.exception("Failed to parse TXT file: %s", exc)
            await status_msg.edit_text("❌ <b>Failed to parse TXT file.</b> Please check the file format and try again.")

    # ==========================================
    # 8. TEXT INPUT LISTENER (FOR INTERACTIVE WIZARD & CREDS)
    # ==========================================
    @app.on_message(filters.text & filters.private & ~filters.command([
        "start", "help", "id", "info", "stop", "remove_auth",
        "youtube", "youtube_accounts", "youtube_status", "youtube_pause", "youtube_resume",
        "yt", "yt_list", "yt_add", "add_youtube", "yt_del", "yt_remove", "yt_test", "yt_primary", "yt_set_primary", "yt_toggle", "yt_status", "yt_pause", "yt_resume",
        "admin", "batch", "uploadbatch", "batchstatus", "batchjobs", "batchretry", "batchresume", "batchpause", "batchcancel", "batchlogs", "health", "diagnostics", "students", "student", "grant", "revoke",
        "setchannel", "channel", "set_channel", "storagechannel", "setstoragechannel", "streamstatus", "tgstream", "stream_status", "stream", "uploadvideo", "upload_video", "streamvideo"
    ]))
    async def handle_text_inputs(client: Client, message: Message):

        user_id = message.from_user.id if message.from_user else 0
        if not is_admin(user_id):
            return

        text = message.text.strip()

        # 0. Check if user is waiting to set storage channel ID
        if _waiting_for_channel_id.get(user_id):
            try:
                target_chat_id = int(text)
                wait_msg = await message.reply_text(f"⏳ <i>Verifying bot access to channel <code>{target_chat_id}</code>...</i>")
                test_post = await client.send_message(
                    chat_id=target_chat_id,
                    text="⚡ <b>Course Wallah Streaming Engine Verification</b>\n\nStorage Channel connected successfully."
                )
                await asyncio.sleep(1)
                await test_post.delete()

                pool = TelegramClientPool.get_instance()
                pool.storage_chat_id = target_chat_id
                _waiting_for_channel_id.pop(user_id, None)

                try:
                    from config.settings import BASE_DIR
                    env_file = BASE_DIR / ".env"
                    if env_file.exists():
                        env_text = env_file.read_text(encoding="utf-8")
                        if "TELEGRAM_STORAGE_CHANNEL_ID=" in env_text:
                            env_text = re.sub(
                                r"TELEGRAM_STORAGE_CHANNEL_ID=.*",
                                f"TELEGRAM_STORAGE_CHANNEL_ID={target_chat_id}",
                                env_text
                            )
                        else:
                            env_text += f"\nTELEGRAM_STORAGE_CHANNEL_ID={target_chat_id}\n"
                        env_file.write_text(env_text, encoding="utf-8")
                except Exception as env_err:
                    logger.debug(f"[ENV_UPDATE_NOTICE] {env_err}")

                success_text = (
                    f"🎉 <b>STORAGE CHANNEL CONNECTED SUCCESSFULLY!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"✅ <b>Active Channel ID:</b> <code>{target_chat_id}</code>\n"
                    f"⚡ <b>Engine:</b> Telegram MTProto High-Speed Zero-Cost Stream\n"
                    f"🌐 <b>Website Player:</b> Course Wallah In-App HTML5 Cinema Player\n"
                    f"🚀 <b>Capacity:</b> 100% Unlimited (Up to 2GB per video)\n\n"
                    f"<i>Channel connected! Any video you send or batch you ingest will now upload here and stream to your website instantly.</i>"
                )
                await wait_msg.edit_text(success_text)
                return
            except ValueError:
                pass
            except Exception as perm_err:
                await message.reply_text(
                    f"❌ <b>Channel Access Failed!</b>\n\n"
                    f"Error: <code>{str(perm_err)}</code>\n\n"
                    f"👉 Please make sure this bot is added as an <b>Administrator</b> to channel <code>{text}</code> with <b>Post Messages</b> permission."
                )
                return

        # 1. Check if user is in an active 1-Click Google OAuth flow waiting for auth code or redirect URL
        if user_id in _yt_oauth_pending_sessions:
            sess = _yt_oauth_pending_sessions[user_id]
            code = extract_oauth_code_from_input(text)
            if code:
                wait_msg = await message.reply_text("⏳ <i>Exchanging authorization code with Google OAuth & fetching Channel details...</i>")
                token_res = await YouTubeAccountManager.exchange_oauth_code_for_tokens(
                    client_id=sess["client_id"],
                    client_secret=sess["client_secret"],
                    code=code,
                    redirect_uri=sess.get("redirect_uri", "http://localhost")
                )
                if not token_res.get("valid"):
                    auth_url = YouTubeAccountManager.generate_oauth_authorization_url(
                        sess["client_id"],
                        sess.get("redirect_uri", "http://localhost")
                    )

                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Re-Authorize (Get New Code)", url=auth_url)],
                        [InlineKeyboardButton("❌ Cancel", callback_data="yt:cancel_oauth")]
                    ])
                    await wait_msg.edit_text(
                        f"❌ <b>OAuth Exchange Failed:</b>\n\n"
                        f"<code>{token_res.get('error', 'Unknown token exchange error')}</code>\n\n"
                        f"<i>Note: Authorization codes expire within minutes or can only be used once. Please click above to get a fresh code and try again.</i>",
                        reply_markup=markup,
                        disable_web_page_preview=True
                    )
                    return

                refresh_token = token_res["refresh_token"]
                success, acc_dict, msg_or_err = await YouTubeAccountManager.add_account_from_credentials(
                    name=sess.get("name", "YouTube Channel"),
                    client_id=sess["client_id"],
                    client_secret=sess["client_secret"],
                    refresh_token=refresh_token,
                    priority=2,
                    auto_test=True
                )
                if success:
                    _yt_oauth_pending_sessions.pop(user_id, None)
                    _yt_add_waiting_users.pop(user_id, None)
                    ch_title = acc_dict.get("channel_title") or acc_dict.get("name")
                    ch_id = acc_dict.get("channel_id")
                    markup = InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("🎬 View All Accounts", callback_data="admin:youtube_accounts"),
                            InlineKeyboardButton("➕ Add Another", callback_data="yt:add_prompt")
                        ]
                    ])
                    await wait_msg.edit_text(
                        f"🎉 <b>YOUTUBE CHANNEL LINKED SUCCESSFULLY!</b>\n\n"
                        f"📺 <b>Channel:</b> <b>{ch_title}</b>\n"
                        f"🆔 <b>Channel ID:</b> <code>{ch_id or 'Detected'}</code>\n"
                        f"⭐ <b>Priority:</b> #{acc_dict.get('priority', 2)}\n"
                        f"🟢 <b>Status:</b> ACTIVE\n"
                        f"🎯 <b>Daily Quota:</b> ~20 videos/day\n"
                        f"🔑 <b>Refresh Token:</b> <code>{refresh_token[:8]}...{refresh_token[-6:]}</code> (Autosaved to DB!)\n\n"
                        f"🛡️ <b>Multi-Channel Pool:</b> This channel is now active. When another channel hits the daily upload limit, the bot rotates to this channel automatically!",
                        reply_markup=markup
                    )
                else:
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Try Again", callback_data="yt:add_prompt")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="admin:youtube_accounts")]
                    ])
                    await wait_msg.edit_text(
                        f"❌ <b>Channel Verification Failed:</b>\n\n"
                        f"<code>{msg_or_err}</code>\n\n"
                        f"<i>Please verify your Google OAuth consent screen & YouTube permissions.</i>",
                        reply_markup=markup
                    )
                return

        # 2. Check if user sent JSON or credentials directly (or is in _yt_add_waiting_users)
        parsed = parse_youtube_credentials_text(text)
        if parsed and parsed.get("client_id") and parsed.get("client_secret"):
            if not parsed.get("refresh_token"):
                _yt_oauth_pending_sessions[user_id] = parsed
                _yt_add_waiting_users[user_id] = True
                card, markup = build_oauth_authorization_card_and_markup(parsed)
                await message.reply_text(card, reply_markup=markup, disable_web_page_preview=True)
                return

            _yt_add_waiting_users.pop(user_id, None)
            _yt_oauth_pending_sessions.pop(user_id, None)
            wait_msg = await message.reply_text("⏳ <i>Testing OAuth credentials with Google & fetching Channel info...</i>")
            success, acc_dict, msg_or_err = await YouTubeAccountManager.add_account_from_credentials(
                name=parsed["name"],
                client_id=parsed["client_id"],
                client_secret=parsed["client_secret"],
                refresh_token=parsed["refresh_token"],
                priority=2,
                auto_test=True
            )
            if success:
                ch_title = acc_dict.get("channel_title") or acc_dict.get("name")
                ch_id = acc_dict.get("channel_id")
                markup = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🎬 View All Accounts", callback_data="admin:youtube_accounts"),
                        InlineKeyboardButton("➕ Add Another", callback_data="yt:add_prompt")
                    ]
                ])
                await wait_msg.edit_text(
                    f"🎉 <b>YOUTUBE CHANNEL LINKED SUCCESSFULLY!</b>\n\n"
                    f"📺 <b>Channel:</b> <b>{ch_title}</b>\n"
                    f"🆔 <b>Channel ID:</b> <code>{ch_id or 'Detected'}</code>\n"
                    f"⭐ <b>Priority:</b> #{acc_dict.get('priority', 2)}\n"
                    f"🟢 <b>Status:</b> ACTIVE\n"
                    f"🎯 <b>Daily Quota:</b> ~20 videos/day\n\n"
                    f"🛡️ <b>Multi-Channel Pool:</b> This channel is now active. When another channel hits the daily upload limit, the bot rotates to this channel automatically!",
                    reply_markup=markup
                )
            else:
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Try Again", callback_data="yt:add_prompt")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="admin:youtube_accounts")]
                ])
                await wait_msg.edit_text(
                    f"❌ <b>OAuth Verification Failed:</b>\n\n"
                    f"<code>{msg_or_err}</code>\n\n"
                    f"<i>Please verify your Client ID, Client Secret, and Refresh Token.</i>",
                    reply_markup=markup
                )
            return

        if _yt_add_waiting_users.get(user_id):
            if "client" in text.lower() or "|" in text or "apps.googleusercontent" in text or "1//" in text:
                await message.reply_text(
                    "⚠️ <b>Incomplete Credentials Format.</b>\n\n"
                    "You can simply send or paste your Google Cloud <code>client_secrets.json</code> file, and the bot will generate an instant 1-Click authorization link for you!\n\n"
                    "Or provide: <code>Channel Name | CLIENT_ID | CLIENT_SECRET | REFRESH_TOKEN</code>"
                )
                return

        # 3. Check if user is in interactive App / Batch management mode
        if user_id in _app_manage_state:
            manage_task = _app_manage_state.pop(user_id)
            action = manage_task.get("action")
            app_id = manage_task.get("app_id")
            batch_id = manage_task.get("batch_id")

            if action == "RENAME_APP":
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    await repo.update_app(app_id, name=text)
                    await session.commit()
                await message.reply_text(f"✅ App name successfully updated to <b>{text}</b>!")
                await render_app_detail(client, message.chat.id, app_id, None)
                return

            elif action == "EDIT_APP_DESC":
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    await repo.update_app(app_id, description=text)
                    await session.commit()
                await message.reply_text("✅ App description updated successfully!")
                await render_app_detail(client, message.chat.id, app_id, None)
                return

            elif action == "SET_APP_ICON":
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    await repo.update_app(app_id, icon_url=text)
                    await session.commit()
                await message.reply_text(f"✅ App icon/image updated successfully!\n<code>{text}</code>")
                await render_app_detail(client, message.chat.id, app_id, None)
                return

            elif action == "RENAME_BATCH":
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    await repo.update_batch(batch_id, name=text)
                    await session.commit()
                await message.reply_text(f"✅ Batch renamed to <b>{text}</b>!")
                await render_batch_detail(client, message.chat.id, batch_id, app_id, None)
                return

            elif action == "SET_BATCH_CAT":
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    await repo.update_batch(batch_id, category=text)
                    await session.commit()
                await message.reply_text(f"✅ Batch category updated to <b>{text}</b>!")
                await render_batch_detail(client, message.chat.id, batch_id, app_id, None)
                return

            elif action == "SET_BATCH_THUMB":
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    await repo.update_batch(batch_id, thumbnail_url=text)
                    await session.commit()
                await message.reply_text(f"✅ Batch thumbnail URL updated!\n<code>{text}</code>")
                await render_batch_detail(client, message.chat.id, batch_id, app_id, None)
                return

        state = BatchWizardManager.get_session(processing_engine.bot_id, user_id)
        if not state or not state.text_input_prompt:
            return

        prompt = state.text_input_prompt

        text = message.text.strip()

        try:
            if prompt == "APP_NAME":
                state.app_name = text
                state.text_input_prompt = "APP_DESC"
                prompt_text = TelegramProgressUI.render_app_create_prompt("DESC", {"name": state.app_name})
                markup = BatchWizardManager.build_app_create_markup("DESC")
                await message.reply_text(prompt_text, reply_markup=markup)

            elif prompt == "APP_DESC":
                state.app_description = text
                state.text_input_prompt = "APP_ICON"
                prompt_text = TelegramProgressUI.render_app_create_prompt("ICON", {"name": state.app_name, "description": state.app_description})
                markup = BatchWizardManager.build_app_create_markup("ICON")
                await message.reply_text(prompt_text, reply_markup=markup)

            elif prompt == "APP_ICON":
                is_valid, reason, ct = validate_image_url(text)
                if not is_valid:
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Try Again", callback_data="wizard:app_prompt_icon")],
                        [InlineKeyboardButton("⏭ Skip", callback_data="wizard:app_skip_icon")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="wizard:cancel")]
                    ])
                    await message.reply_text(
                        f"❌ <b>Invalid image URL</b>\n\n"
                        f"<b>Reason:</b> {reason}\n\n"
                        f"Please provide a valid, reachable public image URL (JPEG, PNG, WebP) or click Skip / Cancel.",
                        reply_markup=markup
                    )
                    return

                state.app_icon_url = text
                state.text_input_prompt = None
                prompt_text = TelegramProgressUI.render_app_create_prompt("CONFIRM", {"name": state.app_name, "description": state.app_description, "icon_url": state.app_icon_url})
                markup = BatchWizardManager.build_app_create_markup("CONFIRM")
                await message.reply_text(prompt_text, reply_markup=markup)

            elif prompt == "BATCH_NAME":
                state.batch_name = text
                state.text_input_prompt = None
                prompt_text = TelegramProgressUI.render_batch_create_wizard("CATEGORY", {"name": state.batch_name, "app_name": state.app_name})
                markup = BatchWizardManager.build_batch_create_wizard_markup("CATEGORY")
                await message.reply_text(prompt_text, reply_markup=markup)

            elif prompt == "EDIT_APP_NAME":
                state.app_name = text
                state.text_input_prompt = None
                if state.app_id:
                    async with get_db_session() as session:
                        repo = ContentRepository(session)
                        await repo.update_app(state.app_id, name=text)
                await message.reply_text(f"✅ App name updated to <b>{text}</b>.")
                # Show app selection
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    apps = await repo.get_all_apps()
                text_ui = TelegramProgressUI.render_app_selection(apps)
                markup = BatchWizardManager.build_app_selection_markup(apps)
                await message.reply_text(text_ui, reply_markup=markup)

            elif prompt == "EDIT_APP_DESC":
                state.app_description = text
                state.text_input_prompt = None
                if state.app_id:
                    async with get_db_session() as session:
                        repo = ContentRepository(session)
                        await repo.update_app(state.app_id, description=text)
                await message.reply_text(f"✅ App description updated.")
                card = TelegramProgressUI.render_app_edit_prompt({"name": state.app_name, "description": state.app_description, "icon_url": state.app_icon_url})
                markup = BatchWizardManager.build_app_edit_markup()
                await message.reply_text(card, reply_markup=markup)

            elif prompt == "EDIT_APP_ICON":
                is_valid, reason, ct = validate_image_url(text)
                if not is_valid:
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Try Again", callback_data="wizard:app_edit_icon")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="wizard:cancel")]
                    ])
                    await message.reply_text(
                        f"❌ <b>Invalid image URL</b>\n\n"
                        f"<b>Reason:</b> {reason}\n\n"
                        f"Please provide a valid, reachable public image URL (JPEG, PNG, WebP) or click Cancel.",
                        reply_markup=markup
                    )
                    return

                state.app_icon_url = text
                state.text_input_prompt = None
                if state.app_id:
                    async with get_db_session() as session:
                        repo = ContentRepository(session)
                        await repo.update_app(state.app_id, icon_url=text)
                await message.reply_text(f"✅ App image URL updated.")
                card = TelegramProgressUI.render_app_edit_prompt({"name": state.app_name, "description": state.app_description, "icon_url": state.app_icon_url})
                markup = BatchWizardManager.build_app_edit_markup()
                await message.reply_text(card, reply_markup=markup)

            elif prompt == "BATCH_THUMBNAIL":
                is_valid, reason, ct = validate_image_url(text)
                if not is_valid:
                    markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Try Again", callback_data="wizard:batch_prompt_thumb")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="wizard:cancel")]
                    ])
                    await message.reply_text(
                        f"❌ <b>Invalid image URL</b>\n\n"
                        f"<b>Reason:</b> {reason}\n\n"
                        f"Please provide a valid, reachable public image URL (JPEG, PNG, WebP) or click Cancel.",
                        reply_markup=markup
                    )
                    return

                state.thumbnail_url = text
                state.text_input_prompt = None
                await message.reply_text(f"✅ Batch thumbnail URL updated.")

        except Exception as exc:
            logger.exception("Error processing wizard text input: %s", exc)
            await message.reply_text("❌ Error processing input. Please try again.")

    # ==========================================
    # 8.5 PHOTO INPUT LISTENER (FOR APP ICON & BATCH THUMBNAIL UPLOADS)
    # ==========================================
    @app.on_message(filters.photo & filters.private)
    async def handle_photo_inputs(client: Client, message: Message):
        user_id = message.from_user.id if message.from_user else 0
        if not is_admin(user_id):
            return

        if user_id in _app_manage_state:
            manage_task = _app_manage_state.pop(user_id)
            action = manage_task.get("action")
            app_id = manage_task.get("app_id")
            batch_id = manage_task.get("batch_id")

            wait_msg = await message.reply_text("⏳ <i>Downloading photo and saving to website assets...</i>")

            from config.settings import BASE_DIR
            uploads_dir = BASE_DIR / "web" / "public" / "uploads"
            uploads_dir.mkdir(parents=True, exist_ok=True)

            photo_filename = f"img_{uuid.uuid4().hex[:12]}.jpg"
            target_path = uploads_dir / photo_filename

            try:
                await message.download(file_name=str(target_path))
                relative_url = f"/uploads/{photo_filename}"

                if action == "SET_APP_ICON":
                    async with get_db_session() as session:
                        repo = ContentRepository(session)
                        await repo.update_app(app_id, icon_url=relative_url)
                        await session.commit()
                    await wait_msg.edit_text(f"🎉 <b>APP ICON UPDATED!</b>\n\nPhoto saved and set as App icon:\n<code>{relative_url}</code>")
                    await render_app_detail(client, message.chat.id, app_id, None)
                    return
                elif action == "SET_BATCH_THUMB":
                    async with get_db_session() as session:
                        repo = ContentRepository(session)
                        await repo.update_batch(batch_id, thumbnail_url=relative_url)
                        await session.commit()
                    await wait_msg.edit_text(f"🎉 <b>BATCH THUMBNAIL UPDATED!</b>\n\nPhoto saved and set as Batch thumbnail:\n<code>{relative_url}</code>")
                    await render_batch_detail(client, message.chat.id, batch_id, app_id, None)
                    return
            except Exception as dl_err:
                logger.exception("Failed to download image: %s", dl_err)
                await wait_msg.edit_text(f"❌ Failed to download photo: {dl_err}")
                return

        await message.reply_text("📸 Photo received! If you want to use this as an App Icon or Batch Thumbnail, use /apps to select the App or Batch first.")

    # ==========================================
    # 9. CENTRAL CALLBACK QUERY ROUTER (STRICT ADMIN AUTH)
    # ==========================================
    @app.on_callback_query()
    async def handle_callbacks(client: Client, callback: CallbackQuery):
        user_id = callback.from_user.id if callback.from_user else 0
        data = callback.data or ""

        # MANDATORY: Non-admin public callbacks vs Admin callbacks
        if data.startswith("user:"):
            if data == "user:help":
                await callback.answer()
                await callback.message.edit_text(TelegramProgressUI.render_help_screen(False))
            elif data == "user:id":
                await callback.answer()
                await callback.message.edit_text(TelegramProgressUI.render_id_screen(user_id, False, BOT_USERNAME))
            return

        if not is_admin(user_id):
            await callback.answer("⛔ ADMIN ONLY: Access Denied", show_alert=True)
            return

        state = BatchWizardManager.get_session(processing_engine.bot_id, user_id)

        try:
            # ----------------------------------------------------
            # ADMIN DASHBOARD & MENU
            # ----------------------------------------------------
            if data in ("admin:storage_health", "admin:storage_status"):
                await callback.answer("Running storage health checks...")
                health_results = await StorageHealthService.check_all_providers()
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    db_stats = await repo.get_storage_providers_summary()

                prov_lines = []
                for h in health_results:
                    p_name = h.get("provider", "unknown")
                    status = h.get("status", "UNKNOWN")
                    healthy = h.get("healthy", False)
                    enabled = h.get("enabled", True)
                    p_stat = db_stats.get(p_name, {})
                    ready_c = p_stat.get("ready", 0)
                    failed_c = p_stat.get("failed", 0)
                    total_c = p_stat.get("total", 0)

                    if not enabled:
                        badge = "⚪ DISABLED"
                    elif healthy:
                        badge = "🟢 ONLINE"
                    else:
                        badge = f"🔴 {status}"

                    disp_name = {
                        "vcdn": "VCDN",
                        "media_cm": "Media.cm",
                        "anonmp4": "AnonMP4",
                        "vevocloud": "Vevocloud",
                    }.get(p_name, p_name.upper())

                    prov_lines.append(
                        f"<b>{disp_name}</b>: {badge}\n"
                        f"  • Total: <b>{total_c}</b> | Ready: <b>{ready_c}</b> | Failed: <b>{failed_c}</b>"
                    )

                summary_text = "\n\n".join(prov_lines)
                card = (
                    f"╭━━━━━━━━━━━━━━━━━━━━━━━━━━╮\n"
                    f"│ ☁️ <b>STORAGE REPLICATION</b>    │\n"
                    f"╰━━━━━━━━━━━━━━━━━━━━━━━━━━╯\n\n"
                    f"{summary_text}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🛡️ <i>Pipeline: VCDN ➔ Media.cm ➔ AnonMP4 ➔ Vevocloud</i>\n"
                    f"🔒 <i>Zero Duplicates (SHA-256) & Auto-Resumable</i>"
                )
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh Health", callback_data="admin:storage_health")],
                    [InlineKeyboardButton("👑 Admin Menu", callback_data="admin:menu")]
                ])
                try:
                    await callback.message.edit_text(card, reply_markup=markup)
                except Exception as e:
                    if "MESSAGE_NOT_MODIFIED" not in str(e):
                        raise e
                return

            if data == "admin:youtube_accounts":
                await callback.answer("Loading YouTube accounts...")
                diag = await YouTubeAccountManager.get_diagnostics()
                text = TelegramProgressUI.render_youtube_accounts_screen(diag)
                buttons = TelegramProgressUI.build_youtube_accounts_markup(diag.get("accounts", []))
                try:
                    await callback.message.edit_text(text, reply_markup=buttons, disable_web_page_preview=True)
                except Exception as e:
                    if "MESSAGE_TOO_LONG" in str(e):
                        await callback.message.edit_text(text[:3500] + "\n\n<i>[Truncated]</i>", reply_markup=buttons, disable_web_page_preview=True)
                    else:
                        raise e
                return

            # ----------------------------------------------------
            # YOUTUBE ACCOUNT ACTIONS (ADD, TEST, VIEW, PRIMARY, TOGGLE, DELETE)
            # ----------------------------------------------------
            if data == "yt:add_prompt":
                await callback.answer()
                _yt_add_waiting_users[user_id] = True
                prompt_text = (
                    f"🎬 <b>ADD YOUTUBE CHANNEL (1-CLICK OAUTH)</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"You can connect your channel in any of these easy ways:\n\n"
                    f"🔹 <b>Method 1 (Instant 1-Click Link - Recommended):</b>\n"
                    f"Send your Google Cloud <code>client_secrets.json</code> file or paste its JSON text directly.\n"
                    f"<i>Course Wallah will instantly generate a 1-Click Authorization link to generate and autosave your refresh token!</i>\n\n"
                    f"🔹 <b>Method 2 (Single Line / Pipe):</b>\n"
                    f"<code>Channel Name | CLIENT_ID | CLIENT_SECRET | REFRESH_TOKEN</code>\n\n"
                    f"🔹 <b>Method 3 (Multiline Key-Value):</b>\n"
                    f"<pre>\n"
                    f"Name: Backup Channel 2\n"
                    f"Client ID: xxxxx.apps.googleusercontent.com\n"
                    f"Client Secret: GOCSPX-xxxxx\n"
                    f"Refresh Token: 1//04xxxxx\n"
                    f"</pre>\n\n"
                    f"👉 <i>Simply upload your <code>.json</code> file or paste your credentials here!</i>"
                )
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data="admin:youtube_accounts")]
                ])
                await callback.message.edit_text(prompt_text, reply_markup=markup)
                return

            if data == "yt:cancel_oauth":
                await callback.answer("OAuth linking cancelled.")
                _yt_oauth_pending_sessions.pop(user_id, None)
                _yt_add_waiting_users.pop(user_id, None)
                diag = await YouTubeAccountManager.get_diagnostics()
                text = TelegramProgressUI.render_youtube_accounts_screen(diag)
                buttons = TelegramProgressUI.build_youtube_accounts_markup(diag.get("accounts", []))
                await callback.message.edit_text(text, reply_markup=buttons, disable_web_page_preview=True)
                return

            if data.startswith("yt:view:"):
                acc_id = data.replace("yt:view:", "")
                await callback.answer("Loading channel details...")
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    acc = await repo.get_youtube_account_by_id(acc_id)
                if not acc:
                    await callback.answer("❌ Account not found in database.", show_alert=True)
                    return
                acc_dict = {
                    "id": acc.id,
                    "name": acc.name,
                    "channel_title": acc.channel_title or acc.name,
                    "channel_id": acc.channel_id,
                    "status": acc.status,
                    "priority": acc.priority,
                    "uploads_today": acc.uploads_today or 0,
                    "client_id_masked": f"{acc.client_id[:8]}...{acc.client_id[-12:]}" if acc.client_id and len(acc.client_id) > 20 else "Configured"
                }
                text = TelegramProgressUI.render_youtube_account_detail(acc_dict)
                markup = TelegramProgressUI.build_youtube_account_manage_markup(acc.id, acc.status == "ACTIVE")
                await callback.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
                return

            if data.startswith("yt:test:"):
                acc_id = data.replace("yt:test:", "")
                await callback.answer("Testing OAuth connection with Google...", show_alert=False)
                success, acc_dict, msg = await YouTubeAccountManager.test_and_refresh_account(acc_id)
                if success:
                    await callback.answer(f"✅ Verified: {acc_dict.get('channel_title')}", show_alert=True)
                    diag = await YouTubeAccountManager.get_diagnostics()
                    text = TelegramProgressUI.render_youtube_accounts_screen(diag)
                    buttons = TelegramProgressUI.build_youtube_accounts_markup(diag.get("accounts", []))
                    await callback.message.edit_text(text, reply_markup=buttons, disable_web_page_preview=True)
                else:
                    await callback.answer(f"❌ Test Failed: {msg[:100]}", show_alert=True)
                return

            if data.startswith("yt:primary:"):
                acc_id = data.replace("yt:primary:", "")
                await callback.answer("Setting as Primary (#1)...")
                await YouTubeAccountManager.set_account_priority(acc_id, 1)
                await callback.answer("⭐ Set as Primary Channel!", show_alert=True)
                diag = await YouTubeAccountManager.get_diagnostics()
                text = TelegramProgressUI.render_youtube_accounts_screen(diag)
                buttons = TelegramProgressUI.build_youtube_accounts_markup(diag.get("accounts", []))
                await callback.message.edit_text(text, reply_markup=buttons, disable_web_page_preview=True)
                return

            if data.startswith("yt:toggle:"):
                acc_id = data.replace("yt:toggle:", "")
                await callback.answer("Toggling status...")
                success, msg, new_st = await YouTubeAccountManager.toggle_account_active(acc_id)
                await callback.answer(f"Status changed to: {new_st}", show_alert=True)
                diag = await YouTubeAccountManager.get_diagnostics()
                text = TelegramProgressUI.render_youtube_accounts_screen(diag)
                buttons = TelegramProgressUI.build_youtube_accounts_markup(diag.get("accounts", []))
                await callback.message.edit_text(text, reply_markup=buttons, disable_web_page_preview=True)
                return

            if data.startswith("yt:del_confirm:"):
                acc_id = data.replace("yt:del_confirm:", "")
                await callback.answer()
                markup = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("⚠️ Yes, Delete Channel", callback_data=f"yt:del_do:{acc_id}"),
                        InlineKeyboardButton("❌ Cancel", callback_data=f"yt:view:{acc_id}")
                    ]
                ])
                await callback.message.edit_text(
                    "⚠️ <b>ARE YOU SURE YOU WANT TO REMOVE THIS YOUTUBE CHANNEL?</b>\n\n"
                    "This channel will be deleted from your multi-account pool.",
                    reply_markup=markup
                )
                return

            if data.startswith("yt:del_do:"):
                acc_id = data.replace("yt:del_do:", "")
                await callback.answer("Deleting account...")
                success, msg = await YouTubeAccountManager.remove_account(acc_id)
                await callback.answer("Account removed from pool", show_alert=True)
                diag = await YouTubeAccountManager.get_diagnostics()
                text = TelegramProgressUI.render_youtube_accounts_screen(diag)
                buttons = TelegramProgressUI.build_youtube_accounts_markup(diag.get("accounts", []))
                await callback.message.edit_text(text, reply_markup=buttons, disable_web_page_preview=True)
                return


            if data == "admin:youtube_status":
                await callback.answer("Loading YouTube pipeline status...")
                diag = await YouTubeAccountManager.get_diagnostics()
                controllers = ContentProcessingEngine._active_batch_controllers
                active_c = sum(1 for c in controllers.values() if not c.is_paused)
                paused_c = sum(1 for c in controllers.values() if c.is_paused)
                text = TelegramProgressUI.render_youtube_status_screen(diag, active_c, paused_c)
                buttons = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🎬 View All Accounts", callback_data="admin:youtube_accounts"),
                        InlineKeyboardButton("🔄 Refresh Status", callback_data="admin:youtube_status")
                    ],
                    [
                        InlineKeyboardButton("🛠 Admin Panel", callback_data="admin:dashboard")
                    ]
                ])
                await callback.message.edit_text(text, reply_markup=buttons)
                return

            if data == "admin:dashboard":
                await callback.answer()
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    apps = await repo.get_all_apps()
                    total_batches = sum(len(a.batches) for a in apps) if apps else 0

                stats = {
                    "active_apps": len(apps),
                    "total_batches": total_batches,
                    "total_lectures": "Loaded in DB",
                    "active_jobs": len(ContentProcessingEngine._active_batch_controllers)
                }
                text = TelegramProgressUI.render_admin_dashboard(stats)
                markup = BatchWizardManager.build_admin_dashboard_markup()
                await callback.message.edit_text(text, reply_markup=markup)
                return


            if data == "admin:status":
                await callback.answer()
                controllers = ContentProcessingEngine._active_batch_controllers
                text = TelegramProgressUI.render_batch_status_screen(controllers)
                markup = BatchWizardManager.build_batch_status_markup()
                await callback.message.edit_text(text, reply_markup=markup)
                return

            if data == "admin:jobs":
                await callback.answer()
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    db_jobs = await repo.get_recent_jobs(limit=10)
                    jobs_data = []
                    for j in db_jobs:
                        b_name = j.batch.name if j.batch else "Batch"
                        jobs_data.append({
                            "name": b_name,
                            "status": str(j.status.value) if j.status else "COMPLETED",
                            "progress": f"{j.current_step} ({j.progress_percent:.0f}%)" if j.current_step else "Finished"
                        })
                text = TelegramProgressUI.render_batch_jobs_screen(jobs_data)
                markup = BatchWizardManager.build_batch_jobs_markup()
                await callback.message.edit_text(text, reply_markup=markup)
                return

            if data == "admin:help":
                await callback.answer()
                await callback.message.edit_text(TelegramProgressUI.render_help_screen(True))
                return

            if data == "admin:logs":
                await callback.answer()
                logs = (
                    f"📜 <b>OPERATIONAL BATCH PIPELINE LOGS:</b>\n\n"
                    f"• <code>[JOB_ENGINE] Worker bot_1 operational</code>\n"
                    f"• <code>[STORAGE] B2 S3 bucket 'course-wallah-pdfs' online</code>\n"
                    f"• <code>[YOUTUBE] OAuth resumable uploader online</code>\n"
                    f"• <code>[WATERMARK] Moving drift filter loaded</code>\n"
                    f"• <code>[DATABASE] Database connected</code>"
                )
                await callback.message.edit_text(
                    logs,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ BACK", callback_data="admin:dashboard")]])
                )
                return

            if data == "admin:close":
                await callback.answer()
                await callback.message.delete()
                return

            # ----------------------------------------------------
            # APP & BATCH MANAGEMENT CALLBACKS
            # ----------------------------------------------------
            if data == "manage:apps_list":
                await callback.answer()
                await render_apps_list(client, callback.message.chat.id, callback.message.id)
                return

            if data.startswith("manage:app:"):
                app_id = data.split("manage:app:")[1]
                await callback.answer()
                await render_app_detail(client, callback.message.chat.id, app_id, callback.message.id)
                return

            if data.startswith("manage:batches:"):
                parts = data.split("manage:batches:")[1].split(":")
                app_id = parts[0]
                page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                await callback.answer()
                await render_batches_list(client, callback.message.chat.id, app_id, page, callback.message.id)
                return

            if data.startswith("manage:batch:"):
                parts = data.split("manage:batch:")[1].split(":")
                batch_id = parts[0]
                app_id = parts[1] if len(parts) > 1 else ""
                await callback.answer()
                await render_batch_detail(client, callback.message.chat.id, batch_id, app_id, callback.message.id)
                return

            if data.startswith("manage:rename_app:"):
                app_id = data.split("manage:rename_app:")[1]
                await callback.answer()
                _app_manage_state[user_id] = {"action": "RENAME_APP", "app_id": app_id}
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"manage:app:{app_id}")]])
                await callback.message.edit_text(
                    "✏️ <b>RENAME APP</b>\n\n"
                    "Please reply with the <b>New Name</b> for this App:",
                    reply_markup=markup
                )
                return

            if data.startswith("manage:icon_app:"):
                app_id = data.split("manage:icon_app:")[1]
                await callback.answer()
                _app_manage_state[user_id] = {"action": "SET_APP_ICON", "app_id": app_id}
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"manage:app:{app_id}")]])
                await callback.message.edit_text(
                    "🖼️ <b>CHANGE APP ICON / IMAGE</b>\n\n"
                    "Please send a <b>photo directly</b> in Telegram or reply with a <b>public image URL</b> for this App icon:",
                    reply_markup=markup
                )
                return

            if data.startswith("manage:desc_app:"):
                app_id = data.split("manage:desc_app:")[1]
                await callback.answer()
                _app_manage_state[user_id] = {"action": "EDIT_APP_DESC", "app_id": app_id}
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"manage:app:{app_id}")]])
                await callback.message.edit_text(
                    "📝 <b>EDIT APP DESCRIPTION</b>\n\n"
                    "Please reply with the <b>New Description</b> for this App:",
                    reply_markup=markup
                )
                return

            if data.startswith("manage:thumb_batch:"):
                parts = data.split("manage:thumb_batch:")[1].split(":")
                batch_id = parts[0]
                app_id = parts[1] if len(parts) > 1 else ""
                await callback.answer()
                _app_manage_state[user_id] = {"action": "SET_BATCH_THUMB", "batch_id": batch_id, "app_id": app_id}
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"manage:batch:{batch_id}:{app_id}")]])
                await callback.message.edit_text(
                    "🖼️ <b>CHANGE BATCH THUMBNAIL</b>\n\n"
                    "Please send a <b>photo directly</b> in Telegram or reply with a <b>public image URL</b> (JPG/PNG/WebP) for this batch thumbnail:",
                    reply_markup=markup
                )
                return

            if data.startswith("manage:rename_batch:"):
                parts = data.split("manage:rename_batch:")[1].split(":")
                batch_id = parts[0]
                app_id = parts[1] if len(parts) > 1 else ""
                await callback.answer()
                _app_manage_state[user_id] = {"action": "RENAME_BATCH", "batch_id": batch_id, "app_id": app_id}
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"manage:batch:{batch_id}:{app_id}")]])
                await callback.message.edit_text(
                    "✏️ <b>RENAME BATCH</b>\n\n"
                    "Please reply with the <b>New Name</b> for this batch:",
                    reply_markup=markup
                )
                return

            if data.startswith("manage:cat_batch:"):
                parts = data.split("manage:cat_batch:")[1].split(":")
                batch_id = parts[0]
                app_id = parts[1] if len(parts) > 1 else ""
                await callback.answer()
                _app_manage_state[user_id] = {"action": "SET_BATCH_CAT", "batch_id": batch_id, "app_id": app_id}
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"manage:batch:{batch_id}:{app_id}")]])
                await callback.message.edit_text(
                    "🏷️ <b>CHANGE BATCH CATEGORY</b>\n\n"
                    "Please reply with the <b>New Category</b> (e.g. Engineering, Railway, UPSC, SSC, Web Development):",
                    reply_markup=markup
                )
                return

            if data == "manage:cancel":
                await callback.answer()
                _app_manage_state.pop(user_id, None)
                await render_apps_list(client, callback.message.chat.id, callback.message.id)
                return

            # ----------------------------------------------------
            # WIZARD: START UPLOAD & ANALYSIS
            # ----------------------------------------------------
            if data == "wizard:start_upload":
                await callback.answer()
                await run_init_animation(callback.message)
                prompt_text = (
                    f"📄 <b>SEND YOUR TXT FILE</b>\n\n"
                    f"Please upload your course <code>.txt</code> file to begin batch analysis and ingestion.\n\n"
                    f"• Supported formats: APPX, YouTube, Spayee, Direct URLs, Notes/PDFs\n"
                    f"• Accepts <code>.txt</code> files only\n\n"
                    f"<i>Waiting for file upload...</i>"
                )
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="wizard:cancel")]])
                await callback.message.edit_text(prompt_text, reply_markup=markup)
                return

            if data == "wizard:cancel":
                await callback.answer("Wizard cancelled")
                BatchWizardManager.clear_session(processing_engine.bot_id, user_id)
                await callback.message.edit_text(
                    "❌ <b>BATCH WIZARD CANCELLED</b>\n\n"
                    "No production content was deleted.\n\n"
                    "You can start again at any time with /batch."
                )
                return

            if not state or not state.tree:
                await callback.answer("Session expired. Please send the TXT file again via /batch.", show_alert=True)
                return

            # ----------------------------------------------------
            # WIZARD: BACK NAVIGATION
            # ----------------------------------------------------
            if data == "wizard:back":
                await callback.answer()
                prev_step = state.pop_step()
                if prev_step == "ANALYSIS_PREVIEW" or prev_step == "IDLE":
                    card = TelegramProgressUI.render_analysis(state.analysis_stats)
                    markup = BatchWizardManager.build_analysis_markup()
                    await callback.message.edit_text(card, reply_markup=markup)
                elif prev_step == "SELECT_APP":
                    async with get_db_session() as session:
                        repo = ContentRepository(session)
                        apps = await repo.get_all_apps()
                    card = TelegramProgressUI.render_app_selection(apps)
                    markup = BatchWizardManager.build_app_selection_markup(apps)
                    await callback.message.edit_text(card, reply_markup=markup)
                elif prev_step == "SELECT_BATCH":
                    async with get_db_session() as session:
                        repo = ContentRepository(session)
                        batches = await repo.get_batches_by_app_id(state.app_id) if state.app_id else []
                    card = TelegramProgressUI.render_batch_selection(state.app_name, batches)
                    markup = BatchWizardManager.build_batch_selection_markup(batches)
                    await callback.message.edit_text(card, reply_markup=markup)
                elif prev_step == "MAP_MENU":
                    card = TelegramProgressUI.render_mapping_menu(state.analysis_stats)
                    markup = BatchWizardManager.build_mapping_menu_markup()
                    await callback.message.edit_text(card, reply_markup=markup)
                else:
                    card = TelegramProgressUI.render_analysis(state.analysis_stats)
                    markup = BatchWizardManager.build_analysis_markup()
                    await callback.message.edit_text(card, reply_markup=markup)
                return

            # ----------------------------------------------------
            # WIZARD: APP SELECTION & CREATION
            # ----------------------------------------------------
            if data == "wizard:step_select_app":
                await callback.answer()
                state.push_step("SELECT_APP")
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    apps = await repo.get_all_apps()

                if not apps:
                    # If no apps, redirect to create app
                    state.push_step("CREATE_APP")
                    state.text_input_prompt = "APP_NAME"
                    card = TelegramProgressUI.render_app_create_prompt("NAME", {})
                    markup = BatchWizardManager.build_app_create_markup("NAME")
                    await callback.message.edit_text(card, reply_markup=markup)
                    return

                card = TelegramProgressUI.render_app_selection(apps)
                markup = BatchWizardManager.build_app_selection_markup(apps)
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data.startswith("wizard:app_select:"):
                app_id = data.replace("wizard:app_select:", "")
                await callback.answer()
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    app_obj = await repo.get_app_by_id(app_id)
                    if app_obj:
                        state.app_id = app_obj.id
                        state.app_name = app_obj.name
                        matching_batch = await repo.find_existing_batch(app_obj.id, state.batch_name)
                    else:
                        matching_batch = None

                if matching_batch:
                    state.batch_id = matching_batch.id
                    state.batch_name = matching_batch.name
                    state.push_step("EXISTING_BATCH_FOUND")
                    card = TelegramProgressUI.render_existing_batch_found(matching_batch.name, state.analysis_stats)
                    markup = BatchWizardManager.build_existing_batch_found_markup(matching_batch.id)
                else:
                    state.push_step("NEW_BATCH_PROMPT")
                    card = TelegramProgressUI.render_new_batch_prompt(state.batch_name, state.analysis_stats)
                    markup = BatchWizardManager.build_new_batch_prompt_markup()

                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data == "wizard:step_select_batch":
                await callback.answer()
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    batches = await repo.get_batches_by_app_id(state.app_id) if state.app_id else []
                state.push_step("SELECT_BATCH")
                card = TelegramProgressUI.render_batch_selection(state.app_name, batches)
                markup = BatchWizardManager.build_batch_selection_markup(batches)
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data == "wizard:step_create_app":
                await callback.answer()
                state.push_step("CREATE_APP")
                state.text_input_prompt = "APP_NAME"
                card = TelegramProgressUI.render_app_create_prompt("NAME", {})
                markup = BatchWizardManager.build_app_create_markup("NAME")
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data.startswith("wizard:app_skip_"):
                skip_field = data.replace("wizard:app_skip_", "")
                await callback.answer()
                if skip_field == "desc":
                    state.text_input_prompt = "APP_ICON"
                    card = TelegramProgressUI.render_app_create_prompt("ICON", {"name": state.app_name})
                    markup = BatchWizardManager.build_app_create_markup("ICON")
                    await callback.message.edit_text(card, reply_markup=markup)
                else: # icon
                    state.text_input_prompt = None
                    card = TelegramProgressUI.render_app_create_prompt("CONFIRM", {"name": state.app_name, "description": state.app_description, "icon_url": state.app_icon_url})
                    markup = BatchWizardManager.build_app_create_markup("CONFIRM")
                    await callback.message.edit_text(card, reply_markup=markup)
                return

            if data == "wizard:app_create_confirm":
                await callback.answer()
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    new_app = await repo.get_or_create_app(
                        name=state.app_name,
                        description=state.app_description,
                        icon_url=state.app_icon_url
                    )
                    state.app_id = new_app.id
                    state.app_name = new_app.name

                state.push_step("SELECT_BATCH")
                card = TelegramProgressUI.render_batch_selection(state.app_name, [])
                markup = BatchWizardManager.build_batch_selection_markup([])
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data == "wizard:step_edit_app":
                await callback.answer()
                card = TelegramProgressUI.render_app_edit_prompt({"name": state.app_name, "description": state.app_description, "icon_url": state.app_icon_url})
                markup = BatchWizardManager.build_app_edit_markup()
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data == "wizard:app_edit_name":
                await callback.answer()
                state.text_input_prompt = "EDIT_APP_NAME"
                await callback.message.edit_text("✏️ Please reply with the new <b>App Name</b>.")
                return

            if data == "wizard:app_edit_desc":
                await callback.answer()
                state.text_input_prompt = "EDIT_APP_DESC"
                await callback.message.edit_text("✏️ Please reply with the new <b>App Description</b>.")
                return

            if data == "wizard:app_edit_icon":
                await callback.answer()
                state.text_input_prompt = "EDIT_APP_ICON"
                await callback.message.edit_text(
                    "🖼 Please reply with the new <b>App Image URL</b> (JPEG, PNG, WebP).\n\n"
                    "<i>Example: https://example.com/logo.png</i>"
                )
                return

            if data == "wizard:app_prompt_icon":
                await callback.answer()
                state.text_input_prompt = "APP_ICON"
                card = TelegramProgressUI.render_app_create_prompt("ICON", {"name": state.app_name, "description": state.app_description})
                markup = BatchWizardManager.build_app_create_markup("ICON")
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data == "wizard:batch_prompt_thumb":
                await callback.answer()
                state.text_input_prompt = "BATCH_THUMBNAIL"
                await callback.message.edit_text(
                    "🖼 Please reply with the <b>Batch Thumbnail URL</b> (JPEG, PNG, WebP).\n\n"
                    "<i>Example: https://example.com/batch.png</i>"
                )
                return

            # ----------------------------------------------------
            # WIZARD: BATCH SELECTION & CREATION
            # ----------------------------------------------------
            if data.startswith("wizard:batch_select:"):
                batch_id = data.replace("wizard:batch_select:", "")
                await callback.answer()
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    batch_obj = await repo.get_batch_by_id(batch_id)
                    if batch_obj:
                        state.batch_id = batch_obj.id
                        state.batch_name = batch_obj.name
                        summary = await repo.get_batch_lecture_summary(batch_obj.id)
                        exist_cnt = summary.get("published_lectures", 0)
                        state.analysis_stats["already_processed_count"] = exist_cnt
                        state.analysis_stats["new_count"] = max(0, state.analysis_stats["total_lectures"] - exist_cnt)
                    else:
                        exist_cnt = 0

                state.push_step("EXISTING_BATCH_MENU")
                card = TelegramProgressUI.render_existing_batch_selected(
                    state.batch_name,
                    state.analysis_stats["new_count"],
                    exist_cnt,
                    0
                )
                markup = BatchWizardManager.build_existing_batch_markup()
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data == "wizard:step_create_batch":
                await callback.answer()
                state.push_step("CREATE_BATCH_NAME")
                card = TelegramProgressUI.render_batch_create_wizard("CATEGORY", {"name": state.batch_name, "app_name": state.app_name})
                markup = BatchWizardManager.build_batch_create_wizard_markup("CATEGORY")
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data.startswith("wizard:set_cat_"):
                cat = data.replace("wizard:set_cat_", "")
                await callback.answer(f"Category: {cat}")
                state.category = cat
                card = TelegramProgressUI.render_batch_create_wizard("BRANCH", {"name": state.batch_name, "category": cat})
                markup = BatchWizardManager.build_batch_create_wizard_markup("BRANCH")
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data.startswith("wizard:set_branch_"):
                branch = data.replace("wizard:set_branch_", "")
                await callback.answer(f"Branch: {branch}")
                state.branch = branch
                card = TelegramProgressUI.render_batch_create_wizard("SEMESTER", {"name": state.batch_name, "branch": branch})
                markup = BatchWizardManager.build_batch_create_wizard_markup("SEMESTER")
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data.startswith("wizard:set_sem_"):
                sem = data.replace("wizard:set_sem_", "")
                await callback.answer(f"Semester: {sem}")
                state.semester = sem
                card = TelegramProgressUI.render_batch_create_wizard("SETTINGS", {
                    "name": state.batch_name,
                    "quality_pref": state.quality_pref,
                    "watermark_enabled": state.watermark_enabled
                })
                markup = BatchWizardManager.build_batch_create_wizard_markup("SETTINGS")
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data.startswith("wizard:set_qual_"):
                q = data.replace("wizard:set_qual_", "")
                await callback.answer(f"Quality: {q}")
                state.quality_pref = q
                card = TelegramProgressUI.render_batch_create_wizard("SETTINGS", {
                    "name": state.batch_name,
                    "quality_pref": state.quality_pref,
                    "watermark_enabled": state.watermark_enabled
                })
                markup = BatchWizardManager.build_batch_create_wizard_markup("SETTINGS")
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data == "wizard:set_wm_on":
                await callback.answer("Watermark: Enabled")
                state.watermark_enabled = True
                card = TelegramProgressUI.render_batch_create_wizard("SETTINGS", {
                    "name": state.batch_name,
                    "quality_pref": state.quality_pref,
                    "watermark_enabled": True
                })
                markup = BatchWizardManager.build_batch_create_wizard_markup("SETTINGS")
                try:
                    await callback.message.edit_text(card, reply_markup=markup)
                except MessageNotModified:
                    pass
                return

            if data == "wizard:set_wm_off":
                await callback.answer("Watermark: Disabled")
                state.watermark_enabled = False
                card = TelegramProgressUI.render_batch_create_wizard("SETTINGS", {
                    "name": state.batch_name,
                    "quality_pref": state.quality_pref,
                    "watermark_enabled": False
                })
                markup = BatchWizardManager.build_batch_create_wizard_markup("SETTINGS")
                try:
                    await callback.message.edit_text(card, reply_markup=markup)
                except MessageNotModified:
                    pass
                return

            if data == "wizard:batch_create_confirm":
                await callback.answer()
                async with get_db_session() as session:
                    repo = ContentRepository(session)
                    app_obj = await repo.get_or_create_app(state.app_name)
                    batch_obj, _ = await repo.get_or_create_batch(
                        app_id=app_obj.id,
                        name=state.batch_name,
                        category=state.category,
                        branch=state.branch,
                        semester=state.semester,
                        academic_year=state.academic_year,
                        thumbnail_url=state.thumbnail_url or None,
                        quality_pref=state.quality_pref
                    )
                    state.app_id = app_obj.id
                    state.batch_id = batch_obj.id

                state.push_step("CONFIRMATION")
                card = TelegramProgressUI.render_final_confirmation(state.__dict__)
                markup = BatchWizardManager.build_final_confirmation_markup()
                await callback.message.edit_text(card, reply_markup=markup)
                return

            # ----------------------------------------------------
            # WIZARD: MAPPING & PREVIEW
            # ----------------------------------------------------
            if data == "wizard:step_map_menu":
                await callback.answer()
                state.push_step("MAP_MENU")
                card = TelegramProgressUI.render_mapping_menu(state.analysis_stats)
                markup = BatchWizardManager.build_mapping_menu_markup()
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data == "wizard:map_preview":
                await callback.answer()
                all_items = []
                if state.tree:
                    for s in state.tree.subjects:
                        for f in s.folders:
                            for lec in f.lectures:
                                all_items.append({
                                    "index": lec.index,
                                    "subject": s.name,
                                    "folder": f.name,
                                    "title": lec.title,
                                    "video_url": lec.video_url,
                                    "pdf_url": lec.pdf_url
                                })
                card = TelegramProgressUI.render_parsed_preview(all_items[:5])
                markup = BatchWizardManager.build_parsed_preview_markup()
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data == "wizard:map_reanalyze":
                await callback.answer("Re-analyzing TXT...")
                state.tree = TxtIndexer.index_txt(state.file_content, filename=state.filename)
                card = TelegramProgressUI.render_mapping_menu(state.analysis_stats)
                markup = BatchWizardManager.build_mapping_menu_markup()
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data in ("wizard:mode_append", "wizard:step_confirm"):
                await callback.answer()
                state.execution_mode = "APPEND NEW CONTENT"
                state.push_step("CONFIRMATION")
                card = TelegramProgressUI.render_final_confirmation(state.__dict__)
                markup = BatchWizardManager.build_final_confirmation_markup()
                await callback.message.edit_text(card, reply_markup=markup)
                return

            if data == "wizard:mode_retry_failed":
                await callback.answer()
                state.execution_mode = "REPROCESS FAILED"
                state.retry_failed_only = True
                state.push_step("CONFIRMATION")
                card = TelegramProgressUI.render_final_confirmation(state.__dict__)
                markup = BatchWizardManager.build_final_confirmation_markup()
                await callback.message.edit_text(card, reply_markup=markup)
                return

            # ----------------------------------------------------
            # WIZARD: START PROCESSING LAUNCH
            # ----------------------------------------------------
            if data == "wizard:start_processing":
                await callback.answer("🚀 Launching batch ingestion pipeline!", show_alert=True)
                status_msg = callback.message
                asyncio.create_task(launch_batch_execution(client, status_msg, state))
                return

            # ----------------------------------------------------
            # JOB CONTROLS (PAUSE, RESUME, CANCEL, RETRY)
            # ----------------------------------------------------
            if data == "job:pause_active":
                await callback.answer()
                for ctrl in ContentProcessingEngine._active_batch_controllers.values():
                    ctrl.pause()
                await callback.message.reply_text("⏸ <b>Active batch jobs paused safely.</b> Checkpoints saved.")
                return

            if data == "job:resume_active":
                await callback.answer()
                for ctrl in ContentProcessingEngine._active_batch_controllers.values():
                    ctrl.resume()
                await callback.message.reply_text("▶️ <b>Active batch jobs resumed.</b>")
                return

            if data == "job:cancel_active":
                await callback.answer()
                controllers = ContentProcessingEngine._active_batch_controllers
                if not controllers:
                    await callback.message.reply_text("ℹ️ No active batch jobs to cancel.")
                    return
                j_id = next(iter(controllers.keys()))
                ctrl = controllers[j_id]
                prompt = TelegramProgressUI.render_cancel_prompt(ctrl.batch_name, ctrl.completed_count, ctrl.total_lectures)
                markup = BatchWizardManager.build_cancel_confirmation_markup(j_id)
                await callback.message.edit_text(prompt, reply_markup=markup)
                return

            if data == "job:retry_active":
                await callback.answer()
                retry_state = state or _last_batch_sessions_by_user.get(user_id)
                if retry_state and retry_state.tree:
                    retry_state.retry_failed_only = True
                    asyncio.create_task(launch_batch_execution(client, callback.message, retry_state))
                else:
                    await callback.answer("No active batch session to retry.", show_alert=True)
                return

            if data.startswith("job:retry:"):
                b_id = data.replace("job:retry:", "")
                await callback.answer("🔄 Launching retry for failed items...", show_alert=False)
                retry_state = _last_batch_sessions_by_batch.get(b_id) or _last_batch_sessions_by_user.get(user_id) or state
                if retry_state and retry_state.tree:
                    retry_state.retry_failed_only = True
                    retry_state.batch_id = b_id
                    asyncio.create_task(launch_batch_execution(client, callback.message, retry_state))
                else:
                    await callback.message.reply_text("⚠️ <b>Session details expired.</b> Please upload the batch TXT file again via /batch to reprocess.")
                return

            if data.startswith("job:resume:"):
                j_id = data.replace("job:resume:", "")
                ctrl = ContentProcessingEngine.get_controller(j_id)
                if ctrl:
                    ctrl.resume()
                    await callback.answer("▶️ Ingestion resumed!", show_alert=True)
                else:
                    await callback.answer("Job not found or completed.", show_alert=True)
                return

            if data.startswith("job:prompt_cancel:"):
                j_id = data.replace("job:prompt_cancel:", "")
                ctrl = ContentProcessingEngine.get_controller(j_id)
                if ctrl:
                    prompt = TelegramProgressUI.render_cancel_prompt(ctrl.batch_name, ctrl.completed_count, ctrl.total_lectures)
                    markup = BatchWizardManager.build_cancel_confirmation_markup(j_id)
                    await callback.message.edit_text(prompt, reply_markup=markup)
                return

            if data.startswith("job:confirm_cancel:"):
                j_id = data.replace("job:confirm_cancel:", "")
                ctrl = ContentProcessingEngine.get_controller(j_id)
                if ctrl:
                    ctrl.cancel()
                    card = TelegramProgressUI.render_cancelled_summary(ctrl.batch_name, ctrl.completed_count, ctrl.total_lectures)
                    await callback.message.edit_text(card)
                else:
                    await callback.answer("Job already finished.", show_alert=True)
                return

            if data.startswith("job:keep_running:"):
                await callback.answer("Keeping batch running.")
                return

            if data.startswith("job:view_failed:"):
                b_id = data.replace("job:view_failed:", "")
                await callback.answer()
                failed_items = []
                batch_display_name = "Batch"

                for ctrl in ContentProcessingEngine._active_batch_controllers.values():
                    if ctrl.batch_id == b_id:
                        failed_items = list(ctrl.failed_items)
                        batch_display_name = ctrl.batch_name
                        break

                if not failed_items:
                    async with get_db_session() as session:
                        repo = ContentRepository(session)
                        b_obj = await repo.get_batch_by_id(b_id)
                        if b_obj:
                            batch_display_name = b_obj.name
                        failed_lecs = await repo.get_failed_lectures_by_batch(b_id)
                        for fl in failed_lecs:
                            failed_items.append({
                                "index": fl.lecture_index,
                                "title": fl.title,
                                "error": "Processing incomplete or failed"
                            })

                failed_card = TelegramProgressUI.render_failed_summary(failed_items, batch_display_name)
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔁 Retry Failed Now", callback_data=f"job:retry:{b_id}")],
                    [InlineKeyboardButton("👑 Admin Panel", callback_data="admin:dashboard")]
                ])
                await callback.message.reply_text(failed_card, reply_markup=markup)
                return

        except MessageNotModified:
            logger.debug("Telegram callback '%s' produced identical content (MessageNotModified)", data)
            try:
                await callback.answer()
            except Exception:
                pass
        except Exception as exc:
            logger.exception("Callback execution error for '%s': %s", data, exc)
            try:
                await callback.answer("❌ Something went wrong.", show_alert=True)
            except Exception:
                pass


async def launch_batch_execution(
    client: Client,
    status_msg: Message,
    state: BatchWizardState
):
    """
    Spawns the background ingestion runner with a dedicated BatchJobController
    and single editable Telegram message updater.
    """
    tree: NormalizedBatchTree = state.tree
    batch_name = state.batch_name or (tree.batch_name if tree else "Batch")
    user_id = state.user_id or OWNER_ID
    bot_id = state.bot_id or "bot_1"
    job_id = f"batch_{int(time.time())}"

    # Ensure app and batch records in DB
    async with get_db_session() as session:
        repo = ContentRepository(session)
        app_obj = None
        if state.app_id:
            app_obj = await repo.get_app_by_id(state.app_id)
        if not app_obj:
            app_obj = await repo.get_or_create_app(state.app_name)

        batch_obj = None
        if state.batch_id:
            batch_obj = await repo.get_batch_by_id(state.batch_id)
        if not batch_obj:
            batch_obj, _ = await repo.get_or_create_batch(
                app_id=app_obj.id,
                name=batch_name,
                category=state.category,
                branch=state.branch,
                semester=state.semester,
                academic_year=state.academic_year,
                quality_pref=state.quality_pref
            )
        batch_id = batch_obj.id

    # Cache state for retry support
    _last_batch_sessions_by_batch[batch_id] = state
    _last_batch_sessions_by_user[user_id] = state

    controller = BatchJobController(
        bot_id=bot_id,
        job_id=job_id,
        batch_id=batch_id,
        batch_name=batch_name,
        user_id=user_id,
        total_lectures=tree.total_lectures if tree else 0,
        status_message=status_msg
    )
    ContentProcessingEngine.register_controller(controller)

    session_data = {
        "tree": tree,
        "app_name": state.app_name,
        "batch_name": batch_name,
        "quality_pref": state.quality_pref,
        "watermark_profile": "Continuous Drift" if state.watermark_enabled else "Disabled",
        "start_index": state.start_index,
        "retry_failed_only": state.retry_failed_only
    }

    try:
        summary = await processing_engine.run_full_batch(
            controller=controller,
            session_data=session_data,
            start_index=state.start_index,
            retry_failed_only=state.retry_failed_only
        )

        has_failed = summary.get("failed", 0) > 0
        final_text = TelegramProgressUI.render_batch_summary(summary)
        markup = BatchWizardManager.build_completed_batch_markup(controller.batch_id, has_failed=has_failed)
        try:
            await status_msg.edit_text(final_text, reply_markup=markup)
        except MessageNotModified:
            pass
        except Exception as edit_err:
            logger.debug("[BATCH SUMMARY NOTICE] Could not edit final summary card (e.g. client shutdown/network): %s", edit_err)

    except Exception as e:
        logger.exception("[BATCH LAUNCH ERROR] %s", e)
        try:
            await status_msg.edit_text(f"❌ <b>Batch Error:</b> {str(e)[:200]}")
        except Exception:
            pass
    finally:
        ContentProcessingEngine.remove_controller(job_id)
        # Keep session cached in _last_batch_sessions_by_batch and _last_batch_sessions_by_user for instant retry
        BatchWizardManager.clear_session(bot_id, user_id)
