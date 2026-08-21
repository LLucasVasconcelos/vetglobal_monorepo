from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Tenant(Base):
    """A clinic. Every row of tenant data carries this id (D26)."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Not unique: a clinic name is a label, not an identity -- two real
    # clinics can share one. Forcing uniqueness would refuse a legitimate
    # registration, and the 409 would say which names are already taken.
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class User(Base):
    """A seeded login. There is no sign-up endpoint -- out of scope, see README.

    `email` is unique across the whole table, not per tenant: login takes only
    an email and a password, so the tenant has to be derivable from the email
    alone. Two users sharing an email in different tenants would make the token
    ambiguous.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
