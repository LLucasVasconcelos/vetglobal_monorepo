from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model.

    Models are imported in `app/models/__init__.py` so that Alembic's
    autogenerate sees them through `Base.metadata`.
    """
