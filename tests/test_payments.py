import json
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from app.db.models import AuditLog, Order, Payment
from app.db.models.enums import GatewayPaymentStatus
from app.db.session import get_session
from app.services import payments
from app.services.order_payments import PENDING_TTL
from app.services.payments.fake import FakeProvider
from tests.helpers import OTHER_PHONE, FakeSms, body, login_student, vendor_login

WEBHOOK = "/api/v1/payments/razorpay/webhook"


@pytest.fixture
def gw() -> Iterator[FakeProvider]:
    payments.get_payments.cache_clear()
    p = payments.get_payments()
    assert isinstance(p, FakeProvider)
    yield p
    payments.get_payments.cache_clear()


def online(*groups: Any) -> Any:
    b = body(*groups)
    b["payment_mode"] = "online"
    return b


async def place_online(client: AsyncClient, shop: Any, *, both: bool = False) -> list[Any]:
    groups = [(shop["dosa"], [(shop["masala"], 2)])]
    if both:
        groups.append((shop["pizza"], [(shop["marg"], 1)]))
    r = await client.post("/api/v1/orders", json=online(*groups))
    assert r.status_code == 201, r.text
    return r.json()["orders"]  # type: ignore[no-any-return]


async def pay(client: AsyncClient, oid: str) -> Any:
    r = await client.post(f"/api/v1/orders/{oid}/pay")
    assert r.status_code == 200, r.text
    return r.json()


def checkout(gw: FakeProvider, gw_order: str, **kw: Any) -> dict[str, str]:
    pid, sig = gw.complete(gw_order, **kw)
    return {
        "razorpay_order_id": gw_order,
        "razorpay_payment_id": pid,
        "razorpay_signature": sig,
    }


def webhook_event(gw: FakeProvider, gw_order: str, pid: str | None = None) -> bytes:
    if pid is None:
        pid, _ = gw.complete(gw_order)
    p = gw.payments[pid]
    return json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": p.id,
                        "order_id": p.order_id,
                        "amount": p.amount_paise,
                        "currency": "INR",
                        "status": "captured",
                    }
                }
            },
        }
    ).encode()


async def send_webhook(client: AsyncClient, raw: bytes, sig: str | None = None) -> int:
    r = await client.post(
        WEBHOOK,
        content=raw,
        headers={
            "x-razorpay-signature": sig or FakeProvider.sign_webhook(raw),
            "content-type": "application/json",
            "x-requested-with": "",  # server-to-server: no CSRF header
        },
    )
    return r.status_code


# --------------------------------------------------------------- happy path
async def test_online_order_paid_via_checkout(
    client: AsyncClient,
    sms: FakeSms,
    shop: Any,
    gw: FakeProvider,
    make_client: Callable[[], AsyncClient],
) -> None:
    await login_student(client, sms)
    (o,) = await place_online(client, shop)
    assert (o["status"], o["payment_status"], o["token_no"]) == ("pending_payment", "unpaid", None)

    async with vendor_login(make_client, "dosa") as v:
        assert (await v.get("/api/v1/vendor/orders")).json() == []  # unpaid: hidden
        # vendor can't act on it either
        assert (await v.post(f"/api/v1/vendor/orders/{o['id']}/mark-paid")).status_code == 404

        p = await pay(client, o["id"])
        assert p["amount_paise"] == 12000 and p["key_id"] == "fake_key"
        assert p["prefill_contact"] == "+919876543210"
        r = await client.post(
            f"/api/v1/orders/{o['id']}/pay/verify", json=checkout(gw, p["provider_order_id"])
        )
        assert r.status_code == 200, r.text
        paid = r.json()
        assert (paid["status"], paid["payment_status"], paid["token_no"]) == (
            "confirmed",
            "paid",
            "DOS-001",
        )
        queue = (await v.get("/api/v1/vendor/orders")).json()
        assert [q["token_no"] for q in queue] == ["DOS-001"]
        stats = (await v.get("/api/v1/vendor/stats")).json()
        assert stats["revenue_today_paise"] == 12000

    async for db in get_session():
        acts = list(await db.scalars(select(AuditLog.action)))
        assert "payment.captured" in acts


async def test_pay_reuses_gateway_order(
    client: AsyncClient, sms: FakeSms, shop: Any, gw: FakeProvider
) -> None:
    await login_student(client, sms)
    (o,) = await place_online(client, shop)
    a, b = await pay(client, o["id"]), await pay(client, o["id"])
    assert a["provider_order_id"] == b["provider_order_id"]
    assert len(gw.orders) == 1


async def test_multi_vendor_paid_separately(
    client: AsyncClient, sms: FakeSms, shop: Any, gw: FakeProvider
) -> None:
    await login_student(client, sms)
    dosa, pizza = await place_online(client, shop, both=True)
    p1, p2 = await pay(client, dosa["id"]), await pay(client, pizza["id"])
    assert (p1["amount_paise"], p2["amount_paise"]) == (12000, 15000)
    r = await client.post(
        f"/api/v1/orders/{dosa['id']}/pay/verify", json=checkout(gw, p1["provider_order_id"])
    )
    assert r.json()["token_no"] == "DOS-001"
    # pizza still unpaid
    assert (await client.get(f"/api/v1/orders/{pizza['id']}")).json()["status"] == (
        "pending_payment"
    )


