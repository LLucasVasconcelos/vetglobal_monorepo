import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class JobStatus(enum.StrEnum):
    """`ENQUEUED -> PROCESSING -> DONE | FAILED` (D21). No other transition."""

    ENQUEUED = "ENQUEUED"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.DONE, JobStatus.FAILED)


class Job(Base):
    """One processing attempt over one document.

    A failed document gets a *new* job, never a rewritten one (D24): the history
    of attempts stays readable.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        # Poll cursor (D23): "the latest job of this document" is
        # `WHERE document_id = :d ORDER BY id DESC LIMIT 1`.
        Index("ix_jobs_document_id_id", "document_id", "id"),
        # Claim hot path (D21): `WHERE status = 'ENQUEUED' ORDER BY id`.
        Index("ix_jobs_status_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status", values_callable=lambda e: [m.value for m in e]),
        default=JobStatus.ENQUEUED,
        server_default=JobStatus.ENQUEUED.value,
    )
    summary: Mapped[str | None] = mapped_column(Text, default=None)

    # Failure, in the D22 shape: a machine `error_code` and a human `message`.
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    message: Mapped[str | None] = mapped_column(Text, default=None)

    attempts: Mapped[int] = mapped_column(default=0, server_default="0")

    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Job timeout, which is NOT the poll timeout (invariant 6). A job still
    # PROCESSING past this instant was abandoned by its worker: it is retried,
    # or fails with PROCESSING_TIMEOUT once attempts run out (D21).
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
