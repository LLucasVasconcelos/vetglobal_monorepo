"""The queue: claiming a job, and closing it exactly once (D21, D18, D27).

These tests run against real Postgres on purpose. `SKIP LOCKED` has no behaviour
at all outside a real database, and it is the single line that makes this a
queue rather than a table two workers fight over.
"""

import asyncio

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.base import PG_INT_MAX
from app.models import JobStatus
from app.schemas.job import MAX_SUMMARY_LENGTH
from tests.conftest import auth

NOTE = (
    b"Patient Rex, 4 years old, male, neutered. Presented with vomiting for two days. "
    b"Hydration adequate. Abdominal palpation unremarkable. Started on a bland diet."
)

INTERNAL = {"X-Internal-Token": settings.internal_token}


async def enqueue(client, token: str, content: bytes = NOTE, pet_name: str = "Rex") -> dict:
    """Upload a document and return `{document_id, job_id, ...}` -- a job in ENQUEUED."""
    pet = await client.post(
        "/pets", json={"name": pet_name, "owner_name": "Ana"}, headers=auth(token)
    )
    response = await client.post(
        f"/pets/{pet.json()['id']}/documents",
        files={"file": ("consultation.txt", content, "text/plain")},
        headers=auth(token),
    )
    assert response.status_code == 202
    return {"pet_id": pet.json()["id"], **response.json()}


async def expire_lease(db, job_id: int) -> None:
    """Age a lease past its deadline, the way a killed worker would leave it."""
    await db.execute(
        text("UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = :id"),
        {"id": job_id},
    )
    await db.commit()


# --- the shared secret (D27) ------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Token": "wrong"}], ids=["missing", "wrong"])
async def test_claim_without_the_internal_token_is_401(client, headers):
    response = await client.post("/internal/jobs/claim", headers=headers)

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_INTERNAL_TOKEN"


async def test_complete_without_the_internal_token_is_401(client, aurora_token):
    """The route that matters most: without the guard, anyone could write any
    text into any clinic's record and call it a summary."""
    upload = await enqueue(client, aurora_token)

    response = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "DONE", "summary": "anything at all"},
    )

    assert response.status_code == 401


async def test_a_jwt_does_not_open_the_internal_routes(client, aurora_token):
    """A tenant's own token is the wrong key here, and is refused like any other."""
    response = await client.post("/internal/jobs/claim", headers=auth(aurora_token))

    assert response.status_code == 401


# --- claim ------------------------------------------------------------------


async def test_claim_on_an_empty_queue_is_204(client):
    response = await client.post("/internal/jobs/claim", headers=INTERNAL)

    assert response.status_code == 204


async def test_claim_returns_the_job_with_the_text_to_summarize(client, aurora_token):
    upload = await enqueue(client, aurora_token)

    response = await client.post("/internal/jobs/claim", headers=INTERNAL)

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == upload["job_id"]
    assert body["document_id"] == upload["document_id"]
    assert body["attempt"] == 1
    assert body["filename"] == "consultation.txt"
    # The whole point of the claim: the worker can start without a second call.
    assert body["content"] == NOTE.decode()
    assert body["lease_expires_at"] is not None


async def test_a_claimed_job_is_processing_and_is_not_claimed_again(client, aurora_token):
    await enqueue(client, aurora_token)

    first = await client.post("/internal/jobs/claim", headers=INTERNAL)
    second = await client.post("/internal/jobs/claim", headers=INTERNAL)

    assert first.status_code == 200
    assert second.status_code == 204, "the same job was handed out twice"


async def test_the_owner_sees_the_job_move_to_processing(client, aurora_token):
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)

    document = await client.get(f"/documents/{upload['document_id']}", headers=auth(aurora_token))

    assert document.json()["job"]["status"] == JobStatus.PROCESSING
    assert document.json()["job"]["attempts"] == 1


async def test_claim_is_oldest_first(client, aurora_token):
    older = await enqueue(client, aurora_token, content=NOTE + b" First.", pet_name="Rex")
    await enqueue(client, aurora_token, content=NOTE + b" Second.", pet_name="Mel")

    response = await client.post("/internal/jobs/claim", headers=INTERNAL)

    assert response.json()["job_id"] == older["job_id"]


async def test_two_simultaneous_claims_get_two_different_jobs(client, aurora_token):
    """`SKIP LOCKED`, which is the whole reason the table can be a queue.

    Without it the second claim blocks on the first one's row lock and, once it
    is released, either waits pointlessly or hands out the same job twice.
    """
    first = await enqueue(client, aurora_token, content=NOTE + b" First.", pet_name="Rex")
    second = await enqueue(client, aurora_token, content=NOTE + b" Second.", pet_name="Mel")

    a, b = await asyncio.gather(
        client.post("/internal/jobs/claim", headers=INTERNAL),
        client.post("/internal/jobs/claim", headers=INTERNAL),
    )

    assert a.status_code == 200 and b.status_code == 200
    assert {a.json()["job_id"], b.json()["job_id"]} == {first["job_id"], second["job_id"]}


