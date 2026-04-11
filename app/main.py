"""
GrooveIQ – Behavioral Music Recommendation Engine
Entry point for FastAPI application.
"""

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.routes import algorithm_config, artists, charts, discovery, downloads, events, health, lastfm, playlists, radio, recommend, stats, tracks, users
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.session import init_db
from app.workers.scheduler import start_scheduler, stop_scheduler

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    logger.info("GrooveIQ starting up...")
    await init_db()

    # Load algorithm config from DB (seeds defaults on first run).
    try:
        from app.services.algorithm_config import load_active_config
        await load_active_config()
    except Exception as e:
        logger.warning(f"Algorithm config load failed on startup: {e}")

    # Build FAISS index from existing embeddings (non-blocking if empty).
    try:
        from app.services.faiss_index import build_index
        indexed = await build_index()
        logger.info(f"FAISS index ready: {indexed} tracks.")
    except Exception as e:
        logger.warning(f"FAISS index build failed on startup: {e}")

    await start_scheduler()
    yield
    logger.info("GrooveIQ shutting down...")
    await stop_scheduler()

    # Shut down analysis worker pool (long-lived subprocesses)
    try:
        from app.services.analysis_worker import shutdown_worker_pool
        await shutdown_worker_pool()
    except Exception as e:
        logger.warning(f"Worker pool shutdown error: {e}")


app = FastAPI(
    title="GrooveIQ",
    description="Behavioral music recommendation engine for self-hosted libraries.",
    version="0.1.0",
    docs_url="/docs" if settings.ENABLE_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_DOCS else None,
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

if settings.allowed_hosts_list:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.allowed_hosts_list,
    )

# Only add CORS middleware when explicit origins are configured.
# Wildcard + credentials is spec-invalid, so credentials are only
# enabled when using an explicit origin list.
if settings.cors_origins_list:
    _cors_is_wildcard = settings.cors_origins_list == ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=not _cors_is_wildcard,  # credentials forbidden with "*"
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Attach security headers to every response."""
    response = await call_next(request)
    response.headers["Server"] = "GrooveIQ"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    # Prevent caches from storing sensitive API responses.
    if request.url.path.startswith("/v1/"):
        response.headers["Cache-Control"] = "no-store, private"
    # HSTS: instruct browsers to only use HTTPS.  The reverse proxy should
    # handle TLS termination, but this header ensures browsers remember.
    response.headers["Strict-Transport-Security"] = (
        "max-age=63072000; includeSubDomains"
    )
    # CSP: all JS is loaded from /static/js/app.js (same-origin).
    # script-src 'self' blocks injected <script> tags (primary XSS vector).
    # script-src-attr 'unsafe-inline' allows onclick/onchange handlers in
    # JS-generated HTML (all user data is escaped via esc()).
    # style-src keeps 'unsafe-inline' for inline styles in generated HTML.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "script-src-attr 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    return response


@app.middleware("http")
async def log_slow_requests(request: Request, call_next):
    """Log requests that take longer than 2s — helps diagnose event loop stalls."""
    t0 = time.monotonic()
    response = await call_next(request)
    elapsed = time.monotonic() - t0
    if elapsed > 2.0:
        logger.warning(
            f"Slow request: {request.method} {request.url.path} "
            f"took {elapsed:.1f}s (status={response.status_code})"
        )
    return response


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(health.router, tags=["health"])
app.include_router(events.router, prefix="/v1", tags=["events"])
app.include_router(tracks.router, prefix="/v1", tags=["tracks"])
app.include_router(users.router, prefix="/v1", tags=["users"])
app.include_router(playlists.router, prefix="/v1", tags=["playlists"])
app.include_router(stats.router, prefix="/v1", tags=["stats"])
app.include_router(recommend.router, prefix="/v1", tags=["recommendations"])
app.include_router(discovery.router, prefix="/v1", tags=["discovery"])
app.include_router(charts.router, prefix="/v1", tags=["charts"])
app.include_router(downloads.router, prefix="/v1", tags=["downloads"])
app.include_router(lastfm.router, prefix="/v1", tags=["lastfm"])
app.include_router(artists.router, prefix="/v1", tags=["artists"])
app.include_router(radio.router, prefix="/v1", tags=["radio"])
app.include_router(algorithm_config.router, prefix="/v1", tags=["algorithm"])

# ---------------------------------------------------------------------------
# Dashboard (static)
# ---------------------------------------------------------------------------

_static_dir = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    return FileResponse(_static_dir / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
