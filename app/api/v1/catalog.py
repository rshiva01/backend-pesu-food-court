"""Public catalog: anyone (logged in or not) can browse vendors and menus."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MenuItem, Vendor
from app.db.session import get_session
from app.schemas.catalog import MenuItemOut, VendorMenuOut, VendorPublic

router = APIRouter(tags=["catalog"])

Slug = Annotated[str, Path(pattern=r"^[a-z0-9-]{1,40}$")]


def vendor_public(v: Vendor) -> VendorPublic:
    return VendorPublic.model_validate(v, from_attributes=True)


def item_out(m: MenuItem) -> MenuItemOut:
    return MenuItemOut.model_validate(m, from_attributes=True)


@router.get("/vendors")
async def list_vendors(db: AsyncSession = Depends(get_session)) -> list[VendorPublic]:
    rows = await db.scalars(select(Vendor).where(Vendor.is_active.is_(True)).order_by(Vendor.name))
    return [vendor_public(v) for v in rows]


@router.get("/vendors/{slug}/menu")
async def vendor_menu(slug: Slug, db: AsyncSession = Depends(get_session)) -> VendorMenuOut:
    v = await db.scalar(select(Vendor).where(Vendor.slug == slug, Vendor.is_active.is_(True)))
    if v is None:
        raise HTTPException(404, "Vendor not found")
    items = await db.scalars(
        select(MenuItem)
        .where(MenuItem.vendor_id == v.id)
        .order_by(MenuItem.category, MenuItem.name)
    )
    return VendorMenuOut(vendor=vendor_public(v), items=[item_out(m) for m in items])
