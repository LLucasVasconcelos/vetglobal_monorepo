from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.security import Principal, verify_password, waste_password_time
from app.models import User


def _rejected() -> DomainError:
    """Same code and same message for an unknown email as for a wrong password:
    told apart, the endpoint becomes a way to ask which addresses are registered
    here. A fresh instance per call -- a shared one would carry the traceback of
    whoever raised it last."""
    return DomainError(401, "INVALID_CREDENTIALS", "Invalid email or password.")


async def authenticate(db: AsyncSession, email: str, password: str) -> Principal:
    user = await db.scalar(select(User).where(User.email == email))

    if user is None:
        waste_password_time()
        raise _rejected()

    if not verify_password(password, user.password_hash):
        raise _rejected()

    return Principal(user_id=user.id, tenant_id=user.tenant_id)
