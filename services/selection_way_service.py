import os
import re
import urllib.parse
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# Config: API URL for deployed Vercel API or local fallback
SELECTION_API_URL = os.environ.get("SELECTION_WAY_API_URL", "").rstrip("/")

# Local data fallback paths
PLATFORM_DIR = Path(__file__).resolve().parent.parent
BUNDLE_DATA_DIR = PLATFORM_DIR / "data" / "selection_way"
WORKSPACE_DIR = PLATFORM_DIR.parent
EXTERNAL_DATA_DIR = WORKSPACE_DIR / "Selection-way-api" / "data"

def _resolve_data_dir() -> Path:
    if BUNDLE_DATA_DIR.exists() and (BUNDLE_DATA_DIR / "batches_index.json").exists():
        return BUNDLE_DATA_DIR
    if EXTERNAL_DATA_DIR.exists() and (EXTERNAL_DATA_DIR / "batches_index.json").exists():
        return EXTERNAL_DATA_DIR
    return BUNDLE_DATA_DIR

# In-memory RAM caches to avoid repeated disk reads
_BATCHES_INDEX_CACHE: Optional[List[Dict[str, Any]]] = None
_BATCH_CACHE: Dict[str, Dict[str, Any]] = {}
_SLUG_TO_ID_MAP: Dict[str, str] = {}

def _load_local_index() -> List[Dict[str, Any]]:
    global _BATCHES_INDEX_CACHE, _SLUG_TO_ID_MAP
    if _BATCHES_INDEX_CACHE is not None:
        return _BATCHES_INDEX_CACHE
        
    data_dir = _resolve_data_dir()
    index_file = data_dir / "batches_index.json"
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
            logger.error("Failed to load local Selection Way index: %s", e)
    return []

def _resolve_batch_id(batch_id_or_slug: str) -> str:
    _load_local_index()
    return _SLUG_TO_ID_MAP.get(batch_id_or_slug, batch_id_or_slug)

