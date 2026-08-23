from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import AUTH_401, AfterJobId, CurrentPrincipal, Db, Limit, Offset, ResourceId
from app.core.config import settings
from app.core.errors import documented_errors
from app.db.base import PG_INT_MAX
from app.schemas.document import DocumentListResponse, DocumentResponse, JobView, PollResponse
from app.services.documents import get_document, list_documents
from app.services.poll import wait_for_job

router = APIRouter(tags=["documents"])

PetFilter = Annotated[
    int | None,
    Query(ge=1, le=PG_INT_MAX, description="Only this pet's documents. Must be your own pet."),
]


@router.get(
    "/documents",
    response_model=DocumentListResponse,
    summary="List your clinic's documents, newest first",
    description=(
        "Every document in **your** clinic, each with its latest job — so one call answers "
        "*which of these are ready*, which is the only reason to list documents at all.\n\n"
        "Narrow it to one pet with `pet_id`. That parameter can only narrow: the tenant "
        "filter comes from your token and is not optional, so another clinic's `pet_id` "
        "matches nothing rather than reaching anything.\n\n"
        "Paginated with `limit` (default 50, max 200) and `offset`, ordered by id descending. "
        "`total` counts the whole matching set, not this page.\n\n"
        "The job travels in the same query as the document, through a lateral join — fetching "
        "it per row would be fifty extra round trips for a fifty-row page."
    ),
    responses=documented_errors(
        **{
            "401": AUTH_401,
            "422": "VALIDATION_ERROR — limit above 200, a negative offset, or a malformed pet_id",
        }
    ),
)
async def get_documents(
    principal: CurrentPrincipal,
    db: Db,
    limit: Limit = 50,
    offset: Offset = 0,
    pet_id: PetFilter = None,
) -> DocumentListResponse:
    rows, total = await list_documents(db, principal, limit, offset, pet_id)

    items = []
    for document, job in rows:
        item = DocumentResponse.model_validate(document)
        item.job = JobView.model_validate(job) if job is not None else None
        items.append(item)

    return DocumentListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/documents/{document_id}",
    response_model=DocumentResponse,
    summary="Read a document and the state of its latest job",
    description=(
        "Metadata plus the latest job, which is where `summary` appears once it is `DONE` "
        "and where `error_code` appears if it failed.\n\n"
        "This answers immediately, whatever the state. To *wait* for a job to finish, use "
        "the poll instead of calling this in a loop.\n\n"
        "A document belonging to another clinic answers `404`, not `403`. Ids are "
        "sequential, so `/documents/41` is a guess anyone can make — and `403` would confirm "
        "the guess was right, which is itself the leak."
    ),
    responses=documented_errors(
        **{
            "401": AUTH_401,
            "404": "DOCUMENT_NOT_FOUND — no such document, or it belongs to another clinic",
        }
    ),
)
async def get_document_by_id(
    document_id: ResourceId, principal: CurrentPrincipal, db: Db
) -> DocumentResponse:
    document, job = await get_document(db, principal, document_id)

    response = DocumentResponse.model_validate(document)
    response.job = JobView.model_validate(job) if job is not None else None
    return response


@router.get(
    "/documents/{document_id}/poll",
    response_model=PollResponse,
    summary="Wait for a document's job to finish",
    description=(
        f"Held open for up to **{settings.poll_timeout_seconds} seconds**, and answers the "
        "instant the job reaches `DONE` or `FAILED` — no polling loop, no wasted requests.\n\n"
        "`after_job_id=0` means *the latest job of this document*, so the front can call this "
        "without having kept anything from the upload. The id it resolved to comes back as "
        "`awaiting_job_id`.\n\n"
        "**On timeout the answer is `200`**, with `result: null` and `timed_out: true` — call "
        "again with the same `awaiting_job_id`. It is not a failure: the request's clock ran "
        "out, the job's did not. The job's own clock failing looks completely different — a "
        "`200` whose `result.job.status` is `FAILED`, with `PROCESSING_TIMEOUT`.\n\n"
        "Under the hood the server holds one `LISTEN document_events` connection and the "
        "`complete` call emits `NOTIFY` in the same transaction as its write, so this returns "
        f"in about a millisecond. Every waiter still re-reads the row every "
        f"{settings.poll_recheck_seconds} seconds, because `NOTIFY` is not persistent: the "
        "notification is the speed, the re-read is the correctness."
    ),
    responses=documented_errors(
        **{
            "401": AUTH_401,
            "404": (
                "DOCUMENT_NOT_FOUND or JOB_NOT_FOUND — no such document, it belongs to "
                "another clinic, or that job is not one of this document's"
            ),
            "422": "VALIDATION_ERROR — after_job_id is negative or wider than an id can be",
        }
    ),
)
async def poll_document_job(
    document_id: ResourceId,
    principal: CurrentPrincipal,
    db: Db,
    after_job_id: AfterJobId = 0,
) -> PollResponse:
    return await wait_for_job(db, principal, document_id, after_job_id)