# --- complete (D18) ---------------------------------------------------------


async def test_complete_marks_the_job_done_and_stores_the_summary(client, aurora_token):
    upload = await enqueue(client, aurora_token)
    claim = (await client.post("/internal/jobs/claim", headers=INTERNAL)).json()

    response = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "DONE", "summary": "Vomiting, hydrated, bland diet.", "attempt": 1},
        headers=INTERNAL,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == JobStatus.DONE
    assert body["applied"] is True
    assert body["finished_at"] is not None

    # And the clinic sees it, which is the only view that actually matters.
    document = await client.get(f"/documents/{upload['document_id']}", headers=auth(aurora_token))
    assert document.json()["job"]["summary"] == "Vomiting, hydrated, bland diet."
    assert claim["attempt"] == 1


async def test_completing_the_same_job_twice_is_200_and_changes_nothing(client, aurora_token):
    """At-least-once delivery guarantees this happens. It is not an error."""
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)
    payload = {"status": "DONE", "summary": "Vomiting, hydrated, bland diet."}

    first = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete", json=payload, headers=INTERNAL
    )
    second = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete", json=payload, headers=INTERNAL
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["applied"] is True
    assert second.json()["applied"] is False, "a redelivery must not be reported as a new write"
    assert second.json()["finished_at"] == first.json()["finished_at"]


async def test_contradicting_a_finished_job_is_409(client, aurora_token):
    """`FAILED` over a job already `DONE` is not a retry, it is a bug. Answering
    200 here would hide it, and would let a summary be replaced by an error."""
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)
    await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "DONE", "summary": "Vomiting, hydrated, bland diet."},
        headers=INTERNAL,
    )

    response = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "FAILED", "error_code": "EXTRACTION_FAILED", "message": "boom"},
        headers=INTERNAL,
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "JOB_NOT_PROCESSING"

    document = await client.get(f"/documents/{upload['document_id']}", headers=auth(aurora_token))
    assert document.json()["job"]["summary"] == "Vomiting, hydrated, bland diet."


async def test_completing_a_job_nobody_claimed_is_409(client, aurora_token):
    upload = await enqueue(client, aurora_token)

    response = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "DONE", "summary": "Summary of work never done."},
        headers=INTERNAL,
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "JOB_NOT_PROCESSING"


async def test_completing_an_unknown_job_is_404(client):
    response = await client.post(
        "/internal/jobs/9999/complete",
        json={"status": "DONE", "summary": "Summary of a job that does not exist."},
        headers=INTERNAL,
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == "JOB_NOT_FOUND"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "DONE"},
        {"status": "FAILED", "message": "something went wrong"},
        {"status": "PROCESSING"},
        {"status": "ENQUEUED"},
    ],
    ids=["done-without-summary", "failed-without-error-code", "processing", "enqueued"],
)
async def test_a_verdict_that_says_nothing_useful_is_422(client, aurora_token, payload):
    """A DONE job with no summary is a document the client polls to completion
    and gets nothing from; only terminal states can be completed at all."""
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)

    response = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete", json=payload, headers=INTERNAL
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


async def test_failure_reaches_the_owner_in_the_d22_shape(client, aurora_token):
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)

    await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={
            "status": "FAILED",
            "error_code": "EXTRACTION_FAILED",
            "message": "Could not read the document.",
        },
        headers=INTERNAL,
    )

    job = (
        await client.get(f"/documents/{upload['document_id']}", headers=auth(aurora_token))
    ).json()["job"]
    assert job["status"] == JobStatus.FAILED
    assert job["error_code"] == "EXTRACTION_FAILED"
    assert job["summary"] is None


# --- the unhappy path: nothing predictable may arrive as a 500 --------------
#
# Every case below was a real 500 before the fix, reproduced against the running
# server. A 500 is supposed to mean a bug on our side (invariant 5), so each one
# was both a wrong answer and a false alarm.


