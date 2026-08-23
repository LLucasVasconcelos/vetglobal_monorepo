from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models import JobStatus


class JobView(BaseModel):
    """A job as the owner of the document sees it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    status: JobStatus
    summary: str | None = None
    error_code: str | None = Field(default=None, description="Set only when status is FAILED.")
    message: str | None = None
    attempts: int
    enqueued_at: datetime
    finished_at: datetime | None = None


class UploadResponse(BaseModel):
    """Answer to an upload. The status code carries what the body does not:
    `202` means this content is new and a job was enqueued, `200` means the
    same file was already here and is being deduplicated (D24)."""

    document_id: int
    job_id: int
    status: JobStatus


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    pet_id: int
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime
    # The latest job of this document, which is where `summary` lives. None
    # only in the window between creating a document and its job, which the
    # upload does in one transaction -- so in practice, never.
    job: JobView | None = None


class DocumentListResponse(BaseModel):
    """A page of the caller's own documents, each with its latest job (D52).

    The job is part of the row and not a second call: "which of these is ready"
    is the only question a document list exists to answer.
    """

    items: list[DocumentResponse]
    total: int = Field(
        description=(
            "How many documents match in YOUR clinic — the whole set, not this page. "
            "Another clinic's documents are not counted."
        )
    )
    limit: int
    offset: int


class PollResponse(BaseModel):
    """Answer to a long poll (D19, D50).

    The third way, where the assignment offered two: `204 No Content` has
    nowhere to carry the cursor, and a bare `null` body cannot tell "not yet"
    apart from "finished, with nothing in it". This envelope says both, and
    hands back what to call again with.
    """

    result: DocumentResponse | None = Field(
        default=None,
        description=(
            "The document, once its job reached DONE or FAILED — the same shape "
            "`GET /documents/{id}` returns, with the job (and its `summary`, or its "
            "`error_code`) inside. Null until then."
        ),
    )
    awaiting_job_id: int = Field(
        description=(
            "The job this poll resolved to — call again with it. Worth reading even on "
            "success: `after_job_id=0` is answered here with the id it meant."
        )
    )
    timed_out: bool = Field(
        description=(
            "True when the 25 seconds of *this request* ran out. The job is still running; "
            "this is not a failure, and not the job's own timeout (which arrives as "
            "`result.job.status: FAILED` with `error_code: PROCESSING_TIMEOUT`)."
        )
    )
