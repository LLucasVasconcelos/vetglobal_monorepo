"""Job observability: the two durations, and who is holding the queue (D55).

The data was already being written -- `enqueued_at`, `claimed_at`, `finished_at`,
`attempts` -- and nothing ever asked it anything. This module is the asking.

**Two durations, not one**, and the split is the whole point:

    enqueued_at ──────► claimed_at ──────► finished_at
           waiting                processing

A rising total tells you something got worse. Only the split tells you *what to
do about it*: waiting grew means there are not enough workers; processing grew
means the work itself got slower. With a single number you know it hurts and not
where.

This is deliberately the only place in the application that reads across every
tenant at once, and it is behind the internal token for exactly that reason
(D27): it answers questions about the queue, which belongs to no clinic.
"""

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, JobStatus
from app.schemas.stats import Durations, QueueStats, Stat, TenantLoad

# One query, so the numbers describe the same instant. `FILTER` narrows each
# aggregate to the rows it makes sense for: a job never claimed has no waiting
# time to average, and one still running has no processing time.
_DURATIONS = text("""
    SELECT
      count(*) FILTER (WHERE claimed_at IS NOT NULL)                       AS waited_n,
      avg(extract(epoch FROM claimed_at - enqueued_at))
          FILTER (WHERE claimed_at IS NOT NULL)                            AS waited_avg,
      percentile_cont(0.95) WITHIN GROUP (
          ORDER BY extract(epoch FROM claimed_at - enqueued_at))
          FILTER (WHERE claimed_at IS NOT NULL)                            AS waited_p95,
      count(*) FILTER (WHERE finished_at IS NOT NULL AND claimed_at IS NOT NULL) AS ran_n,
      avg(extract(epoch FROM finished_at - claimed_at))
          FILTER (WHERE finished_at IS NOT NULL AND claimed_at IS NOT NULL) AS ran_avg,
      percentile_cont(0.95) WITHIN GROUP (
          ORDER BY extract(epoch FROM finished_at - claimed_at))
          FILTER (WHERE finished_at IS NOT NULL AND claimed_at IS NOT NULL) AS ran_p95
    FROM jobs
""")


def _stat(n: int | None, avg: float | None, p95: float | None) -> Durations:
    return Durations(
        samples=n or 0,
        average_seconds=round(avg, 3) if avg is not None else None,
        p95_seconds=round(p95, 3) if p95 is not None else None,
    )


async def queue_stats(db: AsyncSession, top_tenants: int) -> QueueStats:
    by_status = {
        status.value: 0 for status in JobStatus
    } | {
        row.status.value: row.n
        for row in (
            await db.execute(select(Job.status, func.count().label("n")).group_by(Job.status))
        ).all()
    }

    by_error = {
        row.error_code: row.n
        for row in (
            await db.execute(
                select(Job.error_code, func.count().label("n"))
                .where(Job.error_code.is_not(None))
                .group_by(Job.error_code)
            )
        ).all()
    }

    durations = (await db.execute(_DURATIONS)).one()

    # Only the *live* queue, so this stays bounded: it answers "who is holding
    # the line right now", which is the multi-tenant fairness question. History
    # per tenant would be a different, larger report.
    waiting = (
        await db.execute(
            select(Job.tenant_id, func.count().label("n"))
            .where(Job.status.in_((JobStatus.ENQUEUED, JobStatus.PROCESSING)))
            .group_by(Job.tenant_id)
            .order_by(func.count().desc())
            .limit(top_tenants)
        )
    ).all()

    retried = await db.scalar(select(func.count()).select_from(Job).where(Job.attempts > 1))

    return QueueStats(
        jobs=Stat(**by_status),
        failures=by_error,
        retried=retried or 0,
        waiting=_stat(durations.waited_n, durations.waited_avg, durations.waited_p95),
        processing=_stat(durations.ran_n, durations.ran_avg, durations.ran_p95),
        busiest_tenants=[TenantLoad(tenant_id=r.tenant_id, in_flight=r.n) for r in waiting],
    )
