import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints

from app.schemas.catalog import VendorPublic
from app.schemas.common import Strict

Phone = Annotated[str, StringConstraints(pattern=r"^\+91[6-9]\d{9}$")]
Code6 = Annotated[str, StringConstraints(pattern=r"^\d{6}$")]


class OtpRequest(Strict):
    phone: Phone


class OtpRequestOut(BaseModel):
    ok: Literal[True] = True
    resend_after: int


class OtpVerify(Strict):
    phone: Phone
    code: Code6
    remember: bool = True


class UserOut(BaseModel):
    id: uuid.UUID
    phone: str | None
    name: str | None
    role: Literal["student", "vendor", "admin"]


class StaffLogin(Strict):
    username: Annotated[str, StringConstraints(min_length=2, max_length=32, to_lower=True)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    totp: Code6


class StaffUserOut(BaseModel):
    id: uuid.UUID
    username: str
    name: str
    role: Literal["vendor", "admin"]


class VendorMeOut(BaseModel):
    user: StaffUserOut
    vendor: VendorPublic
