from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.security import (
    Principal,
    hash_password,
    validate_password_strength,
    verify_password,
    waste_password_time,
)
from app.models import Tenant, User


def _rejected() -> DomainError:
    """Same code and same message for an unknown email as for a wrong password:
    told apart, the endpoint becomes a way to ask which addresses are registered
    here. A fresh instance per call -- a shared one would carry the traceback of
    whoever raised it last."""
    return DomainError(401, "INVALID_CREDENTIALS", "Invalid email or password.")


def _email_taken() -> DomainError:
    return DomainError(409, "EMAIL_ALREADY_REGISTERED", "That email is already registered.")


async def authenticate(db: AsyncSession, email: str, password: str) -> Principal:
    # Folded the same way it was folded on the way in, or the address someone
    # registered would not be the address they can log in with.
    user = await db.scalar(select(User).where(User.email == email.strip().lower()))

    if user is None:
        waste_password_time()
        raise _rejected()

    if not verify_password(password, user.password_hash):
        raise _rejected()

    return Principal(user_id=user.id, tenant_id=user.tenant_id)


async def register_tenant(
    db: AsyncSession, tenant_name: str, email: str, password: str
) -> tuple[Tenant, User]:
    """A new clinic and its first user.

    Open to anyone, and safe to be: what it creates is an *empty* tenant. The
    caller gets a token for the clinic they just made and for no other, because
    every query filters on the `tenant_id` inside that token (invariant 4).
    Registering cannot reach an existing tenant -- there is no parameter that
    names one. Adding a user to a clinic that already exists is the other
    endpoint, and that one requires being in it already.
    """
    # Everything the caller can fix is checked before anything is written. The
    # other order answers "that name is taken" to someone whose real problem is
    # a weak password -- a true statement that sends them to fix the wrong thing.
    validate_password_strength(password)

    tenant = Tenant(name=tenant_name)
    db.add(tenant)
    await db.flush()

    user = await _add_user(db, tenant.id, email, password)
    await db.commit()
    await db.refresh(tenant)
    await db.refresh(user)
    return tenant, user


async def create_user(db: AsyncSession, principal: Principal, email: str, password: str) -> User:
    """A colleague, inside the caller's own clinic.

    `tenant_id` comes from the token and is not a parameter of this function by
    accident: there is no way to spell "create a user in someone else's clinic".
    """
    validate_password_strength(password)
    user = await _add_user(db, principal.tenant_id, email, password)
    await db.commit()
    await db.refresh(user)
    return user


async def _add_user(db: AsyncSession, tenant_id: int, email: str, password: str) -> User:
    # Emails are compared case-insensitively by storing them folded: otherwise
    # Vet@clinic.com and vet@clinic.com are two accounts, and the second one is
    # a plausible way to impersonate the first.
    normalized = email.strip().lower()

    if await db.scalar(select(func.count()).select_from(User).where(User.email == normalized)):
        raise _email_taken()

    user = User(tenant_id=tenant_id, email=normalized, password_hash=hash_password(password))
    db.add(user)

    try:
        await db.flush()
    except IntegrityError:
        # Two registrations of the same address raced past the check above. The
        # unique index on `email` is what actually decides.
        await db.rollback()
        raise _email_taken() from None

    return user
