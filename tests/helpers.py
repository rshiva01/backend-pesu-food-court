"""Shared test helpers (login flows, seeded staff)."""

from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from httpx import AsyncClient

from app.cli import _add_staff
from app.db.models import Vendor
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
