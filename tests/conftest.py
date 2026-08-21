"""Shared test fixtures.

The tests run against the real Postgres from `docker compose`, not a stub: the
things that carry the weight here -- `SKIP LOCKED`, a unique index, `NOTIFY` --
have no meaningful behaviour outside a real database.

Which makes leftovers a real risk. `clean_tenant_data` empties the three tables
the API writes to, both before and after the run: before, so a crashed earlier
run cannot make today's assertions pass or fail for the wrong reason; after, so
the database is left as it was found. `tenants` and `users` survive -- they are
the seed, not test output.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.db.session import SessionLocal, engine
from app.main import app
from scripts.seed import SEED_PASSWORD, seed

AURORA_EMAIL = "vet@aurora.test"
BOREAL_EMAIL = "vet@boreal.test"

# Truncated in one statement: `jobs` references `documents` references `pets`,
# so any other order needs CASCADE to mean something. RESTART IDENTITY keeps ids
# small and predictable between runs.
WRITTEN_TABLES = "jobs, documents, pets"


async def clean_tenant_data() -> None:
    async with SessionLocal() as session:
        await session.execute(text(f"TRUNCATE {WRITTEN_TABLES} RESTART IDENTITY CASCADE"))
        await session.commit()


@pytest.fixture(scope="session", autouse=True)
async def database():
    """The suite seeds itself, so a fresh clone needs no manual step before
    `pytest`. Seeding is idempotent."""
    await seed(quiet=True)
    yield
    await clean_tenant_data()
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_slate():
    """Every test starts with `pets`, `documents` and `jobs` empty.

    Not merely tidiness: ids restart at 1, so a test can assert on a concrete
    id, and a pet left behind by an earlier test cannot be what makes a dedupe
    or isolation assertion pass.
    """
    await clean_tenant_data()
    yield


@pytest.fixture(scope="session")
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def login(client: AsyncClient, email: str) -> str:
    response = await client.post("/auth/login", json={"email": email, "password": SEED_PASSWORD})
    response.raise_for_status()
    return response.json()["access_token"]


@pytest.fixture(scope="session")
async def aurora_token(client: AsyncClient) -> str:
    return await login(client, AURORA_EMAIL)


@pytest.fixture(scope="session")
async def boreal_token(client: AsyncClient) -> str:
    """The second tenant is what makes cross-tenant isolation testable at all."""
    return await login(client, BOREAL_EMAIL)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
