import os
import re
import json
import logging
import hashlib
from pathlib import Path
from typing import Optional, Dict, Any, List

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

PLATFORM_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PLATFORM_DIR / "data" / "physics_wallah"
SECRET_KEY = os.environ.get("PW_STREAM_SECRET", "sec_8f49ad20e5c10b7b659c21ef458a01cd79a0b12c")

_BATCHES_INDEX_CACHE: Optional[List[Dict[str, Any]]] = None
_BATCH_CACHE: Dict[str, Dict[str, Any]] = {}
_SLUG_TO_ID_MAP: Dict[str, str] = {}
_LECTURE_TO_BATCH_MAP: Dict[str, str] = {}
_STREAM_CACHE: Dict[str, Dict[str, Any]] = {}


def decrypt_stream_payload(encrypted_str: str) -> Optional[Dict[str, Any]]:
    """Decrypts AES-256-GCM encrypted payload from StudyPika / Spark backend."""
    if not encrypted_str or not isinstance(encrypted_str, str) or ":" not in encrypted_str:
        return None
    try:
        parts = encrypted_str.strip().split(":")
        if len(parts) != 2:
            return None
        iv = bytes.fromhex(parts[0])
        ciphertext_with_tag = bytes.fromhex(parts[1])
        key_hash = hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()
        aesgcm = AESGCM(key_hash)
        decrypted_bytes = aesgcm.decrypt(iv, ciphertext_with_tag, None)
        return json.loads(decrypted_bytes.decode("utf-8"))
    except Exception as e:
        logger.error(f"[PW_DECRYPT_ERR] Failed to decrypt stream payload: {e}")
        return None


def _load_local_index() -> List[Dict[str, Any]]:
    global _BATCHES_INDEX_CACHE, _SLUG_TO_ID_MAP
    if _BATCHES_INDEX_CACHE is not None:
        return _BATCHES_INDEX_CACHE

    index_file = DATA_DIR / "batches_index.json"
    if index_file.exists():
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                _BATCHES_INDEX_CACHE = json.load(f)
                _SLUG_TO_ID_MAP = {}
                for b in _BATCHES_INDEX_CACHE:
                    bid = b.get("batchId")
                    bslug = b.get("batchSlug")
                    if bid:
                        _SLUG_TO_ID_MAP[bid] = bid
                    if bslug:
                        _SLUG_TO_ID_MAP[bslug] = bid
                return _BATCHES_INDEX_CACHE
        except Exception as e:
            logger.error("Failed to load local Physics Wallah index: %s", e)
    return []


def _resolve_batch_id(batch_id_or_slug: str) -> str:
    _load_local_index()
    return _SLUG_TO_ID_MAP.get(batch_id_or_slug, batch_id_or_slug)


