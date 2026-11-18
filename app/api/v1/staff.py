from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import staff_out
from app.core.deps import VendorCtx, current_admin, current_vendor
from app.db.models import StaffAccount, User
from app.db.session import get_session
from app.schemas.auth import StaffUserOut, VendorMeOut
from app.schemas.catalog import VendorPublic

router = APIRouter(tags=["staff"])


async def _acct(db: AsyncSession, user: User) -> StaffAccount:
    acct = await db.get(StaffAccount, user.id)
    if acct is None:
        raise HTTPException(401, "Not authenticated")
    return acct


@router.get("/vendor/me")
async def vendor_me(
    ctx: VendorCtx = Depends(current_vendor), db: AsyncSession = Depends(get_session)
) -> VendorMeOut:
    return VendorMeOut(
        user=staff_out(ctx.user, await _acct(db, ctx.user)),
        vendor=VendorPublic.model_validate(ctx.vendor, from_attributes=True),
    )


@router.get("/admin/me")
async def admin_me(
    user: User = Depends(current_admin), db: AsyncSession = Depends(get_session)
) -> StaffUserOut:
    return staff_out(user, await _acct(db, user))
