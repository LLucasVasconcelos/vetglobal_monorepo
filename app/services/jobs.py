"""The queue side of a job: take one, then close it (D21, D18).

The `jobs` table *is* the queue -- there is no broker. Which means every state
change here is a race with another worker, and the shape of the SQL is what
decides the outcome, not the order of statements in this file. Both operations
below are a single conditional `UPDATE`: the check lives in the `WHERE`, inside
the atomic write, so there is no window between deciding and doing (invariant 3).

These are the only queries in the application with no `tenant_id` filter, and
deliberately so: the worker is not a tenant, it serves all of them. That is
exactly why `/internal/*` is closed behind a shared secret rather than a JWT
(D27) -- there is no isolation left here to fall back on.
"""

from base64 import b64encode
from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import DomainError
from app.db.listener import notify_job_finished
from app.models import Document, Job, JobStatus
from app.schemas.job import ClaimedJob, CompleteRequest, CompleteResponse
from app.services.documents import TEXT_CONTENT_TYPE

PROCESSING_TIMEOUT_MESSAGE = (
    "Processing did not finish within the allowed time, after "
    f"{settings.job_max_attempts} attempts."
)


async def _fail_exhausted_leases(db: AsyncSession) -> None:
    """Jobs whose worker died and whose attempts are spent become FAILED.

    Run at claim time rather than from a sweeper process: the queue is only
    read by workers, so a job stuck in `PROCESSING` matters exactly when
    somebody comes looking for work. One extra indexed `UPDATE` per claim buys
    us no second moving part to deploy, supervise and explain.

    This is the second place a job reaches a terminal state, so it announces it
    too (D43). Without the `NOTIFY` a poll open on the job that just ran out of
    attempts would only learn about it on its next recheck -- correct, but
    seconds late, and for the one outcome the client is least happy to wait for.
    """
    failed = (
        await db.execute(
            update(Job)
            .where(
                Job.status == JobStatus.PROCESSING,
                Job.lease_expires_at < func.now(),
                Job.attempts >= settings.job_max_attempts,
            )
            .values(
                status=JobStatus.FAILED,
                error_code="PROCESSING_TIMEOUT",
                message=PROCESSING_TIMEOUT_MESSAGE,
                finished_at=func.now(),
                lease_expires_at=None,
            )
            .returning(Job.id, Job.document_id)
        )
    ).all()

    for job in failed:
        await notify_job_finished(
            db, document_id=job.document_id, job_id=job.id, status=JobStatus.FAILED.value
        )


