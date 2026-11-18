"""Append-only audit trail for staff actions. Callers commit."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


def record(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    actor_label: str,
    action: str,
    entity: str,
    detail: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_user_id=actor_id,
            actor_label=actor_label[:40],
            action=action[:40],
            entity=entity[:80],
            detail=detail[:255] if detail else None,
            before=before,
            after=after,
            ip=ip[:45] if ip else None,
        )
    )
