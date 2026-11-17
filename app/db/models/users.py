import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPk
from app.db.models.enums import Role


class User(UUIDPk, Timestamps, Base):
    __tablename__ = "users"
    # Students log in by phone (E.164). Staff (vendor/admin) may have no phone.
    phone: Mapped[str | None] = mapped_column(String(16), unique=True)
    name: Mapped[str | None] = mapped_column(String(80))
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        CheckConstraint("phone IS NULL OR phone ~ '^\\+91[6-9][0-9]{9}$'", name="phone_e164_in"),
        CheckConstraint("role <> 'student' OR phone IS NOT NULL", name="student_has_phone"),
    )


class StaffAccount(Timestamps, Base):
    """Login for vendors and admins: username + argon2id password + mandatory TOTP."""

    __tablename__ = "staff_accounts"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    totp_secret_enc: Mapped[str] = mapped_column(String(255), nullable=False)  # encrypted at rest
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Session(UUIDPk, Timestamps, Base):
    """Refresh-token session. Token stored only as a hash; rotation keeps the
    family_id so reuse of an old token revokes the whole family."""

    __tablename__ = "sessions"
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, nullable=False)
    refresh_token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip: Mapped[str | None] = mapped_column(String(45))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # set when rotated: lets a second tab that raced the same token get a plain
    # 401 for a few seconds instead of tripping reuse detection
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # "Remember me": refresh cookie survives browser restarts
    persistent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
