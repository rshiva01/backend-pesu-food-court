import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPk
from app.db.models.enums import GatewayPaymentStatus, OrderStatus, PaymentMode, PaymentStatus


class Order(UUIDPk, Timestamps, Base):
    """One order per vendor. A multi-vendor checkout creates several orders that
    share group_id (= the client's idempotency key); a retry with the same key
    returns the existing orders instead of creating new ones."""

    __tablename__ = "orders"
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False
    )
    business_date: Mapped[date] = mapped_column(
        Date, nullable=False
    )  # IST day the token belongs to
    token_no: Mapped[str | None] = mapped_column(String(12))  # e.g. DOS-042; null until confirmed
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"), nullable=False
    )
    payment_mode: Mapped[PaymentMode] = mapped_column(
        Enum(PaymentMode, name="payment_mode"), nullable=False
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"), nullable=False
    )
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        UniqueConstraint("student_id", "group_id", "vendor_id", name="uq_orders_group_vendor"),
        UniqueConstraint("vendor_id", "business_date", "token_no", name="uq_orders_daily_token"),
        CheckConstraint("total_paise >= 0", name="total_nonneg"),
        # vendor queue: today's orders for one vendor by status
        Index("ix_orders_vendor_day_status", "vendor_id", "business_date", "status"),
    )


class OrderItem(UUIDPk, Base):
    __tablename__ = "order_items"
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    menu_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("menu_items.id", ondelete="SET NULL")
    )
    # snapshots: the receipt must not change if the menu does
    name_snapshot: Mapped[str] = mapped_column(String(60), nullable=False)
    unit_price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("qty BETWEEN 1 AND 20", name="qty_range"),
        CheckConstraint("unit_price_paise > 0", name="price_pos"),
    )


class VendorDayCounter(Base):
    """Last token number handed out per vendor per IST day. Bumped with an
    upsert inside the order transaction, so a rolled-back order never burns a
    number and two checkouts can't get the same token."""

    __tablename__ = "vendor_day_counters"
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"), primary_key=True
    )
    business_date: Mapped[date] = mapped_column(Date, primary_key=True)
    last_no: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (CheckConstraint("last_no >= 1", name="last_no_pos"),)


class Payment(UUIDPk, Timestamps, Base):
    __tablename__ = "payments"
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # unique → a replayed webhook can't mark a second payment
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[GatewayPaymentStatus] = mapped_column(
        Enum(GatewayPaymentStatus, name="gateway_payment_status"), nullable=False
    )
    raw_webhook: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (CheckConstraint("amount_paise > 0", name="amount_pos"),)
