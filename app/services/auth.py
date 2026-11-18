"""Sessions, cookies, student OTP login and staff password+TOTP login."""

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import pyotp
from fastapi import HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rate_limit
from app.core.config import get_settings
from app.core.redis import get_redis
from app.core.security import (
    Audience,
    constant_eq,
    decrypt_secret,
    make_access_token,
    new_otp,
    new_refresh_token,
    otp_digest,
    token_digest,
    verify_password,
)
from app.db.models import Session, StaffAccount, User
from app.db.models.enums import Role
from app.services import audit
from app.services.sms import get_sms

log = logging.getLogger(__name__)

OTP_TTL_S = 300
OTP_MAX_TRIES = 5
OTP_RESEND_S = 30
STAFF_MAX_FAILS = 5
STAFF_LOCK = timedelta(minutes=15)
RACE_GRACE = timedelta(seconds=10)

BAD_CODE = "Invalid or expired code"
BAD_STAFF = "Incorrect username, password or code"  # same text as the mock
TOO_MANY = "Too many attempts. Try again later."


def _now() -> datetime:
    return datetime.now(UTC)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------- cookies
def cookie_names(aud: Audience) -> tuple[str, str]:
    """(access, refresh). __Host- prefix in prod = Secure, Path=/, no Domain."""
    tag = "s" if aud == "student" else "staff"
    prefix = "__Host-" if get_settings().cookie_secure else ""
    return f"{prefix}pfc_{tag}_at", f"{prefix}pfc_{tag}_rt"


def _set_cookies(resp: Response, aud: Audience, access: str, refresh: str, sess: Session) -> None:
    s = get_settings()
    at, rt = cookie_names(aud)
    common = {"httponly": True, "secure": s.cookie_secure, "samesite": "strict", "path": "/"}
    resp.set_cookie(at, access, max_age=s.access_ttl_seconds, **common)  # type: ignore[arg-type]
    max_age = int((sess.expires_at - _now()).total_seconds()) if sess.persistent else None
    resp.set_cookie(rt, refresh, max_age=max_age, **common)  # type: ignore[arg-type]


def clear_cookies(resp: Response, aud: Audience) -> None:
    s = get_settings()
    for name in cookie_names(aud):
        resp.delete_cookie(name, path="/", secure=s.cookie_secure, httponly=True, samesite="strict")


# ---------------------------------------------------------------- sessions
def _ttl(persistent: bool) -> timedelta:
    s = get_settings()
    return (
        timedelta(days=s.refresh_ttl_days_remember)
        if persistent
        else timedelta(hours=s.refresh_ttl_hours_session)
    )


async def start_session(
    db: AsyncSession,
    user: User,
    aud: Audience,
    persistent: bool,
    request: Request,
    resp: Response,
    family_id: uuid.UUID | None = None,
) -> Session:
    raw = new_refresh_token()
    sess = Session(
        user_id=user.id,
        family_id=family_id or uuid.uuid4(),
        refresh_token_hash=token_digest(raw),
        user_agent=(request.headers.get("user-agent") or "")[:255] or None,
        ip=client_ip(request)[:45],
        expires_at=_now() + _ttl(persistent),
        persistent=persistent,
    )
    db.add(sess)
    await db.flush()
    _set_cookies(resp, aud, make_access_token(user.id, user.role.value, sess.id, aud), raw, sess)
    return sess


async def rotate(db: AsyncSession, aud: Audience, request: Request, resp: Response) -> bool:
    """Swap a valid refresh token for a new one. Re-use of a rotated token
    (outside a short multi-tab race window) revokes the whole family."""
    raw = request.cookies.get(cookie_names(aud)[1])
    if not raw:
        return False
    sess = await db.scalar(
        select(Session).where(Session.refresh_token_hash == token_digest(raw)).with_for_update()
    )
    if sess is None:
        return False
    now = _now()
    if sess.revoked_at is not None:
        raced = sess.replaced_by is not None and now - sess.revoked_at < RACE_GRACE
        if not raced:
            log.warning(
                "refresh token reuse; revoking family", extra={"ctx": {"user": str(sess.user_id)}}
            )
            await db.execute(
                update(Session)
                .where(Session.family_id == sess.family_id, Session.revoked_at.is_(None))
                .values(revoked_at=now)
            )
        return False
    if sess.expires_at <= now:
        return False
    user = await db.get(User, sess.user_id)
    if user is None or not user.is_active or (aud == "student") != (user.role == Role.student):
        return False
    new = await start_session(db, user, aud, sess.persistent, request, resp, sess.family_id)
    sess.revoked_at, sess.replaced_by = now, new.id
    return True


async def end_session(db: AsyncSession, aud: Audience, request: Request, resp: Response) -> None:
    raw = request.cookies.get(cookie_names(aud)[1])
    if raw:
        await db.execute(
            update(Session)
            .where(Session.refresh_token_hash == token_digest(raw), Session.revoked_at.is_(None))
            .values(revoked_at=_now())
        )
    clear_cookies(resp, aud)


