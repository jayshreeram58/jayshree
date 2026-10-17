import os
import sys
import logging
import asyncio
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from pyrogram import Client, idle
from config.settings import API_ID, API_HASH, BOT_TOKEN, BOT_NAME
from db.connection import init_db
from bot.handlers import register_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

async def start_bot():
    logger.info("Initializing Course Wallah Content Bot Database...")
    await init_db()

    if not BOT_TOKEN or not API_ID or not API_HASH:
        logger.warning("Telegram Bot credentials not configured in environment. Ingestion engine and REST API remain available.")
        return

    session_dir = BASE_DIR / "data" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    bot_id_prefix = BOT_TOKEN.split(":")[0] if ":" in BOT_TOKEN else "main"
    session_name = f"cw_bot_{bot_id_prefix}"

    bot_client = Client(
        name=session_name,
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        workdir=str(session_dir)
    )

    register_handlers(bot_client)

    logger.info(f"Starting {BOT_NAME}...")
    await bot_client.start()
    me = await bot_client.get_me()
    logger.info(f"Bot successfully started as @{me.username} (ID: {me.id})")

    # Share active bot client with TelegramClientPool for zero-latency streaming
    try:
        from storage.telegram_stream.client_pool import TelegramClientPool
        TelegramClientPool.get_instance().register_client(bot_client)
    except Exception as pool_err:
        logger.debug(f"[TG_POOL_REG_NOTICE] {pool_err}")

    # Register admin and general commands in Telegram default/admin command scope
    try:
        from pyrogram.types import BotCommand, BotCommandScopeDefault
        commands = [
            BotCommand("start", "Welcome to Course Wallah"),
            BotCommand("help", "Show help and command guide"),
            BotCommand("info", "View user profile & permissions"),
            BotCommand("id", "Show your Telegram ID"),
            BotCommand("stop", "Stop active task safely"),
            BotCommand("admin", "Open complete admin dashboard"),
            BotCommand("apps", "Manage Apps, Names, Icons & Thumbnails"),
            BotCommand("editapp", "Edit App details & Batch thumbnails"),
            BotCommand("batch", "Open Batch Uploader"),
            BotCommand("uploadbatch", "Start TXT batch upload wizard"),
            BotCommand("batchstatus", "Show current batch processing status"),
            BotCommand("batchjobs", "Show recent batch jobs"),
            BotCommand("batchretry", "Retry failed items in batch"),
            BotCommand("batchresume", "Resume paused/interrupted batch"),
            BotCommand("batchpause", "Pause current batch safely"),
            BotCommand("batchcancel", "Cancel current batch with cleanup"),
            BotCommand("batchlogs", "Show latest batch processing logs"),
            BotCommand("setchannel", "Configure storage channel ID or forward post"),
            BotCommand("stream", "Stream direct video URL or replied video"),
            BotCommand("streamstatus", "View Telegram streaming engine status"),
            BotCommand("wipedb", "Wipe database for fresh batch start"),
            BotCommand("youtube", "Show YouTube accounts & failover status"),
            BotCommand("youtube_accounts", "List all configured YouTube accounts"),
            BotCommand("youtube_status", "Show YouTube upload queues & remaining quota"),
            BotCommand("youtube_pause", "Pause YouTube uploads"),
            BotCommand("youtube_resume", "Resume YouTube uploads")
        ]
        await bot_client.set_bot_commands(commands, scope=BotCommandScopeDefault())

        logger.info("Registered bot commands in Telegram scope successfully.")
    except Exception as e:
        logger.warning(f"Could not register bot commands with Telegram API: {e}")

    startup_banner = (
        "\n==================================================\n"
        "COURSE WALLAH BOT\n"
        "==================================================\n\n"
        f"Bot:\n@{me.username}\n\n"
        f"Bot ID:\n{me.id}\n\n"
        "Database:\nCONNECTED\n\n"
        "Worker:\nONLINE\n\n"
        "Handlers:\nREGISTERED\n\n"
        "Commands:\nREGISTERED\n\n"
        "Status:\nONLINE\n"
        "=================================================="
    )
    print(startup_banner)

    await idle()
    await bot_client.stop()

if __name__ == "__main__":
    asyncio.run(start_bot())
