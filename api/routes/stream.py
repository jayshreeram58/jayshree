import re
import logging
from typing import Optional
from urllib.parse import quote, urljoin
import urllib.parse
import httpx
from fastapi import APIRouter, Request, Response, HTTPException, Header, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from db.connection import get_db_session
from db.models import Video, VideoStorage

from config.settings import OWNER_ID
from api.playback_tokens import verify_playback_token
from storage.telegram_stream.client_pool import TelegramClientPool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["Streaming"])


async def _resolve_storage_chat_id(message_id: int, pool: TelegramClientPool, video_id: Optional[str] = None) -> int:
    """Resolve the Telegram chat used for this stored video before falling back to current config.

    Per-video chat metadata lets previously uploaded videos keep working after the
    active storage channel is changed in the bot.
    """
    try:
        async with get_db_session() as db:
            stmt = (
                select(VideoStorage, Video.telegram_chat_id)
                .outerjoin(Video, Video.id == VideoStorage.video_id)
                .where(
                    VideoStorage.provider.in_(("telegram", "telegram_stream")),
                    VideoStorage.provider_video_id == str(message_id),
                )
                .order_by(VideoStorage.updated_at.desc())
            )
            if video_id:
                stmt = stmt.where(VideoStorage.video_id == video_id)
            result = await db.execute(stmt)
            row = result.first()
            if row:
                storage, video_chat_id = row
                metadata = storage.metadata_json if isinstance(storage.metadata_json, dict) else {}
                stored_chat_id = metadata.get("chat_id") or video_chat_id
                if stored_chat_id is not None:
                    return int(stored_chat_id)
    except Exception:
        logger.exception("[STREAM_CHAT_LOOKUP_ERR] msg_id=%s; falling back to active storage channel", message_id)

    return int(pool.storage_chat_id or OWNER_ID)


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
    video_id: Optional[str] = None,
    token: Optional[str] = None,
):
    """
    Returns media metadata headers for Telegram stream (used by video players to probe content length).
    """
    if not verify_playback_token(token, video_id, message_id):
        raise HTTPException(status_code=403, detail="Playback link expired or invalid. Reopen the lecture.")
    pool = TelegramClientPool.get_instance()
    chat_id = await _resolve_storage_chat_id(message_id, pool, video_id)

    try:
        media, file_size, mime_type = await pool.get_media_info(chat_id, message_id)
        file_name = getattr(media, "file_name", None) or f"lecture_{message_id}.mp4"

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Content-Type": mime_type or "video/mp4",
            "Content-Disposition": _content_disposition(file_name),
            "Cache-Control": "private, no-store, max-age=0",
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
    video_id: Optional[str] = None,
    token: Optional[str] = None,
):
    """
    High-Performance Zero-Disk Telegram Video Stream Proxy.
    Supports HTTP Byte-Ranges (206 Partial Content) for instant HTML5 video seeking.
    """
    if not verify_playback_token(token, video_id, message_id):
        raise HTTPException(status_code=403, detail="Playback link expired or invalid. Reopen the lecture.")
    pool = TelegramClientPool.get_instance()
    chat_id = await _resolve_storage_chat_id(message_id, pool, video_id)

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
        "Cache-Control": "private, no-store, max-age=0",
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


@router.get("/m3u8/proxy")
@router.head("/m3u8/proxy")
async def proxy_m3u8_stream(
    request: Request,
    url: str = Query(..., description="Target M3U8 playlist or video chunk URL"),
):
    """
    Secure High-Performance Stream Proxy for HLS (.m3u8) Playlists & Video Segments (.ts).
    Bypasses third-party CDN CORS restrictions (e.g. hranker.com / b-cdn.net) and ensures
    seamless, zero-lag playback across all desktop and mobile browsers.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="Invalid upstream media URL")

    # Construct upstream headers
    parsed = urllib.parse.urlparse(url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"
    if "hranker.com" in parsed.netloc:
        referer = "https://www.selectionway.com/"

    upstream_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": referer,
        "Origin": referer.rstrip("/"),
    }

    client_range = request.headers.get("range")
    if client_range:
        upstream_headers["Range"] = client_range

    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            if request.method == "HEAD":
                resp = await client.head(url, headers=upstream_headers)
                return Response(
                    status_code=resp.status_code,
                    headers={
                        "Content-Type": resp.headers.get("content-type", "application/vnd.apple.mpegurl"),
                        "Content-Length": resp.headers.get("content-length", "0"),
                        "Accept-Ranges": "bytes",
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Headers": "*",
                        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                    }
                )

            resp = await client.get(url, headers=upstream_headers)
            if resp.status_code not in (200, 206):
                logger.warning(f"[M3U8_PROXY] Upstream status {resp.status_code} for {url}")
                raise HTTPException(status_code=resp.status_code, detail="Upstream media unavailable")

            content_type = resp.headers.get("content-type", "").lower()
            is_playlist = (".m3u8" in url.lower()) or ("mpegurl" in content_type) or (resp.content.startswith(b"#EXTM3U"))

            if is_playlist:
                base_url = url.rsplit("/", 1)[0] + "/"
                text = resp.text
                rewritten_lines = []
                for line in text.splitlines():
                    s = line.strip()
                    if not s:
                        rewritten_lines.append(line)
                    elif s.startswith("#"):
                        def repl(m):
                            target = urljoin(base_url, m.group(1))
                            return f'URI="/api/v1/stream/m3u8/proxy?url={quote(target, safe="")}"'
                        new_line = re.sub(r'URI="([^"]+)"', repl, line)
                        rewritten_lines.append(new_line)
                    else:
                        abs_target = urljoin(base_url, s)
                        rewritten_lines.append(f"/api/v1/stream/m3u8/proxy?url={quote(abs_target, safe='')}")

                rewritten_bytes = "\n".join(rewritten_lines).encode("utf-8")
                return Response(
                    content=rewritten_bytes,
                    status_code=200,
                    media_type="application/vnd.apple.mpegurl",
                    headers={
                        "Content-Type": "application/vnd.apple.mpegurl",
                        "Content-Length": str(len(rewritten_bytes)),
                        "Cache-Control": "public, max-age=600",
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Headers": "*",
                        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                        "X-Content-Type-Options": "nosniff",
                    }
                )

            # Binary TS segment or media chunk
            res_headers = {
                "Content-Type": resp.headers.get("content-type", "video/mp2t"),
                "Cache-Control": "public, max-age=86400",
                "Accept-Ranges": "bytes",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            }
            if "content-range" in resp.headers:
                res_headers["Content-Range"] = resp.headers["content-range"]
            if "content-length" in resp.headers:
                res_headers["Content-Length"] = resp.headers["content-length"]

            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=res_headers
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[M3U8_PROXY_ERR] {url}: {e}")
        raise HTTPException(status_code=502, detail=f"Proxy error streaming media: {str(e)}")

