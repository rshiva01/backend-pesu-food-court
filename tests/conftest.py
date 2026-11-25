import asyncio
import os
from collections.abc import AsyncIterator, Callable

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from alembic import command

os.environ.setdefault("ENV", "test")

from app.core.redis import close_redis, get_redis
from app.db.models import Base
from app.db.session import dispose_engine, get_engine
from app.main import create_app
from tests.helpers import shop, sms  # noqa: F401  (shared fixtures)

# Tests WIPE the database and Redis db they point at. Use dedicated ones:
#   DATABASE_URL=…/pfc_test  REDIS_URL=…/1


@pytest.fixture(scope="session", autouse=True)
async def _schema() -> AsyncIterator[None]:
    url = os.environ.get("DATABASE_URL", "")
    if not os.environ.get("CI") and "test" not in url.rsplit("/", 1)[-1]:
        pytest.exit("refusing to run: DATABASE_URL must point at a *test* database", 2)
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    yield
    await dispose_engine()
    await close_redis()


@pytest.fixture(autouse=True)
async def _clean() -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    await get_redis().flushdb()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Requested-With": "fetch"},
    ) as c:
        yield c


@pytest.fixture
def make_client() -> Callable[[], AsyncClient]:
    """Extra independent browsers (own cookie jar), e.g. two vendors at once.
    Use as `async with make_client() as c:`."""

    def _new() -> AsyncClient:
        return AsyncClient(
            transport=ASGITransport(app=create_app()),
            base_url="http://test",
            headers={"X-Requested-With": "fetch"},
        )

    return _new
