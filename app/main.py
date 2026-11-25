import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.v1 import router as v1
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.middleware import CsrfMiddleware, SecurityHeadersMiddleware
from app.core.redis import close_redis
from app.db.session import dispose_engine
from app.services.orders import OrderConflict

log = logging.getLogger("app")

# Friendly text for fields the UI shows errors for (matches the mock's wording).
FIELD_HINTS = {
    "price_paise": "Price must be between ₹1 and ₹5000",
    "prep_minutes": "Prep time must be 1–90 minutes",  # noqa: RUF001 (UI text)
    "tagline": "Tagline is too long",
    "groups": "You can order from up to 4 counters at once",
}
MENU_HINT = "Check the item details"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()
    await close_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)
    docs = not settings.is_prod  # no public API map in production
    app = FastAPI(
        title="PESU Food Court API",
        lifespan=lifespan,
        docs_url="/api/v1/docs" if docs else None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json" if docs else None,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CsrfMiddleware)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations only — never echo submitted values back.
        fields = [".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()]
        detail = next((FIELD_HINTS[f] for f in fields if f in FIELD_HINTS), None)
        if detail is None and request.url.path.startswith("/api/v1/vendor/menu"):
            detail = MENU_HINT
        return JSONResponse(
            {"detail": detail or "Invalid request", "fields": fields}, status_code=422
        )

    @app.exception_handler(OrderConflict)
    async def _order_conflict(_: Request, exc: OrderConflict) -> JSONResponse:
        # Tells the cart which vendor / item to fix.
        body: dict[str, str] = {"detail": exc.detail}
        if exc.vendor_id:
            body["vendor_id"] = str(exc.vendor_id)
        if exc.item_id:
            body["item_id"] = str(exc.item_id)
        return JSONResponse(body, status_code=409)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"ctx": {"path": request.url.path}})
        return JSONResponse({"detail": "Something went wrong"}, status_code=500)

    app.include_router(v1)
    return app


app = create_app()