def _load_local_batch(batch_id: str) -> Optional[Dict[str, Any]]:
    resolved_id = _resolve_batch_id(batch_id)
    if resolved_id in _BATCH_CACHE:
        return _BATCH_CACHE[resolved_id]
        
    data_dir = _resolve_data_dir()
    sanitized_id = Path(resolved_id).stem.replace("..", "")
    batch_file = data_dir / "batches" / f"{sanitized_id}.json"
    if batch_file.exists():
        try:
            with open(batch_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                _BATCH_CACHE[resolved_id] = data
                return data
        except Exception as e:
            logger.error("Failed to load local Selection Way batch %s: %s", resolved_id, e)
    return None

async def fetch_batches_index() -> List[Dict[str, Any]]:
    """Fetches list of all 168 Selection Way batches."""
    if SELECTION_API_URL and not SELECTION_API_URL.startswith("http://localhost"):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(f"{SELECTION_API_URL}/api/batches")
                if res.status_code == 200:
                    data = res.json()
                    if data.get("status") == "success" and "batches" in data:
                        return data["batches"]
        except Exception as e:
            logger.warning("Remote Selection Way API unreachable (%s), falling back to local: %s", SELECTION_API_URL, e)

    return _load_local_index()

async def fetch_batch_data(batch_id_or_slug: str) -> Optional[Dict[str, Any]]:
    """Fetches detailed batch hierarchy (subjects, lectures, stream URLs, notes)."""
    return _load_local_batch(batch_id_or_slug)

def is_selection_way_batch(batch_slug_or_id: str) -> bool:
    """Checks if the given batch ID or slug belongs to Selection Way."""
    index = _load_local_index()
    for b in index:
        bid = b.get("batchId", "")
        bslug = b.get("batchSlug", "")
        if bid == batch_slug_or_id or bslug == batch_slug_or_id:
            return True
    return False

def format_selection_batch_for_platform(batch_raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transforms Selection Way JSON structure into Course Wallah platform hierarchy:
    Batch -> Subjects -> Folders -> Lectures (with direct M3U8 stream URLs & PDF URLs).
    """
    batch_id = batch_raw.get("batchId", "")
    batch_slug = batch_raw.get("batchSlug", batch_id)
    batch_name = batch_raw.get("batchName", "Selection Way Batch")
    thumbnail = batch_raw.get("thumbnail") or "/static/logo.png"
    category = batch_raw.get("category", "Government & Competitive Exams")
    
    sections = batch_raw.get("sections", [])
    folders_data = []
    total_lecs = 0
    total_notes = 0
    
    for s_idx, sec in enumerate(sections, start=1):
        sec_name = sec.get("sectionName", f"Unit {s_idx}")
        folder_id = f"f_{batch_id}_{s_idx}"
        
        lectures_list = []
        for l_idx, lec in enumerate(sec.get("lectures", []), start=1):
            total_lecs += 1
            lec_id = lec.get("id") or f"{batch_id}-{total_lecs}"
            s_url = lec.get("streamUrl") or lec.get("playerUrl") or lec.get("downloadUrl", "")
            lectures_list.append({
                "id": lec_id,
                "title": lec.get("title", f"Class {l_idx}"),
                "slug": f"lec-{total_lecs}",
                "index": total_lecs,
                "duration_seconds": 2700,
                "has_video": True,
                "has_pdf": False,
                "source_url": s_url,
                "stream_url": s_url,
                "player_url": s_url,
                "download_url": s_url,
                "source_pdf_url": None,
                "thumbnail_url": thumbnail,
                "created_at": "Updated"
            })
            
        for n_idx, note in enumerate(sec.get("notes", []), start=1):
            total_notes += 1
            note_id = note.get("id") or f"{batch_id}-n-{total_notes}"
            n_url = note.get("url") or note.get("pdfUrl") or note.get("downloadUrl", "")
            lectures_list.append({
                "id": note_id,
                "title": note.get("title", f"Notes {n_idx}"),
                "slug": f"note-{total_notes}",
                "index": total_lecs + total_notes,
                "duration_seconds": 0,
                "has_video": False,
                "has_pdf": True,
                "source_url": None,
                "source_pdf_url": n_url,
                "pdf_url": n_url,
                "player_url": n_url,
                "download_url": n_url,
                "thumbnail_url": thumbnail,
                "created_at": "Updated"
            })
            
        folders_data.append({
            "id": folder_id,
            "name": sec_name,
            "slug": f"folder-{s_idx}",
            "unit_number": s_idx,
            "total_lectures": len(lectures_list),
            "lectures": lectures_list
        })
        
    return {
        "id": batch_id,
        "name": batch_name,
        "slug": batch_slug,
        "app_name": "Selection Way",
        "category": category,
        "branch": "General & Competitive",
        "semester": "All",
        "academic_year": "2025-2026",
        "thumbnail_url": thumbnail,
        "total_lectures": total_lecs + total_notes,
        "total_videos": total_lecs,
        "total_pdfs": total_notes,
        "subjects": [
            {
                "id": f"subj_{batch_id}",
                "name": f"{batch_name} - Complete Course",
                "slug": f"course-{batch_slug}",
                "video_count": total_lecs,
                "notes_count": total_notes,
                "folders": folders_data
            }
        ]
    }

async def get_selection_way_app_object() -> Dict[str, Any]:
    """Returns unified App card for Selection Way with all 168 batches."""
    batches = await fetch_batches_index()
    batch_items = []
    for b in batches:
        bid = b.get("batchId")
        bslug = b.get("batchSlug", bid)
        batch_items.append({
            "id": bid,
            "name": b.get("batchName"),
            "slug": bslug,
            "category": b.get("category", "Government & Competitive Exams"),
            "branch": "SSC, Railway, Banking, Teaching & State Exams",
            "semester": "All",
            "academic_year": "2025-2026",
            "thumbnail_url": b.get("thumbnail") or "/static/logo.png",
            "total_lectures": b.get("totalLectures", 0) + b.get("totalNotes", 0),
            "total_videos": b.get("totalLectures", 0),
            "total_pdfs": b.get("totalNotes", 0),
            "total_units": b.get("totalSections", 0),
            "total_subjects": 1
        })
        
    return {
        "id": "app_selection_way",
        "name": "Selection Way",
        "slug": "selection-way",
        "description": "Complete Preparation for SSC CGL, CHSL, MTS, Railway ALP & NTPC, Banking, State Police, and Teaching Exams by Top Educators.",
        "icon_url": "https://selectionwayserver.hranker.com/courses/banners/1791379060936-vo_banner.jpg",
        "total_batches": len(batch_items),
        "batches": batch_items
    }

_LECTURE_TO_BATCH_MAP: Dict[str, str] = {}

def _find_batch_for_lecture(lecture_id: str) -> Optional[Dict[str, Any]]:
    """Locates the raw batch JSON containing this lecture/note ID with O(1) lookup."""
    if lecture_id in _LECTURE_TO_BATCH_MAP:
        cached_batch_id = _LECTURE_TO_BATCH_MAP[lecture_id]
        batch = _load_local_batch(cached_batch_id)
        if batch:
            return batch

    # Check candidate batch IDs derived from lecture ID
    candidates = []
    if "-n-" in lecture_id:
        candidates.append(lecture_id.split("-n-")[0])
    if "-" in lecture_id:
        candidates.append(lecture_id.rsplit("-", 1)[0])

    for possible_batch_id in candidates:
        batch = _load_local_batch(possible_batch_id)
        if batch:
            for sec in batch.get("sections", []):
                for lec in sec.get("lectures", []):
                    if lec.get("id") == lecture_id:
                        _LECTURE_TO_BATCH_MAP[lecture_id] = batch.get("batchId", possible_batch_id)
                        return batch
                for note in sec.get("notes", []):
                    if note.get("id") == lecture_id:
                        _LECTURE_TO_BATCH_MAP[lecture_id] = batch.get("batchId", possible_batch_id)
                        return batch

    index = _load_local_index()
    for b_meta in index:
        bid = b_meta.get("batchId")
        if not bid or bid in candidates:
            continue
        batch = _load_local_batch(bid)
        if not batch:
            continue
        for sec in batch.get("sections", []):
            for lec in sec.get("lectures", []):
                if lec.get("id") == lecture_id:
                    _LECTURE_TO_BATCH_MAP[lecture_id] = bid
                    return batch
            for note in sec.get("notes", []):
                if note.get("id") == lecture_id:
                    _LECTURE_TO_BATCH_MAP[lecture_id] = bid
                    return batch
    return None

async def get_selection_lecture_details(lecture_id: str) -> Optional[Dict[str, Any]]:
    """Returns lecture details formatted for the Course Wallah study player."""
    batch = _find_batch_for_lecture(lecture_id)
    if not batch:
        return None
        
    batch_name = batch.get("batchName", "Selection Way Batch")
    batch_id = batch.get("batchId", "")
    batch_slug = batch.get("batchSlug", batch_id)
    sections = batch.get("sections", [])
    
    all_videos = []
    current_item = None
    target_section_name = "Curriculum"
    
    global_idx = 0
    for sec in sections:
        sec_name = sec.get("sectionName", "General")
        for lec in sec.get("lectures", []):
            global_idx += 1
            s_url = lec.get("streamUrl") or lec.get("playerUrl") or lec.get("downloadUrl", "")
            item = {
                "id": lec.get("id") or f"{batch_id}-{global_idx}",
                "title": lec.get("title", f"Lecture {global_idx}"),
                "index": global_idx,
                "has_video": True,
                "has_pdf": False,
                "section_name": sec_name,
                "stream_url": s_url,
                "player_url": s_url,
                "download_url": s_url,
                "source_url": s_url,
            }
            all_videos.append(item)
            if lec.get("id") == lecture_id or item["id"] == lecture_id:
                current_item = item
                target_section_name = sec_name
                
        for note in sec.get("notes", []):
            global_idx += 1
            n_url = note.get("url") or note.get("pdfUrl") or note.get("downloadUrl", "")
            if note.get("id") == lecture_id:
                current_item = {
                    "id": note.get("id"),
                    "title": note.get("title", "Lecture Notes"),
                    "index": global_idx,
                    "has_video": False,
                    "has_pdf": True,
                    "section_name": sec_name,
                    "source_pdf_url": n_url,
                    "pdf_url": n_url,
                    "player_url": n_url,
                    "download_url": n_url,
                }
                target_section_name = sec_name

    if not current_item:
        return None

    prev_lec = None
    next_lec = None
    playlist = []
    
    for i, v in enumerate(all_videos):
        playlist.append({
            "id": v["id"],
            "title": v["title"],
            "index": v["index"],
            "has_video": True,
            "has_pdf": False,
            "is_active": v["id"] == current_item["id"]
        })
        if v["id"] == current_item["id"]:
            if i > 0:
                prev_lec = {"id": all_videos[i-1]["id"], "title": all_videos[i-1]["title"], "index": all_videos[i-1]["index"]}
            if i + 1 < len(all_videos):
                next_lec = {"id": all_videos[i+1]["id"], "title": all_videos[i+1]["title"], "index": all_videos[i+1]["index"]}

    return {
        "id": current_item["id"],
        "title": current_item["title"],
        "index": current_item["index"],
        "duration_seconds": 2700 if current_item.get("has_video") else 0,
        "has_video": current_item.get("has_video", False),
        "has_pdf": current_item.get("has_pdf", False),
        "source_url": current_item.get("stream_url"),
        "stream_url": current_item.get("stream_url"),
        "player_url": current_item.get("player_url"),
        "download_url": current_item.get("download_url"),
        "source_pdf_url": current_item.get("source_pdf_url"),
        "pdf_url": current_item.get("source_pdf_url"),
        "folder_name": target_section_name,
        "subject_name": f"{batch_name} - Complete Course",
        "batch_name": batch_name,
        "batch_slug": batch_slug,
        "app_name": "Selection Way",
        "app_slug": "selection-way",
        "prev_lecture": prev_lec,
        "next_lecture": next_lec,
        "playlist": playlist
    }

async def get_selection_lecture_access(lecture_id: str) -> Optional[Dict[str, Any]]:
    """Returns secure playback parameters for Selection Way video or note."""
    batch = _find_batch_for_lecture(lecture_id)
    if not batch:
        return None
        
    for sec in batch.get("sections", []):
        for lec in sec.get("lectures", []):
            if lec.get("id") == lecture_id:
                stream_url = lec.get("streamUrl") or lec.get("playerUrl") or lec.get("downloadUrl", "")
                
                # Check for YouTube URL
                yt_id = None
                if stream_url and ("youtube.com" in stream_url or "youtu.be" in stream_url):
                    yt_match = re.search(r'(?:v=|\/|youtu\.be\/|embed\/)([A-Za-z0-9_-]{11})', stream_url)
                    if yt_match:
                        yt_id = yt_match.group(1)

                is_m3u8 = bool(stream_url and (".m3u8" in stream_url))
                
                # Route .m3u8 streams through Course Wallah stream proxy to ensure 100% CORS compliance
                playback_stream = stream_url
                if is_m3u8 and stream_url:
                    needs_proxy = any(dom in stream_url for dom in ["hranker.com", "b-cdn.net", "restreamer.in", "amazonaws.com"])
                    if needs_proxy:
                        playback_stream = f"/api/v1/stream/m3u8/proxy?url={urllib.parse.quote(stream_url, safe='')}"

                return {
                    "has_video": True,
                    "has_pdf": False,
                    "title": lec.get("title"),
                    "duration": 2700,
                    "youtube_video_id": yt_id,
                    "video_id": None,
                    "stream_url": None if yt_id else playback_stream,
                    "playback_url": None if yt_id else playback_stream,
                    "player_url": None if yt_id else playback_stream,
                    "download_url": stream_url,
                    "embed_url": None,
                    "storage_provider": "selection_way" if not yt_id else "youtube",
                    "pdf_download_url": None,
                    "player_config": {
                        "autoplay": False,
                        "controls": True,
                        "branding": "Course Wallah",
                        "quality": "1080p"
                    }
                }
        for note in sec.get("notes", []):
            if note.get("id") == lecture_id:
                note_url = note.get("url") or note.get("pdfUrl") or note.get("downloadUrl", "")
                content_url = f"/api/v1/pdfs/{lecture_id}/content"
                return {
                    "has_video": False,
                    "has_pdf": True,
                    "title": note.get("title"),
                    "duration": 0,
                    "youtube_video_id": None,
                    "video_id": None,
                    "stream_url": None,
                    "playback_url": None,
                    "player_url": content_url,
                    "download_url": note_url,
                    "embed_url": None,
                    "storage_provider": "selection_way",
                    "pdf_download_url": content_url,
                    "source_pdf_url": note_url,
                    "access_url": content_url,
                    "player_config": {
                        "autoplay": False,
                        "controls": True,
                        "branding": "Course Wallah"
                    }
                }
    return None
