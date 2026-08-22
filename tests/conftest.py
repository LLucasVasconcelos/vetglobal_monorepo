"""Shared test fixtures.

The tests run against the real Postgres from `docker compose`, not a stub: the
things that carry the weight here -- `SKIP LOCKED`, a unique index, `NOTIFY` --
have no meaningful behaviour outside a real database.

But not against the database you develop against. The suite needs to truncate
freely between tests, and truncating a database somebody is also using by hand
destroys their work -- which it did, more than once, before this file created
its own. `DB_NAME` is redirected below to a **separate database**, created and
migrated on first run, so "empty everything" is a statement about a database
that holds nothing but test data. Anything you register or upload by hand lives
in the other one and the suite cannot reach it.

The two clinics the suite needs are created through `POST /auth/register`, like
any other client would. That is deliberate: there is no fixture data that only
exists in tests, so a path that works here works for a person with curl.
"""

import os
import subprocess
import sys
from pathlib import Path

# Set before importing anything from `app`: `app.core.config` builds `Settings`
# at import time, and pydantic-settings reads the environment ahead of `.env`.
# Imported first, this line is what redirects the whole suite; imported after
# the application, it would do nothing at all and the tests would quietly run
# against the development database again.
TEST_DB_NAME = os.environ.get("TEST_DB_NAME", "vetglobal_test")
os.environ["DB_NAME"] = TEST_DB_NAME

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent

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


async def _create_test_database_if_missing() -> None:
    """`CREATE DATABASE` cannot run against the database being created, so this
    connects to the maintenance database `postgres` to ask for it."""
    admin = await asyncpg.connect(
        user=settings.db_user,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        database="postgres",
    )
    try:
        exists = await admin.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB_NAME
        )
        if not exists:
            # The name comes from this file or from TEST_DB_NAME, never from a
            # request, and is quoted because it goes into DDL, which takes no
            # bound parameters.
            await admin.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await admin.close()


def _migrate_test_database() -> None:
    """`alembic upgrade head` on the test database, as a subprocess.

    Not imported and called: `migrations/env.py` ends in `asyncio.run()`, which
    refuses to start inside the event loop pytest-asyncio is already running.

    Running the real migrations rather than `metadata.create_all` is the point.
    It costs a second per session and buys a suite that fails when the migrations
    stop matching the models -- a green suite over a schema `alembic upgrade`
    could no longer produce is a green suite that is lying about the deliverable.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "DB_NAME": TEST_DB_NAME},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed on {TEST_DB_NAME}:\n{result.stderr}")


@pytest.fixture(scope="session", autouse=True)
async def database():
    await _create_test_database_if_missing()
    _migrate_test_database()

    # Safe to empty everything, including clinics and users: this database
    # exists only for the suite. A leftover email from an interrupted run would
    # otherwise collide on the unique index.
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
