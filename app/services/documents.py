"""Upload, dedupe and read of clinical documents (D24, D25, D26)."""

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import DomainError
from app.core.security import Principal
from app.models import Document, Job, JobStatus
from app.services.pets import get_pet

TEXT_SUFFIX = ".txt"
TEXT_CONTENT_TYPE = "text/plain"

# The width of `documents.filename`. Checked here rather than left to the
# database: Postgres answers a longer value with a truncation error, which is
# not an IntegrityError, escapes every handler and surfaces as a 500 -- a
# predictable client input producing the one status that is supposed to mean a
# bug on our side (invariant 5).
MAX_FILENAME_LENGTH = 255


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    document: Document
    job: Job
    # False when this exact content was already stored for this pet. The route
    # turns it into 200 instead of 202 (D24).
    created: bool


def document_not_found() -> DomainError:
    return DomainError(404, "DOCUMENT_NOT_FOUND", "Document not found.")


def validate_upload(filename: str, raw: bytes) -> str:
    """The four checks of D25, in the order that gives the most useful answer.

    Extension first: rejecting a `.pdf` for being empty would be true and
    useless. Size before decoding, so a 200 MB file is refused without being
    walked through a UTF-8 decoder.

    Returns the decoded text, which the caller does not currently keep -- the
    bytes are what is stored. Decoding is the *check* that this is really text,
    not a rename of something else.
    """
    if not filename.lower().endswith(TEXT_SUFFIX):
        raise DomainError(
            415,
            "UNSUPPORTED_FILE_TYPE",
            f"Only {TEXT_SUFFIX} files are accepted. PDF support is planned, not yet available.",
        )

    if len(filename) > MAX_FILENAME_LENGTH:
        # Refused rather than truncated: a stored name that is not the name that
        # was uploaded is a quiet lie in a record meant to be evidence (D14).
        raise DomainError(
            422,
            "FILENAME_TOO_LONG",
            f"Filename must be at most {MAX_FILENAME_LENGTH} characters.",
        )

    if len(raw) > settings.max_upload_bytes:
        raise DomainError(
            413,
            "FILE_TOO_LARGE",
            f"File exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB limit.",
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise DomainError(
            415,
            "FILE_CONTENT_MISMATCH",
            "File is named .txt but is not valid UTF-8 text.",
        ) from None

    if len(text.strip()) < settings.min_content_chars:
        # Refused at the door rather than enqueued: there is nothing to
        # summarize, and a job created here would exist only to fail.
        raise DomainError(
            422,
            "FILE_EMPTY_OR_TOO_SHORT",
            f"File must hold at least {settings.min_content_chars} characters of text.",
        )

    return text


async def latest_job(db: AsyncSession, document_id: int) -> Job | None:
    """The most recent attempt at this document -- the one whose status the
    document reports, and the one `after_job_id=0` will resolve to (D23)."""
    return await db.scalar(
        select(Job).where(Job.document_id == document_id).order_by(Job.id.desc()).limit(1)
    )


async def upload_document(
    db: AsyncSession, principal: Principal, pet_id: int, filename: str, raw: bytes
) -> UploadOutcome:
    pet = await get_pet(db, principal, pet_id)
    validate_upload(filename, raw)
    digest = hashlib.sha256(raw).hexdigest()

    existing = await db.scalar(
        select(Document).where(Document.pet_id == pet.id, Document.sha256 == digest)
    )
    if existing is not None:
        return await _reuse(db, principal, existing)

    document = Document(
        pet_id=pet.id,
        tenant_id=principal.tenant_id,
        filename=filename,
        # What we verified it to be, not what the client declared it was.
        content_type=TEXT_CONTENT_TYPE,
        size_bytes=len(raw),
        sha256=digest,
        content=raw,
    )
    db.add(document)

    try:
        await db.flush()
    except IntegrityError:
        # Two uploads of the same content raced past the SELECT above. The
        # unique index (pet_id, sha256) is what decides, and the loser reads
        # back the winner's row -- which is the same answer a moment later.
        await db.rollback()
        winner = await db.scalar(
            select(Document).where(Document.pet_id == pet_id, Document.sha256 == digest)
        )
        if winner is None:
            raise
        return await _reuse(db, principal, winner)

    job = Job(document_id=document.id, tenant_id=principal.tenant_id)
    db.add(job)
    # Document and job commit together: a document with no job would be a file
    # nobody will ever process, and nothing in the API would notice.
    await db.commit()
    await db.refresh(document)
    await db.refresh(job)

    return UploadOutcome(document=document, job=job, created=True)


async def _reuse(db: AsyncSession, principal: Principal, document: Document) -> UploadOutcome:
    """Same content, already here (D24). Answered 200, never 202."""
    previous = await latest_job(db, document.id)

    if previous is not None and previous.status is not JobStatus.FAILED:
        # DONE gives back the summary already computed; ENQUEUED or PROCESSING
        # points at the attempt already under way. Either way, no new work.
        return UploadOutcome(document=document, job=previous, created=False)

    # The last attempt failed, so re-uploading is how a client asks to try
    # again. A new job, not a rewritten one: the failed attempt stays readable.
    retry = Job(document_id=document.id, tenant_id=principal.tenant_id)
    db.add(retry)
    await db.commit()
    await db.refresh(retry)
    return UploadOutcome(document=document, job=retry, created=False)


async def get_document(
    db: AsyncSession, principal: Principal, document_id: int
) -> tuple[Document, Job | None]:
    document = await db.scalar(
        select(Document).where(
            Document.id == document_id, Document.tenant_id == principal.tenant_id
        )
    )
    if document is None:
        raise document_not_found()

    return document, await latest_job(db, document.id)
