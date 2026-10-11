import os
import sys
import math
import time
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List, AsyncGenerator, Tuple
from collections import OrderedDict

from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import Message
from pyrogram.file_id import FileId

from config.settings import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    BASE_DIR,
    OWNER_ID,
    TELEGRAM_STREAM_ENABLED,
)

logger = logging.getLogger(__name__)

# LRU Cache for MP4 initial header chunks (moov/ftyp atoms) to guarantee 1ms instant start & seek
class LRUHeaderCache:
    def __init__(self, max_items: int = 150):
        self.max_items = max_items
        self.cache: OrderedDict[str, bytes] = OrderedDict()
        self.meta: Dict[str, Dict[str, Any]] = {}

    def get_header(self, key: str) -> Optional[bytes]:
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return None

    def set_header(self, key: str, data: bytes, meta: Optional[Dict[str, Any]] = None):
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = data
        if meta:
            self.meta[key] = meta
        if len(self.cache) > self.max_items:
            oldest_key, _ = self.cache.popitem(last=False)
            self.meta.pop(oldest_key, None)

    def get_meta(self, key: str) -> Optional[Dict[str, Any]]:
        return self.meta.get(key)


GLOBAL_HEADER_CACHE = LRUHeaderCache(max_items=200)


class TelegramClientPool:
    """
    High-Performance Multi-Bot Client Pool for Telegram File Streaming.
    Features:
    - Multi-bot worker token load balancing to bypass Telegram per-client rate limits.
    - Zero-disk streaming directly from Telegram MTProto to HTTP response.
    - Byte-range seeking support with 1MB chunk segmentation.
    - In-memory header caching for instant video launch.
    """
    _instance: Optional["TelegramClientPool"] = None

    def __init__(self):
        self.api_id = API_ID
        self.api_hash = API_HASH
        self.main_token = BOT_TOKEN
        self.worker_tokens: List[str] = []
        self.clients: List[Client] = []
        self._current_client_idx = 0
        self._lock = asyncio.Lock()
        self._is_started = False
        self.storage_chat_id: int = OWNER_ID

        # Collect additional worker tokens from environment
        raw_workers = os.environ.get("TELEGRAM_WORKER_TOKENS", "").strip()
        if raw_workers:
            self.worker_tokens.extend([t.strip() for t in raw_workers.split(",") if t.strip()])
        for k, v in os.environ.items():
            if k.startswith("MULTI_TOKEN") and v.strip() and v.strip() not in self.worker_tokens and v.strip() != self.main_token:
                self.worker_tokens.append(v.strip())

        raw_chat = os.environ.get("TELEGRAM_STORAGE_CHANNEL_ID", "").strip()
        if raw_chat and (raw_chat.startswith("-") or raw_chat.isdigit()):
            try:
                self.storage_chat_id = int(raw_chat)
            except ValueError:
                self.storage_chat_id = OWNER_ID

    @classmethod
    def get_instance(cls) -> "TelegramClientPool":
        if cls._instance is None:
            cls._instance = TelegramClientPool()
        return cls._instance

    async def start(self):
        if self._is_started and self.clients:
            return

        async with self._lock:
            if self._is_started and self.clients:
                return

            if not self.main_token or not self.api_id or not self.api_hash:
                logger.warning("[TG_POOL] Telegram credentials not configured. Streaming pool disabled.")
                return

            # 1. Start primary in-memory bot client
            main_id_prefix = self.main_token.split(":")[0] if ":" in self.main_token else "main"
            main_client = Client(
                name=f"stream_main_{main_id_prefix}",
                api_id=self.api_id,
                api_hash=self.api_hash,
                bot_token=self.main_token,
                in_memory=True,
                no_updates=True,
            )
            try:
                await main_client.start()
                if main_client not in self.clients:
                    self.clients.append(main_client)
                logger.info(f"[TG_POOL] Primary streaming client started (Bot ID: {main_id_prefix})")
            except Exception as e:
                logger.error(f"[TG_POOL] Failed to start primary streaming client: {e}")

            # 2. Start worker bot clients for load balancing
            for i, tok in enumerate(self.worker_tokens):
                w_prefix = tok.split(":")[0] if ":" in tok else f"w{i+1}"
                w_client = Client(
                    name=f"stream_worker_{w_prefix}",
                    api_id=self.api_id,
                    api_hash=self.api_hash,
                    bot_token=tok,
                    in_memory=True,
                    no_updates=True,
                )
                try:
                    await w_client.start()
                    if w_client not in self.clients:
                        self.clients.append(w_client)
                    logger.info(f"[TG_POOL] Worker streaming client #{i+1} started (Bot ID: {w_prefix})")
                except Exception as wex:
                    logger.warning(f"[TG_POOL] Could not start worker bot token #{i+1}: {wex}")

            self._is_started = len(self.clients) > 0
            logger.info(f"[TG_POOL] Total active streaming workers in pool: {len(self.clients)}")

    def register_client(self, client: Client):
        """Registers an external active Pyrogram client (e.g. from the Telegram bot)."""
        if client and client not in self.clients:
            self.clients.insert(0, client)
            self._is_started = True
            logger.info(f"[TG_POOL] Registered external bot client into streaming pool. (Total: {len(self.clients)})")

    async def stop(self):
        async with self._lock:
            for c in self.clients:
                try:
                    if getattr(c, "is_connected", False):
                        await c.stop()
                except Exception:
                    pass
            self.clients.clear()
            self._is_started = False
            logger.info("[TG_POOL] All streaming clients stopped.")

    def get_client(self) -> Client:
        if not self.clients:
            raise RuntimeError("No active Telegram streaming clients in pool.")
        self._current_client_idx = (self._current_client_idx + 1) % len(self.clients)
        return self.clients[self._current_client_idx]

    async def upload_video(
        self,
        file_path: str,
        caption: str = "",
        duration: int = 0,
        width: int = 1920,
        height: int = 1080,
        thumb_path: Optional[str] = None,
        progress_cb: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Uploads local video to Telegram storage channel/chat.
        """
        if not self._is_started or not self.clients:
            await self.start()

        if not self.clients:
            raise RuntimeError("Telegram streaming client pool is offline.")

        client = self.clients[0]  # Use primary client for storage channel uploads
        target_chat = self.storage_chat_id or OWNER_ID

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Video file not found: {file_path}")

        file_size = os.path.getsize(file_path)
        logger.info(f"[TG_UPLOAD] Uploading '{Path(file_path).name}' ({file_size} bytes) to chat {target_chat}...")

        async def _pyro_progress(current: int, total: int):
            if progress_cb:
                try:
                    pct = (current / total) * 100.0 if total > 0 else 0.0
                    res = progress_cb(pct, current, total)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

        msg: Message = await client.send_video(
            chat_id=target_chat,
            video=file_path,
            caption=caption[:1024] if caption else None,
            parse_mode=ParseMode.HTML,
            duration=int(duration) if duration else 0,
            width=width or 1920,
            height=height or 1080,
            thumb=thumb_path if (thumb_path and os.path.exists(thumb_path)) else None,
            supports_streaming=True,
            progress=_pyro_progress,
        )

        media = msg.video or msg.document
        if not media:
            raise RuntimeError(f"Telegram upload succeeded but no video/document media found in message {msg.id}")

        file_id = media.file_id
        msg_id = msg.id

        # Cache initial 2MB header directly from local file for ultra-fast initial range requests
        try:
            with open(file_path, "rb") as f:
                header_data = f.read(2 * 1024 * 1024)
                GLOBAL_HEADER_CACHE.set_header(
                    f"{target_chat}:{msg_id}",
                    header_data,
                    meta={
                        "file_size": file_size,
                        "mime_type": "video/mp4",
                        "file_name": getattr(media, "file_name", None) or Path(file_path).name,
                    }
                )
        except Exception as head_err:
            logger.debug(f"[TG_HEADER_CACHE_NOTICE] {head_err}")

        logger.info(f"[TG_UPLOAD_SUCCESS] message_id={msg_id} file_id={file_id} size={file_size}")
        return {
            "success": True,
            "message_id": msg_id,
            "chat_id": target_chat,
            "file_id": file_id,
            "file_size": file_size,
            "mime_type": "video/mp4",
            "file_name": getattr(media, "file_name", None) or Path(file_path).name,
        }

    async def _get_media_info_with_client(self, chat_id: int, message_id: int) -> Tuple[Client, Any, int, str]:
        """
        Resolve a message and keep the client that resolved it together with its media.
        Telegram media references may not be reusable across different client sessions.
        """
        cache_key = f"{chat_id}:{message_id}"
        cached_meta = GLOBAL_HEADER_CACHE.get_meta(cache_key)

        if not self._is_started or not self.clients:
            await self.start()
        if not self.clients:
            raise RuntimeError("Telegram streaming client pool is offline.")

        last_err = None
        for client in self.clients:
            try:
                msg = await client.get_messages(chat_id=chat_id, message_ids=message_id)
                if not msg or msg.empty:
                    continue
                media = msg.video or msg.document
                if not media:
                    continue

                file_size = getattr(media, "file_size", 0) or (cached_meta.get("file_size") if cached_meta else 0)
                mime_type = getattr(media, "mime_type", None) or "video/mp4"
                return client, media, file_size, mime_type
            except Exception as err:
                last_err = err
                logger.debug("[TG_MEDIA_LOOKUP_RETRY] msg_id=%s client=%s error=%s",
                             message_id, getattr(client, "name", "unknown"), err)

        raise ValueError(
            f"Message {message_id} in chat {chat_id} not found or has no video media. "
            f"Last Telegram error: {last_err or 'Empty/non-media message'}"
        )

    async def get_media_info(self, chat_id: int, message_id: int) -> Tuple[Any, int, str]:
        """Public metadata API retained for existing callers."""
        _, media, file_size, mime_type = await self._get_media_info_with_client(chat_id, message_id)
        return media, file_size, mime_type

    async def stream_range(
        self,
        chat_id: int,
        message_id: int,
        start_byte: int,
        end_byte: int,
        chunk_size: int = 1024 * 1024,
        media: Optional[Any] = None,
    ) -> AsyncGenerator[bytes, None]:
        """
        Streams binary chunks for a specific byte range directly from Telegram MTProto.
        """
        if not self._is_started or not self.clients:
            await self.start()

        cache_key = f"{chat_id}:{message_id}"
        cached_header = GLOBAL_HEADER_CACHE.get_header(cache_key)

        # Fast-path: If the requested range falls completely within the cached first 2MB header, serve instantly
        if cached_header and start_byte < len(cached_header):
            avail_end = min(end_byte + 1, len(cached_header))
            header_slice = cached_header[start_byte:avail_end]
            yield header_slice
            start_byte += len(header_slice)
            if start_byte > end_byte:
                return

        if not self.clients:
            return

        # Always resolve the media and stream it with the same client session.
        # A media object fetched by a worker client is not guaranteed to work on clients[0].
        try:
            client, resolved_media, _, _ = await self._get_media_info_with_client(chat_id, message_id)
            media = resolved_media
        except Exception as get_err:
            logger.exception("[STREAM_MEDIA_LOOKUP_ERR] chat_id=%s msg_id=%s", chat_id, message_id)
            raise RuntimeError(f"Unable to resolve Telegram media for message {message_id}") from get_err

        # Pyrogram stream_media works with 1MB chunk offsets
        chunk_offset = math.floor(start_byte / (1024 * 1024))
        chunk_count = math.ceil((end_byte - start_byte + 1) / (1024 * 1024)) + 1

        bytes_to_skip_in_first_chunk = start_byte - (chunk_offset * 1024 * 1024)
        bytes_needed = end_byte - start_byte + 1
        bytes_sent = 0

        first_chunk = True
        try:
            async for chunk in client.stream_media(media, offset=chunk_offset, limit=chunk_count):
                if not chunk:
                    continue

                if first_chunk:
                    first_chunk = False
                    if bytes_to_skip_in_first_chunk > 0:
                        if bytes_to_skip_in_first_chunk < len(chunk):
                            chunk = chunk[bytes_to_skip_in_first_chunk:]
                        else:
                            bytes_to_skip_in_first_chunk -= len(chunk)
                            continue

                if bytes_sent + len(chunk) > bytes_needed:
                    chunk = chunk[: (bytes_needed - bytes_sent)]

                yield chunk
                bytes_sent += len(chunk)

                if bytes_sent >= bytes_needed:
                    break
        except (asyncio.CancelledError, GeneratorExit, ConnectionResetError, BrokenPipeError):
            logger.debug(f"[STREAM_CLIENT_DISCONNECT] Client closed stream early for msg_id={message_id}")
            return
        except Exception as stream_err:
            logger.exception(
                "[STREAM_CHUNK_ERR] chat_id=%s msg_id=%s client=%s",
                chat_id, message_id, getattr(client, "name", "unknown")
            )
            raise RuntimeError(f"Telegram streaming failed for message {message_id}") from stream_err
