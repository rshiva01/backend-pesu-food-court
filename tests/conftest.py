import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "test")

from app.core.redis import close_redis
from app.db.session import dispose_engine
from app.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True, scope="session")
async def _close_pools() -> AsyncIterator[None]:
    # ASGITransport skips lifespan, so close the shared pools ourselves.
    yield
    await dispose_engine()
    await close_redis()
