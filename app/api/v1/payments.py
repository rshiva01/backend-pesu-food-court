"""Gateway → server callbacks. No cookies or CSRF headers here (exempt in
CsrfMiddleware); the HMAC signature over the raw body is the only auth."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import current_student
from app.db.models import Order, Payment, User
from app.db.session import get_session
from app.services import order_payments as pay_svc
from app.services import payments
from app.services.payments.fake import FakeProvider

router = APIRouter(prefix="/payments", tags=["payments"])
log = logging.getLogger("payments.webhook")
MAX_BODY = 64 * 1024


@router.post("/razorpay/webhook")
async def razorpay_webhook(
    request: Request, db: AsyncSession = Depends(get_session)
) -> dict[str, bool]:
    body = await request.body()
    sig = request.headers.get("x-razorpay-signature", "")
    if len(body) > MAX_BODY or not sig or not payments.get_payments().verify_webhook(body, sig):
        raise HTTPException(400, "Bad signature")
    try:
        event = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Bad payload") from None
    try:
        await pay_svc.handle_webhook(db, event)
        await db.commit()
    except HTTPException as e:
        # Signed but unusable (amount mismatch…) or gateway down mid auto-refund.
        # 5xx → Razorpay retries later; 4xx → logged and dropped.
        await db.rollback()
        log.warning("webhook not applied", extra={"ctx": {"status": e.status_code}})
        if e.status_code >= 500:
            raise
    return {"ok": True}


@router.post("/fake/{provider_order_id}/complete")
async def fake_complete(
    provider_order_id: str,
    student: User = Depends(current_student),
    db: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Dev only: stands in for the Razorpay popup. Returns what Checkout would
    hand the browser; the browser still has to call /orders/{id}/pay/verify."""
    provider = payments.get_payments()
    if get_settings().is_prod or not isinstance(provider, FakeProvider):
        raise HTTPException(404, "Not found")
    owner = await db.scalar(
        select(Order.student_id)
        .join(Payment, Payment.order_id == Order.id)
        .where(Payment.provider_order_id == provider_order_id)
    )
    if owner != student.id or provider_order_id not in provider.orders:
        raise HTTPException(404, "Not found")
    pid, sig = provider.complete(provider_order_id)
    return {
        "razorpay_order_id": provider_order_id,
        "razorpay_payment_id": pid,
        "razorpay_signature": sig,
    }
