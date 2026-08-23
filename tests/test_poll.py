"""The long poll: `LISTEN/NOTIFY`, the recheck behind it, and the two clocks
(D13, D19, D20, D23).

Several tests here turn the recheck **off** -- `poll_recheck_seconds` far past
the poll's own timeout -- so the only thing that can possibly answer them is a
notification. Left at its default, a broken `NOTIFY` would still produce a green
suite five seconds later, and the fast path this whole design exists for would
be untested.

`test_a_job_finishing_between_the_read_and_the_wait_is_not_missed` is the one to
keep: it is invariant 2 written as an executable assertion.
"""

import asyncio
import json
import time

import asyncpg
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.base import PG_INT_MAX
from app.db.listener import CHANNEL
from app.models import JobStatus
from app.services import poll as poll_service

# The same document and queue helpers the queue tests use. Imported rather than
# copied: a second definition of "upload a document" is a second thing to keep
# in step with the upload route.
from tests.conftest import auth
from tests.test_internal_jobs import INTERNAL, NOTE, enqueue, expire_lease

SUMMARY = "Vomiting for two days, hydrated, started on a bland diet."

# Longer than any poll in this file, so the recheck cannot be what answers.
NO_RECHECK = 300


async def poll(client, token: str, document_id: int, after_job_id: int = 0):
    return await client.get(
        f"/documents/{document_id}/poll",
        params={"after_job_id": after_job_id},
        headers=auth(token),
    )


async def claim(client) -> dict:
    response = await client.post("/internal/jobs/claim", headers=INTERNAL)
    assert response.status_code == 200
    return response.json()


async def complete(client, job_id: int, payload: dict | None = None):
    return await client.post(
        f"/internal/jobs/{job_id}/complete",
        json=payload or {"status": "DONE", "summary": SUMMARY},
        headers=INTERNAL,
    )


@pytest.fixture
def clocks(monkeypatch):
    """Set the two poll knobs for a test, in seconds."""

    def set(timeout: float, recheck: float):
        monkeypatch.setattr(settings, "poll_timeout_seconds", timeout)
        monkeypatch.setattr(settings, "poll_recheck_seconds", recheck)

    return set


@pytest.fixture(autouse=True)
async def listening():
    """No test starts before this instance is actually subscribed.

    Otherwise the first test of a run races the connection being opened and is
    answered by a recheck -- which, in the tests that turn the recheck off,
    means hanging until the poll times out.
    """
    await asyncio.wait_for(poll_service.document_events.wait_until_listening(), timeout=10)


# --- already finished: the poll must not wait at all ------------------------


@pytest.mark.parametrize(
    "verdict",
    [
        {"status": "DONE", "summary": SUMMARY},
        {"status": "FAILED", "error_code": "EXTRACTION_FAILED", "message": "Unreadable."},
    ],
    ids=["done", "failed"],
)
async def test_a_job_that_finished_before_the_poll_started_answers_at_once(
    client, aurora_token, clocks, verdict
):
    """The other half of invariant 2. The notification for this job was emitted
    before anyone subscribed and is long gone -- the read is what answers."""
    clocks(timeout=25, recheck=NO_RECHECK)
    upload = await enqueue(client, aurora_token)
    await claim(client)
    await complete(client, upload["job_id"], verdict)

    started = time.perf_counter()
    response = await poll(client, aurora_token, upload["document_id"])
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    body = response.json()
    assert body["timed_out"] is False
    assert body["awaiting_job_id"] == upload["job_id"]
    assert body["result"]["job"]["status"] == verdict["status"]
    assert elapsed < 1, "a job that was already finished should not have been waited for"


async def test_a_done_job_carries_its_summary_out_of_the_poll(client, aurora_token, clocks):
    clocks(timeout=25, recheck=NO_RECHECK)
    upload = await enqueue(client, aurora_token)
    await claim(client)
    await complete(client, upload["job_id"])

    body = (await poll(client, aurora_token, upload["document_id"])).json()

    assert body["result"]["job"]["summary"] == SUMMARY
    assert body["result"]["job"]["finished_at"] is not None
    assert body["result"]["job"]["error_code"] is None


# --- the fast path: NOTIFY, with the recheck turned off ---------------------


