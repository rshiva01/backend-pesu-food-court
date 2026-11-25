"""Student orders. Every read is scoped to the caller: another student's order
id is 404 (no existence leak)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import current_student
from app.core.rate_limit import hit
from app.db.models import Order, User
from app.db.session import get_session
from app.schemas.orders import CreateOrderIn, CreateOrderOut, OrderOut, PayOut, VerifyIn
from app.services import order_payments as pay_svc
from app.services import orders as svc
from app.services import payments

router = APIRouter(prefix="/orders", tags=["orders"])
HISTORY_LIMIT = 50
PAY_LIMIT, PAY_WINDOW_S = 30, 600  # per student, pay + verify together


async def _pay_rate(student: User) -> None:
    if not await hit(f"pay:{student.id}", PAY_LIMIT, PAY_WINDOW_S):
        raise HTTPException(429, "Too many attempts — wait a few minutes")


@router.post("", status_code=201)
async def create_orders(
    body: CreateOrderIn,
    response: Response,
    student: User = Depends(current_student),
    db: AsyncSession = Depends(get_session),
) -> CreateOrderOut:
    orders, created = await svc.place_orders(db, student, body)
    if created:
        await db.commit()
    else:
        response.status_code = 200  # idempotent replay
    return CreateOrderOut(group_id=body.idempotency_key, orders=await svc.orders_out(db, orders))


@router.get("/me")
async def my_orders(
    student: User = Depends(current_student), db: AsyncSession = Depends(get_session)
) -> list[OrderOut]:
    if await pay_svc.expire_pending(db, student.id):
        await db.commit()
    rows = await db.scalars(
        select(Order)
        .where(Order.student_id == student.id)
        .order_by(Order.created_at.desc(), Order.token_no)
        .limit(HISTORY_LIMIT)
    )
    return await svc.orders_out(db, list(rows))


@router.get("/{order_id}")
async def get_order(
    order_id: uuid.UUID,
    student: User = Depends(current_student),
    db: AsyncSession = Depends(get_session),
) -> OrderOut:
    if await pay_svc.expire_pending(db, student.id):
        await db.commit()
    o = await db.scalar(select(Order).where(Order.id == order_id, Order.student_id == student.id))
    if o is None:
        raise HTTPException(404, "Order not found")
    return (await svc.orders_out(db, [o]))[0]


@router.post("/{order_id}/pay")
async def start_payment(
    order_id: uuid.UUID,
    student: User = Depends(current_student),
    db: AsyncSession = Depends(get_session),
) -> PayOut:
    await _pay_rate(student)
    sid, phone = student.id, student.phone
    _, p, vendor = await pay_svc.start_payment(db, sid, order_id)
    out = PayOut(
        provider=p.provider,
        key_id=payments.get_payments().key_id,
        provider_order_id=p.provider_order_id,
        amount_paise=p.amount_paise,
        currency="INR",
        name="PESU Food Court",
        description=f"Order at {vendor.name}",
        prefill_contact=phone,
    )
    await db.commit()
    return out


@router.post("/{order_id}/pay/verify")
async def verify_payment(
    order_id: uuid.UUID,
    body: VerifyIn,
    student: User = Depends(current_student),
    db: AsyncSession = Depends(get_session),
) -> OrderOut:
    await _pay_rate(student)
    o = await pay_svc.verify_checkout(
        db,
        student.id,
        order_id,
        body.razorpay_order_id,
        body.razorpay_payment_id,
        body.razorpay_signature,
    )
    await db.commit()
    return (await svc.orders_out(db, [o]))[0]