def _load_local_batch(batch_id: str) -> Optional[Dict[str, Any]]:
    resolved_id = _resolve_batch_id(batch_id)
    if resolved_id in _BATCH_CACHE:
        return _BATCH_CACHE[resolved_id]

    sanitized_id = Path(resolved_id).stem.replace("..", "")
    batch_file = DATA_DIR / "batches" / f"{sanitized_id}.json"
    if batch_file.exists():
        try:
            with open(batch_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                _BATCH_CACHE[resolved_id] = data
                return data
        except Exception as e:
            logger.error("Failed to load local PW batch %s: %s", resolved_id, e)
    return None


async def fetch_batches_index() -> List[Dict[str, Any]]:
    """Returns list of all available PW batches."""
    return _load_local_index()


def is_pw_batch(batch_id_or_slug: str) -> bool:
    resolved_id = _resolve_batch_id(batch_id_or_slug)
    index = _load_local_index()
    for b in index:
        if b.get("batchId") == resolved_id or b.get("batchSlug") == batch_id_or_slug:
            return True
    return False


async def fetch_batch_data(batch_id_or_slug: str) -> Optional[Dict[str, Any]]:
    resolved_id = _resolve_batch_id(batch_id_or_slug)
    return _load_local_batch(resolved_id)


def format_pw_batch_for_platform(batch: Dict[str, Any]) -> Dict[str, Any]:
    """Formats PW raw batch into Course Wallah standard batch hierarchy."""
    batch_id = batch.get("batchId", "")
    batch_slug = batch.get("batchSlug", batch_id)
    batch_name = batch.get("batchName", "Physics Wallah Batch")
    thumbnail = batch.get("thumbnail") or "https://files.catbox.moe/oqcdfv.png"
    category = batch.get("category", "IIT-JEE / NEET")

    # Group sections into Subjects -> Folders
    subject_map: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    notes_by_subject: Dict[str, List[Dict[str, Any]]] = {}

    for sec in batch.get("sections", []):
        s_name = sec.get("subjectName", "General")
        f_name = sec.get("sectionName", "Lectures")
        if s_name not in subject_map:
            subject_map[s_name] = {}
        if f_name not in subject_map[s_name]:
            subject_map[s_name][f_name] = []

        for lec in sec.get("lectures", []):
            subject_map[s_name][f_name].append({
                "id": lec.get("id"),
                "title": lec.get("title"),
                "url": lec.get("url"),
                "is_pdf": False,
                "schedule_id": lec.get("schedule_id"),
                "subject_id": lec.get("subject_id"),
                "batch_id": lec.get("batch_id") or batch_id,
            })

        for note in sec.get("notes", []):
            if s_name not in notes_by_subject:
                notes_by_subject[s_name] = []
            notes_by_subject[s_name].append({
                "id": note.get("id"),
                "title": note.get("title"),
                "url": note.get("url"),
                "is_pdf": True,
                "folder_name": f_name
            })

    subjects_list = []
    total_videos = 0
    total_pdfs = 0

    for s_idx, (s_name, folders) in enumerate(subject_map.items(), start=1):
        folders_list = []
        subj_videos = 0
        subj_pdfs = 0

        for f_idx, (f_name, items) in enumerate(folders.items(), start=1):
            lec_list = []
            for item in items:
                total_videos += 1
                subj_videos += 1
                lec_list.append({
                    "id": item["id"],
                    "title": item["title"],
                    "slug": f"lec-{item['id']}",
                    "index": len(lec_list) + 1,
                    "duration_seconds": 3600,
                    "has_video": True,
                    "has_pdf": False,
                    "stream_url": item["url"],
                    "player_url": item["url"],
                    "download_url": item["url"],
                    "source_url": item["url"],
                    "thumbnail_url": thumbnail,
                    "created_at": "Updated"
                })

            if lec_list:
                folders_list.append({
                    "id": f"f_{batch_id[:6]}_{s_idx}_{f_idx}",
                    "name": f_name,
                    "slug": f"folder-{s_idx}-{f_idx}",
                    "unit_number": f_idx,
                    "total_lectures": len(lec_list),
                    "lectures": lec_list
                })

        # Append PDF notes folder if present
        s_notes = notes_by_subject.get(s_name, [])
        if s_notes:
            notes_folder_lecs = []
            for n_idx, note in enumerate(s_notes, start=1):
                total_pdfs += 1
                subj_pdfs += 1
                notes_folder_lecs.append({
                    "id": note["id"],
                    "title": note["title"],
                    "slug": f"note-{note['id']}",
                    "index": len(notes_folder_lecs) + 1,
                    "duration_seconds": 0,
                    "has_video": False,
                    "has_pdf": True,
                    "source_url": None,
                    "source_pdf_url": note["url"],
                    "pdf_url": note["url"],
                    "player_url": note["url"],
                    "download_url": note["url"],
                    "thumbnail_url": thumbnail,
                    "created_at": "Updated"
                })
            folders_list.append({
                "id": f"f_{batch_id[:6]}_{s_idx}_notes",
                "name": "Class Notes & Assignments (PDF)",
                "slug": f"folder-{s_idx}-notes",
                "unit_number": len(folders_list) + 1,
                "total_lectures": len(notes_folder_lecs),
                "lectures": notes_folder_lecs
            })

        subjects_list.append({
            "id": f"subj_{batch_id[:6]}_{s_idx}",
            "name": s_name,
            "slug": f"subj-{s_idx}",
            "video_count": subj_videos,
            "notes_count": subj_pdfs,
            "folders": folders_list
        })

    return {
        "id": batch_id,
        "name": batch_name,
        "slug": batch_slug,
        "app_name": "Physics Wallah",
        "category": category,
        "branch": "IIT-JEE, NEET & Foundation",
        "semester": "All Batches",
        "academic_year": "2026-2027",
        "thumbnail_url": thumbnail,
        "total_lectures": total_videos + total_pdfs,
        "total_videos": total_videos,
        "total_pdfs": total_pdfs,
        "subjects": subjects_list
    }


async def get_pw_app_object() -> Dict[str, Any]:
    """Returns unified App card for Physics Wallah with all batches."""
    batches = await fetch_batches_index()
    batch_items = []
    for b in batches:
        bid = b.get("batchId")
        bslug = b.get("batchSlug", bid)
        batch_items.append({
            "id": bid,
            "name": b.get("batchName"),
            "slug": bslug,
            "category": b.get("category", "IIT-JEE / NEET"),
            "branch": "Physics Wallah Verified",
            "semester": "All",
            "academic_year": "2026-2027",
            "thumbnail_url": b.get("thumbnail") or "https://files.catbox.moe/oqcdfv.png",
            "total_lectures": b.get("totalLectures", 0) + b.get("totalNotes", 0),
            "total_videos": b.get("totalLectures", 0),
            "total_pdfs": b.get("totalNotes", 0),
            "total_units": b.get("totalSections", 0),
            "total_subjects": 4
        })

    return {
        "id": "app_physics_wallah",
        "name": "Physics Wallah [PW]",
        "slug": "physics-wallah",
        "description": "Official IIT-JEE, NEET, GATE & Board batches with high-speed direct stream playback.",
        "icon_url": "https://files.catbox.moe/oqcdfv.png",
        "total_batches": len(batch_items),
        "batches": batch_items
    }


def _find_batch_for_lecture(lecture_id: str) -> Optional[tuple[Dict[str, Any], Dict[str, Any]]]:
    """Finds raw batch and lecture item by lecture ID."""
    index = _load_local_index()
    for b_meta in index:
        bid = b_meta.get("batchId")
        if not bid:
            continue
        batch = _load_local_batch(bid)
        if not batch:
            continue
        for sec in batch.get("sections", []):
            for lec in sec.get("lectures", []):
                if lec.get("id") == lecture_id:
                    return batch, lec
            for note in sec.get("notes", []):
                if note.get("id") == lecture_id:
                    return batch, note
    return None


async def get_pw_lecture_details(lecture_id: str) -> Optional[Dict[str, Any]]:
    result = _find_batch_for_lecture(lecture_id)
    if not result:
        return None
    batch, item = result
    batch_id = batch.get("batchId", "")
    batch_name = batch.get("batchName", "Physics Wallah")
    is_pdf = item.get("is_pdf", False) or item.get("url", "").endswith(".pdf")

    return {
        "id": item.get("id"),
        "title": item.get("title"),
        "index": 1,
        "has_video": not is_pdf,
        "has_pdf": is_pdf,
        "duration_seconds": 0 if is_pdf else 3600,
        "source_url": item.get("url"),
        "stream_url": item.get("url"),
        "player_url": item.get("url"),
        "pdf_url": item.get("url") if is_pdf else None,
        "download_url": item.get("url"),
        "thumbnail_url": batch.get("thumbnail") or "https://files.catbox.moe/oqcdfv.png",
        "batch_id": batch_id,
        "batch_name": batch_name,
        "subject_name": "Physics Wallah",
        "folder_name": "Lectures",
        "schedule_id": item.get("schedule_id"),
        "subject_id": item.get("subject_id")
    }


async def resolve_pw_stream_url(batch_id: str, subject_id: Optional[str], schedule_id: str) -> Dict[str, Any]:
    """Resolves PW video stream via direct APIs with multi-tier failover."""
    cache_key = f"{batch_id}:{schedule_id}"
    if cache_key in _STREAM_CACHE:
        return _STREAM_CACHE[cache_key]

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Tier 1: Direct StudyPika video2 (fastest, direct JSON)
        try:
            ep1 = f"https://pw.studypika.eu.cc/api/video2?batchId={batch_id}&subjectId={subject_id or ''}&scheduleId={schedule_id}"
            res1 = await client.get(ep1, headers={"Accept": "application/json"})
            if res1.status_code == 200:
                raw = res1.json()
                data = raw.get("data")
                if isinstance(data, str):
                    data = decrypt_stream_payload(data)
                elif not data and isinstance(raw, dict):
                    data = raw
                if data and data.get("video_url") and data.get("video_url") != "404":
                    result = {
                        "video_url": data["video_url"],
                        "clear_keys": data.get("clearKeys") or {},
                        "success": True,
                        "proxy_provider": "pearl.mscilearn.in"
                    }
                    _STREAM_CACHE[cache_key] = result
                    return result
        except Exception as e:
            logger.warning(f"[PW_RESOLVE_WARN] Tier 1 failed: {e}")

        # Tier 2: Direct Spark MSCILearn upstream with Bearer Token
        try:
            tok_res = await client.get("https://pw.studypika.eu.cc/api/token", timeout=5.0)
            if tok_res.status_code == 200:
                tok = tok_res.json().get("token")
                if tok:
                    spark_url = f"https://spark.mscilearn.in/api/video2?batchId={batch_id}&subjectId={subject_id or ''}&scheduleId={schedule_id}"
                    spark_res = await client.get(spark_url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"}, timeout=10.0)
                    if spark_res.status_code == 200:
                        raw = spark_res.json()
                        data = raw.get("data")
                        if isinstance(data, str):
                            data = decrypt_stream_payload(data)
                        elif not data and isinstance(raw, dict):
                            data = raw
                        if data and data.get("video_url") and data.get("video_url") != "404":
                            result = {
                                "video_url": data["video_url"],
                                "clear_keys": data.get("clearKeys") or {},
                                "success": True,
                                "proxy_provider": "spark.mscilearn.in"
                            }
                            _STREAM_CACHE[cache_key] = result
                            return result
        except Exception as e:
            logger.warning(f"[PW_RESOLVE_WARN] Tier 2 Spark failed: {e}")

        # Tier 3: Secondary endpoints (/video and /video3)
        secondary_endpoints = [
            f"https://pw.studypika.eu.cc/api/video?batchId={batch_id}&subjectId={subject_id or ''}&scheduleId={schedule_id}",
            f"https://pw.studypika.eu.cc/api/video3?batchId={batch_id}&subjectId={subject_id or ''}&scheduleId={schedule_id}"
        ]
        for ep in secondary_endpoints:
            try:
                res = await client.get(ep, headers={"Accept": "application/json"})
                if res.status_code == 200:
                    raw = res.json()
                    data = raw.get("data")
                    if isinstance(data, str):
                        data = decrypt_stream_payload(data)
                    elif not data and isinstance(raw, dict):
                        data = raw
                    if data and data.get("video_url") and data.get("video_url") != "404":
                        result = {
                            "video_url": data["video_url"],
                            "clear_keys": data.get("clearKeys") or {},
                            "success": True,
                            "proxy_provider": "pearl.mscilearn.in"
                        }
                        _STREAM_CACHE[cache_key] = result
                        return result
            except Exception as e:
                logger.warning(f"[PW_RESOLVE_WARN] Secondary endpoint {ep} failed: {e}")

    return {
        "success": False,
        "video_url": None,
        "clear_keys": {},
        "error": "Stream not accessible or requires subscription"
    }


async def get_pw_lecture_access(lecture_id: str) -> Optional[Dict[str, Any]]:
    """Resolves direct playable stream credentials with zero-token friction and precise format detection."""
    result = _find_batch_for_lecture(lecture_id)
    if not result:
        return None

    batch, item = result
    batch_id = batch.get("batchId", "")
    is_pdf = item.get("is_pdf", False) or item.get("url", "").endswith(".pdf")

    if is_pdf:
        return {
            "lecture_id": lecture_id,
            "has_video": False,
            "has_pdf": True,
            "pdf_url": item.get("url"),
            "storage_provider": "pw_static"
        }

    # Extract query params from URL if not on item
    schedule_id = item.get("schedule_id")
    subject_id = item.get("subject_id")
    url = item.get("url", "")

    if not schedule_id and "scheduleId=" in url:
        m = re.search(r"scheduleId=([a-zA-Z0-9]+)", url)
        if m:
            schedule_id = m.group(1)
    if not subject_id and "subjectId=" in url:
        m = re.search(r"subjectId=([a-zA-Z0-9]+)", url)
        if m:
            subject_id = m.group(1)
    if not batch_id and "batchId=" in url:
        m = re.search(r"batchId=([a-zA-Z0-9]+)", url)
        if m:
            batch_id = m.group(1)

    # Check for companion Class Notes / DPP PDF attached to this video lecture
    companion_pdf = item.get("pdf_url") or item.get("source_pdf_url")
    if not companion_pdf and schedule_id:
        for sec in batch.get("sections", []):
            for note in sec.get("notes", []):
                if note.get("schedule_id") == schedule_id or (note.get("title") and item.get("title") and note.get("title") in item.get("title")):
                    companion_pdf = note.get("url")
                    break
            if companion_pdf:
                break

    has_companion_pdf = bool(companion_pdf)

    if schedule_id and batch_id:
        stream_info = await resolve_pw_stream_url(batch_id, subject_id, schedule_id)
        if stream_info.get("success") and stream_info.get("video_url"):
            raw_url = stream_info["video_url"]
            clear_keys = stream_info.get("clear_keys") or {}

            # 1. YouTube Stream (Embed, nocookie, or direct video ID)
            yt_match = re.search(r"(?:embed/|v=|youtu\.be/)([a-zA-Z0-9_-]{11})", raw_url)
            if yt_match or "youtube" in raw_url or "youtu.be" in raw_url:
                yt_id = yt_match.group(1) if yt_match else "rZoFqSetuvY"
                return {
                    "lecture_id": lecture_id,
                    "has_video": True,
                    "has_pdf": has_companion_pdf,
                    "pdf_url": companion_pdf,
                    "pdf_view_url": companion_pdf,
                    "pdf_download_url": companion_pdf,
                    "stream_url": f"https://www.youtube-nocookie.com/embed/{yt_id}",
                    "playback_url": f"https://www.youtube-nocookie.com/embed/{yt_id}",
                    "youtube_video_id": yt_id,
                    "stream_type": "youtube",
                    "clear_keys": {},
                    "storage_provider": "youtube",
                    "playback_mode": "plyr_youtube"
                }

            # 2. DASH DRM / Non-DRM Stream (.mpd or clearKeys present)
            if ".mpd" in raw_url or bool(clear_keys):
                return {
                    "lecture_id": lecture_id,
                    "has_video": True,
                    "has_pdf": has_companion_pdf,
                    "pdf_url": companion_pdf,
                    "pdf_view_url": companion_pdf,
                    "pdf_download_url": companion_pdf,
                    "stream_url": raw_url,
                    "playback_url": raw_url,
                    "stream_type": "dash",
                    "clear_keys": clear_keys,
                    "storage_provider": "pearl_mscilearn",
                    "proxy_domain": "pearl.mscilearn.in",
                    "playback_mode": "shaka"
                }

            # 3. HLS Stream (.m3u8)
            if ".m3u8" in raw_url:
                return {
                    "lecture_id": lecture_id,
                    "has_video": True,
                    "has_pdf": has_companion_pdf,
                    "pdf_url": companion_pdf,
                    "pdf_view_url": companion_pdf,
                    "pdf_download_url": companion_pdf,
                    "stream_url": raw_url,
                    "playback_url": raw_url,
                    "stream_type": "hls",
                    "clear_keys": {},
                    "storage_provider": "hls_stream",
                    "playback_mode": "hls"
                }

            # 4. Direct MP4
            return {
                "lecture_id": lecture_id,
                "has_video": True,
                "has_pdf": has_companion_pdf,
                "pdf_url": companion_pdf,
                "pdf_view_url": companion_pdf,
                "pdf_download_url": companion_pdf,
                "stream_url": raw_url,
                "playback_url": raw_url,
                "stream_type": "direct",
                "clear_keys": {},
                "storage_provider": "direct_video",
                "playback_mode": "native"
            }

    # Fallback to direct URL if resolution returned 404
    clean_url = "https://" + url.split("https://")[-1] if "https://" in url else url
    yt_fallback = re.search(r"(?:embed/|v=|youtu\.be/)([a-zA-Z0-9_-]{11})", clean_url)
    if yt_fallback:
        yt_id = yt_fallback.group(1)
        return {
            "lecture_id": lecture_id,
            "has_video": True,
            "has_pdf": has_companion_pdf,
            "pdf_url": companion_pdf,
            "pdf_view_url": companion_pdf,
            "pdf_download_url": companion_pdf,
            "stream_url": f"https://www.youtube-nocookie.com/embed/{yt_id}",
            "playback_url": f"https://www.youtube-nocookie.com/embed/{yt_id}",
            "youtube_video_id": yt_id,
            "stream_type": "youtube",
            "clear_keys": {},
            "storage_provider": "youtube",
            "playback_mode": "plyr_youtube"
        }

    return {
        "lecture_id": lecture_id,
        "has_video": True,
        "has_pdf": False,
        "stream_url": clean_url,
        "playback_url": clean_url,
        "stream_type": "embed",
        "storage_provider": "courseswala",
        "playback_mode": "browser"
    }
