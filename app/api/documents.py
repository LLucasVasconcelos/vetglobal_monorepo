from fastapi import APIRouter

from app.api.deps import CurrentPrincipal, Db, ResourceId
from app.core.errors import documented_errors
from app.schemas.document import DocumentResponse, JobView
from app.services.documents import get_document

router = APIRouter(tags=["documents"])


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
            "401": "NOT_AUTHENTICATED — no token, or an invalid one",
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
