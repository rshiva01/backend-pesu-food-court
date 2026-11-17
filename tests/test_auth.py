from datetime import UTC, datetime, timedelta

import jwt
import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.cli import _add_staff
from app.core.redis import get_redis
from app.db.models import Session, Vendor
from app.db.models.enums import Role
from app.db.session import get_session
from app.services import auth as svc

PHONE = "+919876543210"


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


# ------------------------------------------------------------------ OTP
async def test_otp_login_creates_student_and_sets_cookies(
    client: AsyncClient, sms: FakeSms
) -> None:
    await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})
    r = await client.post(
        "/api/v1/auth/otp/verify", json={"phone": PHONE, "code": sms.sent[PHONE], "remember": True}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["phone"] == PHONE and body["role"] == "student"
    cookies = r.headers.get_list("set-cookie")
    assert len(cookies) == 2
    for c in cookies:
        assert "HttpOnly" in c and "SameSite=strict" in c and "Path=/" in c
    # no token-like value in the JSON body
    assert "eyJ" not in r.text
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200 and me.json()["id"] == body["id"]


async def test_otp_is_single_use(client: AsyncClient, sms: FakeSms) -> None:
    await login_student(client, sms)
    r = await client.post(
        "/api/v1/auth/otp/verify", json={"phone": PHONE, "code": sms.sent[PHONE], "remember": True}
    )
    assert r.status_code == 400


async def test_otp_burned_after_five_wrong_tries(client: AsyncClient, sms: FakeSms) -> None:
    await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})
    good = sms.sent[PHONE]
    wrong = "000000" if good != "000000" else "111111"
    for _ in range(5):
        r = await client.post("/api/v1/auth/otp/verify", json={"phone": PHONE, "code": wrong})
        assert r.status_code == 400
    r = await client.post("/api/v1/auth/otp/verify", json={"phone": PHONE, "code": good})
    assert r.status_code == 400  # even the right code is dead now


async def test_otp_resend_cooldown_and_hourly_limit(client: AsyncClient, sms: FakeSms) -> None:
    r1 = await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})
    first = sms.sent[PHONE]
    r2 = await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})
    assert r2.status_code == 202 and 0 < r2.json()["resend_after"] <= 30
    assert sms.sent[PHONE] == first  # nothing re-sent during cooldown
    assert r1.json() == {"ok": True, "resend_after": 30}
    for _ in range(4):
        await get_redis().delete(f"otp:cool:{PHONE}")
        assert (
            await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})
        ).status_code == 202
    await get_redis().delete(f"otp:cool:{PHONE}")
    r = await client.post("/api/v1/auth/otp/request", json={"phone": PHONE})
    assert r.status_code == 429


@pytest.mark.parametrize(
    "phone", ["9876543210", "+915876543210", "+91987654321", "+1987654321", "+91 9876543210"]
)
async def test_invalid_phone_rejected(client: AsyncClient, sms: FakeSms, phone: str) -> None:
    r = await client.post("/api/v1/auth/otp/request", json={"phone": phone})
    assert r.status_code == 422
    assert phone not in r.text  # never echo input


async def test_extra_fields_rejected(client: AsyncClient, sms: FakeSms) -> None:
    r = await client.post("/api/v1/auth/otp/request", json={"phone": PHONE, "role": "admin"})
    assert r.status_code == 422


# ------------------------------------------------------------------ sessions
async def test_refresh_rotates_and_reuse_revokes_family(
    client: AsyncClient, sms: FakeSms, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "RACE_GRACE", timedelta(0))
    await login_student(client, sms)
    _, rt = svc.cookie_names("student")
    old_rt = client.cookies.get(rt)
    r = await client.post("/api/v1/auth/refresh")
    assert r.status_code == 204
    assert client.cookies.get(rt) != old_rt
    assert (await client.get("/api/v1/auth/me")).status_code == 200

    # attacker replays the stolen old refresh token
    new_cookies = dict(client.cookies)
    client.cookies.clear()
    client.cookies.set(rt, old_rt or "")
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401

    # the legitimate (rotated) session was revoked too
    client.cookies.clear()
    for k, v in new_cookies.items():
        client.cookies.set(k, v)
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401


async def test_parallel_tab_refresh_is_not_treated_as_theft(
    client: AsyncClient, sms: FakeSms
) -> None:
    await login_student(client, sms)
    _, rt = svc.cookie_names("student")
    old_rt = client.cookies.get(rt) or ""
    assert (await client.post("/api/v1/auth/refresh")).status_code == 204
    keep = dict(client.cookies)
    client.cookies.clear()
    client.cookies.set(rt, old_rt)
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401  # just a miss
    client.cookies.clear()
    for k, v in keep.items():
        client.cookies.set(k, v)
    assert (await client.get("/api/v1/auth/me")).status_code == 200  # still logged in


