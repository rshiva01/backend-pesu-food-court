import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.db.models import AuditLog, MenuItem, Order, OrderItem, Vendor
from app.db.session import get_session
from app.services import orders as svc
from tests.helpers import FakeSms, code_at, login_student, make_vendor_staff, staff_login

OTHER_PHONE = "+919812345678"


# ------------------------------------------------------------------ helpers
async def add_menu_item(
    vendor: Vendor, name: str, price: int, *, available: bool = True
) -> MenuItem:
    async for db in get_session():
        mi = MenuItem(
            vendor_id=vendor.id,
            name=name,
            category="Main",
            price_paise=price,
            is_available=available,
        )
        db.add(mi)
        await db.commit()
        return mi
    raise AssertionError("unreachable")


async def set_vendor(vendor: Vendor, **fields: Any) -> None:
    async for db in get_session():
        v = await db.get(Vendor, vendor.id)
        assert v is not None
        for k, val in fields.items():
            setattr(v, k, val)
        await db.commit()


async def count(model: Any) -> int:
    async for db in get_session():
        return int(await db.scalar(select(func.count()).select_from(model)) or 0)
    return 0


def body(*groups: tuple[Vendor, list[tuple[MenuItem, int]]], key: str | None = None) -> Any:
    return {
        "groups": [
            {
                "vendor_id": str(v.id),
                "items": [{"menu_item_id": str(m.id), "qty": q} for m, q in lines],
            }
            for v, lines in groups
        ],
        "payment_mode": "counter",
        "idempotency_key": key or str(uuid.uuid4()),
    }


@asynccontextmanager
async def vendor_login(
    make_client: Callable[[], AsyncClient], username: str
) -> AsyncIterator[AsyncClient]:
    async with make_client() as c:
        assert await staff_login(c, username, f"{username}-pass-123", code_at()) == 200
        yield c


@pytest.fixture
async def shop() -> dict[str, Any]:
    dosa = await make_vendor_staff("dosa", "dosa-pass-123", "DOS", "Dosa Corner")
    pizza = await make_vendor_staff("pizza", "pizza-pass-123", "PIZ", "Pizza Hut")
    return {
        "dosa": dosa,
        "pizza": pizza,
        "masala": await add_menu_item(dosa, "Masala Dosa", 6000),
        "plain": await add_menu_item(dosa, "Plain Dosa", 4000),
        "sold": await add_menu_item(dosa, "Rava Dosa", 7000, available=False),
        "marg": await add_menu_item(pizza, "Margherita", 15000),
    }


# ---------------------------------------------------------------- checkout
async def test_multi_vendor_counter_order(client: AsyncClient, sms: FakeSms, shop: Any) -> None:
    await login_student(client, sms)
    r = await client.post(
        "/api/v1/orders",
        json=body(
            (shop["dosa"], [(shop["masala"], 2), (shop["plain"], 1)]),
            (shop["pizza"], [(shop["marg"], 1)]),
        ),
    )
    assert r.status_code == 201, r.text
    out = r.json()
    dosa, pizza = out["orders"]  # request order kept
    assert (dosa["token_no"], pizza["token_no"]) == ("DOS-001", "PIZ-001")
    assert dosa["total_paise"] == 2 * 6000 + 4000
    assert pizza["total_paise"] == 15000
    assert dosa["status"] == "awaiting_payment" and dosa["payment_status"] == "unpaid"
    assert dosa["group_id"] == pizza["group_id"] == out["group_id"]
    assert dosa["vendor"] == {
        "id": str(shop["dosa"].id),
        "slug": "dosa-corner",
        "name": "Dosa Corner",
    }
    assert {i["name"]: i["qty"] for i in dosa["items"]} == {"Masala Dosa": 2, "Plain Dosa": 1}

    # next order at dosa gets the next number
    r = await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["plain"], 1)])))
    assert r.json()["orders"][0]["token_no"] == "DOS-002"

    mine = (await client.get("/api/v1/orders/me")).json()
    assert len(mine) == 3
    one = await client.get(f"/api/v1/orders/{dosa['id']}")
    assert one.status_code == 200 and one.json()["token_no"] == "DOS-001"


