import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

from storage.telegram_stream.client_pool import TelegramClientPool, LRUHeaderCache
from storage.providers.telegram_stream import TelegramStreamStorageProvider
from storage.base import StorageProviderStatus
from api.server import app
from db.connection import get_db_session
from db.models import (
    App, Batch, Subject, Folder, Lecture, Video,
    VideoStorage, VideoStorageStatus, PublicationStatus
)


@pytest.mark.asyncio
async def test_lru_header_cache():
    cache = LRUHeaderCache(max_items=2)
    cache.set_header("item1", b"header1", {"file_size": 100})
    cache.set_header("item2", b"header2", {"file_size": 200})

    assert cache.get_header("item1") == b"header1"
    assert cache.get_meta("item1") == {"file_size": 100}

    # Adding a 3rd item should evict oldest (item2 since item1 was touched)
    cache.set_header("item3", b"header3", {"file_size": 300})
    assert cache.get_header("item3") == b"header3"
    assert cache.get_header("item1") == b"header1"
    assert cache.get_header("item2") is None


@pytest.mark.asyncio
async def test_telegram_stream_storage_provider_upload(tmp_path):
    video_file = tmp_path / "test_lecture.mp4"
    video_file.write_bytes(b"dummy video content for telegram stream testing" * 100)

    provider = TelegramStreamStorageProvider(enabled=True)

    mock_pool_res = {
        "success": True,
        "message_id": 998877,
        "chat_id": 123456,
        "file_id": "BAADBAAD_test_file_id",
        "file_size": os.path.getsize(str(video_file)),
        "mime_type": "video/mp4",
        "file_name": "test_lecture.mp4",
    }

    with patch.object(provider.pool, "upload_video", new=AsyncMock(return_value=mock_pool_res)):
        res = await provider.upload(
            file_path=str(video_file),
            title="Introduction to Digital Electronics",
            metadata={"batch_id": "test_batch", "duration": 120}
        )

        assert res.success is True
        assert res.status == StorageProviderStatus.READY.value
        assert res.provider == "telegram"
        assert res.provider_video_id == "998877"
        assert res.playback_url == "/api/v1/stream/tg/998877"
        assert res.hls_url == "/api/v1/stream/tg/998877"
        assert res.embed_url is None


@pytest.mark.asyncio
async def test_fastapi_stream_endpoint_range_requests():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        pool = TelegramClientPool.get_instance()

        mock_media = MagicMock()
        mock_media.file_size = 5000000  # 5 MB
        mock_media.mime_type = "video/mp4"
        mock_media.file_name = "∮◯⚡ Course Wallah 🎓🔥.mp4"

        async def fake_stream_range(chat_id, msg_id, start, end, chunk_size=1024*1024, media=None, **kwargs):
            # Yield simulated byte chunks
            chunk = b"A" * (end - start + 1)
            yield chunk

        with patch.object(pool, "get_media_info", new=AsyncMock(return_value=(mock_media, 5000000, "video/mp4"))), \
             patch.object(pool, "stream_range", side_effect=fake_stream_range):

            # 1. Test HEAD metadata request with a valid short-lived playback token
            from api.playback_tokens import create_playback_token
            token = create_playback_token("test-video", 998877)
            stream_url = f"/api/v1/stream/tg/998877?video_id=test-video&token={token}"
            head_res = await client.head(stream_url)
            assert head_res.status_code == 200
            assert head_res.headers.get("Accept-Ranges") == "bytes"
            assert head_res.headers.get("Content-Length") == "5000000"
            assert "video/mp4" in head_res.headers.get("Content-Type")
            assert "filename*=UTF-8''" in head_res.headers.get("Content-Disposition")
            assert "🔥" not in head_res.headers.get("Content-Disposition")

            # 2. Test Range request: bytes=0-1048575 (First 1MB)
            range_res = await client.get(stream_url, headers={"Range": "bytes=0-1048575"})
            assert range_res.status_code == 206
            assert "filename*=UTF-8''" in range_res.headers.get("Content-Disposition")
            assert "🔥" not in range_res.headers.get("Content-Disposition")
            assert range_res.headers.get("Content-Range") == "bytes 0-1048575/5000000"
            assert range_res.headers.get("Content-Length") == "1048576"
            assert len(range_res.content) == 1048576

            # 3. Test Range request: Seeking to middle bytes=2000000-2999999
            seek_res = await client.get(stream_url, headers={"Range": "bytes=2000000-2999999"})
            assert seek_res.status_code == 206
            assert seek_res.headers.get("Content-Range") == "bytes 2000000-2999999/5000000"
            assert seek_res.headers.get("Content-Length") == "1000000"
            assert len(seek_res.content) == 1000000

            # 4. Test Invalid Range out of bounds
            bad_range_res = await client.get(stream_url, headers={"Range": "bytes=6000000-7000000"})
            assert bad_range_res.status_code == 416



