from fastapi import APIRouter

from app.api.deps import Db
from app.core.config import settings
from app.core.errors import documented_errors
from app.core.security import create_access_token
from app.schemas.auth import LoginRequest, TokenResponse
from app.services.auth import authenticate

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Step 1 — exchange seeded credentials for a token",
    description=(
        "There is no sign-up: users come from `uv run python -m scripts.seed`, which creates "
        "`vet@aurora.test` and `vet@boreal.test`, both with password `vetglobal`.\n\n"
        "Copy `access_token` into **Authorize** at the top of this page and every padlocked "
        "route below opens.\n\n"
        "The token carries `{sub, tenant_id}` and is signed. `tenant_id` is read from it on "
        "every request and from nowhere else — which is why no endpoint in this API has a "
        "tenant field you could set.\n\n"
        "A wrong password and an unknown email give the same answer on purpose: told apart, "
        "this route would become a way to ask which addresses are registered here."
    ),
    responses=documented_errors(
        **{
            "401": "INVALID_CREDENTIALS — wrong password, or no such user",
            "422": "VALIDATION_ERROR — malformed body, or the email is not an email",
        }
    ),
)
async def login(payload: LoginRequest, db: Db) -> TokenResponse:
    principal = await authenticate(db, payload.email, payload.password)

    return TokenResponse(
        access_token=create_access_token(principal),
        expires_in=settings.jwt_expire_minutes * 60,
    )
