import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPk


class Vendor(UUIDPk, Timestamps, Base):
    __tablename__ = "vendors"
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    slug: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(4), unique=True, nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(255))
    tagline: Mapped[str | None] = mapped_column(String(60))
    prep_minutes: Mapped[int | None] = mapped_column(Integer)
    is_open: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), unique=True
    )

    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug_format"),
        CheckConstraint("token_prefix ~ '^[A-Z]{2,4}$'", name="token_prefix_format"),
        CheckConstraint("prep_minutes IS NULL OR prep_minutes BETWEEN 1 AND 90", name="prep_range"),
    )


class MenuItem(UUIDPk, Timestamps, Base):
    __tablename__ = "menu_items"
    vendor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str | None] = mapped_column(String(140))
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    is_veg: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        # same bounds as the mock's validMenuInput: ₹1 to ₹5000
        CheckConstraint("price_paise BETWEEN 100 AND 500000", name="price_range"),
    )