@pytest.mark.asyncio
async def test_stream_rejects_missing_or_invalid_playback_token():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/stream/tg/998877?video_id=test-video")
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_stream_resolves_chat_id_saved_for_video():
    from api.routes.stream import _resolve_storage_chat_id

    pool = MagicMock()
    pool.storage_chat_id = -1004341532959

    storage = MagicMock()
    storage.metadata_json = {"chat_id": "-1004469977085"}
    query_result = MagicMock()
    query_result.first.return_value = (storage, None)

    session = MagicMock()
    session.execute = AsyncMock(return_value=query_result)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)

    with patch("api.routes.stream.get_db_session", return_value=context):
        chat_id = await _resolve_storage_chat_id(3, pool)

    assert chat_id == -1004469977085


@pytest.mark.asyncio
async def test_lecture_access_returns_telegram_stream_url():
    import uuid
    uid = uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        app_rec = App(name=f"Stream App {uid}", slug=f"stream-app-{uid}")
        db.add(app_rec)
        await db.flush()

        batch_rec = Batch(app_id=app_rec.id, name=f"Stream Batch {uid}", slug=f"stream-batch-{uid}")
        db.add(batch_rec)
        await db.flush()

        subj_rec = Subject(batch_id=batch_rec.id, name=f"Digital Electronics {uid}", slug=f"dig-elec-{uid}")
        db.add(subj_rec)
        await db.flush()

        folder_rec = Folder(subject_id=subj_rec.id, name=f"Module 1 {uid}", slug=f"mod-1-{uid}")
        db.add(folder_rec)
        await db.flush()

        lec_rec = Lecture(
            batch_id=batch_rec.id,
            subject_id=subj_rec.id,
            folder_id=folder_rec.id,
            title=f"Lecture 01 - Binary Logic {uid}",
            slug=f"lec-01-bin-{uid}",
            lecture_index=1,
            has_video=True,
            publication_status=PublicationStatus.PUBLISHED
        )
        db.add(lec_rec)
        await db.flush()

        vid_rec = Video(lecture_id=lec_rec.id, title=f"Lecture 01 - Binary Logic {uid}", duration=1800.0)
        db.add(vid_rec)
        await db.flush()

        tg_st = VideoStorage(
            video_id=vid_rec.id,
            provider="telegram",
            status=VideoStorageStatus.READY.value,
            provider_video_id="554433",
            playback_url="/api/v1/stream/tg/554433",
            hls_url="/api/v1/stream/tg/554433",
        )
        db.add(tg_st)
        await db.commit()

        lec_id = lec_rec.id
        video_rec_id = vid_rec.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get(f"/api/v1/lectures/{lec_id}/access")
        assert res.status_code == 200
        data = res.json()
        assert data["has_video"] is True
        assert data["storage_provider"] == "telegram"
        assert data["youtube_video_id"] is None
        assert data["stream_url"].startswith(f"/api/v1/stream/tg/554433?video_id={video_rec_id}&token=")
        assert data["playback_url"] == data["stream_url"]
        assert data["embed_url"] is None


