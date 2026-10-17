import os
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

PLATFORM_DIR = Path(__file__).resolve().parent.parent
BUNDLE_DATA_DIR = PLATFORM_DIR / "data" / "tutedude"

def _resolve_data_dir() -> Path:
    return BUNDLE_DATA_DIR

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
            logger.error("Failed to load local TuteDude index: %s", e)
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
            logger.error("Failed to load local TuteDude batch %s: %s", resolved_id, e)
    return None

async def fetch_batches_index() -> List[Dict[str, Any]]:
    """Fetches list of all 80 TuteDude batches."""
    return _load_local_index()

async def fetch_batch_data(batch_id_or_slug: str) -> Optional[Dict[str, Any]]:
    """Fetches detailed batch hierarchy (sections, lectures, stream URLs)."""
    return _load_local_batch(batch_id_or_slug)

def is_tutedude_batch(batch_slug_or_id: str) -> bool:
    """Checks if the given batch ID or slug belongs to TuteDude."""
    index = _load_local_index()
    for b in index:
        bid = b.get("batchId", "")
        bslug = b.get("batchSlug", "")
        if bid == batch_slug_or_id or bslug == batch_slug_or_id:
            return True
    return False

def format_tutedude_batch_for_platform(batch_raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transforms TuteDude JSON structure into Course Wallah platform hierarchy:
    Batch -> Subjects -> Folders -> Lectures (with direct M3U8 stream URLs).
    """
    batch_id = batch_raw.get("batchId", "")
    batch_slug = batch_raw.get("batchSlug", batch_id)
    batch_name = batch_raw.get("batchName", "TuteDude Course")
    thumbnail = batch_raw.get("thumbnail") or "/static/logo.png"
    category = batch_raw.get("category", "Technical & Development")
    
    sections = batch_raw.get("sections", [])
    folders_data = []
    total_lecs = 0
    total_notes = 0
    
    for s_idx, sec in enumerate(sections, start=1):
        sec_name = sec.get("sectionName", f"Module {s_idx}")
        folder_id = f"f_{batch_id}_{s_idx}"
        
        lectures_list = []
        for l_idx, lec in enumerate(sec.get("lectures", []), start=1):
            total_lecs += 1
            lec_id = lec.get("id") or f"td-{batch_id}-{total_lecs}"
            s_url = lec.get("streamUrl") or lec.get("playerUrl") or lec.get("downloadUrl", "")
            duration = lec.get("duration", 900)
            lectures_list.append({
                "id": lec_id,
                "title": lec.get("title", f"Lecture {l_idx}"),
                "slug": f"lec-{total_lecs}",
                "index": total_lecs,
                "duration_seconds": duration,
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
            note_id = note.get("id") or f"td-note-{batch_id}-{total_notes}"
            n_url = note.get("url") or note.get("pdfUrl") or note.get("downloadUrl", "")
            lectures_list.append({
                "id": note_id,
                "title": note.get("title", f"Assignment {n_idx}"),
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
        "app_name": "TuteDude",
        "category": category,
        "branch": "Technology & Practical Skills",
        "semester": "All",
        "academic_year": "2025-2026",
        "thumbnail_url": thumbnail,
        "total_lectures": total_lecs + total_notes,
        "total_videos": total_lecs,
        "total_pdfs": total_notes,
        "subjects": [
            {
                "id": f"subj_{batch_id}",
                "name": f"{batch_name} - Full Course",
                "slug": f"course-{batch_slug}",
                "video_count": total_lecs,
                "notes_count": total_notes,
                "folders": folders_data
            }
        ]
    }

async def get_tutedude_app_object() -> Dict[str, Any]:
    """Returns unified App card for TuteDude with all 80 batches."""
    batches = await fetch_batches_index()
    batch_items = []
    for b in batches:
        bid = b.get("batchId")
        bslug = b.get("batchSlug", bid)
        batch_items.append({
            "id": bid,
            "name": b.get("batchName"),
            "slug": bslug,
            "category": b.get("category", "Technology & Practical Skills"),
            "branch": "Programming, Data, Design & Cloud",
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
        "id": "app_tutedude",
        "name": "TuteDude",
        "slug": "tutedude",
        "description": "Comprehensive practical courses across Full Stack, DSA, AI/ML, Cloud, Cybersecurity, Video Editing, UI/UX and Marketing.",
        "icon_url": "https://www.tutedude.com/assets/svg/ComboPackIcons_PYTHON.svg",
        "total_batches": len(batch_items),
        "batches": batch_items
    }

def _find_batch_for_lecture(lecture_id: str) -> Optional[Dict[str, Any]]:
    """Locates the raw batch JSON containing this lecture ID."""
    # format: td-{slug}-{idx}
    if lecture_id.startswith("td-"):
        parts = lecture_id.split("-")
        if len(parts) >= 3:
            slug = "-".join(parts[1:-1])
            batch = _load_local_batch(slug)
            if batch:
                return batch

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
                    return batch
            for note in sec.get("notes", []):
                if note.get("id") == lecture_id:
                    return batch
    return None

async def get_tutedude_lecture_details(lecture_id: str) -> Optional[Dict[str, Any]]:
    """Returns lecture details formatted for the Course Wallah study player."""
    batch = _find_batch_for_lecture(lecture_id)
    if not batch:
        return None
        
    batch_name = batch.get("batchName", "TuteDude Course")
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
            duration = lec.get("duration", 900)
            item = {
                "id": lec.get("id") or f"td-{batch_id}-{global_idx}",
                "title": lec.get("title", f"Lecture {global_idx}"),
                "index": global_idx,
                "has_video": True,
                "has_pdf": False,
                "duration_seconds": duration,
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
                    "title": note.get("title", "Assignment"),
                    "index": global_idx,
                    "has_video": False,
                    "has_pdf": True,
                    "duration_seconds": 0,
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
        "duration_seconds": current_item.get("duration_seconds", 900),
        "has_video": current_item.get("has_video", False),
        "has_pdf": current_item.get("has_pdf", False),
        "source_url": current_item.get("stream_url"),
        "stream_url": current_item.get("stream_url"),
        "player_url": current_item.get("player_url"),
        "download_url": current_item.get("download_url"),
        "source_pdf_url": current_item.get("source_pdf_url"),
        "pdf_url": current_item.get("source_pdf_url"),
        "folder_name": target_section_name,
        "subject_name": f"{batch_name} - Full Course",
        "batch_name": batch_name,
        "batch_slug": batch_slug,
        "app_name": "TuteDude",
        "app_slug": "tutedude",
        "prev_lecture": prev_lec,
        "next_lecture": next_lec,
        "playlist": playlist
    }

async def get_tutedude_lecture_access(lecture_id: str) -> Optional[Dict[str, Any]]:
    """Returns secure playback parameters for TuteDude video or note."""
    batch = _find_batch_for_lecture(lecture_id)
    if not batch:
        return None
        
    for sec in batch.get("sections", []):
        for lec in sec.get("lectures", []):
            if lec.get("id") == lecture_id:
                stream_url = lec.get("streamUrl") or lec.get("playerUrl") or lec.get("downloadUrl", "")
                return {
                    "has_video": True,
                    "has_pdf": False,
                    "title": lec.get("title"),
                    "duration": lec.get("duration", 900),
                    "youtube_video_id": None,
                    "video_id": None,
                    "stream_url": stream_url,
                    "playback_url": stream_url,
                    "player_url": stream_url,
                    "download_url": stream_url,
                    "embed_url": None,
                    "storage_provider": "tutedude",
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
                return {
                    "has_video": False,
                    "has_pdf": True,
                    "title": note.get("title"),
                    "duration": 0,
                    "youtube_video_id": None,
                    "video_id": None,
                    "stream_url": None,
                    "playback_url": None,
                    "player_url": note_url,
                    "download_url": note_url,
                    "embed_url": None,
                    "storage_provider": "tutedude",
                    "pdf_download_url": note_url,
                    "access_url": note_url,
                    "player_config": {
                        "autoplay": False,
                        "controls": True,
                        "branding": "Course Wallah"
                    }
                }
    return None
