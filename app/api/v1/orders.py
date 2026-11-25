"""Student orders. Every read is scoped to the caller: another student's order
id is 404 (no existence leak)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import current_student
from app.db.models import Order, User
from app.db.session import get_session
from app.schemas.orders import CreateOrderIn, CreateOrderOut, OrderOut
from app.services import orders as svc

router = APIRouter(prefix="/orders", tags=["orders"])
HISTORY_LIMIT = 50


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
    o = await db.scalar(select(Order).where(Order.id == order_id, Order.student_id == student.id))
    if o is None:
        raise HTTPException(404, "Order not found")
    return (await svc.orders_out(db, [o]))[0]
