"""GrooveIQ – User management routes."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.security import require_api_key
from app.db.session import get_session
from app.models.db import User
from app.models.schemas import UserCreate, UserResponse

router = APIRouter()

@router.post("/users", response_model=UserResponse, status_code=201, summary="Register a user")
async def create_user(
    body: UserCreate,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    result = await session.execute(select(User).where(User.user_id == body.user_id))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="User already exists.")
    user = User(user_id=body.user_id, display_name=body.display_name)
    session.add(user)
    return user

@router.get("/users/{user_id}", response_model=UserResponse, summary="Get a user")
async def get_user(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return user
