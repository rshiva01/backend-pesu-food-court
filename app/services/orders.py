"""Orders: checkout (all-or-nothing, idempotent), token numbers, and the
vendor-side state machine. Prices always come from menu_items, never the client.
Callers commit."""

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import hit
from app.db.models import MenuItem, Order, OrderItem, User, Vendor, VendorDayCounter
from app.db.models.enums import VENDOR_TRANSITIONS, OrderStatus, PaymentMode, PaymentStatus
from app.schemas.orders import (
    CreateOrderIn,
    OrderLineOut,
    OrderOut,
    OrderVendor,
    VendorOrderOut,
    VendorStatsOut,
)

IST = timezone(timedelta(hours=5, minutes=30))  # India has no DST
ORDER_LIMIT, ORDER_WINDOW_S = 10, 600  # per student
OPEN = (
    OrderStatus.awaiting_payment,
    OrderStatus.confirmed,
    OrderStatus.preparing,
    OrderStatus.ready,
)
NOT_COUNTED = (OrderStatus.cancelled, OrderStatus.refunded)


class OrderConflict(Exception):
    """409 that tells the cart which vendor/item to fix."""

    def __init__(
        self, detail: str, vendor_id: uuid.UUID | None = None, item_id: uuid.UUID | None = None
    ) -> None:
        super().__init__(detail)
        self.detail, self.vendor_id, self.item_id = detail, vendor_id, item_id


def business_today() -> date:
    """Tokens restart every IST day."""
    return datetime.now(IST).date()


def student_label(phone: str | None) -> str:
    return f"Student ··{(phone or '')[-3:]}"


async def next_token(db: AsyncSession, vendor: Vendor, day: date) -> str:
    # Upsert takes a row lock until commit; a rollback undoes the bump.
    stmt = (
        pg_insert(VendorDayCounter)
        .values(vendor_id=vendor.id, business_date=day, last_no=1)
        .on_conflict_do_update(
            index_elements=["vendor_id", "business_date"],
            set_={"last_no": VendorDayCounter.last_no + 1},
        )
        .returning(VendorDayCounter.last_no)
    )
    n = await db.scalar(stmt)
    return f"{vendor.token_prefix}-{n:03d}"


# ------------------------------------------------------------------ output
async def orders_out(db: AsyncSession, orders: Sequence[Order]) -> list[OrderOut]:
    if not orders:
        return []
    ids = [o.id for o in orders]
    lines: dict[uuid.UUID, list[OrderLineOut]] = {i: [] for i in ids}
    for it in await db.scalars(
        select(OrderItem).where(OrderItem.order_id.in_(ids)).order_by(OrderItem.name_snapshot)
    ):
        lines[it.order_id].append(
            OrderLineOut(
                menu_item_id=it.menu_item_id,
                name=it.name_snapshot,
                unit_price_paise=it.unit_price_paise,
                qty=it.qty,
            )
        )
    vendors = {
        v.id: OrderVendor(id=v.id, slug=v.slug, name=v.name)
        for v in await db.scalars(
            select(Vendor).where(Vendor.id.in_({o.vendor_id for o in orders}))
        )
    }
    return [
        OrderOut(
            id=o.id,
            vendor=vendors[o.vendor_id],
            token_no=o.token_no,
            group_id=o.group_id,
            status=o.status,
            payment_mode=o.payment_mode,
            payment_status=o.payment_status,
            total_paise=o.total_paise,
            items=lines[o.id],
            created_at=o.created_at,
            ready_at=o.ready_at,
        )
        for o in orders
    ]


async def vendor_orders_out(db: AsyncSession, orders: Sequence[Order]) -> list[VendorOrderOut]:
    if not orders:
        return []
    base = await orders_out(db, orders)
    rows = await db.execute(
        select(User.id, User.phone).where(User.id.in_({o.student_id for o in orders}))
    )
    phones: dict[uuid.UUID, str | None] = {uid: phone for uid, phone in rows.tuples()}
    return [
        VendorOrderOut(**b.model_dump(), student_label=student_label(phones.get(o.student_id)))
        for b, o in zip(base, orders, strict=True)
    ]


# ---------------------------------------------------------------- checkout
async def _existing_group(db: AsyncSession, student_id: uuid.UUID, key: uuid.UUID) -> list[Order]:
    rows = await db.scalars(
        select(Order)
        .where(Order.student_id == student_id, Order.group_id == key)
        .order_by(Order.created_at, Order.token_no)
    )
    return list(rows)


