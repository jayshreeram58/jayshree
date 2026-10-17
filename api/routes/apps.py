import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)

from db.connection import get_db_dependency
from db.models import App, Batch, Subject, Folder, Lecture, PublicationStatus

router = APIRouter(prefix="/apps", tags=["Apps"])

def extract_batch_stats(b: Batch):
    total_lectures = 0
    total_videos = 0
    total_pdfs = 0
    total_units = 0

    if b.subjects:
        for subj in b.subjects:
            if subj.folders:
                total_units += len(subj.folders)
                for folder in subj.folders:
                    if folder.lectures:
                        for lec in folder.lectures:
                            if lec.publication_status == PublicationStatus.PUBLISHED:
                                total_lectures += 1
                                if lec.has_video:
                                    total_videos += 1
                                if lec.has_pdf:
                                    total_pdfs += 1

    return {
        "id": b.id,
        "name": b.name,
        "slug": b.slug,
        "category": b.category or "Engineering",
        "branch": b.branch,
        "semester": b.semester,
        "academic_year": b.academic_year,
        "thumbnail_url": b.thumbnail_url or "/static/logo.png",
        "total_lectures": total_lectures,
        "total_videos": total_videos,
        "total_pdfs": total_pdfs,
        "total_units": total_units,
        "total_subjects": len(b.subjects) if b.subjects else 0
    }

from services.apna_college_service import (
    get_apna_college_app_object,
    fetch_batches_index
)
from services.selection_way_service import (
    get_selection_way_app_object
)

@router.get("")
async def list_apps(db: AsyncSession = Depends(get_db_dependency)):
    """Returns all active apps for the Course Wallah student dashboard."""
    # 1. Fetch DB apps (skipping external high-speed service apps)
    stmt = (
        select(App)
        .options(
            selectinload(App.batches)
            .selectinload(Batch.subjects)
            .selectinload(Subject.folders)
            .selectinload(Folder.lectures)
        )
        .where(and_(App.status == "ACTIVE", App.slug != "apna-college", App.slug != "selection-way"))
        .order_by(App.name)
    )
    res = await db.execute(stmt)
    apps = res.scalars().all()
    
    app_results = [
        {
            "id": app.id,
            "name": app.name,
            "slug": app.slug,
            "description": app.description,
            "icon_url": app.icon_url or "/static/logo.png",
            "total_batches": len([b for b in app.batches if b.status == "ACTIVE"]),
            "batches": [
                extract_batch_stats(b)
                for b in app.batches if b.status == "ACTIVE"
            ]
        }
        for app in apps
    ]
    
    # 2. Add Apna College served via high-speed service
    try:
        apna_app = await get_apna_college_app_object()
        app_results.append(apna_app)
    except Exception as e:
        logger.error("Failed to load Apna College API app: %s", e)

    # 3. Add Selection Way served via high-speed service
    try:
        selection_app = await get_selection_way_app_object()
        app_results.append(selection_app)
    except Exception as e:
        logger.error("Failed to load Selection Way API app: %s", e)

    return app_results

@router.get("/{slug}")
async def get_app_detail(slug: str, db: AsyncSession = Depends(get_db_dependency)):
    # If requesting Apna College, serve instantly from high-speed service
    if slug in ("apna-college", "app_apna_college", "app_apna"):
        return await get_apna_college_app_object()

    # If requesting Selection Way, serve instantly from high-speed service
    if slug in ("selection-way", "app_selection_way", "selectionway"):
        return await get_selection_way_app_object()

    stmt = (
        select(App)
        .options(
            selectinload(App.batches)
            .selectinload(Batch.subjects)
            .selectinload(Subject.folders)
            .selectinload(Folder.lectures)
        )
        .where(App.slug == slug)
    )
    res = await db.execute(stmt)
    app = res.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="App not found")

    return {
        "id": app.id,
        "name": app.name,
        "slug": app.slug,
        "description": app.description,
        "icon_url": app.icon_url or "/static/logo.png",
        "batches": [
            extract_batch_stats(b)
            for b in app.batches if b.status == "ACTIVE"
        ]
    }

