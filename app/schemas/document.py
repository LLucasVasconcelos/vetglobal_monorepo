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
