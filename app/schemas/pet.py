from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.text import SafeText


class PetCreate(BaseModel):
    # No `tenant_id` here, and there never will be: it comes from the signed
    # token (invariant 4). A field for it would be a field an attacker can set.
    name: SafeText = Field(min_length=1, max_length=120)
    owner_name: SafeText = Field(min_length=1, max_length=120)


class PetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    owner_name: str
    created_at: datetime


class PetListResponse(BaseModel):
    """A page of the caller's own pets.

    `total` is deliberately part of the answer and not an afterthought: it is
    the isolation of D26 stated as a number. Two clinics with pets in the same
    table read two different totals from the same request, which is a thing a
    person can check in one call -- unlike the 404 of a single document, which
    only proves the negative half.
    """

    items: list[PetResponse]
    total: int = Field(
        description="How many pets YOUR clinic has. Another clinic's pets are not counted."
    )
    limit: int
    offset: int
