import re
import logging
from typing import Optional
from urllib.parse import quote
from fastapi import APIRouter, Request, Response, HTTPException, Header
from fastapi.responses import StreamingResponse

from config.settings import OWNER_ID
from storage.telegram_stream.client_pool import TelegramClientPool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["Streaming"])


def _content_disposition(file_name: str) -> str:
    """Build an ASCII-safe Content-Disposition header while preserving Unicode filenames."""
    # Keep a conservative ASCII fallback for clients that do not support filename*.
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", file_name).strip("._") or "video.mp4"
    encoded_name = quote(file_name, safe="")
    return f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'


@router.head("/tg/{message_id}")
async def head_telegram_video_stream(
    message_id: int,
    request: Request,
):
    """
    Returns media metadata headers for Telegram stream (used by video players to probe content length).
    """
    pool = TelegramClientPool.get_instance()
    chat_id = pool.storage_chat_id or OWNER_ID

    try:
        media, file_size, mime_type = await pool.get_media_info(chat_id, message_id)
        file_name = getattr(media, "file_name", None) or f"lecture_{message_id}.mp4"

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Content-Type": mime_type or "video/mp4",
            "Content-Disposition": _content_disposition(file_name),
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
        }
        return Response(status_code=200, headers=headers)
    except Exception as ex:
        logger.warning(f"[STREAM_HEAD_ERR] msg_id={message_id}: {ex}")
        raise HTTPException(status_code=404, detail="Video stream not found or unavailable")


@router.get("/tg/{message_id}")
async def stream_telegram_video(
    message_id: int,
    request: Request,
    range: Optional[str] = Header(None),
):
    """
    High-Performance Zero-Disk Telegram Video Stream Proxy.
    Supports HTTP Byte-Ranges (206 Partial Content) for instant HTML5 video seeking.
    """
    pool = TelegramClientPool.get_instance()
    chat_id = pool.storage_chat_id or OWNER_ID

    try:
        media, file_size, mime_type = await pool.get_media_info(chat_id, message_id)
        file_name = getattr(media, "file_name", None) or f"lecture_{message_id}.mp4"
    except Exception as ex:
        logger.warning(f"[STREAM_LOOKUP_ERR] msg_id={message_id}: {ex}")
        raise HTTPException(status_code=404, detail="Video stream not found or unavailable on Telegram")

    if file_size <= 0:
        raise HTTPException(status_code=500, detail="Invalid video file size from Telegram")

    # Base response headers
    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": mime_type or "video/mp4",
        "Content-Disposition": _content_disposition(file_name),
        "Cache-Control": "public, max-age=86400",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
    }

    # If no Range requested, return full stream
    if not range:
        base_headers["Content-Length"] = str(file_size)
        return StreamingResponse(
            pool.stream_range(chat_id, message_id, 0, file_size - 1, media=media),
            status_code=200,
            headers=base_headers,
            media_type=mime_type or "video/mp4",
        )

    # Parse HTTP Range header (e.g. "bytes=0-1048575", "bytes=50000-")
    range_match = re.match(r"^bytes=(\d+)-(\d+)?$", range.strip())
    if not range_match:
        base_headers["Content-Range"] = f"bytes */{file_size}"
        return Response(status_code=416, headers=base_headers)

    start_byte = int(range_match.group(1))
    end_byte_str = range_match.group(2)

    if start_byte >= file_size:
        base_headers["Content-Range"] = f"bytes */{file_size}"
        return Response(status_code=416, headers=base_headers)

    if end_byte_str:
        end_byte = min(int(end_byte_str), file_size - 1)
    else:
        # Default chunk burst size: 4MB for high responsiveness
        burst_size = 4 * 1024 * 1024
        end_byte = min(start_byte + burst_size - 1, file_size - 1)

    content_length = end_byte - start_byte + 1

    partial_headers = dict(base_headers)
    partial_headers["Content-Range"] = f"bytes {start_byte}-{end_byte}/{file_size}"
    partial_headers["Content-Length"] = str(content_length)

    return StreamingResponse(
        pool.stream_range(chat_id, message_id, start_byte, end_byte, media=media),
        status_code=206,
        headers=partial_headers,
        media_type=mime_type or "video/mp4",
    )