def test_caption_formatter_matches_design_template():
    from storage.telegram_stream.caption_formatter import format_telegram_channel_caption

    caption = format_telegram_channel_caption(
        title="Class-128 | Preposition Practice",
        metadata={
            "lecture_index": 9,
            "subject_name": "Rakesh Sir & Team.",
            "batch_name": "SSC Pratham Batch-02",
            "folder_name": "English — Vocab (Live",
            "unit_number": "4",
            "topic_name": "Vocab (Live",
            "resolution": "720p",
            "width": 1280,
            "height": 720,
        },
        width=1280,
        height=720,
        resolution="720p"
    )

    assert "<blockquote>——— ✦ 009 ✦——— ❞</blockquote>" in caption
    assert "<blockquote>📚 Rakesh Sir &amp; Team. ❞</blockquote>" in caption
    assert "<blockquote>📖 SSC Pratham Batch-02 ❞</blockquote>" in caption
    assert "<blockquote>📌 Unit 4 — English — Vocab (Live ❞</blockquote>" in caption
    assert "<blockquote>📝 Topic : Vocab (Live ❞</blockquote>" in caption
    assert "<blockquote>🎬 Title : ) Class-128 | Preposition Practice ❞</blockquote>" in caption
    assert "<blockquote>├── Extention : ∮◯⚡ Course Wallah 🎓 🔥.mp4 ❞\n├── Resolution : 720p (1280 × 720)</blockquote>" in caption
    assert "<blockquote>📚 Subject » Rakesh Sir &amp; Team. ❞</blockquote>" in caption
    assert "<blockquote>📚 Course » SSC Pratham Batch-02 ❞</blockquote>" in caption
    assert "<blockquote>🌟 Extracted By : ∮◯⚡ 🅲🅾🆄🆁🆂🅴 🆆🅰🅻🅻🅰🅷 💻 ❞\n∮◯🎓🔥</blockquote>" in caption
    assert len(caption) <= 1024


def test_caption_formatter_html_escape_and_truncation():
    from storage.telegram_stream.caption_formatter import format_telegram_channel_caption

    # Test with special HTML characters
    caption = format_telegram_channel_caption(
        title="<Math> & Science: 100% <Real>",
        metadata={
            "lecture_index": 1,
            "subject_name": "Physics & Chemistry <Advanced>",
            "batch_name": "NEET 2026 > Ultimate",
            "folder_name": "Optics & Waves",
            "topic_name": "Ray Optics",
        }
    )
    assert "&lt;Math&gt; &amp; Science: 100% &lt;Real&gt;" in caption
    assert "Physics &amp; Chemistry &lt;Advanced&gt;" in caption
    assert "NEET 2026 &gt; Ultimate" in caption
    assert len(caption) <= 1024

    # Test with massive length
    long_title = "A" * 1500
    caption_long = format_telegram_channel_caption(title=long_title)
    assert len(caption_long) <= 1024
    assert caption_long.endswith("∮◯🎓🔥</blockquote>")

@pytest.mark.asyncio
async def test_stream_range_uses_same_client_that_resolved_media():
    """A media object resolved by a worker must be streamed by that same worker."""
    pool = TelegramClientPool()
    pool._is_started = True

    first = MagicMock()
    first.name = "first-client"
    first.get_messages = AsyncMock(side_effect=RuntimeError("cannot access message"))
    first.stream_media = MagicMock()

    media = MagicMock()
    media.file_size = 16
    media.mime_type = "video/mp4"
    media.file_name = "lecture.mp4"
    message = MagicMock()
    message.empty = False
    message.video = media
    message.document = None

    second = MagicMock()
    second.name = "second-client"
    second.get_messages = AsyncMock(return_value=message)

    async def chunks(*args, **kwargs):
        yield b"0123456789abcdef"

    second.stream_media = MagicMock(side_effect=chunks)
    pool.clients = [first, second]

    # Ensure this test exercises Telegram rather than the local header cache.
    from storage.telegram_stream.client_pool import GLOBAL_HEADER_CACHE
    GLOBAL_HEADER_CACHE.cache.clear()
    GLOBAL_HEADER_CACHE.meta.clear()

    received = b"".join([
        chunk async for chunk in pool.stream_range(
            chat_id=-1004469977085,
            message_id=4,
            start_byte=0,
            end_byte=15,
        )
    ])

    assert received == b"0123456789abcdef"
    first.stream_media.assert_not_called()
    second.stream_media.assert_called_once_with(media, offset=0, limit=2)