@pytest.mark.parametrize(
    "job_id",
    [PG_INT_MAX + 1, 9_999_999_999, 0, -5],
    ids=["over-int4", "far-over-int4", "zero", "negative"],
)
async def test_an_id_no_row_could_have_is_422_and_not_a_crash(client, job_id):
    """`2147483648` does not fit the `integer` primary key. Postgres raises, and
    the error is not an `IntegrityError`, so it used to escape every handler."""
    response = await client.post(
        f"/internal/jobs/{job_id}/complete",
        json={"status": "DONE", "summary": "Summary for an id that cannot exist."},
        headers=INTERNAL,
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


async def test_an_attempt_above_the_column_width_is_422(client, aurora_token):
    """Same overflow, one layer in: `attempt` is compared against an `integer`."""
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)

    response = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "DONE", "summary": "A summary.", "attempt": PG_INT_MAX + 1},
        headers=INTERNAL,
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "token",
    # Sent as bytes, which is what the wire actually carries: httpx refuses to
    # encode a non-ASCII `str` into a header, while `curl -H 'X-Internal-Token:
    # café'` puts the bytes on the socket without asking. Starlette then decodes
    # them as latin-1 and hands the guard a `str` it cannot compare.
    [b"caf\xe9", b"\xd1\x82\xd0\xbe\xd0\xba\xd0\xb5\xd0\xbd", b"", b" ", b"\x00", b"x" * 5000],
    ids=["accented", "cyrillic", "empty", "space", "null-byte", "very-long"],
)
async def test_no_token_value_can_crash_the_guard(client, token):
    """`compare_digest` raises `TypeError` on a `str` holding anything non-ASCII.

    So the check meant to *stop* an unauthenticated caller crashed on their
    input: 500 where every other rejection answers 401 -- which also told them
    their token had not even been compared.
    """
    response = await client.post("/internal/jobs/claim", headers={"X-Internal-Token": token})

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_INTERNAL_TOKEN"


async def test_a_token_that_is_a_prefix_of_the_real_one_is_refused(client):
    response = await client.post(
        "/internal/jobs/claim", headers={"X-Internal-Token": settings.internal_token[:-1]}
    )

    assert response.status_code == 401


async def test_a_misspelled_field_is_refused_instead_of_ignored(client, aurora_token):
    """`atempt` would otherwise be dropped silently, turning the fencing token
    off with nothing in the answer saying so."""
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)

    response = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "DONE", "summary": "A summary.", "atempt": 1},
        headers=INTERNAL,
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "DONE", "summary": "   "},
        {"status": "DONE", "summary": "\n\t "},
        {"status": "FAILED", "error_code": "  "},
        {"status": "DONE", "summary": "x" * (MAX_SUMMARY_LENGTH + 1)},
    ],
    ids=["blank-summary", "whitespace-summary", "blank-error-code", "oversized-summary"],
)
async def test_a_blank_or_runaway_verdict_is_refused(client, aurora_token, payload):
    """A summary of three spaces is blank to every reader, and passes any check
    that only asks whether a value was sent."""
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)

    response = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete", json=payload, headers=INTERNAL
    )

    assert response.status_code == 422

    # And the job is untouched -- a refused verdict must not half-close a job.
    document = await client.get(f"/documents/{upload['document_id']}", headers=auth(aurora_token))
    assert document.json()["job"]["status"] == JobStatus.PROCESSING


async def test_two_simultaneous_completes_apply_exactly_once(client, aurora_token):
    """The race D18 exists for. Both calls answer 200 -- but only one of them
    wrote, and they agree on the result."""
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)
    payload = {"status": "DONE", "summary": "Vomiting 2d, hydrated, bland diet."}

    a, b = await asyncio.gather(
        client.post(f"/internal/jobs/{upload['job_id']}/complete", json=payload, headers=INTERNAL),
        client.post(f"/internal/jobs/{upload['job_id']}/complete", json=payload, headers=INTERNAL),
    )

    assert a.status_code == b.status_code == 200
    assert [a.json()["applied"], b.json()["applied"]].count(True) == 1
    assert a.json()["finished_at"] == b.json()["finished_at"]


async def test_a_job_nobody_claimed_reports_the_status_not_the_attempt(client, aurora_token):
    """Sending `attempt` for an unclaimed job is a worker bug, and the useful
    answer names the state -- not a lease that never existed."""
    upload = await enqueue(client, aurora_token)

    response = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "DONE", "summary": "A summary.", "attempt": 1},
        headers=INTERNAL,
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "JOB_NOT_PROCESSING"
    assert "ENQUEUED" in response.json()["message"]


# --- the lease: what happens when a worker dies (D21) -----------------------


async def test_an_expired_lease_is_reclaimed_on_the_next_claim(client, aurora_token, db):
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)
    await expire_lease(db, upload["job_id"])

    response = await client.post("/internal/jobs/claim", headers=INTERNAL)

    assert response.status_code == 200, "a job whose worker died was stranded"
    assert response.json()["job_id"] == upload["job_id"]
    assert response.json()["attempt"] == 2


