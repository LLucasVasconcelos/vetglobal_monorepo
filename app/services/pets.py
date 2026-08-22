from sqlalchemy import func, select
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


async def list_pets(
    db: AsyncSession, principal: Principal, limit: int, offset: int
) -> tuple[list[Pet], int]:
    """One page of this tenant's pets, newest first, plus how many it has.

    The same `tenant_id` filter goes on both statements. Counting without it
    would be the subtler half of a leak: the list would be right and the number
    would quietly report how many pets the whole database holds.
    """
    mine = Pet.tenant_id == principal.tenant_id

    pets = (
        await db.scalars(
            # Newest first: the pet somebody just created is the one they are
            # looking for. `id` and not `created_at` because two rows written in
            # the same transaction share a timestamp and would order at random.
            select(Pet).where(mine).order_by(Pet.id.desc()).limit(limit).offset(offset)
        )
    ).all()

    total = await db.scalar(select(func.count()).select_from(Pet).where(mine)) or 0

    return list(pets), total


async def get_pet(db: AsyncSession, principal: Principal, pet_id: int) -> Pet:
    pet = await db.scalar(
        # The tenant filter is part of the lookup, not a check afterwards. A
        # check afterwards is a check someone can forget to write.
        select(Pet).where(Pet.id == pet_id, Pet.tenant_id == principal.tenant_id)
    )
    if pet is None:
        raise pet_not_found()
    return pet
