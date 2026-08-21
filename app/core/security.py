"""Password hashing, JWT and the internal shared secret (D26, D27)."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import settings
from app.core.errors import DomainError

# Hash of no password anyone holds. Used to spend the same time on a login with
# an unknown email as on one with a real user, so response time stops answering
# "does this email exist here?".
_DUMMY_HASH = bcrypt.hashpw(b"", bcrypt.gensalt()).decode()


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling, as carried by the token -- and the ONLY source of
    `tenant_id` in the whole application (invariant 4)."""

    user_id: int
    tenant_id: int


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def waste_password_time() -> None:
    bcrypt.checkpw(b"", _DUMMY_HASH.encode())


def create_access_token(principal: Principal) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(principal.user_id),
        "tenant_id": principal.tenant_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Principal:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            # The accepted algorithm is pinned, never read from the token's own
            # header: a decoder that trusts `alg` accepts `none`, or lets an
            # attacker sign with the public key of an asymmetric pair.
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError:
        raise DomainError(401, "TOKEN_EXPIRED", "Token expired. Log in again.") from None
    except jwt.InvalidTokenError:
        raise DomainError(401, "INVALID_TOKEN", "Invalid token.") from None

    try:
        return Principal(user_id=int(payload["sub"]), tenant_id=int(payload["tenant_id"]))
    except (KeyError, TypeError, ValueError):
        # Signature valid but the payload is not one of ours.
        raise DomainError(401, "INVALID_TOKEN", "Invalid token.") from None


def internal_token_is_valid(candidate: str) -> bool:
    """`compare_digest` and not `==`: constant time, so the comparison does not
    leak how many leading characters were right (D27)."""
    return secrets.compare_digest(candidate, settings.internal_token)
