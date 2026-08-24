from fastapi import APIRouter, File, Response, UploadFile, status

from app.api.deps import AUTH_401, CurrentPrincipal, Db, Limit, Offset, ResourceId
from app.core.errors import documented_errors
from app.schemas.document import UploadResponse
from app.schemas.pet import PetCreate, PetListResponse, PetResponse
from app.services.documents import upload_document
from app.services.pets import create_pet, list_pets

router = APIRouter(tags=["pets"])

DEFAULT_PAGE_SIZE = 50


@router.post(
    "/pets",
    response_model=PetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Step 2 — create a pet",
    description=(
        "The pet belongs to the tenant in your token. There is no `tenant_id` field in this "
        "body, and that is the design: a field for it would be a field a caller could set to "
        "someone else's clinic."
    ),
    responses=documented_errors(**{"401": AUTH_401}),
)
async def post_pet(data: PetCreate, principal: CurrentPrincipal, db: Db) -> PetResponse:
    pet = await create_pet(db, principal, data)
    return PetResponse.model_validate(pet)


@router.get(
    "/pets",
    response_model=PetListResponse,
    summary="Step 2b — list your clinic's pets, and see the isolation as a number",
    description=(
        "Every pet in **your** clinic, newest first — and nothing from anyone else's.\n\n"
        "**This is the route to check tenant isolation on.** Register a second clinic, create "
        "pets in both, and call this with each token: the two answers share a table and have "
        "nothing in common. `total` counts only your clinic, so the isolation is a number you "
        "can read rather than a claim you have to trust.\n\n"
        "It is worth contrasting with `GET /documents/{id}`, which proves the same rule from "
        "the other side: there, someone else's record answers `404`. That proves the negative "
        "half — you cannot reach what is not yours. This proves the positive half — what you "
        "*do* reach is exactly yours, and the count agrees.\n\n"
        "There is no `tenant_id` parameter here, and that is the design: the filter comes from "
        "the token, so there is nothing in this request to point somewhere else.\n\n"
        "Paginated with `limit` (default 50, max 200) and `offset`."
    ),
    responses=documented_errors(
        **{
            "401": AUTH_401,
            "422": "VALIDATION_ERROR — limit above 200, or a negative offset",
        }
    ),
)
async def get_pets(
    principal: CurrentPrincipal,
    db: Db,
    limit: Limit = DEFAULT_PAGE_SIZE,
    offset: Offset = 0,
) -> PetListResponse:
    pets, total = await list_pets(db, principal, limit, offset)

    return PetListResponse(
        items=[PetResponse.model_validate(pet) for pet in pets],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/pets/{pet_id}/documents",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Step 3 — upload a document and enqueue its job",
    description=(
        "Send a `.txt` file. The bytes are stored in Postgres, not on disk, so any instance "
        "of the API can serve them (D14).\n\n"
        "**Two success codes, and they mean different things.**\n\n"
        "- `202 Accepted` — new content. A document and a job were created; the job is "
        "`ENQUEUED` and a worker will pick it up.\n"
        "- `200 OK` — this exact content was already uploaded for this pet. It is identified "
        "by the SHA-256 of the bytes, so a re-upload never creates a second copy. You get the "
        "existing `document_id` back, along with the job that already covers it — including "
        "its summary if it is already `DONE`.\n\n"
        "Re-uploading a document whose last job **failed** is how you ask for a retry: a new "
        "job is created for the same document, and the failed attempt stays in the history "
        "rather than being overwritten.\n\n"
        "Then take `document_id` and `job_id` to the poll in step 4."
    ),
    responses=documented_errors(
        **{
            "200": "Already uploaded — deduplicated, no second copy stored",
            "401": AUTH_401,
            "404": "PET_NOT_FOUND — no such pet, or it belongs to another clinic",
            "413": "FILE_TOO_LARGE — over the 10 MB limit",
            "415": "UNSUPPORTED_FILE_TYPE or FILE_CONTENT_MISMATCH — not a .txt, or not UTF-8",
            "422": "FILE_EMPTY_OR_TOO_SHORT, FILENAME_TOO_LONG or FILENAME_INVALID",
        }
    ),
)
async def post_document(
    pet_id: ResourceId,
    principal: CurrentPrincipal,
    db: Db,
    response: Response,
    file: UploadFile = File(description="A UTF-8 .txt file, up to 10 MB."),  # noqa: B008
) -> UploadResponse:
    raw = await file.read()
    outcome = await upload_document(db, principal, pet_id, file.filename or "", raw)

    if not outcome.created:
        # 200 and not 202: nothing was accepted for processing that was not
        # already accepted before (D24).
        response.status_code = status.HTTP_200_OK

    return UploadResponse(
        document_id=outcome.document.id,
        job_id=outcome.job.id,
        status=outcome.job.status,
    )
