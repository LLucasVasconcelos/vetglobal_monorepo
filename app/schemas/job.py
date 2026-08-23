from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.db.base import PG_INT_MAX
from app.models import JobStatus


class ClaimedJob(BaseModel):
    """One job handed to one worker, with everything it needs to do the work.

    The document text travels with the claim instead of behind a second call:
    the worker would fetch it immediately anyway, and a second round trip is a
    second thing that can fail between claiming a job and starting it.
    """

    job_id: int
    document_id: int
    attempt: int = Field(
        description=(
            "Which attempt this claim is -- 1 the first time. Send it back to "
            "`complete` as `attempt` and a worker whose lease expired can no "
            "longer overwrite the result of the worker that replaced it."
        )
    )
    filename: str
    content: str
    lease_expires_at: datetime = Field(
        description=(
            "Finish before this instant. Past it the job is reclaimed by another "
            "worker, or failed with PROCESSING_TIMEOUT once attempts run out. "
            "This is the *job* clock, not the poll clock (invariant 6)."
        )
    )


# Generous ceilings, not expected sizes: a summary of a consultation note runs to
# hundreds of characters. They are here because `summary` and `message` land in
# unbounded `text` columns, and a worker looping on its own output would
# otherwise write until the disk says no. A bound two orders of magnitude above
# any real value costs nothing and caps the damage.
MAX_SUMMARY_LENGTH = 10_000
MAX_MESSAGE_LENGTH = 2_000


class CompleteRequest(BaseModel):
    """The worker's verdict. `DONE` carries a summary, `FAILED` carries a reason."""

    # A misspelled field is refused instead of ignored. `{"status": "DONE",
    # "atempt": 2}` would otherwise be accepted with the fencing token silently
    # dropped -- the guard turned off by a typo, with nothing in the answer
    # saying so.
    model_config = ConfigDict(extra="forbid")

    status: Literal[JobStatus.DONE, JobStatus.FAILED] = Field(
        description="The terminal state to move to. ENQUEUED and PROCESSING are not accepted."
    )
    summary: str | None = Field(
        default=None, max_length=MAX_SUMMARY_LENGTH, description="Required when status is DONE."
    )
    error_code: str | None = Field(
        default=None,
        max_length=64,
        description="Required when status is FAILED. Machine-readable, in the D22 shape.",
    )
    message: str | None = Field(
        default=None,
        max_length=MAX_MESSAGE_LENGTH,
        description="Human-readable reason for a failure.",
    )
    error: str | None = Field(
        default=None,
        max_length=MAX_MESSAGE_LENGTH,
        description=(
            "Accepted as a synonym for `message`, because it is the field name in the "
            "assignment's own failure example. Prefer `error_code` + `message`: this one "
            "cannot say *what kind* of failure it was, which is what a client branches on."
        ),
    )
    attempt: int | None = Field(
        default=None,
        ge=1,
        # The same Postgres `integer` ceiling as any id: a larger number is not a
        # wrong attempt, it is a value the comparison column cannot hold, and it
        # left through the 500 handler before this bound existed.
        le=PG_INT_MAX,
        description=(
            "The `attempt` from the claim. Optional so the flow can be driven by hand with "
            "curl; a real worker always sends it, and is then answered `409` instead of "
            "silently overwriting a job that was reclaimed from it."
        ),
    )

    @model_validator(mode="after")
    def check_payload_matches_status(self) -> "CompleteRequest":
        # The assignment's failure example is `{"status": "FAILED", "error":
        # "Could not parse document"}` -- one human string, where D22 asks for a
        # machine `error_code` and a human `message`. Both are accepted, because
        # answering the example printed in the brief with a 422 is a contract
        # argument won at the reader's expense. `error` fills `message`, and
        # stands in for a code only when none was given (D49).
        if self.error is not None:
            if self.status is JobStatus.DONE:
                raise ValueError("error is a failure field and cannot be sent with DONE")
            self.message = self.message or self.error
            self.error_code = self.error_code or "WORKER_REPORTED_FAILURE"

        # Refused here rather than stored: a DONE job with no summary is a
        # document the client will poll to completion and get nothing from.
        # `.strip()` and not merely emptiness -- a summary of three spaces is
        # blank to every reader, and passes any check that only asks if a value
        # was sent.
        if self.status is JobStatus.DONE and not (self.summary or "").strip():
            raise ValueError("summary is required when status is DONE, and cannot be blank")
        if self.status is JobStatus.FAILED and not (self.error_code or "").strip():
            raise ValueError("error_code is required when status is FAILED, and cannot be blank")
        return self


class CompleteResponse(BaseModel):
    """The job as it now stands -- after this call, or already before it."""

    id: int
    document_id: int
    status: JobStatus
    attempts: int
    summary: str | None = None
    error_code: str | None = None
    message: str | None = None
    finished_at: datetime | None = None
    applied: bool = Field(
        description=(
            "False when this call changed nothing because the job was already in the state "
            "being asked for -- a redelivery of the same completion. Still `200`: at-least-once "
            "delivery makes the repeat normal, not an error (D18)."
        )
    )