# ---------------------------------------------------------------- student OTP
async def request_otp(phone: str, ip: str) -> int:
    """Returns seconds until resend is allowed. Same answer whether or not the
    number has an account."""
    r = get_redis()
    cool = await r.ttl(f"otp:cool:{phone}")
    if cool > 0:
        return int(cool)
    if not await rate_limit.hit(f"otp:ip:{ip}", 20, 3600) or not await rate_limit.hit(
        f"otp:phone:{phone}", 5, 3600
    ):
        raise HTTPException(429, TOO_MANY)
    code = new_otp()
    pipe = r.pipeline()
    pipe.hset(f"otp:{phone}", mapping={"d": otp_digest(phone, code), "tries": 0})
    pipe.expire(f"otp:{phone}", OTP_TTL_S)
    pipe.set(f"otp:cool:{phone}", 1, ex=OTP_RESEND_S)
    await pipe.execute()
    await get_sms().send_otp(phone, code)
    return OTP_RESEND_S


async def verify_otp(db: AsyncSession, phone: str, code: str, ip: str) -> User:
    if not await rate_limit.hit(f"otpv:ip:{ip}", 60, 3600):
        raise HTTPException(429, TOO_MANY)
    r = get_redis()
    key = f"otp:{phone}"
    stored = cast(str | None, await r.hget(key, "d"))  # type: ignore[misc]
    if not stored:
        raise HTTPException(400, BAD_CODE)
    tries = int(await r.hincrby(key, "tries", 1))  # type: ignore[misc]
    if tries > OTP_MAX_TRIES:
        await r.delete(key)
        raise HTTPException(400, BAD_CODE)
    if not constant_eq(stored, otp_digest(phone, code)):
        if tries >= OTP_MAX_TRIES:
            await r.delete(key)  # burn it: brute force gets 5 guesses in 10^6
        raise HTTPException(400, BAD_CODE)
    await r.delete(key)  # single use

    user = await db.scalar(select(User).where(User.phone == phone))
    if user is None:
        user = User(phone=phone, role=Role.student)
        db.add(user)
        await db.flush()
    elif user.role != Role.student or not user.is_active:
        raise HTTPException(400, BAD_CODE)
    return user


# ---------------------------------------------------------------- staff login
def _totp_step(secret: str, code: str, now: float) -> int | None:
    """Time step the code belongs to (±1 window), or None."""
    t = pyotp.TOTP(secret)
    base = int(now) // t.interval
    for step in (base, base - 1, base + 1):
        if constant_eq(t.generate_otp(step), code):
            return step
    return None


async def staff_login(
    db: AsyncSession, username: str, password: str, totp: str, ip: str
) -> tuple[User, StaffAccount]:
    if not await rate_limit.hit(f"staff:ip:{ip}", 20, 900) or not await rate_limit.hit(
        f"staff:user:{username}", 10, 900
    ):
        raise HTTPException(429, TOO_MANY)

    acct = await db.scalar(
        select(StaffAccount).where(StaffAccount.username == username).with_for_update()
    )
    user = await db.get(User, acct.user_id) if acct else None
    # always run the password hash, so unknown usernames take as long as known ones
    pw_ok = verify_password(acct.password_hash if acct else None, password)
    now = _now()
    locked = acct is not None and acct.locked_until is not None and acct.locked_until > now

    step = None
    if acct and pw_ok and not locked:
        step = _totp_step(decrypt_secret(acct.totp_secret_enc), totp, time.time())
        if step is not None:
            last = await get_redis().get(f"totp:last:{acct.user_id}")
            if last is not None and step <= int(last):
                step = None  # replayed code

    ok = (
        acct is not None
        and user is not None
        and user.is_active
        and user.role in (Role.vendor, Role.admin)
        and pw_ok
        and not locked
        and step is not None
    )
    if not ok:
        if acct is not None and not locked:
            acct.failed_logins += 1
            if acct.failed_logins >= STAFF_MAX_FAILS:
                acct.locked_until, acct.failed_logins = now + STAFF_LOCK, 0
                audit.record(
                    db,
                    actor_id=acct.user_id,
                    actor_label=username,
                    action="auth.locked",
                    entity="staff",
                    ip=ip,
                )
            await db.commit()
        raise HTTPException(401, BAD_STAFF)

    if acct is None or user is None or step is None:  # pragma: no cover (narrowing)
        raise HTTPException(401, BAD_STAFF)
    await get_redis().set(f"totp:last:{acct.user_id}", step, ex=180)
    acct.failed_logins, acct.locked_until = 0, None
    audit.record(
        db,
        actor_id=user.id,
        actor_label=username,
        action="auth.login",
        entity=user.role.value,
        ip=ip,
    )
    return user, acct