async def test_an_open_poll_wakes_the_instant_the_job_is_completed(client, aurora_token, clocks):
    """The bonus point of the assignment, and the reason for the whole listener.

    The recheck is disabled, so answering at all proves the `NOTIFY` arrived:
    without it this would sit here for the full 25 seconds.
    """
    clocks(timeout=25, recheck=NO_RECHECK)
    upload = await enqueue(client, aurora_token)
    await claim(client)

    started = time.perf_counter()
    waiting = asyncio.create_task(poll(client, aurora_token, upload["document_id"]))
    await asyncio.sleep(0.2)  # let the poll subscribe and read PROCESSING first
    await complete(client, upload["job_id"])
    response = await asyncio.wait_for(waiting, timeout=10)
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert response.json()["result"]["job"]["summary"] == SUMMARY
    assert elapsed < 3, "the poll did not wake on the notification"


async def test_a_job_finishing_between_the_read_and_the_wait_is_not_missed(
    client, aurora_token, clocks, monkeypatch
):
    """Invariant 2, as an assertion.

    The job is completed *after* the poll has read `PROCESSING` and before it
    starts waiting -- the exact instant the wrong order loses the notification.
    Subscribe-then-read survives it because the waiter was already registered
    when the `NOTIFY` was delivered, so the event is set before anything waits
    on it. Read-then-subscribe would hang here until the poll timed out, which
    with the recheck disabled is the full 25 seconds.
    """
    clocks(timeout=25, recheck=NO_RECHECK)
    upload = await enqueue(client, aurora_token)
    await claim(client)

    read_job = poll_service._read_job
    finished = False

    async def complete_it_right_after_the_read(db, job_id):
        nonlocal finished
        view = await read_job(db, job_id)
        if not finished:
            finished = True
            assert (await complete(client, job_id)).status_code == 200
        return view

    monkeypatch.setattr(poll_service, "_read_job", complete_it_right_after_the_read)

    started = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            poll(client, aurora_token, upload["document_id"]), timeout=10
        )
    except TimeoutError:
        pytest.fail("invariant 2: the poll subscribed after reading and lost the notification")
    elapsed = time.perf_counter() - started

    assert finished, "the test did not exercise the window it exists for"
    assert response.json()["timed_out"] is False
    assert response.json()["result"]["job"]["status"] == JobStatus.DONE
    assert elapsed < 3, "the notification was emitted with nobody listening"


async def test_two_polls_on_the_same_document_both_wake(client, aurora_token, clocks):
    """The fan-out. One notification, every waiter on that document."""
    clocks(timeout=25, recheck=NO_RECHECK)
    upload = await enqueue(client, aurora_token)
    await claim(client)

    first = asyncio.create_task(poll(client, aurora_token, upload["document_id"]))
    second = asyncio.create_task(poll(client, aurora_token, upload["document_id"]))
    await asyncio.sleep(0.2)
    await complete(client, upload["job_id"])

    a, b = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)

    assert a.json()["result"]["job"]["summary"] == b.json()["result"]["job"]["summary"] == SUMMARY


async def test_a_completion_on_another_document_does_not_answer_this_poll(
    client, aurora_token, clocks
):
    """The fan-out is keyed by document, so a busy queue does not wake every
    poll on the instance every time any job anywhere finishes."""
    clocks(timeout=2, recheck=NO_RECHECK)
    mine = await enqueue(client, aurora_token, content=NOTE + b" Mine.", pet_name="Rex")
    other = await enqueue(client, aurora_token, content=NOTE + b" Other.", pet_name="Mel")
    await claim(client)  # mine, oldest first
    await claim(client)  # other

    waiting = asyncio.create_task(poll(client, aurora_token, mine["document_id"]))
    await asyncio.sleep(0.2)
    await complete(client, other["job_id"])
    response = await asyncio.wait_for(waiting, timeout=10)

    assert response.json()["timed_out"] is True
    assert response.json()["awaiting_job_id"] == mine["job_id"]


# --- the safety net: correctness does not depend on the notification --------


