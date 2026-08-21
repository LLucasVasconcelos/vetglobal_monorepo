from fastapi import APIRouter, status

from app.api.deps import CurrentPrincipal, Db
from app.core.config import settings
from app.core.errors import documented_errors
from app.core.security import Principal, create_access_token
from app.schemas.auth import (
    CreateUserRequest,
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    TenantResponse,
    TokenResponse,
    UserResponse,
)
from app.services.auth import authenticate, create_user, register_tenant

router = APIRouter(prefix="/auth", tags=["auth"])

PASSWORD_RULES = (
    "At least 8 characters, with a lowercase letter, an uppercase letter and a symbol. "
    "A password that misses any of these answers `422 WEAK_PASSWORD`, and the message names "
    "every rule that is missing at once."
)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Step 1 — exchange credentials for a token",
    description=(
        "For an account you already have. If you do not have one yet, `POST /auth/register` "
        "creates a clinic and an account in it and returns a token directly.\n\n"
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


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Step 0 — create a clinic and its first user",
    description=(
        "Start here if you are trying this API for the first time. It creates a clinic and "
        "your account in it, and hands back a token, so registering and being logged in are "
        "one step.\n\n"
        f"**Password rules.** {PASSWORD_RULES}\n\n"
        "**Why this is open to anyone.** What it creates is an *empty* clinic. The token you "
        "get back names that clinic and no other, and every query in this API filters on the "
        "`tenant_id` inside the token — so there is no parameter here, and no parameter "
        "anywhere, that names someone else's clinic. To add a user to a clinic that already "
        "exists, use `POST /auth/users`, which requires already being in it.\n\n"
        "Register twice to get two clinics, and you can see for yourself that neither can "
        "read the other's documents."
    ),
    responses=documented_errors(
        **{
            "409": "EMAIL_ALREADY_REGISTERED — addresses are unique across the whole API",
            "422": "WEAK_PASSWORD, or VALIDATION_ERROR for a malformed email",
        }
    ),
)
async def register(payload: RegisterRequest, db: Db) -> RegisterResponse:
    tenant, user = await register_tenant(db, payload.tenant_name, payload.email, payload.password)
    principal = Principal(user_id=user.id, tenant_id=tenant.id)

    return RegisterResponse(
        tenant=TenantResponse.model_validate(tenant),
        user=UserResponse.model_validate(user),
        access_token=create_access_token(principal),
        expires_in=settings.jwt_expire_minutes * 60,
    )


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a colleague to your clinic",
    description=(
        "Creates another user inside **your** clinic — the one in your token.\n\n"
        f"**Password rules.** {PASSWORD_RULES}\n\n"
        "There is no `tenant_id` field here, which is the point: no request can spell "
        "\"create a user in someone else's clinic\". Growing a clinic requires already "
        "belonging to it."
    ),
    responses=documented_errors(
        **{
            "401": "NOT_AUTHENTICATED — no token, or an invalid one",
            "409": "EMAIL_ALREADY_REGISTERED — addresses are unique across the whole API",
            "422": "WEAK_PASSWORD, or VALIDATION_ERROR for a malformed email",
        }
    ),
)
async def post_user(
    payload: CreateUserRequest, principal: CurrentPrincipal, db: Db
) -> UserResponse:
    user = await create_user(db, principal, payload.email, payload.password)
    return UserResponse.model_validate(user)
