import os
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# Config: API URL for deployed Vercel API or local fallback
APNA_API_URL = os.environ.get("APNA_COLLEGE_API_URL", "").rstrip("/")

# Local data fallback paths
PLATFORM_DIR = Path(__file__).resolve().parent.parent
BUNDLE_DATA_DIR = PLATFORM_DIR / "data" / "apna_college"
WORKSPACE_DIR = PLATFORM_DIR.parent
EXTERNAL_DATA_DIR = WORKSPACE_DIR / "apna-college-api" / "data"

def _resolve_data_dir() -> Path:
    if BUNDLE_DATA_DIR.exists() and (BUNDLE_DATA_DIR / "batches_index.json").exists():
        return BUNDLE_DATA_DIR
    if EXTERNAL_DATA_DIR.exists() and (EXTERNAL_DATA_DIR / "batches_index.json").exists():
        return EXTERNAL_DATA_DIR
    return BUNDLE_DATA_DIR

LOCAL_DATA_DIR = _resolve_data_dir()

# In-memory RAM caches to avoid repeated disk reads or HTTP requests
_BATCHES_INDEX_CACHE: Optional[List[Dict[str, Any]]] = None
_BATCH_CACHE: Dict[str, Dict[str, Any]] = {}

def _load_local_index() -> List[Dict[str, Any]]:
    global _BATCHES_INDEX_CACHE
    if _BATCHES_INDEX_CACHE is not None:
        return _BATCHES_INDEX_CACHE
        
    data_dir = _resolve_data_dir()
    index_file = data_dir / "batches_index.json"
    if index_file.exists():
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                _BATCHES_INDEX_CACHE = json.load(f)
                return _BATCHES_INDEX_CACHE
        except Exception as e:
            logger.error("Failed to load local Apna College index: %s", e)
    return []

