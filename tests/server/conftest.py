import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.config import LookAheadSettings
from server.lookahead.store import SnapshotStore
from server.routers.lookahead import create_router


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_router(
            settings=LookAheadSettings(enabled=True),
            store=SnapshotStore(),
            worker_ready=lambda: True,
        )
    )
    return app


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest_asyncio.fixture
async def limited_client() -> AsyncClient:
    app = FastAPI()
    app.include_router(
        create_router(
            settings=LookAheadSettings(enabled=True, max_active_streams=1),
            store=SnapshotStore(),
            worker_ready=lambda: True,
        )
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
