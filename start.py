import os
import sys
import asyncio
import logging
import argparse
from pathlib import Path
from typing import Awaitable, Callable

import uvicorn

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.settings import BOT_TOKEN, API_ID, API_HASH, BOT_NAME
from db.connection import init_db

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] [STARTUP] %(message)s",
)
logger = logging.getLogger("CourseWallahSupervisor")


async def run_web(host: str = "0.0.0.0", port: int = 8000):
    """Starts FastAPI Uvicorn server."""
    logger.info("Starting FastAPI Web Server on %s:%s...", host, port)
    from api.server import app

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        access_log=True,
    )
    await uvicorn.Server(config).serve()


async def run_bot():
    """Starts Telegram Bot client."""
    if not BOT_TOKEN or not API_ID or not API_HASH:
        logger.error(
            "Telegram bot NOT started: configure BOT_TOKEN (or BOT_1_TOKEN), "
            "API_ID, and API_HASH in the Koyeb service environment."
        )
        # Return to the supervisor so it can report/retry without taking down the API.
        return

    logger.info("Starting Telegram Bot (%s)...", BOT_NAME)
    from bot.bot_app import start_bot
    await start_bot()


async def run_worker():
    """Starts Background Ingestion & Processing Worker."""
    logger.info("Starting Background Queue Worker...")
    from worker import PlatformWorker

    worker = PlatformWorker(worker_id=os.environ.get("WORKER_ID", "course_wallah_worker"))
    await worker.start()


async def _supervise_service(
    name: str,
    service: Callable[[], Awaitable[None]],
    initial_backoff: float = 2.0,
    max_backoff: float = 60.0,
):
    """Keep one component's failure from terminating the other components."""
    backoff = initial_backoff
    while True:
        try:
            await service()
            logger.warning("%s exited unexpectedly; restarting in %.1fs.", name, backoff)
        except asyncio.CancelledError:
            logger.info("Stopping %s supervisor.", name)
            raise
        except Exception:
            logger.exception("%s crashed; restarting in %.1fs.", name, backoff)

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


async def run_all(host: str, port: int):
    """Run API, Telegram bot, and worker with independent restart supervision."""
    logger.info("=" * 60)
    logger.info("COURSE WALLAH PLATFORM - UNIFIED CLOUD SUPERVISOR")
    logger.info("Target Host: %s | Port: %s", host, port)
    logger.info("Services: FastAPI API + Telegram Bot + Background Worker")
    logger.info("=" * 60)

    # Fail fast on database problems so the platform does not appear healthy without its DB.
    await init_db()

    services = [
        asyncio.create_task(
            _supervise_service("FastAPI_WebServer", lambda: run_web(host, port)),
            name="FastAPI_WebServer",
        ),
        asyncio.create_task(
            _supervise_service("Telegram_Bot", run_bot),
            name="Telegram_Bot",
        ),
        asyncio.create_task(
            _supervise_service("Background_Worker", run_worker),
            name="Background_Worker",
        ),
    ]

    try:
        await asyncio.gather(*services)
    finally:
        for task in services:
            task.cancel()
        await asyncio.gather(*services, return_exceptions=True)
        logger.info("All supervised services stopped.")


def main():
    parser = argparse.ArgumentParser(description="Course Wallah Production Supervisor")
    parser.add_argument(
        "--service",
        choices=["all", "web", "bot", "worker"],
        default=os.environ.get("SERVICE_TYPE", "all"),
        help="Service to start (all, web, bot, worker)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Port for Web server",
    )
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host interface to bind")
    args = parser.parse_args()

    service_mode = args.service.lower()
    logger.info("Selected Service Mode: '%s'", service_mode)

    if service_mode == "web":
        asyncio.run(run_web(host=args.host, port=args.port))
    elif service_mode == "bot":
        asyncio.run(_supervise_service("Telegram_Bot", run_bot))
    elif service_mode == "worker":
        asyncio.run(_supervise_service("Background_Worker", run_worker))
    else:
        asyncio.run(run_all(host=args.host, port=args.port))


if __name__ == "__main__":
    main()
