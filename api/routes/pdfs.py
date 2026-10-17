import logging
import asyncio
import urllib.parse
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.connection import get_db_dependency
from db.models import Lecture, PDF, PublicationStatus
from engines.b2_storage import B2StorageManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pdfs", tags=["PDFs"])

@router.get("/proxy")
async def proxy_pdf(url: str = Query(..., description="External PDF URL to proxy")):
    """
    Proxies external PDF documents securely to PDF.js canvas viewer.
    Bypasses third-party CDN CORS limitations and ensures immediate rendering.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://appx.co.in/"
            }
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"Failed to fetch remote PDF: status {resp.status_code}")
                raise HTTPException(status_code=resp.status_code, detail="Unable to fetch remote PDF document")
            
            return Response(
                content=resp.content,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": "inline",
                    "Cache-Control": "public, max-age=86400",
                    "Access-Control-Allow-Origin": "*",
                    "X-Content-Type-Options": "nosniff"
                }
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error proxying PDF from {url}: {e}")
        raise HTTPException(status_code=502, detail=f"PDF proxy error: {str(e)}")

@router.get("/{lecture_id}/access")
async def get_pdf_access(lecture_id: str, db: AsyncSession = Depends(get_db_dependency)):
    """
    Generates a secure access URL for PDF notes.
    """
    # 1. Fast check for Selection Way dynamic notes
    try:
        from services.selection_way_service import get_selection_lecture_access
        selection_access = await get_selection_lecture_access(lecture_id)
        if selection_access and selection_access.get("has_pdf") and selection_access.get("pdf_download_url"):
            pdf_url = selection_access["pdf_download_url"]
            return {
                "lecture_id": lecture_id,
                "lecture_title": selection_access.get("title", "Notes PDF"),
                "file_name": f"{lecture_id}.pdf",
                "file_size": 0,
                "page_count": 1,
                "download_url": pdf_url,
                "access_url": pdf_url,
                "expires_in_seconds": 86400
            }
    except Exception as e:
        logger.warning(f"Error checking Selection Way for PDF {lecture_id}: {e}")

    # 2. Fast check for Apna College dynamic notes
    try:
        from services.apna_college_service import get_apna_lecture_access
        apna_access = await get_apna_lecture_access(lecture_id)
        if apna_access and apna_access.get("has_pdf") and apna_access.get("pdf_download_url"):
            pdf_url = apna_access["pdf_download_url"]
            return {
                "lecture_id": lecture_id,
                "lecture_title": apna_access.get("title", "Notes PDF"),
                "file_name": f"{lecture_id}.pdf",
                "file_size": 0,
                "page_count": 1,
                "download_url": pdf_url,
                "access_url": pdf_url,
                "expires_in_seconds": 86400
            }
    except Exception as e:
        logger.warning(f"Error checking Apna College for PDF {lecture_id}: {e}")

    stmt = (
        select(Lecture)
        .options(selectinload(Lecture.pdf))
        .where(Lecture.id == lecture_id)
    )
    res = await db.execute(stmt)
    lecture = res.scalar_one_or_none()
    if not lecture or lecture.publication_status != PublicationStatus.PUBLISHED:
        raise HTTPException(status_code=404, detail="PDF notes not found or lecture is not published")

    if not lecture.pdf and not lecture.source_pdf_url:
        raise HTTPException(status_code=404, detail="No PDF associated with this lecture")

    presigned_url = None
    file_name = f"{lecture.slug}.pdf"
    file_size = 0
    page_count = 1

    if lecture.pdf:
        pdf = lecture.pdf
        file_name = pdf.file_name
        file_size = pdf.file_size
        page_count = pdf.page_count
        presigned_url = B2StorageManager.generate_presigned_url(
            object_key=pdf.b2_object_key,
            bucket_name=pdf.b2_bucket,
            expires_in_seconds=900
        )

    download_url = presigned_url or lecture.source_pdf_url or f"/api/v1/pdfs/{lecture.id}/content"

    return {
        "lecture_id": lecture.id,
        "lecture_title": lecture.title,
        "file_name": file_name,
        "file_size": file_size,
        "page_count": page_count,
        "download_url": download_url,
        "access_url": f"/api/v1/pdfs/{lecture.id}/content",
        "expires_in_seconds": 900
    }

@router.get("/{lecture_id}/content")
async def get_pdf_content(lecture_id: str, db: AsyncSession = Depends(get_db_dependency)):
    """
    Streams PDF content securely to PDF.js canvas viewer.
    Eliminates all CORS restrictions and handles both B2 storage and external source URLs.
    """
    stmt = (
        select(Lecture)
        .options(selectinload(Lecture.pdf))
        .where(Lecture.id == lecture_id)
    )
    res = await db.execute(stmt)
    lecture = res.scalar_one_or_none()
    if not lecture or lecture.publication_status != PublicationStatus.PUBLISHED:
        raise HTTPException(status_code=404, detail="PDF notes not found or lecture is not published")

    pdf_bytes = None
    file_name = f"{lecture.slug}.pdf"

    if lecture.pdf:
        pdf = lecture.pdf
        file_name = pdf.file_name
        try:
            pdf_bytes = await asyncio.to_thread(B2StorageManager.get_pdf_bytes, pdf.b2_object_key, pdf.b2_bucket)
        except Exception as e:
            logger.warning(f"B2 storage fetch failed for lecture {lecture_id}: {e}")

    # Fallback to source_pdf_url if B2 is not populated or failed
    if not pdf_bytes and lecture.source_pdf_url:
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Referer": "https://appx.co.in/"
                }
                resp = await client.get(lecture.source_pdf_url, headers=headers)
                if resp.status_code == 200:
                    pdf_bytes = resp.content
        except Exception as e:
            logger.error(f"Failed to fetch source PDF for lecture {lecture_id}: {e}")

    if not pdf_bytes:
        raise HTTPException(status_code=404, detail="Unable to retrieve PDF data from secure storage")

    ascii_filename = "".join(c if ord(c) < 128 and (c.isalnum() or c in ".-_ ") else "_" for c in file_name).strip("_")
    if not ascii_filename.endswith(".pdf"):
        ascii_filename += ".pdf"
    encoded_filename = urllib.parse.quote(file_name)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{ascii_filename}"; filename*=UTF-8\'\'{encoded_filename}',
            "Cache-Control": "private, max-age=1800",
            "Access-Control-Allow-Origin": "*",
            "X-Content-Type-Options": "nosniff"
        }
    )



