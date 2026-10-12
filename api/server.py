import os
import logging
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pathlib import Path

from config.settings import BASE_DIR, DATA_DIR, SECURITY_TEST_MODE
from db.connection import init_db
from api.routes import (
    apps_router,
    batches_router,
    lectures_router,
    pdfs_router,
    search_router,
    admin_router,
    auth_router,
    ai_router,
    contact_router,
    stream_router
)

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Course Wallah Backend API starting up...")
    await init_db()
    # Initialize Telegram streaming client pool if credentials configured
    try:
        from storage.telegram_stream.client_pool import TelegramClientPool
        pool = TelegramClientPool.get_instance()
        await pool.start()
    except Exception as pool_err:
        logger.warning(f"Telegram streaming pool startup notice: {pool_err}")
    yield
    try:
        from storage.telegram_stream.client_pool import TelegramClientPool
        pool = TelegramClientPool.get_instance()
        await pool.stop()
    except Exception:
        pass
    logger.info("Course Wallah Backend API shutting down...")

app = FastAPI(
    title="Course Wallah Student Platform API",
    version="1.0.0",
    docs_url="/api/docs" if SECURITY_TEST_MODE else None,
    redoc_url="/api/redoc" if SECURITY_TEST_MODE else None,
    openapi_url="/api/openapi.json" if SECURITY_TEST_MODE else None,
    lifespan=lifespan
)

# CORS Configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response

# Mount Versioned API Routes (both /api and /api/v1 for seamless compatibility)
for pfx in ["/api", "/api/v1"]:
    app.include_router(apps_router, prefix=pfx)
    app.include_router(batches_router, prefix=pfx)
    app.include_router(lectures_router, prefix=pfx)
    app.include_router(stream_router, prefix=pfx)
    app.include_router(pdfs_router, prefix=pfx)
    app.include_router(search_router, prefix=pfx)
    app.include_router(auth_router, prefix=pfx)
    app.include_router(ai_router, prefix=pfx)
    app.include_router(contact_router, prefix=pfx)
    app.include_router(admin_router, prefix=pfx)

# Static files for Web App & Thumbnails
web_build_dir = BASE_DIR / "web" / "public"
web_build_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(web_build_dir)), name="static")

@app.head("/health")
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "Course Wallah Platform",
        "version": "1.0.0",
        "security_test_mode": SECURITY_TEST_MODE
    }

@app.get("/api/logs")
@app.get("/api/v1/logs")
async def public_logs_alias(limit: int = 200, level: str = None, category: str = None, search: str = None):
    from config.logger_buffer import GLOBAL_LOG_HANDLER
    logs = GLOBAL_LOG_HANDLER.get_logs(limit=min(limit, 1000), level=level, category=category, search=search)
    return {"status": "success", "summary": GLOBAL_LOG_HANDLER.get_summary(), "logs": logs}

@app.get("/api/diagnostics")
@app.get("/api/v1/diagnostics")
async def public_diagnostics_alias():
    from config.logger_buffer import GLOBAL_LOG_HANDLER
    from engines.youtube_account_manager import YouTubeAccountManager
    yt_diag = await YouTubeAccountManager.get_diagnostics()
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "summary": GLOBAL_LOG_HANDLER.get_summary(),
        "youtube": yt_diag
    }


# SPA Entrypoint fallback
@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    index_html = web_build_dir / "index.html"
    if index_html.exists():
        return FileResponse(str(index_html))
    return HTMLResponse("<h1>Course Wallah Student Platform</h1>")