async def test_the_recheck_answers_when_no_notification_was_ever_sent(
    client, aurora_token, clocks, db
):
    """`NOTIFY` is not persistent, so a notification can simply be lost -- the
    listener reconnecting, another instance holding the waiter, a job closed by
    something other than `complete`. The job is finished here behind the API's
    back, with no notification at all, and the poll still answers correctly.

    This is the test that says the design is correct rather than merely fast.
    """
    clocks(timeout=25, recheck=1)
    upload = await enqueue(client, aurora_token)
    await claim(client)

    started = time.perf_counter()
    waiting = asyncio.create_task(poll(client, aurora_token, upload["document_id"]))
    await asyncio.sleep(0.2)
    await db.execute(
        text(
            "UPDATE jobs SET status = 'DONE', summary = :s, finished_at = now() WHERE id = :id"
        ),
        {"s": "Closed without telling anybody.", "id": upload["job_id"]},
    )
    await db.commit()
    response = await asyncio.wait_for(waiting, timeout=15)
    elapsed = time.perf_counter() - started

    assert response.json()["result"]["job"]["summary"] == "Closed without telling anybody."
    assert elapsed < 5, "the recheck did not pick the job up"


async def test_the_lease_running_out_wakes_an_open_poll(client, aurora_token, clocks, db):
    """The *other* clock (D21, D43). A job that spent its attempts is failed by
    the next claim, which is the second place a job reaches a terminal state --
    and it announces it too, so the poll does not sit out its recheck for the
    one answer the client least wants to wait for.
    """
    clocks(timeout=25, recheck=NO_RECHECK)
    upload = await enqueue(client, aurora_token)
    for _ in range(settings.job_max_attempts):
        await claim(client)
        await expire_lease(db, upload["job_id"])

    waiting = asyncio.create_task(poll(client, aurora_token, upload["document_id"]))
    await asyncio.sleep(0.2)
    # Nothing to hand out; the sweep inside the claim is what closes the job.
    assert (await client.post("/internal/jobs/claim", headers=INTERNAL)).status_code == 204
    response = await asyncio.wait_for(waiting, timeout=10)

    body = response.json()
    assert body["timed_out"] is False, "the poll waited for its recheck instead"
    assert body["result"]["job"]["status"] == JobStatus.FAILED
    assert body["result"]["job"]["error_code"] == "PROCESSING_TIMEOUT"


# --- timing out is not failing (D19, invariant 6) ---------------------------


async def test_a_poll_that_runs_out_of_time_is_200_with_the_cursor(client, aurora_token, clocks):
    clocks(timeout=1, recheck=1)
    upload = await enqueue(client, aurora_token)
    await claim(client)

    response = await poll(client, aurora_token, upload["document_id"])

    assert response.status_code == 200, "a timeout is not an error"
    assert response.json() == {
        "result": None,
        "awaiting_job_id": upload["job_id"],
        "timed_out": True,
    }

    # And the job is untouched: the request's clock ran out, not the job's.
    document = await client.get(
        f"/documents/{upload['document_id']}", headers=auth(aurora_token)
    )
    assert document.json()["job"]["status"] == JobStatus.PROCESSING


async def test_a_poll_that_timed_out_can_be_resumed_with_what_it_returned(
    client, aurora_token, clocks
):
    """The point of returning the cursor: the client re-calls with the body it
    just got, and never has to remember anything between requests."""
    clocks(timeout=1, recheck=1)
    upload = await enqueue(client, aurora_token)
    await claim(client)

    first = (await poll(client, aurora_token, upload["document_id"])).json()
    await complete(client, first["awaiting_job_id"])
    second = (
        await poll(client, aurora_token, upload["document_id"], first["awaiting_job_id"])
    ).json()

    assert second["timed_out"] is False
    assert second["awaiting_job_id"] == first["awaiting_job_id"]
    assert second["result"]["job"]["summary"] == SUMMARY


# --- which job is being waited for (D20, D23) -------------------------------


async def test_after_job_id_zero_resolves_to_the_latest_job(client, aurora_token, clocks):
    """A failed document that was re-uploaded has two jobs (D24). `0` means the
    second one -- the attempt actually under way."""
    clocks(timeout=1, recheck=1)
    upload = await enqueue(client, aurora_token)
    await claim(client)
    await complete(
        client,
        upload["job_id"],
        {"status": "FAILED", "error_code": "EXTRACTION_FAILED", "message": "Unreadable."},
    )
    retry = (
        await client.post(
            f"/pets/{upload['pet_id']}/documents",
            files={"file": ("consultation.txt", NOTE, "text/plain")},
            headers=auth(aurora_token),
        )
    ).json()

    body = (await poll(client, aurora_token, upload["document_id"])).json()

    assert retry["job_id"] != upload["job_id"]
    assert body["awaiting_job_id"] == retry["job_id"]
    assert body["timed_out"] is True, "the latest job is still enqueued"


