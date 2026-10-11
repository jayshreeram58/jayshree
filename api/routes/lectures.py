from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload

from db.connection import get_db_dependency
from db.models import (
    Lecture, Video, PDF, Subject, Folder, Batch,
    PublicationStatus, Playlist, PlaylistItem,
    VideoStorage, VideoStorageStatus
)

router = APIRouter(prefix="/lectures", tags=["Lectures"])

@router.get("/{lecture_id}")
async def get_lecture_details(lecture_id: str, db: AsyncSession = Depends(get_db_dependency)):
    stmt = (
        select(Lecture)
        .options(
            selectinload(Lecture.video),
            selectinload(Lecture.pdf),
            selectinload(Lecture.folder),
            selectinload(Lecture.subject),
            selectinload(Lecture.batch).selectinload(Batch.app)
        )
        .where(Lecture.id == lecture_id)
    )
    res = await db.execute(stmt)
    lecture = res.scalar_one_or_none()
    if not lecture:
        raise HTTPException(status_code=404, detail="Lecture not found")

    # Find next and previous lectures in the same subject / folder
    sibling_stmt = (
        select(Lecture)
        .where(
            and_(
                Lecture.subject_id == lecture.subject_id,
                Lecture.publication_status == PublicationStatus.PUBLISHED
            )
        )
        .order_by(Lecture.lecture_index)
    )
    siblings_res = await db.execute(sibling_stmt)
    siblings = list(siblings_res.scalars().all())

    prev_lec = None
    next_lec = None
    for i, s in enumerate(siblings):
        if s.id == lecture.id:
            if i > 0:
                prev_lec = {"id": siblings[i-1].id, "title": siblings[i-1].title, "index": siblings[i-1].lecture_index}
            if i + 1 < len(siblings):
                next_lec = {"id": siblings[i+1].id, "title": siblings[i+1].title, "index": siblings[i+1].lecture_index}
            break

    return {
        "id": lecture.id,
        "title": lecture.title,
        "index": lecture.lecture_index,
        "duration_seconds": lecture.duration_seconds,
        "has_video": lecture.has_video,
        "has_pdf": lecture.has_pdf,
        "folder_name": lecture.folder.name if lecture.folder else "General",
        "subject_name": lecture.subject.name if lecture.subject else "Subject",
        "batch_name": lecture.batch.name if lecture.batch else "Batch",
        "batch_slug": lecture.batch.slug if lecture.batch else "engg-math",
        "app_name": lecture.batch.app.name if lecture.batch and lecture.batch.app else "Course Wallah",
        "app_slug": lecture.batch.app.slug if lecture.batch and lecture.batch.app else "course-wallah-e2e",
        "prev_lecture": prev_lec,
        "next_lecture": next_lec,
        "playlist": [
            {
                "id": item.id,
                "title": item.title,
                "index": item.lecture_index,
                "has_video": item.has_video,
                "has_pdf": item.has_pdf,
                "is_active": item.id == lecture.id
            }
            for item in siblings
        ]
    }

@router.get("/{lecture_id}/access")
async def get_lecture_playback_access(lecture_id: str, db: AsyncSession = Depends(get_db_dependency)):
    """
    Returns secure video playback access parameters for Course Wallah custom player.
    Never exposes internal tokens or storage credentials.
    """
    stmt = (
        select(Lecture)
        .options(
            selectinload(Lecture.video).selectinload(Video.storages),
            selectinload(Lecture.pdf)
        )
        .where(Lecture.id == lecture_id)
    )
    res = await db.execute(stmt)
    lecture = res.scalar_one_or_none()
    if not lecture or lecture.publication_status != PublicationStatus.PUBLISHED:
        raise HTTPException(status_code=404, detail="Lecture not available or unpublished")

    video = lecture.video
    has_storage_video = False
    embed_url = None
    hls_url = None
    playback_url = None
    stream_url = None
    storage_provider = None

    if video and video.storages:
        provider_order = {
            "telegram": 1,
            "telegram_stream": 1,
            "vcdn": 2,
            "vevocloud": 3,
            "anonmp4": 4,
            "media_cm": 5
        }
        sorted_storages = sorted(video.storages, key=lambda s: provider_order.get(s.provider, 99))
        for st in sorted_storages:
            if st.status in ("READY", "PROCESSING", VideoStorageStatus.READY.value, VideoStorageStatus.PROCESSING.value):
                if st.provider in ("telegram", "telegram_stream"):
                    msg_id = st.provider_video_id
                    tg_url = f"/api/v1/stream/tg/{msg_id}?video_id={video.id}"
                    stream_url = tg_url
                    playback_url = tg_url
                    hls_url = None
                    embed_url = None
                    storage_provider = "telegram"
                    has_storage_video = True
                    break
                elif st.provider == "media_cm":
                    filecode = st.provider_video_id or (st.embed_url.rstrip("/").split("/")[-1] if st.embed_url else "")
                    playback_url = f"https://media.cm/{filecode}"
                    embed_url = None  # Media.cm free tier blocks iframe embed for >100MB files; use Cinema launcher
                    storage_provider = "media_cm"
                    has_storage_video = True
                    break
                else:
                    if st.embed_url:
                        embed_url = st.embed_url.strip()
                    if st.hls_url:
                        hls_url = st.hls_url.strip()
                    if st.playback_url:
                        playback_url = st.playback_url.strip()
                    if embed_url or hls_url or playback_url:
                        storage_provider = st.provider
                        has_storage_video = True
                        break

    is_yt_valid = bool(
        video
        and video.youtube_video_id
        and not video.youtube_video_id.startswith(("cw_temp_", "yt_id_", "EXISTING_YT", "YT_PERSIST", "dQw4w9WgXcQ", "tg_"))
    )
    has_video = has_storage_video or is_yt_valid or bool(lecture.source_url)

    if not has_video:
        return {
            "has_video": False,
            "has_pdf": lecture.has_pdf,
            "message": "This lecture contains study material only or video is being prepared."
        }

    # Only provide direct stream_url if storage gave a direct HLS / mp4 link or as fallback when no embed
    if not stream_url:
        if hls_url:
            stream_url = hls_url
        elif playback_url and not embed_url and storage_provider != "media_cm":
            stream_url = playback_url
        elif not embed_url and not is_yt_valid and lecture.source_url and any(lecture.source_url.lower().endswith(ext) for ext in (".m3u8", ".mp4")):
            stream_url = lecture.source_url

    pdf_url = lecture.source_pdf_url or (f"/api/v1/pdfs/{lecture.id}/content" if lecture.pdf else None)

    # When high-speed Telegram or storage stream is available, NEVER return youtube_video_id to player
    effective_yt_id = None if has_storage_video else (video.youtube_video_id if is_yt_valid else None)

    return {
        "has_video": True,
        "has_pdf": lecture.has_pdf,
        "title": lecture.title,
        "duration": video.duration if video else 0,
        "youtube_video_id": effective_yt_id,
        "stream_url": stream_url,
        "playback_url": playback_url,
        "embed_url": embed_url,
        "storage_provider": storage_provider,
        "pdf_download_url": pdf_url,
        "player_config": {
            "autoplay": False,
            "controls": True,
            "branding": "Course Wallah",
            "quality": (video.resolution if video else None) or "1080p"
        }
    }

