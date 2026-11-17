import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

Phone = Annotated[str, StringConstraints(pattern=r"^\+91[6-9]\d{9}$")]
Code6 = Annotated[str, StringConstraints(pattern=r"^\d{6}$")]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


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


class VendorPublic(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    image_url: str | None
    is_open: bool
    tagline: str | None
    prep_minutes: int | None


class VendorMeOut(BaseModel):
    user: StaffUserOut
    vendor: VendorPublic