async def test_an_explicit_job_id_waits_for_that_job_and_not_the_latest(
    client, aurora_token, clocks
):
    """A client holding the `job_id` of its own upload keeps getting an answer
    about *that* attempt, even after somebody re-uploaded and moved 'latest'."""
    clocks(timeout=1, recheck=1)
    upload = await enqueue(client, aurora_token)
    await claim(client)
    await complete(
        client,
        upload["job_id"],
        {"status": "FAILED", "error_code": "EXTRACTION_FAILED", "message": "Unreadable."},
    )
    await client.post(
        f"/pets/{upload['pet_id']}/documents",
        files={"file": ("consultation.txt", NOTE, "text/plain")},
        headers=auth(aurora_token),
    )

    body = (await poll(client, aurora_token, upload["document_id"], upload["job_id"])).json()

    assert body["awaiting_job_id"] == upload["job_id"]
    assert body["result"]["job"]["error_code"] == "EXTRACTION_FAILED"


async def test_a_job_belonging_to_another_document_is_404(client, aurora_token, clocks):
    """Ids are global, documents are not: `?after_job_id=` is not a way to watch
    a job that is none of this document's business."""
    clocks(timeout=1, recheck=1)
    mine = await enqueue(client, aurora_token, content=NOTE + b" Mine.", pet_name="Rex")
    other = await enqueue(client, aurora_token, content=NOTE + b" Other.", pet_name="Mel")

    response = await poll(client, aurora_token, mine["document_id"], other["job_id"])

    assert response.status_code == 404
    assert response.json()["error_code"] == "JOB_NOT_FOUND"


# --- isolation, and the unhappy path ----------------------------------------


async def test_polling_another_clinics_document_is_404(client, aurora_token, boreal_token, clocks):
    """`403` would confirm the document exists, which is itself the leak (D26)."""
    clocks(timeout=1, recheck=1)
    upload = await enqueue(client, boreal_token)

    started = time.perf_counter()
    response = await poll(client, aurora_token, upload["document_id"])

    assert response.status_code == 404
    assert response.json()["error_code"] == "DOCUMENT_NOT_FOUND"
    # Refused before waiting: holding the connection open would answer the
    # question the 404 exists to avoid answering.
    assert time.perf_counter() - started < 1


async def test_polling_a_document_that_does_not_exist_is_404(client, aurora_token, clocks):
    clocks(timeout=1, recheck=1)

    response = await poll(client, aurora_token, 9999)

    assert response.status_code == 404
    assert response.json()["error_code"] == "DOCUMENT_NOT_FOUND"


async def test_polling_without_a_token_is_401(client):
    response = await client.get("/documents/1/poll")

    assert response.status_code == 401
    assert response.json()["error_code"] == "NOT_AUTHENTICATED"


