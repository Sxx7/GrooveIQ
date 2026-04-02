"""GrooveIQ – User management routes."""
from __future__ import annotations
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.security import require_api_key
from app.db.session import get_session
from app.models.db import User, ListenEvent
from app.models.schemas import UserCreate, UserResponse

router = APIRouter()


@router.get("/users", summary="List all users")
async def list_users(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # Get users with their event counts
    q = (
        select(
            User,
            func.count(ListenEvent.id).label("event_count"),
        )
        .outerjoin(ListenEvent, User.user_id == ListenEvent.user_id)
        .group_by(User.id)
        .order_by(User.last_seen.desc().nullslast())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(q)
    rows = result.all()
    return [
        {
            "user_id": user.user_id,
            "display_name": user.display_name,
            "created_at": user.created_at,
            "last_seen": user.last_seen,
            "event_count": count,
        }
        for user, count in rows
    ]


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