async def test_price_from_client_rejected_and_ignored(
    client: AsyncClient, sms: FakeSms, shop: Any
) -> None:
    await login_student(client, sms)
    b = body((shop["dosa"], [(shop["masala"], 1)]))
    b["groups"][0]["items"][0]["unit_price_paise"] = 1
    assert (await client.post("/api/v1/orders", json=b)).status_code == 422
    b["groups"][0]["items"][0].pop("unit_price_paise")
    b["total_paise"] = 1
    assert (await client.post("/api/v1/orders", json=b)).status_code == 422
    assert await count(Order) == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["groups"][0]["items"][0].update(qty=0),
        lambda b: b["groups"][0]["items"][0].update(qty=21),
        lambda b: b["groups"][0]["items"][0].update(qty="2"),
        lambda b: b.update(groups=[]),
        lambda b: b.update(groups=b["groups"] * 2),  # same vendor twice
        lambda b: b.update(idempotency_key="not-a-uuid"),
        lambda b: b.update(payment_mode="free"),
    ],
)
async def test_bad_bodies(client: AsyncClient, sms: FakeSms, shop: Any, mutate: Any) -> None:
    await login_student(client, sms)
    b = body((shop["dosa"], [(shop["masala"], 1)]))
    mutate(b)
    assert (await client.post("/api/v1/orders", json=b)).status_code == 422
    assert await count(Order) == 0


async def test_too_many_vendors_message(client: AsyncClient, sms: FakeSms, shop: Any) -> None:
    await login_student(client, sms)
    b = body((shop["dosa"], [(shop["masala"], 1)]))
    b["groups"] = [{**b["groups"][0], "vendor_id": str(uuid.uuid4())} for _ in range(5)]
    r = await client.post("/api/v1/orders", json=b)
    assert r.status_code == 422
    assert r.json()["detail"] == "You can order from up to 4 counters at once"


async def test_online_payment_not_available_yet(
    client: AsyncClient, sms: FakeSms, shop: Any
) -> None:
    await login_student(client, sms)
    b = body((shop["dosa"], [(shop["masala"], 1)]))
    b["payment_mode"] = "online"
    r = await client.post("/api/v1/orders", json=b)
    assert r.status_code == 422
    assert "coming soon" in r.json()["detail"]
    assert await count(Order) == 0


async def test_idempotent_retry(client: AsyncClient, sms: FakeSms, shop: Any) -> None:
    await login_student(client, sms)
    b = body((shop["dosa"], [(shop["masala"], 1)]), (shop["pizza"], [(shop["marg"], 1)]))
    first = await client.post("/api/v1/orders", json=b)
    again = await client.post("/api/v1/orders", json=b)
    assert first.status_code == 201 and again.status_code == 200
    assert [o["id"] for o in first.json()["orders"]] == [o["id"] for o in again.json()["orders"]]
    assert await count(Order) == 2


async def test_concurrent_same_key_creates_once(
    client: AsyncClient, sms: FakeSms, shop: Any
) -> None:
    await login_student(client, sms)
    b = body((shop["dosa"], [(shop["masala"], 1)]))
    rs = await asyncio.gather(*(client.post("/api/v1/orders", json=b) for _ in range(4)))
    assert sorted(r.status_code for r in rs) == [200, 200, 200, 201]
    assert len({r.json()["orders"][0]["id"] for r in rs}) == 1
    assert await count(Order) == 1


async def test_key_is_per_student(
    client: AsyncClient, sms: FakeSms, shop: Any, make_client: Callable[[], AsyncClient]
) -> None:
    """Another student reusing my key must not get my orders back."""
    await login_student(client, sms)
    b = body((shop["dosa"], [(shop["masala"], 1)]))
    mine = (await client.post("/api/v1/orders", json=b)).json()
    async with make_client() as other:
        await login_student(other, sms, OTHER_PHONE)
        theirs = await other.post("/api/v1/orders", json=b)
        assert theirs.status_code == 201
        assert theirs.json()["orders"][0]["id"] != mine["orders"][0]["id"]