async def place_orders(
    db: AsyncSession, student: User, body: CreateOrderIn
) -> tuple[list[Order], bool]:
    """Returns (orders, created). A retry with the same idempotency key returns
    the orders made the first time with created=False."""
    sid = student.id  # plain value: a rollback below expires ORM objects
    prior = await _existing_group(db, sid, body.idempotency_key)
    if prior:
        return prior, False

    if body.payment_mode == PaymentMode.online:  # Razorpay arrives in M5
        raise HTTPException(422, "Online payment is coming soon — pay at the counter for now")
    vendor_ids = [g.vendor_id for g in body.groups]
    if len(set(vendor_ids)) != len(vendor_ids):
        raise HTTPException(422, "Invalid request")
    if not await hit(f"order:{sid}", ORDER_LIMIT, ORDER_WINDOW_S):
        raise HTTPException(429, "Too many orders — wait a few minutes")

    # Pass 1: validate everything; nothing is written unless all of it passes.
    vendors = {
        v.id: v
        for v in await db.scalars(
            select(Vendor).where(Vendor.id.in_(vendor_ids), Vendor.is_active.is_(True))
        )
    }
    item_ids = {line.menu_item_id for g in body.groups for line in g.items}
    menu = {m.id: m for m in await db.scalars(select(MenuItem).where(MenuItem.id.in_(item_ids)))}
    planned: list[tuple[Vendor, list[tuple[MenuItem, int]]]] = []
    for g in body.groups:
        v = vendors.get(g.vendor_id)
        if v is None:
            raise OrderConflict("A counter in your cart no longer exists", g.vendor_id)
        if not v.is_open:
            raise OrderConflict(f"{v.name} is closed right now", v.id)
        lines: list[tuple[MenuItem, int]] = []
        for line in g.items:
            mi = menu.get(line.menu_item_id)
            if mi is None or mi.vendor_id != v.id or not mi.is_available:
                name = mi.name if mi is not None and mi.vendor_id == v.id else "An item"
                raise OrderConflict(f"{name} at {v.name} just sold out", v.id, line.menu_item_id)
            lines.append((mi, line.qty))
        planned.append((v, lines))

    # Pass 2: write. Counters are locked in vendor-id order so two multi-vendor
    # checkouts can't deadlock on each other's token rows.
    day, now = business_today(), datetime.now(UTC)
    created: dict[uuid.UUID, Order] = {}
    try:
        for v, lines in sorted(planned, key=lambda p: p[0].id):
            order = Order(
                id=uuid.uuid4(),
                group_id=body.idempotency_key,
                student_id=sid,
                vendor_id=v.id,
                business_date=day,
                token_no=await next_token(db, v, day),
                status=OrderStatus.awaiting_payment,
                payment_mode=PaymentMode.counter,
                payment_status=PaymentStatus.unpaid,
                total_paise=sum(mi.price_paise * qty for mi, qty in lines),
                created_at=now,
            )
            db.add(order)
            db.add_all(
                OrderItem(
                    order_id=order.id,
                    menu_item_id=mi.id,
                    name_snapshot=mi.name,
                    unit_price_paise=mi.price_paise,
                    qty=qty,
                )
                for mi, qty in lines
            )
            created[v.id] = order
        await db.flush()
    except IntegrityError:
        # Same key sent twice at once: the other request won; return its orders.
        await db.rollback()
        prior = await _existing_group(db, sid, body.idempotency_key)
        if prior:
            return prior, False
        raise
    return [created[v.id] for v, _ in planned], True


# ------------------------------------------------------------------ vendor
async def lock_vendor_order(db: AsyncSession, vendor: Vendor, order_id: uuid.UUID) -> Order:
    """Row-locked so two counter tablets can't both act on one order.
    Another vendor's order id is just 'not found'."""
    o = await db.scalar(
        select(Order).where(Order.id == order_id, Order.vendor_id == vendor.id).with_for_update()
    )
    if o is None:
        raise HTTPException(404, "Order not found")
    return o


def change_status(o: Order, to: OrderStatus) -> None:
    if to not in VENDOR_TRANSITIONS.get(o.status, frozenset()):
        raise HTTPException(409, f"Can’t move an order from {o.status} to {to}")  # noqa: RUF001 (UI text)
    if to == OrderStatus.cancelled and o.payment_status == PaymentStatus.paid:
        o.payment_status = PaymentStatus.refunded  # gateway refund: M5
    if to == OrderStatus.ready:
        o.ready_at = datetime.now(UTC)
    o.status = to


def mark_paid(o: Order) -> None:
    if o.status != OrderStatus.awaiting_payment:
        raise HTTPException(409, "Order is not awaiting payment")
    o.status, o.payment_status = OrderStatus.confirmed, PaymentStatus.paid


def collect(o: Order, by: User) -> None:
    if o.status != OrderStatus.ready:
        raise HTTPException(409, "Order is not ready yet")
    o.status, o.collected_at, o.collected_by = OrderStatus.collected, datetime.now(UTC), by.id


async def vendor_stats(db: AsyncSession, vendor: Vendor) -> VendorStatsOut:
    counted = Order.status.not_in(NOT_COUNTED)
    row = (
        await db.execute(
            select(
                func.count().filter(counted),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                counted & (Order.payment_status == PaymentStatus.paid),
                                Order.total_paise,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.count().filter(Order.status.in_(OPEN)),
            ).where(Order.vendor_id == vendor.id, Order.business_date == business_today())
        )
    ).one()
    return VendorStatsOut(orders_today=row[0], revenue_today_paise=row[1], open_orders=row[2])