async def claim_next_job(db: AsyncSession) -> ClaimedJob | None:
    """Take the oldest available job, or None when there is nothing to do.

    `FOR UPDATE SKIP LOCKED` is what makes this a queue: a row another worker
    is already claiming is stepped over instead of waited on, so N workers make
    N different claims at the same instant rather than queueing behind the
    first. The subquery sits *inside* the `UPDATE`, so selecting the candidate
    and marking it `PROCESSING` are one statement and one lock.

    Available means `ENQUEUED`, or `PROCESSING` with an expired lease -- a job
    whose worker was killed mid-run. Without that second case the lease columns
    would be written and never read, and a crashed worker would strand its
    document forever.
    """
    await _fail_exhausted_leases(db)

    candidate = (
        select(Job.id)
        .where(
            or_(
                Job.status == JobStatus.ENQUEUED,
                and_(Job.status == JobStatus.PROCESSING, Job.lease_expires_at < func.now()),
            )
        )
        # Oldest first, and the id order is the arrival order. Also what lets
        # the claim ride the (status, id) index instead of scanning the table.
        .order_by(Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )

    claimed = (
        await db.execute(
            update(Job)
            .where(Job.id == candidate)
            .values(
                status=JobStatus.PROCESSING,
                attempts=Job.attempts + 1,
                claimed_at=func.now(),
                # The database's clock, not this process's: two API instances
                # with drifting clocks would otherwise disagree on when a lease
                # ended, and the lease is what decides who owns the job.
                lease_expires_at=func.now() + timedelta(seconds=settings.job_lease_seconds),
            )
            .returning(Job.id, Job.document_id, Job.attempts, Job.lease_expires_at)
        )
    ).first()

    if claimed is None:
        await db.commit()
        return None

    document = await db.get(Document, claimed.document_id)
    if document is None:
        # A job always has a document: they are created in one transaction, and
        # documents are never deleted. Reaching here means the invariant broke,
        # which is a 500 and not a queue state.
        raise RuntimeError(f"Job {claimed.id} points at missing document {claimed.document_id}")

    await db.commit()

    is_text = document.content_type == TEXT_CONTENT_TYPE

    return ClaimedJob(
        job_id=claimed.id,
        document_id=claimed.document_id,
        attempt=claimed.attempts,
        filename=document.filename,
        content_type=document.content_type,
        # Text goes as text so the loop stays drivable by hand -- reading a
        # consultation note out of a claim is half of what makes this endpoint
        # a demonstration. Decoding cannot fail: the upload refused anything
        # that was not UTF-8 before the document was stored (D25).
        content=document.content.decode("utf-8") if is_text else None,
        # A PDF has no readable form in JSON, so it goes as base64 (D51).
        content_base64=None if is_text else b64encode(document.content).decode("ascii"),
        lease_expires_at=claimed.lease_expires_at,
    )


async def complete_job(db: AsyncSession, job_id: int, data: CompleteRequest) -> CompleteResponse:
    """Close a job. Safe to call twice with the same verdict, refused with 409
    when the second call contradicts the first (D18).

    At-least-once delivery *guarantees* the worker will sometimes send the same
    completion twice -- that is normal and must be silent. A worker sending
    `FAILED` over a job already `DONE` is not a retry, it is a bug, and 409
    keeps that signal instead of swallowing it.
    """
    conditions = [Job.id == job_id, Job.status == JobStatus.PROCESSING]
    if data.attempt is not None:
        # The fencing token. A worker that stalled past its lease had the job
        # taken from it and is now writing about work somebody else redid.
        conditions.append(Job.attempts == data.attempt)

    updated = (
        await db.execute(
            update(Job)
            .where(*conditions)
            .values(
                status=data.status,
                summary=data.summary,
                error_code=data.error_code,
                message=data.message,
                finished_at=func.now(),
                # The job is over; there is nothing left to lease.
                lease_expires_at=None,
            )
            .returning(Job)
        )
    ).scalar_one_or_none()

    if updated is not None:
        # The only place a job is closed by a worker, so the only place the
        # poll has to be woken from (D13). Before the commit, not after: the
        # notification is queued by Postgres and delivered *at* the commit, so
        # it cannot arrive ahead of the row it describes, and a failed commit
        # takes the announcement down with it.
        await notify_job_finished(
            db,
            document_id=updated.document_id,
            job_id=updated.id,
            status=updated.status.value,
        )
        await db.commit()
        return _view(updated, applied=True)

    # Zero rows. The write already happened -- or provably did not -- so this
    # read is not a check-then-act: it only decides which answer explains why.
    current = await db.get(Job, job_id)
    if current is None:
        raise DomainError(404, "JOB_NOT_FOUND", "Job not found.")

    same_attempt = data.attempt is None or current.attempts == data.attempt

    if current.status is data.status and same_attempt:
        return _view(current, applied=False)

    # Which of the two `WHERE` conditions failed decides which answer is useful.
    # A job that somebody else is holding, or already finished under a different
    # attempt, is a lost lease: naming the status there would be unhelpful, and
    # when the other worker reached the same verdict it would be absurd -- "job
    # is DONE, so it cannot be completed as DONE".
    #
    # `ENQUEUED` is the exception, and the reason the status is checked at all:
    # nobody holds that job, so no lease was lost. Its attempt not matching only
    # says the caller never claimed it, and the state is the clearer diagnosis.
    if not same_attempt and current.status is not JobStatus.ENQUEUED:
        raise DomainError(
            409,
            "JOB_LEASE_LOST",
            f"Job is on attempt {current.attempts}, not {data.attempt}. This claim no longer "
            "owns it, so the result being reported is stale.",
        )

    raise DomainError(
        409,
        "JOB_NOT_PROCESSING",
        f"Job is {current.status.value}, so it cannot be completed as {data.status.value}.",
    )


def _view(job: Job, *, applied: bool) -> CompleteResponse:
    return CompleteResponse(
        id=job.id,
        document_id=job.document_id,
        status=job.status,
        attempts=job.attempts,
        summary=job.summary,
        error_code=job.error_code,
        message=job.message,
        finished_at=job.finished_at,
        applied=applied,
    )