@pytest.mark.parametrize("problem", ["sold_out", "closed", "missing_vendor", "foreign_item"])
async def test_all_or_nothing_conflicts(
    client: AsyncClient, sms: FakeSms, shop: Any, problem: str
) -> None:
    await login_student(client, sms)
    good = (shop["dosa"], [(shop["masala"], 1)])
    bad_item = None
    if problem == "sold_out":
        bad, bad_item = (shop["pizza"], [(shop["marg"], 1)]), shop["marg"]
        await set_vendor(shop["pizza"], is_open=True)
        async for db in get_session():
            m = await db.get(MenuItem, shop["marg"].id)
            assert m is not None
            m.is_available = False
            await db.commit()
        expect = "Margherita at Pizza Hut just sold out"
    elif problem == "closed":
        bad = (shop["pizza"], [(shop["marg"], 1)])
        await set_vendor(shop["pizza"], is_open=False)
        expect = "Pizza Hut is closed right now"
    elif problem == "missing_vendor":
        await set_vendor(shop["pizza"], is_active=False)
        bad = (shop["pizza"], [(shop["marg"], 1)])
        expect = "A counter in your cart no longer exists"
    else:  # a dosa item smuggled into the pizza group
        bad, bad_item = (shop["pizza"], [(shop["masala"], 1)]), shop["masala"]
        expect = "An item at Pizza Hut just sold out"
    r = await client.post("/api/v1/orders", json=body(good, bad))
    assert r.status_code == 409, r.text
    j = r.json()
    assert j["detail"] == expect
    assert j["vendor_id"] == str(shop["pizza"].id)
    assert j.get("item_id") == (str(bad_item.id) if bad_item else None)
    assert await count(Order) == 0 and await count(OrderItem) == 0
    # the failed checkout didn't burn a token number
    r = await client.post("/api/v1/orders", json=body(good))
    assert r.json()["orders"][0]["token_no"] == "DOS-001"


async def test_sold_out_item_rejected(client: AsyncClient, sms: FakeSms, shop: Any) -> None:
    await login_student(client, sms)
    r = await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["sold"], 1)])))
    assert r.status_code == 409
    assert r.json()["item_id"] == str(shop["sold"].id)


async def test_tokens_restart_each_day(
    client: AsyncClient, sms: FakeSms, shop: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await login_student(client, sms)
    monkeypatch.setattr(svc, "business_today", lambda: date(2026, 11, 24))
    r = await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["plain"], 1)])))
    assert r.json()["orders"][0]["token_no"] == "DOS-001"
    monkeypatch.setattr(svc, "business_today", lambda: date(2026, 11, 25))
    r = await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["plain"], 1)])))
    assert r.json()["orders"][0]["token_no"] == "DOS-001"


async def test_order_rate_limit(client: AsyncClient, sms: FakeSms, shop: Any) -> None:
    await login_student(client, sms)
    for _ in range(svc.ORDER_LIMIT):
        r = await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["plain"], 1)])))
        assert r.status_code == 201
    r = await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["plain"], 1)])))
    assert r.status_code == 429


async def test_orders_need_student_login(client: AsyncClient, shop: Any) -> None:
    assert (await client.get("/api/v1/orders/me")).status_code == 401
    r = await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["plain"], 1)])))
    assert r.status_code == 401


# --------------------------------------------------------------------- IDOR
async def test_student_cannot_read_others_order(
    client: AsyncClient, sms: FakeSms, shop: Any, make_client: Callable[[], AsyncClient]
) -> None:
    await login_student(client, sms)
    r = await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["plain"], 1)])))
    oid = r.json()["orders"][0]["id"]
    async with make_client() as other:
        await login_student(other, sms, OTHER_PHONE)
        assert (await other.get(f"/api/v1/orders/{oid}")).status_code == 404
        assert (await other.get("/api/v1/orders/me")).json() == []
    assert (await client.get(f"/api/v1/orders/{uuid.uuid4()}")).status_code == 404


async def test_vendor_cannot_touch_other_vendors_order(
    client: AsyncClient, sms: FakeSms, shop: Any, make_client: Callable[[], AsyncClient]
) -> None:
    await login_student(client, sms)
    r = await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["plain"], 1)])))
    oid = r.json()["orders"][0]["id"]
    async with vendor_login(make_client, "pizza") as pizza:
        assert (await pizza.get("/api/v1/vendor/orders")).json() == []
        base = f"/api/v1/vendor/orders/{oid}"
        assert (await pizza.post(f"{base}/mark-paid")).status_code == 404
        assert (await pizza.post(f"{base}/collect")).status_code == 404
        r = await pizza.patch(f"{base}/status", json={"status": "cancelled"})
        assert r.status_code == 404
    # students can't use vendor routes
    assert (await client.get("/api/v1/vendor/orders")).status_code == 401


