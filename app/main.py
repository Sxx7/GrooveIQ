"""
GrooveIQ – Behavioral Music Recommendation Engine
Entry point for FastAPI application.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.api.routes import events, health, tracks, users
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
    await start_scheduler()
    yield
    logger.info("GrooveIQ shutting down...")
    await stop_scheduler()


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(health.router, tags=["health"])
app.include_router(events.router, prefix="/v1", tags=["events"])
app.include_router(tracks.router, prefix="/v1", tags=["tracks"])
app.include_router(users.router, prefix="/v1", tags=["users"])
