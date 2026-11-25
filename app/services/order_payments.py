"""Online payment for orders (one gateway payment per order; a multi-vendor
cart pays each vendor separately).

An order becomes paid only in confirm_payment, and only after the gateway has
vouched for the payment: either a checkout signature *plus* a server-side fetch
of the payment, or a signed webhook. The browser's "success" is never trusted.
Callers commit."""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, Payment, Vendor
from app.db.models.enums import GatewayPaymentStatus, OrderStatus, PaymentMode, PaymentStatus
from app.services import audit, payments
from app.services.orders import business_today, next_token
from app.services.payments.base import GatewayError, GatewayPayment

log = logging.getLogger("payments")

PENDING_TTL = timedelta(minutes=15)
GATEWAY_DOWN = HTTPException(502, "Payment service is not responding — try again in a minute")
NOT_VERIFIED = HTTPException(400, "Payment could not be verified")


async def _vendor(db: AsyncSession, vendor_id: uuid.UUID) -> Vendor:
    v = await db.get(Vendor, vendor_id)
    if v is None:  # FK makes this impossible
        raise RuntimeError("order without vendor")
    return v


def _expired(o: Order, now: datetime) -> bool:
    return o.status == OrderStatus.pending_payment and o.created_at < now - PENDING_TTL


async def expire_pending(db: AsyncSession, student_id: uuid.UUID | None = None) -> int:
    """Unpaid online orders older than PENDING_TTL → cancelled. A payment that
    still lands later is refunded by confirm_payment."""
    stmt = update(Order).where(
        Order.status == OrderStatus.pending_payment,
        Order.created_at < datetime.now(UTC) - PENDING_TTL,
    )
    if student_id is not None:
        stmt = stmt.where(Order.student_id == student_id)
    res = await db.execute(
        stmt.values(status=OrderStatus.cancelled).execution_options(synchronize_session=False)
    )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


async def _lock_student_order(
    db: AsyncSession, student_id: uuid.UUID, order_id: uuid.UUID
) -> Order:
    o = await db.scalar(
        select(Order).where(Order.id == order_id, Order.student_id == student_id).with_for_update()
    )
    if o is None:
        raise HTTPException(404, "Order not found")
    return o


async def start_payment(
    db: AsyncSession, student_id: uuid.UUID, order_id: uuid.UUID
) -> tuple[Order, Payment, Vendor]:
    """Create (or reuse) the gateway order for this order's server-side total."""
    o = await _lock_student_order(db, student_id, order_id)
    if _expired(o, datetime.now(UTC)):
        o.status = OrderStatus.cancelled
        await db.commit()
        raise HTTPException(409, "This order expired — please order again")
    if o.payment_mode != PaymentMode.online or o.status != OrderStatus.pending_payment:
        raise HTTPException(409, "This order doesn’t need payment")  # noqa: RUF001 (UI text)
    vendor = await _vendor(db, o.vendor_id)
    pay = await db.scalar(
        select(Payment).where(
            Payment.order_id == o.id, Payment.status == GatewayPaymentStatus.created
        )
    )
    if pay is not None and pay.amount_paise == o.total_paise:
        return o, pay, vendor
    try:
        gw = await payments.get_payments().create_order(
            o.total_paise, receipt=str(o.id), notes={"order_id": str(o.id)}
        )
    except GatewayError:
        raise GATEWAY_DOWN from None
    pay = Payment(
        order_id=o.id,
        provider=payments.get_payments().name,
        provider_order_id=gw.id,
        amount_paise=o.total_paise,
        status=GatewayPaymentStatus.created,
    )
    db.add(pay)
    await db.flush()
    return o, pay, vendor


async def verify_checkout(
    db: AsyncSession,
    student_id: uuid.UUID,
    order_id: uuid.UUID,
    gw_order_id: str,
    gw_payment_id: str,
    signature: str,
) -> Order:
    """Browser hands back what Razorpay Checkout gave it. Signature proves it came
    from the gateway; the fetch proves the money actually moved."""
    o = await _lock_student_order(db, student_id, order_id)
    pay = await db.scalar(
        select(Payment).where(Payment.order_id == o.id, Payment.provider_order_id == gw_order_id)
    )
    provider = payments.get_payments()
    if pay is None or not provider.verify_checkout_signature(gw_order_id, gw_payment_id, signature):
        raise NOT_VERIFIED
    if pay.provider_payment_id == gw_payment_id:
        return o  # already confirmed (webhook got here first, or a retry)
    try:
        gp = await provider.fetch_payment(gw_payment_id)
    except GatewayError:
        raise GATEWAY_DOWN from None
    if gp.order_id != gw_order_id:
        raise NOT_VERIFIED
    if gp.status == "authorized":
        raise HTTPException(202, "Payment is processing — your token will appear shortly")
    if gp.status != "captured":
        raise NOT_VERIFIED
    await confirm_payment(db, o, pay, gp)
    return o