async def test_a_lease_that_expires_with_no_attempts_left_fails_the_job(
    client, aurora_token, db
):
    """The *job* timeout, which is not the poll timeout (invariant 6). After
    `JOB_MAX_ATTEMPTS` the document stops being retried and says so."""
    upload = await enqueue(client, aurora_token)

    for _ in range(settings.job_max_attempts):
        claim = await client.post("/internal/jobs/claim", headers=INTERNAL)
        assert claim.status_code == 200
        await expire_lease(db, upload["job_id"])

    assert (await client.post("/internal/jobs/claim", headers=INTERNAL)).status_code == 204

    job = (
        await client.get(f"/documents/{upload['document_id']}", headers=auth(aurora_token))
    ).json()["job"]
    assert job["status"] == JobStatus.FAILED
    assert job["error_code"] == "PROCESSING_TIMEOUT"
    assert job["attempts"] == settings.job_max_attempts


async def test_a_worker_that_lost_its_lease_cannot_overwrite_the_new_one(
    client, aurora_token, db
):
    """The fencing token. The first worker stalled, its job was given to a
    second one, and its late answer is about work somebody else already redid.
    Without `attempt` in the `WHERE`, that stale result would simply win."""
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)
    await expire_lease(db, upload["job_id"])
    await client.post("/internal/jobs/claim", headers=INTERNAL)  # attempt 2 owns it now

    stale = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "DONE", "summary": "Written by the worker that stalled.", "attempt": 1},
        headers=INTERNAL,
    )

    assert stale.status_code == 409
    assert stale.json()["error_code"] == "JOB_LEASE_LOST"

    fresh = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "DONE", "summary": "Written by the worker that finished.", "attempt": 2},
        headers=INTERNAL,
    )
    assert fresh.status_code == 200
    assert fresh.json()["summary"] == "Written by the worker that finished."

    # And now that the job is finished, the stale worker retrying is still told
    # its result is stale -- not "job is DONE, so it cannot be completed as
    # DONE", which is what comparing only the status would have said.
    late = await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "DONE", "summary": "Written by the worker that stalled.", "attempt": 1},
        headers=INTERNAL,
    )
    assert late.status_code == 409
    assert late.json()["error_code"] == "JOB_LEASE_LOST"


# --- back around: a failed job can be retried by re-uploading (D24) ---------


async def test_reuploading_after_a_failure_creates_a_claimable_job(client, aurora_token):
    upload = await enqueue(client, aurora_token)
    await client.post("/internal/jobs/claim", headers=INTERNAL)
    await client.post(
        f"/internal/jobs/{upload['job_id']}/complete",
        json={"status": "FAILED", "error_code": "EXTRACTION_FAILED", "message": "boom"},
        headers=INTERNAL,
    )

    retry = await client.post(
        f"/pets/{upload['pet_id']}/documents",
        files={"file": ("consultation.txt", NOTE, "text/plain")},
        headers=auth(aurora_token),
    )

    assert retry.status_code == 200, "same content, so no second document"
    assert retry.json()["job_id"] != upload["job_id"]

    claim = await client.post("/internal/jobs/claim", headers=INTERNAL)
    assert claim.status_code == 200
    assert claim.json()["job_id"] == retry.json()["job_id"]
    assert claim.json()["attempt"] == 1, "a new job starts its own count"


async def test_an_exhausted_lease_is_only_noticed_when_somebody_claims(client, aurora_token, db):
    """The cost of having no sweeper, pinned so it is a decision and not a
    surprise (D21).

    The sweep runs inside the claim, because the queue is only ever read by
    workers: a job stuck in `PROCESSING` matters exactly when somebody comes
    looking for work. The price is this -- with an idle queue, a job whose
    worker died stays `PROCESSING` for as long as nobody asks for work, and a
    poll on it keeps timing out in the meantime. Correct, and the alternative is
    a second process to deploy, supervise and explain.
    """
    upload = await enqueue(client, aurora_token)
    for _ in range(settings.job_max_attempts):
        await client.post("/internal/jobs/claim", headers=INTERNAL)
        await expire_lease(db, upload["job_id"])

    # Time passes, the API keeps serving, and nobody claims.
    document = await client.get(f"/documents/{upload['document_id']}", headers=auth(aurora_token))
    assert document.json()["job"]["status"] == JobStatus.PROCESSING
    assert document.json()["job"]["error_code"] is None

    # One claim -- which finds nothing to hand out -- is what settles it.
    assert (await client.post("/internal/jobs/claim", headers=INTERNAL)).status_code == 204

    document = await client.get(f"/documents/{upload['document_id']}", headers=auth(aurora_token))
    assert document.json()["job"]["status"] == JobStatus.FAILED
    assert document.json()["job"]["error_code"] == "PROCESSING_TIMEOUT"