def _load_local_batch(batch_id: str) -> Optional[Dict[str, Any]]:
    if batch_id in _BATCH_CACHE:
        return _BATCH_CACHE[batch_id]
        
    data_dir = _resolve_data_dir()
    sanitized_id = Path(batch_id).stem.replace("..", "")
    batch_file = data_dir / "batches" / f"{sanitized_id}.json"
    if batch_file.exists():
        try:
            with open(batch_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                _BATCH_CACHE[batch_id] = data
                return data
        except Exception as e:
            logger.error("Failed to load local Apna College batch %s: %s", batch_id, e)
    return None

async def fetch_batches_index() -> List[Dict[str, Any]]:
    """Fetches list of all 44 Apna College batches (via API or local cache)."""
    if APNA_API_URL and not APNA_API_URL.startswith("http://localhost"):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(f"{APNA_API_URL}/api/batches")
                if res.status_code == 200:
                    data = res.json()
                    if data.get("success") and "batches" in data:
                        return data["batches"]
        except Exception as e:
            logger.warning("Remote Apna College API unreachable (%s), falling back to local: %s", APNA_API_URL, e)

    return _load_local_index()

async def fetch_batch_data(batch_id_or_slug: str) -> Optional[Dict[str, Any]]:
    """Fetches detailed batch hierarchy (subjects, lectures, stream URLs, notes)."""
    if APNA_API_URL and not APNA_API_URL.startswith("http://localhost"):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(f"{APNA_API_URL}/api/batch", params={"id": batch_id_or_slug})
                if res.status_code == 200:
                    data = res.json()
                    if data.get("success") and "batch" in data:
                        return data["batch"]
        except Exception as e:
            logger.warning("Remote Apna College API error fetching batch %s: %s", batch_id_or_slug, e)

    return _load_local_batch(batch_id_or_slug)

def is_apna_college_batch(batch_slug_or_id: str) -> bool:
    """Checks if the given batch ID or slug belongs to Apna College."""
    index = _load_local_index()
    for b in index:
        bid = b.get("batchId", "")
        if bid == batch_slug_or_id or bid.replace("_", "-") == batch_slug_or_id:
            return True
    return False

def format_apna_batch_for_platform(batch_raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transforms the Apna College JSON structure into Course Wallah platform hierarchy:
    Batch -> Subjects -> Folders -> Lectures (with direct M3U8 stream URLs).
    """
    batch_id = batch_raw.get("batchId", "")
    batch_name = batch_raw.get("batchName", "Apna College Batch")
    thumbnail = batch_raw.get("thumbnail") or "/static/logo.png"
    
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
            lec_id = lec.get("id") or f"{batch_id}_l_{total_lecs}"
            lectures_list.append({
                "id": lec_id,
                "title": lec.get("title", f"Lecture {l_idx}"),
                "slug": f"lec-{total_lecs}",
                "index": total_lecs,
                "duration_seconds": 1800,
                "has_video": True,
                "has_pdf": False,
                "source_url": lec.get("streamUrl", ""),
                "source_pdf_url": None,
                "thumbnail_url": thumbnail,
                "created_at": "Updated"
            })
            
        for n_idx, note in enumerate(sec.get("notes", []), start=1):
            total_notes += 1
            note_id = note.get("id") or f"{batch_id}_n_{total_notes}"
            lectures_list.append({
                "id": note_id,
                "title": note.get("title", f"Notes {n_idx}"),
                "slug": f"note-{total_notes}",
                "index": total_lecs + total_notes,
                "duration_seconds": 0,
                "has_video": False,
                "has_pdf": True,
                "source_url": None,
                "source_pdf_url": note.get("url", ""),
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
        "slug": batch_id,
        "app_name": "Apna College",
        "category": "Coding & Development",
        "branch": "Computer Science",
        "semester": "All",
        "academic_year": "2025-2026",
        "thumbnail_url": thumbnail,
        "total_lectures": total_lecs + total_notes,
        "total_videos": total_lecs,
        "total_pdfs": total_notes,
        "subjects": [
            {
                "id": f"subj_{batch_id}",
                "name": f"{batch_name} - Complete Curriculum",
                "slug": f"curriculum-{batch_id}",
                "video_count": total_lecs,
                "notes_count": total_notes,
                "folders": folders_data
            }
        ]
    }

async def get_apna_college_app_object() -> Dict[str, Any]:
    """Returns the unified App card for Apna College with real-time batches count."""
    batches = await fetch_batches_index()
    batch_items = []
    for b in batches:
        batch_items.append({
            "id": b.get("batchId"),
            "name": b.get("batchName"),
            "slug": b.get("batchId"),
            "category": "Coding & Development",
            "branch": "Computer Science",
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
        "id": "app_apna_college",
        "name": "Apna College",
        "slug": "apna-college",
        "description": "Full Stack Web Development (Delta), DSA with C++ & Java (Alpha/Sigma), System Design & Placements by Aman Dhattarwal & Shradha Khapra.",
        "icon_url": "https://lwfiles.mycourse.app/62a6cd5e1e9e2fbf212d608d-public/a592a05df20f1e645643849fea5e7ff6.jpg",
        "total_batches": len(batch_items),
        "batches": batch_items
    }

def _find_batch_for_lecture(lecture_id: str) -> Optional[Dict[str, Any]]:
    """Locates the raw batch JSON containing this lecture/note ID."""
    if "-" in lecture_id:
        possible_batch_id = lecture_id.rsplit("-", 1)[0]
        batch = _load_local_batch(possible_batch_id)
        if batch:
            for sec in batch.get("sections", []):
                for lec in sec.get("lectures", []):
                    if lec.get("id") == lecture_id:
                        return batch
                for note in sec.get("notes", []):
                    if note.get("id") == lecture_id:
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

async def get_apna_lecture_details(lecture_id: str) -> Optional[Dict[str, Any]]:
    """Returns lecture details formatted for the Course Wallah study player."""
    batch = _find_batch_for_lecture(lecture_id)
    if not batch:
        return None
        
    batch_name = batch.get("batchName", "Apna College Batch")
    batch_id = batch.get("batchId", "")
    sections = batch.get("sections", [])
    
    all_videos = []
    current_item = None
    target_section_name = "Curriculum"
    
    global_idx = 0
    for sec in sections:
        sec_name = sec.get("sectionName", "General")
        for lec in sec.get("lectures", []):
            global_idx += 1
            item = {
                "id": lec.get("id") or f"{batch_id}_l_{global_idx}",
                "title": lec.get("title", f"Lecture {global_idx}"),
                "index": global_idx,
                "has_video": True,
                "has_pdf": False,
                "section_name": sec_name,
                "stream_url": lec.get("streamUrl", "")
            }
            all_videos.append(item)
            if lec.get("id") == lecture_id or item["id"] == lecture_id:
                current_item = item
                target_section_name = sec_name
                
        for note in sec.get("notes", []):
            global_idx += 1
            if note.get("id") == lecture_id:
                current_item = {
                    "id": note.get("id"),
                    "title": note.get("title", "Lecture Notes"),
                    "index": global_idx,
                    "has_video": False,
                    "has_pdf": True,
                    "section_name": sec_name,
                    "source_pdf_url": note.get("url", "")
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
        "duration_seconds": 1800 if current_item.get("has_video") else 0,
        "has_video": current_item.get("has_video", False),
        "has_pdf": current_item.get("has_pdf", False),
        "folder_name": target_section_name,
        "subject_name": f"{batch_name} - Complete Curriculum",
        "batch_name": batch_name,
        "batch_slug": batch_id,
        "app_name": "Apna College",
        "app_slug": "apna-college",
        "prev_lecture": prev_lec,
        "next_lecture": next_lec,
        "playlist": playlist
    }

async def get_apna_lecture_access(lecture_id: str) -> Optional[Dict[str, Any]]:
    """Returns secure playback parameters for Apna College video or note."""
    batch = _find_batch_for_lecture(lecture_id)
    if not batch:
        return None
        
    for sec in batch.get("sections", []):
        for lec in sec.get("lectures", []):
            if lec.get("id") == lecture_id:
                stream_url = lec.get("streamUrl", "")
                is_m3u8 = bool(stream_url and (".m3u8" in stream_url))
                return {
                    "has_video": True,
                    "has_pdf": False,
                    "title": lec.get("title"),
                    "duration": 1800,
                    "youtube_video_id": None,
                    "stream_url": stream_url if is_m3u8 else None,
                    "playback_url": stream_url,
                    "embed_url": stream_url if not is_m3u8 else None,
                    "storage_provider": "wistia" if "wistia" in stream_url else "apna_college",
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
                return {
                    "has_video": False,
                    "has_pdf": True,
                    "title": note.get("title"),
                    "duration": 0,
                    "youtube_video_id": None,
                    "stream_url": None,
                    "playback_url": None,
                    "embed_url": None,
                    "storage_provider": "apna_college",
                    "pdf_download_url": note.get("url"),
                    "player_config": {
                        "autoplay": False,
                        "controls": True,
                        "branding": "Course Wallah"
                    }
                }
    return None
