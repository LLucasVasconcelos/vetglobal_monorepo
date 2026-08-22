"""Shared route dependencies: who is calling, and may they call this at all."""

from typing import Annotated

from fastapi import Depends, Header, Path, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.security import Principal, decode_access_token, internal_token_is_valid
from app.db.base import PG_INT_MAX
from app.db.session import get_db
from app.services.auth import resolve_principal

# auto_error=False so the failure comes out of our handler in the D22 envelope.
# Left to itself, HTTPBearer answers 403 with `{"detail": "Not authenticated"}`
# -- the wrong status, in a second error format.
_bearer = HTTPBearer(auto_error=False, description="JWT issued by POST /auth/login")


async def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Principal:
    """The `tenant_id` of every query comes from here, and from nowhere else --
    never from the body, never from a query parameter (invariant 4).

    Two steps, and neither of them touches the database from this layer: decode
    the token, then hand the claim to `resolve_principal`, which is where the
    row that confirms or refuses it is read (D40). Keeping the query in the
    service is not ceremony -- "rota -> service -> model" is a rule the whole
    project follows, and the one file allowed to break it is the file that
    teaches the next person the rule is optional.
    """
    if credentials is None:
        raise DomainError(401, "NOT_AUTHENTICATED", "Missing bearer token.")

    return await resolve_principal(db, decode_access_token(credentials.credentials))


async def require_internal_token(
    x_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    """Guards `/internal/*`, which can mark any job as done with any text (D27)."""
    if x_internal_token is None or not internal_token_is_valid(x_internal_token):
        raise DomainError(401, "INVALID_INTERNAL_TOKEN", "Invalid or missing X-Internal-Token.")


# The four ways a protected route can answer 401, in one place. `error_code` is
# a machine contract (D22), so a code the API emits and the docs never name is a
# branch a client cannot write until it meets one in production.
AUTH_401 = (
    "NOT_AUTHENTICATED, INVALID_TOKEN, TOKEN_EXPIRED or TOKEN_REVOKED — no token; "
    "a malformed or wrongly-signed one; an expired one; or one whose account no longer "
    "exists or moved clinic"
)

CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
Db = Annotated[AsyncSession, Depends(get_db)]

# Every id in a path, bounded to what the column can actually hold. Same shape of
# bug as the over-long filename of D36 -- predictable client input arriving as a
# 500 -- and the same answer: refuse it at the edge, where it is a 422 and not a
# bug on our side.
#
# Declared once and shared, so a route added later inherits the bound instead of
# having to remember it. `ge=1` comes along for free: ids start at 1, so 0 and
# negatives are malformed rather than missing, and refusing them leaks nothing --
# no tenant can own an id that cannot exist (D26).
ResourceId = Annotated[int, Path(ge=1, le=PG_INT_MAX)]

# The poll's cursor. Same bound as `ResourceId` and for the same reason, but
# `ge=0`: zero is not an id here, it is the D23 shorthand for "whatever the
# latest job of this document is".
AfterJobId = Annotated[
    int,
    Query(
        ge=0,
        le=PG_INT_MAX,
        description="The job to wait for. `0` means the latest job of this document (D23).",
    ),
]