async def test_webhook_confirms_when_tab_closed(
    client: AsyncClient, sms: FakeSms, shop: Any, gw: FakeProvider
) -> None:
    await login_student(client, sms)
    (o,) = await place_online(client, shop)
    p = await pay(client, o["id"])
    raw = webhook_event(gw, p["provider_order_id"])
    assert await send_webhook(client, raw) == 200
    got = (await client.get(f"/api/v1/orders/{o['id']}")).json()
    assert (got["status"], got["token_no"]) == ("confirmed", "DOS-001")
    # replay: no change, no second token
    assert await send_webhook(client, raw) == 200
    async for db in get_session():
        assert (await db.scalar(select(Payment))).raw_webhook is not None  # type: ignore[union-attr]
    r = await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["plain"], 1)])))
    assert r.json()["orders"][0]["token_no"] == "DOS-002"


async def test_verify_then_webhook_gives_one_token(
    client: AsyncClient, sms: FakeSms, shop: Any, gw: FakeProvider
) -> None:
    await login_student(client, sms)
    (o,) = await place_online(client, shop)
    p = await pay(client, o["id"])
    data = checkout(gw, p["provider_order_id"])
    r = await client.post(f"/api/v1/orders/{o['id']}/pay/verify", json=data)
    assert r.status_code == 200
    raw = webhook_event(gw, p["provider_order_id"], data["razorpay_payment_id"])
    assert await send_webhook(client, raw) == 200
    r = await client.post(f"/api/v1/orders/{o['id']}/pay/verify", json=data)  # retry
    assert r.status_code == 200 and r.json()["token_no"] == "DOS-001"
    assert gw.refunds == []


# ------------------------------------------------------------------ attacks
async def test_forged_checkout_rejected(
    client: AsyncClient, sms: FakeSms, shop: Any, gw: FakeProvider
) -> None:
    await login_student(client, sms)
    (o,) = await place_online(client, shop)
    p = await pay(client, o["id"])
    data = checkout(gw, p["provider_order_id"])
    url = f"/api/v1/orders/{o['id']}/pay/verify"

    bad_sig = {**data, "razorpay_signature": "0" * 64}
    assert (await client.post(url, json=bad_sig)).status_code == 400
    # signature for a payment id that the gateway never saw
    ghost = "pay_ghost123"
    forged = {
        **data,
        "razorpay_payment_id": ghost,
        "razorpay_signature": gw.sign(data["razorpay_order_id"], ghost),
    }
    assert (await client.post(url, json=forged)).status_code == 502  # fetch fails → not paid
    assert (await client.get(f"/api/v1/orders/{o['id']}")).json()["status"] == "pending_payment"


async def test_short_payment_rejected(
    client: AsyncClient, sms: FakeSms, shop: Any, gw: FakeProvider
) -> None:
    """Paying ₹1 for a ₹120 order must not confirm it."""
    await login_student(client, sms)
    (o,) = await place_online(client, shop)
    p = await pay(client, o["id"])
    data = checkout(gw, p["provider_order_id"], amount_paise=100)
    r = await client.post(f"/api/v1/orders/{o['id']}/pay/verify", json=data)
    assert r.status_code == 400
    assert (await client.get(f"/api/v1/orders/{o['id']}")).json()["payment_status"] == "unpaid"


async def test_payment_for_other_order_rejected(
    client: AsyncClient, sms: FakeSms, shop: Any, gw: FakeProvider
) -> None:
    """A cheap order's payment replayed against an expensive one."""
    await login_student(client, sms)
    dosa, pizza = await place_online(client, shop, both=True)
    p1 = await pay(client, dosa["id"])
    await pay(client, pizza["id"])
    data = checkout(gw, p1["provider_order_id"])
    r = await client.post(f"/api/v1/orders/{pizza['id']}/pay/verify", json=data)
    assert r.status_code == 400


async def test_webhook_bad_signature(
    client: AsyncClient, sms: FakeSms, shop: Any, gw: FakeProvider
) -> None:
    await login_student(client, sms)
    (o,) = await place_online(client, shop)
    p = await pay(client, o["id"])
    raw = webhook_event(gw, p["provider_order_id"])
    assert await send_webhook(client, raw, sig="f" * 64) == 400
    tampered = raw.replace(b'"amount": 12000', b'"amount": 100')
    assert await send_webhook(client, tampered, sig=FakeProvider.sign_webhook(raw)) == 400
    assert (await client.get(f"/api/v1/orders/{o['id']}")).json()["status"] == "pending_payment"


async def test_webhook_unknown_order_ignored(client: AsyncClient, gw: FakeProvider) -> None:
    raw = json.dumps(
        {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_x",
                        "order_id": "order_elsewhere",
                        "amount": 100,
                        "currency": "INR",
                        "status": "captured",
                    }
                }
            },
        }
    ).encode()
    assert await send_webhook(client, raw) == 200


