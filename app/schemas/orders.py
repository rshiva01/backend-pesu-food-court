import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StrictInt

from app.db.models.enums import OrderStatus, PaymentMode, PaymentStatus
from app.schemas.common import Strict

# Mirrors MAX_CART_VENDORS in frontend/lib/types.ts.
MAX_CART_VENDORS = 4
MAX_LINES_PER_VENDOR = 30
Qty = Annotated[StrictInt, Field(ge=1, le=20)]


class OrderLineIn(Strict):
    menu_item_id: uuid.UUID
    qty: Qty


class OrderGroupIn(Strict):
    vendor_id: uuid.UUID
    items: Annotated[list[OrderLineIn], Field(min_length=1, max_length=MAX_LINES_PER_VENDOR)]


class CreateOrderIn(Strict):
    """No prices anywhere: the server reads them from the menu."""

    groups: Annotated[list[OrderGroupIn], Field(min_length=1, max_length=MAX_CART_VENDORS)]
    payment_mode: PaymentMode
    idempotency_key: uuid.UUID


class OrderVendor(BaseModel):
    id: uuid.UUID
    slug: str
    name: str


class OrderLineOut(BaseModel):
    menu_item_id: uuid.UUID | None  # None once the vendor deletes the item
    name: str
    unit_price_paise: int
    qty: int


class OrderOut(BaseModel):
    id: uuid.UUID
    vendor: OrderVendor
    token_no: str | None
    group_id: uuid.UUID
    status: OrderStatus
    payment_mode: PaymentMode
    payment_status: PaymentStatus
    total_paise: int
    items: list[OrderLineOut]
    created_at: datetime
    ready_at: datetime | None


class VendorOrderOut(OrderOut):
    student_label: str  # "Student ··210" — never the full phone


class CreateOrderOut(BaseModel):
    group_id: uuid.UUID
    orders: list[OrderOut]


class StatusIn(Strict):
    status: OrderStatus  # allowed moves checked against VENDOR_TRANSITIONS → 409


class VendorStatsOut(BaseModel):
    orders_today: int
    revenue_today_paise: int
    open_orders: int