async def confirm_payment(
    db: AsyncSession, o: Order, pay: Payment, gp: GatewayPayment, raw: dict[str, Any] | None = None
) -> None:
    """The only place an online order becomes paid. `o` must be row-locked.
    Idempotent: the same gateway payment twice changes nothing."""
    if pay.provider_payment_id == gp.id:
        return
    if (
        gp.amount_paise != pay.amount_paise
        or gp.currency != "INR"
        or gp.order_id != pay.provider_order_id
    ):
        log.warning("payment mismatch", extra={"ctx": {"order": str(o.id), "payment": gp.id}})
        raise NOT_VERIFIED
    if raw is not None:
        pay.raw_webhook = raw

    if pay.provider_payment_id is None and o.status == OrderStatus.pending_payment:
        pay.provider_payment_id, pay.status = gp.id, GatewayPaymentStatus.captured
        day = business_today()
        vendor = await _vendor(db, o.vendor_id)
        o.business_date, o.token_no = day, await next_token(db, vendor, day)
        o.status, o.payment_status = OrderStatus.confirmed, PaymentStatus.paid
        audit.record(
            db,
            actor_id=None,
            actor_label=pay.provider,
            action="payment.captured",
            entity=o.token_no,
            detail=f"₹{o.total_paise / 100:g}",
        )
        return

    # Money arrived for an order that can't take it any more (expired, cancelled,
    # or already paid by another attempt): give it straight back.
    try:
        await payments.get_payments().refund(gp.id, gp.amount_paise)
    except GatewayError:
        log.error("auto-refund failed", extra={"ctx": {"order": str(o.id), "payment": gp.id}})
        raise GATEWAY_DOWN from None
    if pay.provider_payment_id is None:
        pay.provider_payment_id = gp.id
        pay.status = GatewayPaymentStatus.refunded
    if o.payment_status == PaymentStatus.unpaid:
        o.payment_status = PaymentStatus.refunded  # student sees "refunded", not "unpaid"
    audit.record(
        db,
        actor_id=None,
        actor_label=pay.provider,
        action="payment.auto_refund",
        entity=o.token_no or str(o.id),
        detail=f"₹{gp.amount_paise / 100:g} ({o.status})",
    )


async def refund_order(db: AsyncSession, o: Order) -> None:
    """Full refund of a paid online order through the gateway. Raises (and the
    caller's transaction rolls back) if the gateway refuses."""
    pay = await db.scalar(
        select(Payment).where(
            Payment.order_id == o.id, Payment.status == GatewayPaymentStatus.captured
        )
    )
    if pay is None or pay.provider_payment_id is None:
        raise HTTPException(409, "No captured payment to refund")
    try:
        await payments.get_payments().refund(pay.provider_payment_id, pay.amount_paise)
    except GatewayError:
        raise GATEWAY_DOWN from None
    pay.status = GatewayPaymentStatus.refunded
    o.payment_status = PaymentStatus.refunded


async def handle_webhook(db: AsyncSession, event: dict[str, Any]) -> None:
    """Signed gateway event (signature already checked by the route)."""
    kind = event.get("event")
    if kind not in ("payment.captured", "order.paid"):
        return  # failed/authorized/refund events need no action
    try:
        entity = event["payload"]["payment"]["entity"]
        from app.services.payments.razorpay import payment_from_entity

        gp = payment_from_entity(entity)
    except (KeyError, TypeError, ValueError):
        log.warning("webhook: unexpected payload", extra={"ctx": {"event": kind}})
        return
    if gp.status != "captured":
        return
    pay = await db.scalar(select(Payment).where(Payment.provider_order_id == gp.order_id))
    if pay is None:
        return  # not ours (or another environment's) — ignore, still 200
    o = await db.scalar(select(Order).where(Order.id == pay.order_id).with_for_update())
    if o is None:  # FK makes this impossible
        raise RuntimeError("payment without order")
    await db.refresh(pay)  # re-read after the lock: verify may have just confirmed it
    await confirm_payment(db, o, pay, gp, raw=event)
