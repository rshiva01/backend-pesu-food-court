"""Vendor console: own menu + shop settings. Every query is scoped to the
caller's vendor; another vendor's item id is simply 404 (no existence leak)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.catalog import item_out, vendor_public
from app.core.deps import VendorCtx, current_vendor
from app.db.models import MenuItem, StaffAccount
from app.db.session import get_session
from app.schemas.catalog import MenuItemIn, MenuItemOut, MenuItemPatch, ShopPatch, VendorPublic
from app.services import audit
from app.services.auth import client_ip

router = APIRouter(prefix="/vendor", tags=["vendor"])


def _rupees(paise: int) -> str:
    return f"₹{paise / 100:g}"


async def _log(
    db: AsyncSession, ctx: VendorCtx, request: Request, action: str, entity: str, **kw: object
) -> None:
    acct = await db.get(StaffAccount, ctx.user.id)
    audit.record(
        db,
        actor_id=ctx.user.id,
        actor_label=acct.username if acct else ctx.vendor.slug,
        action=action,
        entity=entity,
        ip=client_ip(request),
        **kw,  # type: ignore[arg-type]
    )


async def _own_item(db: AsyncSession, ctx: VendorCtx, item_id: uuid.UUID) -> MenuItem:
    item = await db.scalar(
        select(MenuItem).where(MenuItem.id == item_id, MenuItem.vendor_id == ctx.vendor.id)
    )
    if item is None:
        raise HTTPException(404, "Item not found")
    return item


@router.get("/menu")
async def my_menu(
    ctx: VendorCtx = Depends(current_vendor), db: AsyncSession = Depends(get_session)
) -> list[MenuItemOut]:
    rows = await db.scalars(
        select(MenuItem)
        .where(MenuItem.vendor_id == ctx.vendor.id)
        .order_by(MenuItem.category, MenuItem.name)
    )
    return [item_out(m) for m in rows]


@router.post("/menu", status_code=201)
async def create_item(
    body: MenuItemIn,
    request: Request,
    ctx: VendorCtx = Depends(current_vendor),
    db: AsyncSession = Depends(get_session),
) -> MenuItemOut:
    item = MenuItem(vendor_id=ctx.vendor.id, **body.model_dump())
    db.add(item)
    await db.flush()
    await _log(db, ctx, request, "menu.create", item.name, detail=_rupees(item.price_paise))
    await db.commit()
    return item_out(item)


@router.patch("/menu/{item_id}")
async def update_item(
    item_id: uuid.UUID,
    body: MenuItemPatch,
    request: Request,
    ctx: VendorCtx = Depends(current_vendor),
    db: AsyncSession = Depends(get_session),
) -> MenuItemOut:
    item = await _own_item(db, ctx, item_id)
    changes = {k: v for k, v in body.changes().items() if getattr(item, k) != v}
    if changes:
        before = {k: getattr(item, k) for k in changes}
        for k, v in changes.items():
            setattr(item, k, v)
        if "price_paise" in changes:
            detail: str | None = (
                f"price {_rupees(before['price_paise'])} → {_rupees(item.price_paise)}"
            )
        elif "is_available" in changes:
            detail = "available" if item.is_available else "sold out"
        else:
            detail = None
        await _log(
            db, ctx, request, "menu.update", item.name, detail=detail, before=before, after=changes
        )
        await db.commit()
    return item_out(item)


@router.delete("/menu/{item_id}", status_code=204)
async def delete_item(
    item_id: uuid.UUID,
    request: Request,
    ctx: VendorCtx = Depends(current_vendor),
    db: AsyncSession = Depends(get_session),
) -> Response:
    # Past orders keep name/price snapshots; order_items.menu_item_id → NULL.
    item = await _own_item(db, ctx, item_id)
    await _log(db, ctx, request, "menu.delete", item.name)
    await db.delete(item)
    await db.commit()
    return Response(status_code=204)


@router.patch("/shop")
async def update_shop(
    body: ShopPatch,
    request: Request,
    ctx: VendorCtx = Depends(current_vendor),
    db: AsyncSession = Depends(get_session),
) -> VendorPublic:
    v = ctx.vendor
    changes = {k: val for k, val in body.changes().items() if getattr(v, k) != val}
    if changes:
        before = {k: getattr(v, k) for k in changes}
        for k, val in changes.items():
            setattr(v, k, val)
        await _log(db, ctx, request, "shop.update", v.name, before=before, after=changes)
        await db.commit()
    return vendor_public(v)
