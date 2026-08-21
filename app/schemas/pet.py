from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PetCreate(BaseModel):
    # No `tenant_id` here, and there never will be: it comes from the signed
    # token (invariant 4). A field for it would be a field an attacker can set.
    name: str = Field(min_length=1, max_length=120)
    owner_name: str = Field(min_length=1, max_length=120)


class PetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    owner_name: str
    created_at: datetime
