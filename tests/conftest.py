"""Shared test fixtures.

The tests run against the real Postgres from `docker compose`, not a stub: the
things that carry the weight here -- `SKIP LOCKED`, a unique index, `NOTIFY` --
have no meaningful behaviour outside a real database.

Which makes leftovers a real risk. The three tables the API writes to are
emptied before every test, and everything is emptied at the end of the run, so
the database is left as it was found.

The two clinics the suite needs are created through `POST /auth/register`, like
any other client would. That is deliberate: there is no fixture data that only
exists in tests, so a path that works here works for a person with curl.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.db.session import SessionLocal, engine
from app.main import app

PASSWORD = "Vetglobal#2026"

# Truncated in one statement: `jobs` references `documents` references `pets`,
# so any other order needs CASCADE to mean something. RESTART IDENTITY keeps ids
# small and predictable between runs.
WRITTEN_TABLES = "jobs, documents, pets"
ALL_TABLES = "jobs, documents, pets, users, tenants"


async def truncate(tables: str) -> None:
    async with SessionLocal() as session:
        await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await session.commit()


@pytest.fixture(scope="session", autouse=True)
async def database():
    # Clinics and users too: the suite registers its own, so anything already
    # here is a leftover, and a leftover email would collide on the unique index.
    await truncate(ALL_TABLES)
    yield
    await truncate(ALL_TABLES)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_slate():
    """Every test starts with `pets`, `documents` and `jobs` empty.

    Not merely tidiness: ids restart at 1, so a test can assert on a concrete
    id, and a pet left behind by an earlier test cannot be what makes a dedupe
    or isolation assertion pass.
    """
    await truncate(WRITTEN_TABLES)
    yield


@pytest.fixture
async def db():
    """A session for a test to look at the database directly -- to check what
    the API stored, or to force a job into a state the worker would produce."""
    async with SessionLocal() as session:
        yield session


@pytest.fixture(scope="session")
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def register(client: AsyncClient, clinic: str, email: str) -> dict:
    response = await client.post(
        "/auth/register",
        json={"tenant_name": clinic, "email": email, "password": PASSWORD},
    )
    response.raise_for_status()
    return response.json()


@pytest.fixture(scope="session")
async def aurora(client: AsyncClient) -> dict:
    return await register(client, "Clinica Aurora", "vet@aurora.example.com")


@pytest.fixture(scope="session")
async def boreal(client: AsyncClient) -> dict:
    """The second clinic is what makes cross-tenant isolation testable at all."""
    return await register(client, "Clinica Boreal", "vet@boreal.example.com")


@pytest.fixture(scope="session")
async def aurora_token(aurora: dict) -> str:
    return aurora["access_token"]


@pytest.fixture(scope="session")
async def aurora_email(aurora: dict) -> str:
    return aurora["user"]["email"]


@pytest.fixture(scope="session")
async def boreal_token(boreal: dict) -> str:
    return boreal["access_token"]


@pytest.fixture(scope="session")
async def boreal_email(boreal: dict) -> str:
    return boreal["user"]["email"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
