import asyncio
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import select

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.settings import MAX_CONCURRENT_JOBS
from db.connection import get_db_session, init_db
from db.models import Job, JobStatus

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] [WORKER] %(message)s",
)
logger = logging.getLogger("CourseWallahWorker")


class PlatformWorker:
    """
    Background queue monitor.

    IMPORTANT: A queued Job currently stores identifiers and step_data, but the
    repository does not yet persist the complete NormalizedBatchTree/session_data
    needed by ContentProcessingEngine.run_full_batch(). Do not mark a job as
    DOWNLOADING until a real executor has claimed and begun processing it.
    The previous implementation changed statuses and then did no work, leaving
    jobs permanently stuck in DOWNLOADING.
    """

    def __init__(self, worker_id: str = "worker_main"):
        self.worker_id = worker_id
        self.running = False
        self._last_queue_count = None

    async def start(self):
        logger.info("Initializing Course Wallah worker (%s)...", self.worker_id)
        await init_db()
        self.running = True
        logger.info(
            "Worker %s started. Poll interval=5s; concurrency limit=%s",
            self.worker_id,
            MAX_CONCURRENT_JOBS,
        )

        while self.running:
            try:
                await self._poll_queue()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker queue polling failed; will retry.")
            await asyncio.sleep(max(2, int(os.environ.get("WORKER_POLL_SECONDS", "5"))))

    async def stop(self):
        logger.info("Stopping worker %s...", self.worker_id)
        self.running = False

    async def _poll_queue(self):
        async with get_db_session() as session:
            stmt = (
                select(Job.id, Job.status, Job.job_type, Job.step_data)
                .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RETRYING]))
                .order_by(Job.created_at.asc())
                .limit(max(1, MAX_CONCURRENT_JOBS))
            )
            rows = (await session.execute(stmt)).all()

        if not rows:
            self._last_queue_count = 0
            return

        count = len(rows)
        if count != self._last_queue_count:
            logger.warning(
                "Found %s queued/retrying job(s). No jobs were changed: "
                "a durable batch-payload executor is required before processing.",
                count,
            )
            self._last_queue_count = count

        for job_id, status, job_type, step_data in rows:
            payload = step_data if isinstance(step_data, dict) else {}
            if not payload:
                logger.warning(
                    "Job %s (%s) has no step_data payload; leaving status=%s.",
                    job_id,
                    job_type,
                    getattr(status, "value", status),
                )


async def main():
    worker = PlatformWorker()
    try:
        await worker.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(main())
