import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis
from app.db.session import get_session

router = APIRouter(tags=["health"])
log = logging.getLogger(__name__)


@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    checks = {"db": "ok", "redis": "ok"}
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        log.exception("health: db check failed")
        checks["db"] = "down"
    try:
        await get_redis().ping()
    except Exception:
        log.exception("health: redis check failed")
        checks["redis"] = "down"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if ok else "degraded", **checks}, status_code=200 if ok else 503
    )
