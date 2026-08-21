from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Explicit naming convention so every index, unique key and foreign key has a
# predictable name. Without it Postgres invents names and Alembic autogenerate
# produces diffs that differ between machines.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model.

    Models are imported in `app/models/__init__.py` so that Alembic's
    autogenerate sees them through `Base.metadata`.

    No `relationship()` is declared anywhere: under asyncio a lazy load outside
    an await raises `MissingGreenlet`, and every read here is an explicit query
    in a service. Joins are written by hand where they are needed.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
