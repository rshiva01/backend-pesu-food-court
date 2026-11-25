"""Shared test helpers (login flows, seeded staff)."""

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.cli import _add_staff
from app.db.models import MenuItem, Vendor
from app.db.models.enums import Role
from app.db.session import get_session
from app.services import auth as svc

PHONE = "+919876543210"
SECRET = pyotp.random_base32()


class FakeSms:
    def __init__(self) -> None:
        self.sent: dict[str, str] = {}

    async def send_otp(self, phone: str, code: str) -> None:
        self.sent[phone] = code


@pytest.fixture
def sms(monkeypatch: pytest.MonkeyPatch) -> FakeSms:
    fake = FakeSms()
    monkeypatch.setattr(svc, "get_sms", lambda: fake)
    return fake


async def login_student(client: AsyncClient, sms: FakeSms, phone: str = PHONE) -> dict[str, str]:
    r = await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    assert r.status_code == 202, r.text
    r = await client.post(
        "/api/v1/auth/otp/verify", json={"phone": phone, "code": sms.sent[phone], "remember": True}
    )
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def make_vendor_staff(
    username: str = "dosa",
    password: str = "dosa-pass-123",
    prefix: str = "DOS",
    name: str = "Dosa Corner",
) -> Vendor:
    user = await _add_staff(username, password, Role.vendor, f"{name} staff", SECRET)
    assert user is not None
    async for db in get_session():
        v = Vendor(
            slug=f"{username}-corner",
            name=name,
            token_prefix=prefix,
            owner_user_id=user.id,
            is_open=True,
        )
        db.add(v)
        await db.commit()
        return v
    raise AssertionError("unreachable")


def code_at(offset_steps: int = 0) -> str:
    t = pyotp.TOTP(SECRET)
    return t.at(datetime.now(UTC) + timedelta(seconds=offset_steps * t.interval))


async def staff_login(client: AsyncClient, username: str, password: str, totp: str) -> int:
    r = await client.post(
        "/api/v1/auth/staff/login", json={"username": username, "password": password, "totp": totp}
    )
    return r.status_code


# ------------------------------------------------------------- order helpers
OTHER_PHONE = "+919812345678"


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
