from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# The width of every primary key here: SQLAlchemy maps `Mapped[int]` to Postgres
# `integer`, so this is the largest id any of these tables can hold. Anything
# larger arriving from a client is not a record we failed to find, it is a value
# the column cannot store -- Postgres raises, the error is not an IntegrityError
# so no handler catches it, and `GET /documents/2147483648` (a URL anyone can
# type) comes back 500, against invariant 5. Lives here, next to the fact it
# describes, and is enforced at the edge by `ResourceId` in `app/api/deps.py`.
PG_INT_MAX = 2_147_483_647

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
