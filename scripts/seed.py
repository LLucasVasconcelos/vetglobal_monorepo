"""Seed the two demo tenants and their users.

    uv run python -m scripts.seed

There is no sign-up endpoint (out of scope, see README), so every login comes
from here. Two tenants is not decoration: it is what makes the cross-tenant
isolation test possible -- tenant A asking for tenant B's document has to get a
404 (D26).

Idempotent: re-running it does not duplicate anything and does not rewrite the
password hashes of existing users.
"""

import asyncio

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SessionLocal, engine
from app.models import Tenant, User

# Demo credentials, documented in the README on purpose.
SEED_PASSWORD = "vetglobal"

SEED = [
    ("Clinica Aurora", "vet@aurora.test"),
    ("Clinica Boreal", "vet@boreal.test"),
]


async def seed(quiet: bool = False) -> None:
    """Idempotent. `tests/conftest.py` calls this too, which is why it does not
    dispose the engine -- the caller owns that."""

    def say(line: str) -> None:
        if not quiet:
            print(line)

    async with SessionLocal() as session:
        for tenant_name, email in SEED:
            tenant = await session.scalar(select(Tenant).where(Tenant.name == tenant_name))
            if tenant is None:
                tenant = Tenant(name=tenant_name)
                session.add(tenant)
                await session.flush()

            user = await session.scalar(select(User).where(User.email == email))
            if user is None:
                session.add(
                    User(
                        tenant_id=tenant.id,
                        email=email,
                        password_hash=hash_password(SEED_PASSWORD),
                    )
                )
                say(f"created  {email:<20} tenant={tenant_name} (id={tenant.id})")
            else:
                say(f"exists   {email:<20} tenant={tenant_name} (id={tenant.id})")

        await session.commit()

    say(f"\npassword for every seeded user: {SEED_PASSWORD}")


async def main() -> None:
    try:
        await seed()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
