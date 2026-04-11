"""GrooveIQ – Health check endpoint."""
from fastapi import APIRouter

from app.core.config import settings

router = APIRouter()

@router.get("/health", summary="Health check")
async def health():
    return {
        "status": "ok",
        "service": "grooveiq",
        "auth_disabled": settings.DISABLE_AUTH and not settings.api_keys_list,
    }