async def test_logout_revokes_immediately(client: AsyncClient, sms: FakeSms) -> None:
    await login_student(client, sms)
    at, _ = svc.cookie_names("student")
    access = client.cookies.get(at) or ""
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    client.cookies.set(at, access)  # replay the still-unexpired access token
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_forged_tokens_rejected(client: AsyncClient, sms: FakeSms) -> None:
    user = await login_student(client, sms)
    at, _ = svc.cookie_names("student")
    async for db in get_session():
        sid = await db.scalar(select(Session.id))
    now = datetime.now(UTC)
    claims = {
        "iss": "pesu-food-court",
        "aud": "student",
        "sub": user["id"],
        "sid": str(sid),
        "role": "student",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    forged = [
        jwt.encode(claims, "attacker-guess-" * 3, algorithm="HS256"),  # alg confusion
        jwt.encode(claims, None, algorithm="none"),  # type: ignore[arg-type]
    ]
    for tok in forged:
        client.cookies.clear()
        client.cookies.set(at, tok)
        assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_student_token_not_valid_for_staff(client: AsyncClient, sms: FakeSms) -> None:
    await login_student(client, sms)
    at_s, _ = svc.cookie_names("student")
    at_staff, _ = svc.cookie_names("staff")
    client.cookies.set(at_staff, client.cookies.get(at_s) or "")  # wrong audience
    assert (await client.get("/api/v1/vendor/me")).status_code == 401
    assert (await client.get("/api/v1/admin/me")).status_code == 401


async def test_csrf_header_required_on_login(client: AsyncClient, sms: FakeSms) -> None:
    r = await client.post(
        "/api/v1/auth/otp/request", json={"phone": PHONE}, headers={"X-Requested-With": ""}
    )
    assert r.status_code == 403


# ------------------------------------------------------------------ staff
SECRET = pyotp.random_base32()


async def make_vendor_staff(username: str = "dosa", password: str = "dosa-pass-123") -> None:
    user = await _add_staff(username, password, Role.vendor, "Dosa staff", SECRET)
    assert user is not None
    async for db in get_session():
        db.add(
            Vendor(
                slug=f"{username}-corner",
                name="Dosa Corner",
                token_prefix="DOS",
                owner_user_id=user.id,
                is_open=True,
            )
        )
        await db.commit()


def code_at(offset_steps: int = 0) -> str:
    t = pyotp.TOTP(SECRET)
    return t.at(datetime.now(UTC) + timedelta(seconds=offset_steps * t.interval))


async def staff_login(client: AsyncClient, username: str, password: str, totp: str) -> int:
    r = await client.post(
        "/api/v1/auth/staff/login", json={"username": username, "password": password, "totp": totp}
    )
    return r.status_code


async def test_vendor_login_and_me(client: AsyncClient) -> None:
    await make_vendor_staff()
    r = await client.post(
        "/api/v1/auth/staff/login",
        json={"username": "DOSA", "password": "dosa-pass-123", "totp": code_at()},
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "vendor" and r.json()["username"] == "dosa"
    me = await client.get("/api/v1/vendor/me")
    assert me.status_code == 200 and me.json()["vendor"]["slug"] == "dosa-corner"
    assert (await client.get("/api/v1/admin/me")).status_code == 403  # no escalation
    assert (await client.get("/api/v1/auth/me")).status_code == 401  # not a student
    assert (await client.post("/api/v1/vendor/logout")).status_code == 204
    assert (await client.get("/api/v1/vendor/me")).status_code == 401


async def test_admin_login(client: AsyncClient) -> None:
    await _add_staff("boss", "boss-pass-1234", Role.admin, "Admin", SECRET)
    assert await staff_login(client, "boss", "boss-pass-1234", code_at()) == 200
    assert (await client.get("/api/v1/admin/me")).json()["role"] == "admin"
    assert (await client.get("/api/v1/vendor/me")).status_code == 403


async def test_staff_failures_look_identical(client: AsyncClient) -> None:
    await make_vendor_staff()
    bodies = set()
    for u, p, c in [
        ("nobody", "x", code_at()),
        ("dosa", "wrong", code_at()),
        ("dosa", "dosa-pass-123", "000000" if code_at() != "000000" else "111111"),
    ]:
        r = await client.post(
            "/api/v1/auth/staff/login", json={"username": u, "password": p, "totp": c}
        )
        assert r.status_code == 401
        bodies.add(r.text)
    assert len(bodies) == 1


async def test_totp_replay_rejected(client: AsyncClient) -> None:
    await make_vendor_staff()
    code = code_at()
    assert await staff_login(client, "dosa", "dosa-pass-123", code) == 200
    await client.post("/api/v1/vendor/logout")
    assert await staff_login(client, "dosa", "dosa-pass-123", code) == 401


async def test_lockout_after_five_failures(client: AsyncClient) -> None:
    await make_vendor_staff()
    for _ in range(5):
        assert await staff_login(client, "dosa", "wrong", code_at()) == 401
    # correct credentials are refused while locked
    assert await staff_login(client, "dosa", "dosa-pass-123", code_at()) == 401


async def test_disabled_vendor_is_logged_out(client: AsyncClient) -> None:
    await make_vendor_staff()
    assert await staff_login(client, "dosa", "dosa-pass-123", code_at()) == 200
    async for db in get_session():
        v = await db.scalar(select(Vendor))
        assert v is not None
        v.is_active = False
        await db.commit()
    assert (await client.get("/api/v1/vendor/me")).status_code == 401


async def test_secret_encrypted_at_rest() -> None:
    await make_vendor_staff()
    from app.db.models import StaffAccount

    async for db in get_session():
        acct = await db.scalar(select(StaffAccount))
        assert acct is not None
        assert SECRET not in acct.totp_secret_enc
        assert acct.password_hash.startswith("$argon2id$")


async def test_unknown_session_id_rejected(client: AsyncClient, sms: FakeSms) -> None:
    await login_student(client, sms)
    async for db in get_session():
        for s in (await db.scalars(select(Session))).all():
            await db.delete(s)
        await db.commit()
    assert (await client.get("/api/v1/auth/me")).status_code == 401
