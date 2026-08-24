from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import Db, ResourceId, require_internal_token
from app.core.errors import documented_errors
from app.schemas.job import ClaimedJob, CompleteRequest, CompleteResponse
from app.schemas.stats import QueueStats
from app.services.jobs import claim_next_job, complete_job
from app.services.stats import queue_stats

# The guard is on the router, not on each route: a new `/internal/*` route added
# later is protected because it is here, not because somebody remembered to
# decorate it (D27).
router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    dependencies=[Depends(require_internal_token)],
    responses=documented_errors(
        **{"401": "INVALID_INTERNAL_TOKEN — missing or wrong X-Internal-Token header"}
    ),
)


@router.post(
    "/jobs/claim",
    response_model=ClaimedJob,
    summary="Step 5a — take the next job off the queue",
    description=(
        "What the worker calls. Answers `200` with one job, now marked `PROCESSING`, or "
        "`204 No Content` when the queue is empty — the worker sleeps a second and asks "
        "again.\n\n"
        "The claim is a single `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED "
        "LIMIT 1)`. Two workers claiming at the same instant get **different** jobs instead "
        "of one waiting on the other, and neither can get the same job twice. Run "
        "`docker compose up --scale worker=2` and it is visible.\n\n"
        "A job whose worker died is picked up again once its lease expires, and fails with "
        "`PROCESSING_TIMEOUT` once its attempts are spent. `lease_expires_at` in the answer "
        "is your deadline — the *job* clock, which is not the poll clock.\n\n"
        "The document text comes back with the claim, so there is no second call to make "
        "before starting work. Send `attempt` back to `complete`."
    ),
    # Declared by hand, not through `documented_errors`: a 204 must not carry a
    # body, so it cannot be described with the error envelope like the rest.
    responses={204: {"description": "Queue is empty — nothing to do right now"}},
)
async def post_claim(db: Db) -> ClaimedJob | Response:
    job = await claim_next_job(db)
    if job is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return job


@router.post(
    "/jobs/{job_id}/complete",
    response_model=CompleteResponse,
    summary="Step 5b — record the result of a job",
    description=(
        "The other half of the worker's loop, and the endpoint that answers *what happens "
        "if the worker completes the same job twice?*\n\n"
        "It is one conditional write — `UPDATE ... WHERE id = :id AND status = 'PROCESSING'` "
        "— never a read followed by a write. Two simultaneous calls both reading "
        "`PROCESSING` before either writes is precisely the bug that shape removes.\n\n"
        "- **`200`, `applied: true`** — the job moved to `DONE` or `FAILED`.\n"
        "- **`200`, `applied: false`** — the job was already in that exact state. A queue "
        "delivers at-least-once, so the worker *will* send this twice; a repeat is normal "
        "and stays silent.\n"
        "- **`409`** — the job is in a different state, or was reclaimed from you after your "
        "lease expired. Not a retry, so it is not swallowed.\n\n"
        "`DONE` requires `summary`; `FAILED` requires `error_code`."
    ),
    responses=documented_errors(
        **{
            "404": "JOB_NOT_FOUND — no job with this id",
            "409": "JOB_NOT_PROCESSING or JOB_LEASE_LOST — the job is not yours to close",
            "422": "VALIDATION_ERROR — DONE without a summary, or FAILED without an error_code",
        }
    ),
)
async def post_complete(job_id: ResourceId, data: CompleteRequest, db: Db) -> CompleteResponse:
    return await complete_job(db, job_id, data)


@router.get(
    "/stats",
    response_model=QueueStats,
    summary="How the queue is doing — the two durations, and who is holding it",
    description=(
        "Operational view of the queue, across **every** clinic. It lives behind the "
        "internal token and not the JWT for the same reason the other two routes do: the "
        "queue belongs to no tenant, so there is no isolation here to fall back on.\n\n"
        "**Two durations, and the split is the point.** `waiting` is `enqueued_at → "
        "claimed_at`; `processing` is `claimed_at → finished_at`. A rising total says "
        "something got worse — only the split says what to do about it. Waiting grew: add "
        "workers. Processing grew: the work itself got slower.\n\n"
        "`p95` alongside the average, because an average hides the tail, and the tail is "
        "what someone waiting on a poll actually feels.\n\n"
        "`retried` counts jobs that took more than one attempt — which is the rate at which "
        "workers are dying. `busiest_tenants` answers the fairness question: the queue is "
        "ordered by arrival, so one clinic uploading ten thousand documents delays everyone "
        "behind it."
    ),
)
async def get_stats(
    db: Db,
    top_tenants: Annotated[int, Query(ge=1, le=50)] = 5,
) -> QueueStats:
    return await queue_stats(db, top_tenants)
