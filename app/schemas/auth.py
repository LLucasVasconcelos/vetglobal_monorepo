from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    # Deliberately `str` and not `EmailStr`. On login the address is a lookup
    # key compared against a stored value, not a new record to validate: format
    # checking here only turns "no such user" (401) into "malformed" (422), and
    # it rejects the RFC 2606 reserved domains the seed uses on purpose --
    # `vet@aurora.test` cannot collide with a domain someone really owns.
    # An address that must be *deliverable* would be validated at sign-up,
    # which this API does not have.
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Seconds until the token expires.")