@pytest.mark.parametrize(
    "after_job_id",
    [-1, PG_INT_MAX + 1, "abc"],
    ids=["negative", "over-int4", "not-a-number"],
)
async def test_an_after_job_id_no_row_could_have_is_422(client, aurora_token, after_job_id):
    """Same class of bug as the ids of D38: predictable client input must not
    reach the database and come back as a 500 (invariant 5)."""
    response = await client.get(
        "/documents/1/poll",
        params={"after_job_id": after_job_id},
        headers=auth(aurora_token),
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


# --- the listener itself, as infrastructure ---------------------------------


async def listener_backend_pid(db) -> int:
    """The Postgres backend holding this instance's `LISTEN`.

    Found by its last statement: `add_listener` issues `LISTEN "..."` and that
    connection never runs anything else, so it is the only backend on this
    database whose current query starts that way.
    """
    pid = await db.scalar(
        text(
            "SELECT pid FROM pg_stat_activity "
            "WHERE datname = current_database() AND query ILIKE 'LISTEN%'"
        )
    )
    await db.rollback()
    assert pid is not None, "no backend is listening -- the lifespan did not run"
    return pid


async def test_a_notification_from_another_connection_wakes_the_poll(
    client, aurora_token, clocks, db
):
    """What "stateless across instances" means in practice, and the reason the
    fan-out in memory does not break invariant 1.

    The job is closed and announced from a connection this process does not
    serve requests on -- the shape of a second API instance behind a load
    balancer, or of a worker writing straight to the database. The waiter is
    here, in this process's memory, and wakes anyway: what binds the two is the
    Postgres channel, not shared memory. With the recheck off, nothing else
    could have answered.
    """
    clocks(timeout=25, recheck=NO_RECHECK)
    upload = await enqueue(client, aurora_token)
    await claim(client)

    waiting = asyncio.create_task(poll(client, aurora_token, upload["document_id"]))
    await asyncio.sleep(0.2)

    outsider = await asyncpg.connect(
        user=settings.db_user,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    )
    try:
        await outsider.execute(
            "UPDATE jobs SET status = 'DONE', summary = $1, finished_at = now() WHERE id = $2",
            "Written by another instance entirely.",
            upload["job_id"],
        )
        await outsider.execute(
            "SELECT pg_notify($1, $2)",
            CHANNEL,
            json.dumps(
                {
                    "document_id": upload["document_id"],
                    "job_id": upload["job_id"],
                    "status": "DONE",
                }
            ),
        )
    finally:
        await outsider.close()

    response = await asyncio.wait_for(waiting, timeout=10)

    assert response.json()["result"]["job"]["summary"] == "Written by another instance entirely."


async def test_the_listener_reconnects_after_its_connection_is_killed(
    client, aurora_token, clocks, db
):
    """The database restarts, or an admin kills the backend. The fast path has
    to come back on its own -- a listener that stays down would leave every poll
    on this instance silently degraded to the recheck for the life of the
    process, which is the kind of failure nobody notices until a demo.

    Killing it is also a live rehearsal of the safety net: while the connection
    is gone, correctness is entirely the recheck's job.
    """
    clocks(timeout=25, recheck=NO_RECHECK)
    pid = await listener_backend_pid(db)

    await db.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    await db.commit()

    for _ in range(100):
        if not poll_service.document_events._listening.is_set():
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("the listener did not notice its connection had been terminated")

    await asyncio.wait_for(poll_service.document_events.wait_until_listening(), timeout=20)

    # And it is a working listener, not merely a connected one.
    reborn = await listener_backend_pid(db)
    assert reborn != pid, "the same backend cannot have come back from the dead"

    upload = await enqueue(client, aurora_token)
    await claim(client)
    waiting = asyncio.create_task(poll(client, aurora_token, upload["document_id"]))
    await asyncio.sleep(0.2)
    await complete(client, upload["job_id"])

    response = await asyncio.wait_for(waiting, timeout=10)
    assert response.json()["result"]["job"]["summary"] == SUMMARY


async def test_the_answer_is_the_document_and_not_only_the_job(client, aurora_token, clocks):
    """What the assignment asks a finished poll to return: *the document* (D50).

    Returning the job alone was ambiguous in the worst possible way -- `result.id`
    was the job id, sitting next to a `document_id` in the url that is usually
    the same small number, and nothing in the payload said which one it was.
    """
    clocks(timeout=25, recheck=NO_RECHECK)
    upload = await enqueue(client, aurora_token)
    await claim(client)
    await complete(client, upload["job_id"])

    result = (await poll(client, aurora_token, upload["document_id"])).json()["result"]

    assert result["id"] == upload["document_id"]
    assert result["pet_id"] == upload["pet_id"]
    assert result["filename"] == "consultation.txt"
    assert result["job"]["id"] == upload["job_id"]
    assert result["job"]["summary"] == SUMMARY
    # The same shape the plain read returns, so a client has one parser, not two.
    direct = (
        await client.get(f"/documents/{upload['document_id']}", headers=auth(aurora_token))
    ).json()
    assert result == direct


async def test_the_document_is_not_carried_around_with_its_bytes(client, aurora_token, clocks):
    """The poll reads the columns it answers with, never `content`: a 10 MB
    file held for 25 seconds per waiting client is not metadata."""
    clocks(timeout=25, recheck=NO_RECHECK)
    upload = await enqueue(client, aurora_token)
    await claim(client)
    await complete(client, upload["job_id"])

    result = (await poll(client, aurora_token, upload["document_id"])).json()["result"]

    assert "content" not in result