# ------------------------------------------------------------ vendor queue
async def test_full_counter_flow(
    client: AsyncClient, sms: FakeSms, shop: Any, make_client: Callable[[], AsyncClient]
) -> None:
    await login_student(client, sms)
    r = await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["masala"], 2)])))
    oid = r.json()["orders"][0]["id"]
    base = f"/api/v1/vendor/orders/{oid}"
    async with vendor_login(make_client, "dosa") as v:
        queue = (await v.get("/api/v1/vendor/orders")).json()
        assert len(queue) == 1
        assert queue[0]["student_label"] == "Student ··210"
        assert "9876543210" not in str(queue)

        # can't start cooking before the student pays
        r = await v.patch(f"{base}/status", json={"status": "preparing"})
        assert r.status_code == 409
        assert (await v.post(f"{base}/collect")).status_code == 409

        r = await v.post(f"{base}/mark-paid")
        assert r.status_code == 200
        assert (r.json()["status"], r.json()["payment_status"]) == ("confirmed", "paid")
        assert (await v.post(f"{base}/mark-paid")).status_code == 409

        assert (await v.patch(f"{base}/status", json={"status": "preparing"})).status_code == 200
        r = await v.patch(f"{base}/status", json={"status": "ready"})
        assert r.status_code == 200 and r.json()["ready_at"] is not None

        # student sees it
        assert (await client.get(f"/api/v1/orders/{oid}")).json()["status"] == "ready"

        r = await v.post(f"{base}/collect")
        assert r.status_code == 200 and r.json()["status"] == "collected"
        # terminal: nothing moves after collect
        for s in ("preparing", "ready", "cancelled", "collected"):
            assert (await v.patch(f"{base}/status", json={"status": s})).status_code == 409

        stats = (await v.get("/api/v1/vendor/stats")).json()
        assert stats == {"orders_today": 1, "revenue_today_paise": 12000, "open_orders": 0}

    async for db in get_session():
        actions = [
            a
            for a in await db.scalars(select(AuditLog.action).order_by(AuditLog.at))
            if a.startswith("order.")
        ]
        assert actions == ["order.mark_paid", "order.preparing", "order.ready", "order.collected"]
        o = await db.get(Order, uuid.UUID(oid))
        assert o is not None and o.collected_by is not None and o.collected_at is not None


async def test_cancel_rules_and_stats(
    client: AsyncClient, sms: FakeSms, shop: Any, make_client: Callable[[], AsyncClient]
) -> None:
    await login_student(client, sms)
    ids = []
    for _ in range(3):
        r = await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["plain"], 1)])))
        ids.append(r.json()["orders"][0]["id"])
    unpaid, paid, open_ = ids
    async with vendor_login(make_client, "dosa") as v:
        r = await v.patch(f"/api/v1/vendor/orders/{unpaid}/status", json={"status": "cancelled"})
        assert (r.json()["status"], r.json()["payment_status"]) == ("cancelled", "unpaid")

        await v.post(f"/api/v1/vendor/orders/{paid}/mark-paid")
        r = await v.patch(f"/api/v1/vendor/orders/{paid}/status", json={"status": "cancelled"})
        assert (r.json()["status"], r.json()["payment_status"]) == ("cancelled", "refunded")

        await v.post(f"/api/v1/vendor/orders/{open_}/mark-paid")
        stats = (await v.get("/api/v1/vendor/stats")).json()
        assert stats == {"orders_today": 1, "revenue_today_paise": 4000, "open_orders": 1}


async def test_vendor_queue_is_today_only(
    client: AsyncClient,
    sms: FakeSms,
    shop: Any,
    make_client: Callable[[], AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await login_student(client, sms)
    real = svc.business_today
    monkeypatch.setattr(svc, "business_today", lambda: date(2020, 1, 1))
    await client.post("/api/v1/orders", json=body((shop["dosa"], [(shop["plain"], 1)])))
    monkeypatch.setattr(svc, "business_today", real)
    async with vendor_login(make_client, "dosa") as v:
        assert (await v.get("/api/v1/vendor/orders")).json() == []
        assert (await v.get("/api/v1/vendor/stats")).json()["orders_today"] == 0
    # but the student still sees it in history
    assert len((await client.get("/api/v1/orders/me")).json()) == 1
