from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.security import Principal
from app.models import Pet
from app.schemas.pet import PetCreate


def pet_not_found() -> DomainError:
    """404 and not 403, even when the pet exists under another tenant: 403
    would confirm the record is real, and that confirmation is the leak (D26)."""
    return DomainError(404, "PET_NOT_FOUND", "Pet not found.")


async def create_pet(db: AsyncSession, principal: Principal, data: PetCreate) -> Pet:
    pet = Pet(tenant_id=principal.tenant_id, name=data.name, owner_name=data.owner_name)
    db.add(pet)
    await db.commit()
    await db.refresh(pet)
    return pet


async def get_pet(db: AsyncSession, principal: Principal, pet_id: int) -> Pet:
    pet = await db.scalar(
        # The tenant filter is part of the lookup, not a check afterwards. A
        # check afterwards is a check someone can forget to write.
        select(Pet).where(Pet.id == pet_id, Pet.tenant_id == principal.tenant_id)
    )
    if pet is None:
        raise pet_not_found()
    return pet