async def test_cannot_pay_others_or_paid_or_counter_orders(
    client: AsyncClient,
    sms: FakeSms,
    shop: Any,
    gw: FakeProvider,
    make_client: Callable[[], AsyncClient],
) -> None:
    await login_student(client, sms)
    (o,) = await place_online(client, shop)
    async with make_client() as other:
        await login_student(other, sms, OTHER_PHONE)
        assert (await other.post(f"/api/v1/orders/{o['id']}/pay")).status_code == 404
        r = await other.post(
            f"/api/v1/payments/fake/{(await pay(client, o['id']))['provider_order_id']}/complete"
        )
        assert r.status_code == 404
    p = await pay(client, o["id"])
    await client.post(
        f"/api/v1/orders/{o['id']}/pay/verify", json=checkout(gw, p["provider_order_id"])
    )
    assert (await client.post(f"/api/v1/orders/{o['id']}/pay")).status_code == 409
    r = await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["plain"], 1)])))
    counter_id = r.json()["orders"][0]["id"]
    assert (await client.post(f"/api/v1/orders/{counter_id}/pay")).status_code == 409


async def test_gateway_down(client: AsyncClient, sms: FakeSms, shop: Any, gw: FakeProvider) -> None:
    await login_student(client, sms)
    (o,) = await place_online(client, shop)
    gw.fail_next = True
    r = await client.post(f"/api/v1/orders/{o['id']}/pay")
    assert r.status_code == 502
    assert (await client.post(f"/api/v1/orders/{o['id']}/pay")).status_code == 200


# ------------------------------------------------------- expiry & refunds
async def _age(order_id: str) -> None:
    async for db in get_session():
        await db.execute(
            update(Order)
            .where(Order.id == uuid.UUID(order_id))
            .values(created_at=datetime.now(UTC) - PENDING_TTL - timedelta(minutes=1))
        )
        await db.commit()


async def test_unpaid_order_expires(
    client: AsyncClient, sms: FakeSms, shop: Any, gw: FakeProvider
) -> None:
    await login_student(client, sms)
    (o,) = await place_online(client, shop)
    await _age(o["id"])
    got = (await client.get(f"/api/v1/orders/{o['id']}")).json()
    assert got["status"] == "cancelled"
    assert (await client.post(f"/api/v1/orders/{o['id']}/pay")).status_code == 409


async def test_late_payment_after_expiry_is_refunded(
    client: AsyncClient, sms: FakeSms, shop: Any, gw: FakeProvider
) -> None:
    await login_student(client, sms)
    (o,) = await place_online(client, shop)
    p = await pay(client, o["id"])
    await _age(o["id"])
    assert (await client.get("/api/v1/orders/me")).json()[0]["status"] == "cancelled"
    raw = webhook_event(gw, p["provider_order_id"])
    assert await send_webhook(client, raw) == 200
    assert len(gw.refunds) == 1 and gw.refunds[0][1] == 12000
    got = (await client.get(f"/api/v1/orders/{o['id']}")).json()
    assert (got["status"], got["payment_status"], got["token_no"]) == (
        "cancelled",
        "refunded",
        None,
    )
    assert await send_webhook(client, raw) == 200  # replay: no second refund
    assert len(gw.refunds) == 1


async def test_vendor_cancel_refunds_online(
    client: AsyncClient,
    sms: FakeSms,
    shop: Any,
    gw: FakeProvider,
    make_client: Callable[[], AsyncClient],
) -> None:
    await login_student(client, sms)
    (o,) = await place_online(client, shop)
    p = await pay(client, o["id"])
    await client.post(
        f"/api/v1/orders/{o['id']}/pay/verify", json=checkout(gw, p["provider_order_id"])
    )
    url = f"/api/v1/vendor/orders/{o['id']}/status"
    async with vendor_login(make_client, "dosa") as v:
        gw.fail_next = True  # refund fails → nothing changes
        assert (await v.patch(url, json={"status": "cancelled"})).status_code == 502
        assert (await client.get(f"/api/v1/orders/{o['id']}")).json()["status"] == "confirmed"

        r = await v.patch(url, json={"status": "cancelled"})
        assert r.status_code == 200
        assert (r.json()["status"], r.json()["payment_status"]) == ("cancelled", "refunded")
    assert gw.refunds == [(gw.refunds[0][0], 12000)]
    async for db in get_session():
        pay_row = await db.scalar(select(Payment))
        assert pay_row is not None and pay_row.status == GatewayPaymentStatus.refunded


async def test_fake_complete_and_config(
    client: AsyncClient, sms: FakeSms, shop: Any, gw: FakeProvider
) -> None:
    assert (await client.get("/api/v1/config")).json() == {
        "online_payments": True,
        "payment_provider": "fake",
    }
    await login_student(client, sms)
    (o,) = await place_online(client, shop)
    p = await pay(client, o["id"])
    r = await client.post(f"/api/v1/payments/fake/{p['provider_order_id']}/complete")
    assert r.status_code == 200
    r = await client.post(f"/api/v1/orders/{o['id']}/pay/verify", json=r.json())
    assert r.json()["status"] == "confirmed"
