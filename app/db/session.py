from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    # Without this, SQLAlchemy appends the bound parameters to the message of
    # any database error -- and one of the parameters of the document INSERT is
    # the file itself. A single failed insert would put a clinical record, in
    # plain text, into the application log, where it outlives the request and is
    # read by anyone who can read logs.
    #
    # The trade-off is deliberate: debugging a query without seeing its values
    # is harder. Hard-coded rather than made configurable, because the moment
    # someone would reach for the switch is while investigating production --
    # exactly when the data is real.
    hide_parameters=True,
)

# expire_on_commit=False: after a commit the ORM would otherwise mark every
# attribute as stale and re-fetch it on access -- which under asyncio means a
# lazy load in the middle of building a response.
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
