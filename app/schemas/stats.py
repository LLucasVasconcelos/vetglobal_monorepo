from pydantic import BaseModel, Field


class Stat(BaseModel):
    """How many jobs sit in each state right now."""

    ENQUEUED: int = 0
    PROCESSING: int = 0
    DONE: int = 0
    FAILED: int = 0


class Durations(BaseModel):
    """One of the two clocks of a job's life, in seconds.

    `p95` and not only the average: an average hides the tail, and the tail is
    what a person waiting on a poll actually experiences.
    """

    samples: int = Field(description="How many jobs this was computed from.")
    average_seconds: float | None = None
    p95_seconds: float | None = None


class TenantLoad(BaseModel):
    tenant_id: int
    in_flight: int = Field(description="Jobs of this tenant currently ENQUEUED or PROCESSING.")


class QueueStats(BaseModel):
    jobs: Stat
    failures: dict[str, int] = Field(
        default_factory=dict, description="Count per `error_code`, across every clinic."
    )
    retried: int = Field(description="Jobs that took more than one attempt — dead workers.")
    waiting: Durations = Field(description="enqueued_at → claimed_at. Grows when workers are few.")
    processing: Durations = Field(
        description="claimed_at → finished_at. Grows when the work itself got slower."
    )
    busiest_tenants: list[TenantLoad] = Field(
        default_factory=list,
        description="Who is holding the live queue right now — the fairness question.",
    )
