"""The only way routes get an authenticated user. Every check here is
server-side; the frontend's route guards are UX only."""

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Audience, read_access_token
from app.db.models import Session, User, Vendor
from app.db.models.enums import Role
from app.db.session import get_session
from app.services.auth import cookie_names

UNAUTH = HTTPException(401, "Not authenticated")


async def _user_from_cookie(request: Request, db: AsyncSession, aud: Audience) -> User:
    token = request.cookies.get(cookie_names(aud)[0])
    claims = read_access_token(token, aud) if token else None
    if claims is None:
        raise UNAUTH
    try:
        uid, sid = uuid.UUID(claims["sub"]), uuid.UUID(claims["sid"])
    except (KeyError, ValueError):
        raise UNAUTH from None
    # Session must still be live: logout / reuse-revocation take effect at once.
    sess = await db.get(Session, sid)
    if sess is None or sess.user_id != uid or sess.revoked_at is not None:
        raise UNAUTH
    user = await db.get(User, uid)
    if user is None or not user.is_active:
        raise UNAUTH
    return user


async def current_student(request: Request, db: AsyncSession = Depends(get_session)) -> User:
    user = await _user_from_cookie(request, db, "student")
    if user.role != Role.student:
        raise UNAUTH
    return user


async def current_staff(request: Request, db: AsyncSession = Depends(get_session)) -> User:
    user = await _user_from_cookie(request, db, "staff")
    if user.role not in (Role.vendor, Role.admin):
        raise UNAUTH
    return user


async def current_admin(user: User = Depends(current_staff)) -> User:
    if user.role != Role.admin:
        raise HTTPException(403, "Forbidden")
    return user


@dataclass
class VendorCtx:
    user: User
    vendor: Vendor


async def current_vendor(
    user: User = Depends(current_staff), db: AsyncSession = Depends(get_session)
) -> VendorCtx:
    if user.role != Role.vendor:
        raise HTTPException(403, "Forbidden")
    vendor = await db.scalar(
        select(Vendor).where(Vendor.owner_user_id == user.id, Vendor.is_active.is_(True))
    )
    if vendor is None:  # disabled by admin → logged out
        raise UNAUTH
    return VendorCtx(user, vendor)
