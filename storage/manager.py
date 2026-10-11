import os
import time
import logging
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any, List, Callable, Tuple
from sqlalchemy import select, and_

from config.settings import (
    STORAGE_MAX_RETRIES,
    STORAGE_RETRY_DELAY,
    STORAGE_REQUIRED_PROVIDERS,
)
from db.connection import get_db_session
from db.models import Video, VideoStorage, VideoStorageStatus
from storage.base import (
    BaseVideoStorageProvider,
    StorageProviderResult,
    StorageProviderStatus,
    StorageReplicationError,
    StorageReplicationProcessingError,
    StorageReplicationFailedError,
)
from storage.hashing import compute_file_sha256_async
from storage.providers.vcdn import VcdnStorageProvider
from storage.providers.media_cm import MediaCmStorageProvider
from storage.providers.anonmp4 import AnonMp4StorageProvider
from storage.providers.vevocloud import VevocloudStorageProvider
from storage.providers.telegram_stream import TelegramStreamStorageProvider

logger = logging.getLogger(__name__)


class MultiStorageManager:
    """
    Central Orchestrator for Multi-Storage Cloud Video Replication.
    Enforces sequential ingest across providers:
    Telegram Stream (Direct HTML5) -> VCDN -> Media.cm -> AnonMP4 -> Vevocloud.
    
    Features:
    - Zero duplicate uploads via SHA-256 content addressing.
    - Resumable state recovery across bot restarts without re-uploading active tasks.
    - Real verification with no fake statuses.
    - Live Telegram UI progress telemetry.
    - Truthful replication completion policy.
    - Isolated provider failover and graceful degradation.
    """

    def __init__(
        self,
        providers: Optional[List[BaseVideoStorageProvider]] = None,
        max_retries: int = STORAGE_MAX_RETRIES or 3,
        retry_delay: int = STORAGE_RETRY_DELAY or 5,
        required_providers: Optional[List[str]] = None,
    ):
        self.providers: List[BaseVideoStorageProvider] = providers or [
            TelegramStreamStorageProvider(priority=1),
            VcdnStorageProvider(priority=2),
            MediaCmStorageProvider(priority=3),
            AnonMp4StorageProvider(priority=4),
            VevocloudStorageProvider(priority=5),
        ]
        # Sort by priority
        self.providers.sort(key=lambda p: p.priority)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.required_providers = required_providers or STORAGE_REQUIRED_PROVIDERS or ["telegram", "media_cm"]

    async def replicate_video(
        self,
        video_id: str,
        video_file_path: str,
        title: str,
        metadata: Optional[Dict[str, Any]] = None,
        progress_ui_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> Dict[str, Any]:
        """
        Executes sequential replication of a finalized video file to configured storage providers.
        """
        if not os.path.exists(video_file_path):
            raise FileNotFoundError(f"Final video file not found for replication: {video_file_path}")

        file_size = os.path.getsize(video_file_path)
        filename = os.path.basename(video_file_path)

        # Step 1: Compute SHA-256 for duplicate protection
        logger.info(f"[STORAGE] Computing SHA-256 hash for video '{title}' ({file_size} bytes)...")
        file_sha256 = await compute_file_sha256_async(video_file_path)
        logger.info(f"[STORAGE] Video SHA-256: {file_sha256}")

        # Update Video record with SHA-256 and filename in DB
        async with get_db_session() as session:
            video_rec = await session.get(Video, video_id)
            if video_rec:
                video_rec.sha256 = file_sha256
                video_rec.filename = filename
                video_rec.file_size = file_size
                await session.flush()

        # Step 2: Query DB for existing replication records (Restart recovery & duplicates)
        existing_storages: Dict[str, VideoStorage] = {}
        async with get_db_session() as session:
            stmt = select(VideoStorage).where(VideoStorage.video_id == video_id)
            res = await session.execute(stmt)
            for row in res.scalars().all():
                existing_storages[row.provider] = row

            # Check if matching SHA-256 exists in another video with READY status
            sha_stmt = (
                select(VideoStorage)
                .join(Video, VideoStorage.video_id == Video.id)
                .where(and_(Video.sha256 == file_sha256, VideoStorage.status == VideoStorageStatus.READY.value))
            )
            sha_res = await session.execute(sha_stmt)
            for ready_sha_row in sha_res.scalars().all():
                if ready_sha_row.provider not in existing_storages:
                    # Clone existing ready record for current video
                    new_storage = VideoStorage(
                        video_id=video_id,
                        provider=ready_sha_row.provider,
                        status=VideoStorageStatus.READY.value,
                        provider_video_id=ready_sha_row.provider_video_id,
                        watch_url=ready_sha_row.watch_url,
                        embed_url=ready_sha_row.embed_url,
                        hls_url=ready_sha_row.hls_url,
                        playback_url=ready_sha_row.playback_url,
                        thumbnail_url=ready_sha_row.thumbnail_url,
                        delete_url=ready_sha_row.delete_url,
                        remote_size=ready_sha_row.remote_size,
                        remote_duration=ready_sha_row.remote_duration,
                        completed_at=datetime.utcnow(),
                    )
                    session.add(new_storage)
                    await session.flush()
                    existing_storages[ready_sha_row.provider] = new_storage
                    logger.info(f"[STORAGE] Reused exact SHA-256 replica for {ready_sha_row.provider} -> READY")

        # Provider state tracker for UI
        provider_states: Dict[str, Dict[str, Any]] = {}
        for p in self.providers:
            existing = existing_storages.get(p.name)
            if not p.enabled:
                st = StorageProviderStatus.DISABLED.value
            elif existing and existing.status == VideoStorageStatus.READY.value:
                st = StorageProviderStatus.READY.value
            elif existing and existing.status == VideoStorageStatus.PROCESSING.value:
                st = StorageProviderStatus.PROCESSING.value
            else:
                st = StorageProviderStatus.PENDING.value

            provider_states[p.name] = {
                "name": p.name,
                "status": st,
                "progress": 100.0 if st == StorageProviderStatus.READY.value else (50.0 if st == StorageProviderStatus.PROCESSING.value else 0.0),
                "attempt": existing.attempt_count if existing else 1,
                "max_attempts": self.max_retries,
                "error": existing.last_error if existing else None,
                "embed_url": existing.embed_url if existing else None,
                "hls_url": existing.hls_url if existing else None,
                "watch_url": existing.watch_url if existing else None,
            }

        async def _emit_ui(current_provider_name: str, force: bool = False):
            if not progress_ui_callback:
                return
            completed_count = sum(1 for p_st in provider_states.values() if p_st["status"] in (StorageProviderStatus.READY.value, StorageProviderStatus.DISABLED.value))
            ready_count = sum(1 for p_st in provider_states.values() if p_st["status"] == StorageProviderStatus.READY.value)
            payload = {
                "title": title,
                "file_size": file_size,
                "sha256": file_sha256,
                "current_provider": current_provider_name,
                "provider_states": provider_states,
                "completed_count": completed_count,
                "ready_count": ready_count,
                "total_providers": len(self.providers),
                "force": force,
            }
            try:
                res = progress_ui_callback(payload)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as ui_e:
                logger.debug("[STORAGE] Non-fatal progress UI callback notice: %s", ui_e)

        # Initial UI render
        await _emit_ui(self.providers[0].name, force=True)

        results: Dict[str, StorageProviderResult] = {}

        # Step 3: Sequential Execution across all providers
        for provider in self.providers:
            p_name = provider.name
            p_state = provider_states[p_name]
            existing = existing_storages.get(p_name)

            # 1. If provider is disabled, mark and skip
            if not provider.enabled:
                logger.info(f"[STORAGE] Provider {p_name} is DISABLED; skipping.")
                p_state["status"] = StorageProviderStatus.DISABLED.value
                await _persist_storage_status(video_id, p_name, StorageProviderStatus.DISABLED.value)
                await _emit_ui(p_name)
                continue

            # 2. If provider is already READY (from restart or SHA-256 match), skip
            if p_state["status"] == StorageProviderStatus.READY.value:
                logger.info(f"[STORAGE] Provider {p_name} already VERIFIED READY; skipping upload.")
                results[p_name] = StorageProviderResult(
                    success=True,
                    status=StorageProviderStatus.READY.value,
                    provider=p_name,
                    provider_video_id=existing.provider_video_id if existing else None,
                    watch_url=existing.watch_url if existing else None,
                    embed_url=existing.embed_url if existing else None,
                    hls_url=existing.hls_url if existing else None,
                    playback_url=existing.playback_url if existing else None,
                )
                await _emit_ui(p_name)
                continue

            # 3. If provider was in PROCESSING state with persisted provider_video_id, check remote status without re-uploading
            if existing and existing.status == VideoStorageStatus.PROCESSING.value and existing.provider_video_id:
                logger.info(f"[STORAGE] [{p_name.upper()}] Resuming verification for existing remote asset: ID={existing.provider_video_id}")
                p_state["status"] = StorageProviderStatus.VERIFYING.value
                await _emit_ui(p_name)

                check_res = await provider.verify(existing.provider_video_id)
                if check_res.success and check_res.status == StorageProviderStatus.READY.value:
                    logger.info(f"[STORAGE] [{p_name.upper()}] Remote asset confirmed READY on resume!")
                    p_state["status"] = StorageProviderStatus.READY.value
                    p_state["progress"] = 100.0
                    p_state["embed_url"] = check_res.embed_url
                    p_state["hls_url"] = check_res.hls_url
                    p_state["watch_url"] = check_res.watch_url
                    await _persist_storage_success(video_id, p_name, check_res, attempt_count=existing.attempt_count)
                    await _emit_ui(p_name, force=True)
                    results[p_name] = check_res
                    continue
                elif check_res.status == StorageProviderStatus.PROCESSING.value:
                    logger.info(f"[STORAGE] [{p_name.upper()}] Remote asset still transcoding on resume. Retaining PROCESSING state.")
                    p_state["status"] = StorageProviderStatus.PROCESSING.value
                    p_state["progress"] = 50.0
                    p_state["embed_url"] = check_res.embed_url
                    p_state["watch_url"] = check_res.watch_url
                    await _persist_storage_status(video_id, p_name, StorageProviderStatus.PROCESSING.value, result=check_res)
                    await _emit_ui(p_name)
                    results[p_name] = check_res
                    continue

            # 4. Provider needs upload & verification
            p_result: Optional[StorageProviderResult] = None
            attempt = 1
            last_err_msg = None

            while attempt <= self.max_retries:
                p_state["attempt"] = attempt
                p_state["status"] = StorageProviderStatus.UPLOADING.value
                p_state["error"] = None
                await _persist_storage_status(video_id, p_name, StorageProviderStatus.UPLOADING.value, attempt_count=attempt)
                await _emit_ui(p_name)

                logger.info(f"[STORAGE] [{p_name.upper()}] Starting attempt {attempt}/{self.max_retries} for '{title}'...")

                def _prov_prog_cb(pct: float, uploaded: int, total: int):
                    p_state["progress"] = pct
                    asyncio.create_task(_emit_ui(p_name))

                try:
                    p_result = await provider.upload(
                        file_path=video_file_path,
                        title=title,
                        metadata=metadata,
                        progress_cb=_prov_prog_cb,
                    )
                except Exception as up_ex:
                    logger.exception(f"[STORAGE] [{p_name.upper()}] Exception during upload attempt {attempt}: %s", up_ex)
                    p_result = StorageProviderResult(
                        success=False,
                        status=StorageProviderStatus.FAILED.value,
                        provider=p_name,
                        error=str(up_ex),
                    )

                if p_result and p_result.success and p_result.status == StorageProviderStatus.READY.value:
                    # Upload + verification confirmed READY!
                    logger.info(f"[STORAGE] [{p_name.upper()}] Verification confirmed: READY (attempt {attempt})")
                    p_state["status"] = StorageProviderStatus.READY.value
                    p_state["progress"] = 100.0
                    p_state["embed_url"] = p_result.embed_url
                    p_state["hls_url"] = p_result.hls_url
                    p_state["watch_url"] = p_result.watch_url

                    await _persist_storage_success(video_id, p_name, p_result, attempt_count=attempt)
                    await _emit_ui(p_name, force=True)
                    results[p_name] = p_result
                    break

                elif p_result and p_result.status == StorageProviderStatus.PROCESSING.value:
                    # Upload accepted by provider and remote transcoding is actively in progress
                    logger.info(f"[STORAGE] [{p_name.upper()}] Upload accepted; remote transcoding in progress: ID={p_result.provider_video_id}")
                    p_state["status"] = StorageProviderStatus.PROCESSING.value
                    p_state["progress"] = 50.0
                    p_state["embed_url"] = p_result.embed_url
                    p_state["watch_url"] = p_result.watch_url

                    await _persist_storage_status(
                        video_id,
                        p_name,
                        StorageProviderStatus.PROCESSING.value,
                        attempt_count=attempt,
                        result=p_result,
                    )
                    await _emit_ui(p_name, force=True)
                    results[p_name] = p_result
                    break

                else:
                    last_err_msg = p_result.error if p_result else "Unknown upload failure"
                    logger.warning(f"[STORAGE] [{p_name.upper()}] Attempt {attempt}/{self.max_retries} failed: {last_err_msg}")
                    p_state["error"] = last_err_msg

                    if attempt < self.max_retries:
                        p_state["status"] = StorageProviderStatus.RETRYING.value
                        await _persist_storage_status(
                            video_id,
                            p_name,
                            StorageProviderStatus.RETRYING.value,
                            attempt_count=attempt,
                            last_error=last_err_msg,
                        )
                        await _emit_ui(p_name)
                        await asyncio.sleep(self.retry_delay * attempt)
                    else:
                        # Permanent failure after max retries
                        logger.error(f"[STORAGE] [{p_name.upper()}] Permanent failure after {self.max_retries} attempts.")
                        p_state["status"] = StorageProviderStatus.FAILED.value
                        await _persist_storage_status(
                            video_id,
                            p_name,
                            StorageProviderStatus.FAILED.value,
                            attempt_count=attempt,
                            last_error=last_err_msg,
                        )
                        await _emit_ui(p_name, force=True)
                        results[p_name] = p_result or StorageProviderResult(
                            success=False,
                            status=StorageProviderStatus.FAILED.value,
                            provider=p_name,
                            error=last_err_msg,
                        )

                attempt += 1

        # Step 4: Strict Truthful Replication Policy Evaluation
        ready_count = sum(1 for r in results.values() if r.status == StorageProviderStatus.READY.value)
        processing_count = sum(1 for r in results.values() if r.status == StorageProviderStatus.PROCESSING.value)
        total_enabled = sum(1 for p in self.providers if p.enabled)

        # Check required providers policy: every required enabled provider MUST be strictly READY
        all_required_succeeded = True
        processing_required: List[str] = []
        failed_required: List[str] = []

        req_set = set(self.required_providers)
        if "all" in req_set:
            req_set = {p.name for p in self.providers if p.enabled}

        for p_name in req_set:
            res = results.get(p_name)
            p_obj = next((p for p in self.providers if p.name == p_name), None)
            if p_obj and p_obj.enabled:
                if not res or res.status != StorageProviderStatus.READY.value:
                    all_required_succeeded = False
                    if res and res.status == StorageProviderStatus.PROCESSING.value:
                        processing_required.append(p_name)
                        logger.warning(
                            f"[STORAGE] Required provider '{p_name}' is still PROCESSING remotely (ID={res.provider_video_id})."
                        )
                    else:
                        failed_required.append(p_name)
                        logger.warning(
                            f"[STORAGE] Required provider '{p_name}' failed or did not complete (status={res.status if res else 'MISSING'})."
                        )

        logger.info(
            f"[STORAGE] Replication pipeline finished: ready={ready_count}, processing={processing_count}, "
            f"enabled={total_enabled}, required_succeeded={all_required_succeeded}, "
            f"processing_required={processing_required}, failed_required={failed_required}"
        )

        return {
            "success": all_required_succeeded,
            "sha256": file_sha256,
            "ready_count": ready_count,
            "processing_count": processing_count,
            "total_enabled": total_enabled,
            "provider_states": provider_states,
            "results": results,
            "processing_required": processing_required,
            "failed_required": failed_required,
        }


# ==========================================
# DATABASE PERSISTENCE HELPERS
# ==========================================

async def _persist_storage_status(
    video_id: str,
    provider: str,
    status: str,
    attempt_count: int = 1,
    last_error: Optional[str] = None,
    result: Optional[StorageProviderResult] = None,
):
    """Safely records intermediate, retryable, or processing provider status in DB for restart recovery."""
    try:
        async with get_db_session() as session:
            stmt = select(VideoStorage).where(and_(VideoStorage.video_id == video_id, VideoStorage.provider == provider))
            res = await session.execute(stmt)
            record = res.scalar_one_or_none()

            now = datetime.utcnow()
            if not record:
                record = VideoStorage(
                    video_id=video_id,
                    provider=provider,
                    status=status,
                    provider_video_id=result.provider_video_id if result else None,
                    watch_url=result.watch_url if result else None,
                    embed_url=result.embed_url if result else None,
                    hls_url=result.hls_url if result else None,
                    attempt_count=attempt_count,
                    last_error=last_error,
                    last_attempt_at=now,
                    started_at=now,
                    metadata_json=result.raw_metadata if (result and result.raw_metadata) else {},
                )
                session.add(record)
            else:
                record.status = status
                record.attempt_count = attempt_count
                if last_error:
                    record.last_error = last_error
                if result:
                    if result.provider_video_id:
                        record.provider_video_id = result.provider_video_id
                    if result.watch_url:
                        record.watch_url = result.watch_url
                    if result.embed_url:
                        record.embed_url = result.embed_url
                    if result.hls_url:
                        record.hls_url = result.hls_url
                    if result.raw_metadata:
                        record.metadata_json = result.raw_metadata
                record.last_attempt_at = now
            await session.flush()
    except Exception as db_e:
        logger.debug("[STORAGE] DB status update notice (%s, %s): %s", provider, status, db_e)


async def _persist_storage_success(
    video_id: str,
    provider: str,
    result: StorageProviderResult,
    attempt_count: int = 1,
):
    """Persists verified READY provider record to DB."""
    try:
        async with get_db_session() as session:
            stmt = select(VideoStorage).where(and_(VideoStorage.video_id == video_id, VideoStorage.provider == provider))
            res = await session.execute(stmt)
            record = res.scalar_one_or_none()

            now = datetime.utcnow()
            if not record:
                record = VideoStorage(
                    video_id=video_id,
                    provider=provider,
                    status=StorageProviderStatus.READY.value,
                    provider_video_id=result.provider_video_id,
                    watch_url=result.watch_url,
                    embed_url=result.embed_url,
                    hls_url=result.hls_url,
                    playback_url=result.playback_url,
                    thumbnail_url=result.thumbnail_url,
                    delete_url=result.delete_url,
                    remote_size=result.remote_size,
                    remote_duration=result.remote_duration,
                    attempt_count=attempt_count,
                    completed_at=now,
                    metadata_json=result.raw_metadata or {},
                )
                session.add(record)
            else:
                record.status = StorageProviderStatus.READY.value
                record.provider_video_id = result.provider_video_id
                record.watch_url = result.watch_url
                record.embed_url = result.embed_url
                record.hls_url = result.hls_url
                record.playback_url = result.playback_url
                record.thumbnail_url = result.thumbnail_url
                record.delete_url = result.delete_url
                if result.remote_size:
                    record.remote_size = result.remote_size
                if result.remote_duration:
                    record.remote_duration = result.remote_duration
                record.attempt_count = attempt_count
                record.last_error = None
                record.completed_at = now
                if result.raw_metadata:
                    record.metadata_json = result.raw_metadata
            # Persist the exact Telegram chat alongside the message ID. The active
            # storage channel can change later, but existing lecture URLs must keep
            # resolving to the channel where their media was uploaded.
            if provider in ("telegram", "telegram_stream") and result.raw_metadata:
                raw_chat_id = result.raw_metadata.get("chat_id")
                raw_message_id = result.raw_metadata.get("message_id") or result.provider_video_id
                video_rec = await session.get(Video, video_id)
                if video_rec:
                    if raw_chat_id is not None:
                        video_rec.telegram_chat_id = int(raw_chat_id)
                    if raw_message_id is not None:
                        video_rec.telegram_message_id = int(raw_message_id)

            await session.flush()
    except Exception as db_e:
        logger.debug("[STORAGE] DB success persistence notice (%s): %s", provider, db_e)
