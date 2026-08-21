from fastapi import APIRouter, File, Response, UploadFile, status

from app.api.deps import CurrentPrincipal, Db
from app.core.errors import documented_errors
from app.schemas.document import UploadResponse
from app.schemas.pet import PetCreate, PetResponse
from app.services.documents import upload_document
from app.services.pets import create_pet

router = APIRouter(tags=["pets"])


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
    responses=documented_errors(**{"401": "NOT_AUTHENTICATED — no token, or an invalid one"}),
)
async def post_pet(data: PetCreate, principal: CurrentPrincipal, db: Db) -> PetResponse:
    pet = await create_pet(db, principal, data)
    return PetResponse.model_validate(pet)


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
            "401": "NOT_AUTHENTICATED — no token, or an invalid one",
            "404": "PET_NOT_FOUND — no such pet, or it belongs to another clinic",
            "413": "FILE_TOO_LARGE — over the 10 MB limit",
            "415": "UNSUPPORTED_FILE_TYPE or FILE_CONTENT_MISMATCH — not a .txt, or not UTF-8",
            "422": "FILE_EMPTY_OR_TOO_SHORT or FILENAME_TOO_LONG",
        }
    ),
)
async def post_document(
    pet_id: int,
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
