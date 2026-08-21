from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Document(Base):
    """An uploaded clinical file.

    The bytes live in `content` (bytea), not on local disk: the API has to stay
    stateless across instances, and this way file and metadata commit in the
    same transaction (D14).
    """

    __tablename__ = "documents"
    __table_args__ = (
        # Dedupe key (D24): the same content uploaded twice to the same pet is
        # the same document. Unique in the database, not only in the service --
        # two concurrent uploads would otherwise both pass the SELECT.
        Index("uq_documents_pet_id_sha256", "pet_id", "sha256", unique=True),
        # Listing a tenant's documents newest first (pagination).
        Index("ix_documents_tenant_id_id", "tenant_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pet_id: Mapped[int] = mapped_column(ForeignKey("pets.id"))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120))
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
