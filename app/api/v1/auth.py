from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import current_student
from app.core.security import Audience
from app.db.models import StaffAccount, User
from app.db.session import get_session
from app.schemas.auth import (
    OtpRequest,
    OtpRequestOut,
    OtpVerify,
    StaffLogin,
    StaffUserOut,
    UserOut,
)
from app.services import auth as svc

router = APIRouter(prefix="/auth", tags=["auth"])


def user_out(u: User) -> UserOut:
    return UserOut(id=u.id, phone=u.phone, name=u.name, role=u.role.value)


def staff_out(u: User, a: StaffAccount) -> StaffUserOut:
    return StaffUserOut(id=u.id, username=a.username, name=u.name or a.username, role=u.role.value)


@router.post("/otp/request", status_code=202)
async def otp_request(body: OtpRequest, request: Request) -> OtpRequestOut:
    wait = await svc.request_otp(body.phone, svc.client_ip(request))
    return OtpRequestOut(resend_after=wait)


@router.post("/otp/verify")
async def otp_verify(
    body: OtpVerify,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> UserOut:
    user = await svc.verify_otp(db, body.phone, body.code, svc.client_ip(request))
    await svc.start_session(db, user, "student", body.remember, request, response)
    await db.commit()
    return user_out(user)


@router.get("/me")
async def me(user: User = Depends(current_student)) -> UserOut:
    return user_out(user)


@router.post("/refresh", status_code=204)
async def refresh(
    request: Request, response: Response, db: AsyncSession = Depends(get_session)
) -> Response:
    # Refreshes whichever sessions this browser has (student and/or staff).
    any_ok = False
    auds: tuple[Audience, ...] = ("student", "staff")
    for aud in auds:
        if await svc.rotate(db, aud, request, response):
            any_ok = True
    await db.commit()
    if not any_ok:
        raise HTTPException(401, "No session")
    response.status_code = 204
    return response


@router.post("/logout", status_code=204)
async def logout(
    request: Request, response: Response, db: AsyncSession = Depends(get_session)
) -> Response:
    await svc.end_session(db, "student", request, response)
    await db.commit()
    response.status_code = 204
    return response


@router.post("/staff/login")
async def staff_login(
    body: StaffLogin,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> StaffUserOut:
    user, acct = await svc.staff_login(
        db, body.username, body.password, body.totp, svc.client_ip(request)
    )
    await svc.start_session(db, user, "staff", False, request, response)
    await db.commit()
    return staff_out(user, acct)


staff_router = APIRouter(tags=["staff"])


@staff_router.post("/vendor/logout", status_code=204)
@staff_router.post("/admin/logout", status_code=204)
async def staff_logout(
    request: Request, response: Response, db: AsyncSession = Depends(get_session)
) -> Response:
    await svc.end_session(db, "staff", request, response)
    await db.commit()
    response.status_code = 204
    return response
