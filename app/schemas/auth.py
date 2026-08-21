from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# bcrypt silently ignores everything past the 72nd byte. Declaring the ceiling
# means a long password is refused out loud instead of quietly truncated -- and
# a user never ends up with a password that is not the one they typed.
BCRYPT_MAX_BYTES = 72


class LoginRequest(BaseModel):
    # Deliberately `str` and not `EmailStr`. On login the address is a lookup
    # key compared against a stored value, not a new record to validate: format
    # checking here only turns "no such user" (401) into "malformed" (422).
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1)


# Only the technical ceiling lives in the schema. The policy itself -- length
# and composition -- is `validate_password_strength`, so that every rule is
# stated once and answers with `WEAK_PASSWORD` instead of a generic 422.
Password = Annotated[str, Field(max_length=BCRYPT_MAX_BYTES)]


class RegisterRequest(BaseModel):
    """Creates a clinic and its first user, in one call."""

    # `EmailStr` here and not on login, because this is where a new record is
    # written: a typo accepted now is a login nobody can ever perform.
    tenant_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: Password


class CreateUserRequest(BaseModel):
    """Adds a colleague to the clinic of whoever is calling."""

    email: EmailStr
    password: Password


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    tenant_id: int
    created_at: datetime


class TenantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class RegisterResponse(BaseModel):
    """The token comes back with it, so registering and being logged in are one
    step -- there is no reason to make a new clinic call /auth/login next."""

    tenant: TenantResponse
    user: UserResponse
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Seconds until the token expires.")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Seconds until the token expires.")
