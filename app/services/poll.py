"""Waiting for a job to finish -- the long poll (D13, D19, D20, D23).

The whole endpoint rests on the order of three lines in `wait_for_job`:

    subscribe  ->  read  ->  wait

and it is the bug this project is most likely to have. Read first and subscribe
after, and a job that finishes in between emits its `NOTIFY` with nobody
listening: the notification is gone, the read already said `PROCESSING`, and the
client waits the full 25 seconds for something that happened before it started
waiting. Subscribing first turns that race into a wake-up that arrives early,
which costs one extra read and nothing else.

The same reasoning is why `clear()` sits above the read rather than below it.
Any notification from before the clear describes a commit the read that follows
will see; any notification after it leaves the event set, and the wait returns
at once.

The two clocks are also here, and they are not the same thing (invariant 6):
`POLL_TIMEOUT_SECONDS` ends this HTTP request while the job keeps running, and
answers `timed_out: true`. The lease of D21 is what ends the *job*, and it
answers `FAILED` with `PROCESSING_TIMEOUT`.
"""

import asyncio
import contextlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import DomainError
from app.core.security import Principal
from app.db.listener import document_events
from app.models import Document, Job
from app.schemas.document import DocumentResponse, JobView, PollResponse
from app.services.documents import document_not_found

LATEST_JOB = 0

# What `DocumentResponse` needs, and nothing else. `select(Document)` would drag
# the file itself along -- up to 10 MB of `bytea` per poll, held for 25 seconds,
# to answer with metadata that does not include it.
_DOCUMENT_COLUMNS = (
    Document.id,
    Document.pet_id,
    Document.filename,
    Document.content_type,
    Document.size_bytes,
    Document.sha256,
    Document.created_at,
)


async def wait_for_job(
    db: AsyncSession, principal: Principal, document_id: int, after_job_id: int
) -> PollResponse:
    with document_events.subscribe(document_id) as woken:
        document = await _resolve_document(db, principal, document_id)
        job_id = await _resolve_job_id(db, document_id, after_job_id)
        deadline = asyncio.get_running_loop().time() + settings.poll_timeout_seconds

        while True:
            woken.clear()
            job = await _read_job(db, job_id)

            if job.status.is_terminal:
                # The document, with the job inside -- which is what the
                # assignment asks a finished poll to return (D50). The document
                # is read once and reused: its metadata cannot change while its
                # job runs, and only the job is worth re-reading.
                return PollResponse(
                    result=document.model_copy(update={"job": job}),
                    awaiting_job_id=job_id,
                    timed_out=False,
                )

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return PollResponse(result=None, awaiting_job_id=job_id, timed_out=True)

            # Whichever comes first: the notification, or the recheck that
            # covers the notification never arriving. Sleeping the whole
            # remaining time here instead of a recheck interval is what would
            # make this design fast but not correct.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    woken.wait(), timeout=min(settings.poll_recheck_seconds, remaining)
                )


async def _resolve_document(
    db: AsyncSession, principal: Principal, document_id: int
) -> DocumentResponse:
    """The document being polled, or a 404 that says nothing about why."""
    row = (
        await db.execute(
            select(*_DOCUMENT_COLUMNS).where(
                Document.id == document_id, Document.tenant_id == principal.tenant_id
            )
        )
    ).one_or_none()

    if row is None:
        # Another clinic's document is a 404, never a 403 (D26, invariant 4).
        raise document_not_found()

    return DocumentResponse.model_validate(row)


async def _resolve_job_id(db: AsyncSession, document_id: int, after_job_id: int) -> int:
    """Which job this poll is actually about, resolved once and then held.

    Resolved once on purpose: with `after_job_id=0` the answer is "the latest
    job of this document" (D23), and a re-upload during the poll would otherwise
    move the poll onto a newer job halfway through -- so the client would be
    told about a job it never asked for, under an `awaiting_job_id` that changed
    mid-request.

    The tenant was already settled by `_resolve_document`, and every lookup here
    is scoped to that document -- so `after_job_id` is not a way around it.
    """
    if after_job_id == LATEST_JOB:
        latest = await db.scalar(
            select(Job.id).where(Job.document_id == document_id).order_by(Job.id.desc()).limit(1)
        )
        if latest is None:
            raise DomainError(
                404, "JOB_NOT_FOUND", "This document has no job to wait for."
            )
        return latest

    # Scoped to the document, which is already scoped to the tenant: asking for
    # a job by id cannot be a way around the check above.
    job_id = await db.scalar(
        select(Job.id).where(Job.id == after_job_id, Job.document_id == document_id)
    )
    if job_id is None:
        raise DomainError(
            404, "JOB_NOT_FOUND", f"Job {after_job_id} does not belong to this document."
        )
    return job_id


async def _read_job(db: AsyncSession, job_id: int) -> JobView:
    """The current state of the job, as a snapshot that outlives the transaction.

    `populate_existing` because this is the same session on every pass of the
    loop: without it the identity map hands back the row as it was read the
    first time, and the poll would never see the job change.

    The `rollback` is what keeps 40 open polls from being 40 connections idle in
    a transaction: the session gives its connection back to the pool while it
    sleeps, and takes another for the next read. It also means the view has to
    be built *before* it -- a rollback expires every attribute, and touching one
    afterwards is a lazy load in the middle of an async request.
    """
    job = await db.get(Job, job_id, populate_existing=True)
    if job is None:
        # The job was resolved from the database moments ago and jobs are never
        # deleted; reaching here means that stopped being true.
        raise DomainError(404, "JOB_NOT_FOUND", "Job not found.")

    view = JobView.model_validate(job)
    await db.rollback()
    return view
