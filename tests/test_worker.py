"""The worker (D28, D47): the loop, and what it does when things go wrong.

The worker is driven here over `ASGITransport` rather than a socket, so these
tests exercise the real `run_once` against the real API without a server. What
they cannot exercise is the network between them -- and that is the honest limit
of testing a client this way.
"""

import asyncio

import httpx
import pytest
from httpx import ASGITransport

import worker
from app.core.config import settings
from app.main import app
from app.models import JobStatus
from tests.conftest import auth
from tests.test_internal_jobs import INTERNAL, NOTE, enqueue, expire_lease

# One long enough to be trimmed, with sentence ends the splitter must respect.
LONG_NOTE = (
    b"Patient Nina, 3 years old, spayed female. Presented with lethargy since Monday. "
    b"Temperature 39.8, mucous membranes pale. Bloodwork ordered. Owner reports reduced "
    b"appetite over the weekend. Started on fluids."
)


@pytest.fixture
async def worker_client():
    """A worker pointed at the application in this process.

    Same headers a real one sends, same two routes, no server in between.
    """
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-Internal-Token": settings.internal_token},
    ) as client:
        yield client


# --- the placeholder summarizer ---------------------------------------------


def test_the_summary_is_the_opening_sentences():
    summary = worker.summarize(LONG_NOTE.decode())

    assert summary == (
        "Patient Nina, 3 years old, spayed female. Presented with lethargy since Monday. "
        "Temperature 39.8, mucous membranes pale."
    )


def test_the_same_text_always_summarizes_the_same_way():
    """Determinism is not fussiness: a job retried after a lease expired must
    produce the attempt that died's answer, not a different one."""
    text = LONG_NOTE.decode()

    assert worker.summarize(text) == worker.summarize(text)


def test_a_long_summary_is_cut_on_a_word_boundary():
    text = "word " * 400

    summary = worker.summarize(text)

    assert len(summary) <= worker.SUMMARY_MAX_CHARS + 1  # the ellipsis
    assert summary.endswith("…")
    assert "wor…" not in summary, "cut mid-word, which reads as corruption"


def test_text_with_no_sentence_end_is_still_summarized():
    """A note somebody typed without punctuation is not a failure."""
    assert worker.summarize("lethargy since monday no appetite") == (
        "lethargy since monday no appetite"
    )


@pytest.mark.parametrize("text", ["", "   ", "\n\t "], ids=["empty", "spaces", "whitespace"])
def test_nothing_to_summarize_is_raised_and_not_returned_blank(text):
    """A blank summary would be refused by `complete` as a 422 and the job would
    hang; failing here turns it into a FAILED the client can read."""
    with pytest.raises(worker.NothingToSummarize):
        worker.summarize(text)


# --- one turn of the loop ---------------------------------------------------


async def test_a_worker_takes_a_job_and_finishes_it(client, aurora_token, worker_client):
    upload = await enqueue(client, aurora_token, content=LONG_NOTE)

    assert await worker.run_once(worker_client) is True

    document = await client.get(f"/documents/{upload['document_id']}", headers=auth(aurora_token))
    job = document.json()["job"]
    assert job["status"] == JobStatus.DONE
    assert job["summary"] == worker.summarize(LONG_NOTE.decode())
    assert job["attempts"] == 1


async def test_an_empty_queue_is_not_work(worker_client):
    assert await worker.run_once(worker_client) is False


async def test_the_worker_answers_a_polling_client(client, aurora_token, worker_client):
    """End to end, the way it actually runs: the client is already waiting when
    the worker picks the job up, and the completion is what answers it."""
    upload = await enqueue(client, aurora_token, content=LONG_NOTE)

    waiting = asyncio.create_task(
        client.get(
            f"/documents/{upload['document_id']}/poll",
            params={"after_job_id": upload["job_id"]},
            headers=auth(aurora_token),
        )
    )
    await asyncio.sleep(0.2)
    await worker.run_once(worker_client)

    polled = await asyncio.wait_for(waiting, timeout=10)
    assert polled.json()["result"]["job"]["summary"] == worker.summarize(LONG_NOTE.decode())


async def test_two_workers_take_two_different_jobs(client, aurora_token):
    """`SKIP LOCKED` through the worker rather than through curl -- the
    `--scale worker=2` demonstration, run as a test."""
    first = await enqueue(client, aurora_token, content=NOTE + b" First.", pet_name="Rex")
    second = await enqueue(client, aurora_token, content=NOTE + b" Second.", pet_name="Mel")

    async def one_worker():
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"X-Internal-Token": settings.internal_token},
        ) as client_:
            return await worker.run_once(client_)

    assert all(await asyncio.gather(one_worker(), one_worker()))

    for upload in (first, second):
        document = await client.get(
            f"/documents/{upload['document_id']}", headers=auth(aurora_token)
        )
        assert document.json()["job"]["status"] == JobStatus.DONE


# --- when the work goes wrong -----------------------------------------------


async def test_a_document_it_cannot_summarize_becomes_a_failed_job(
    client, aurora_token, worker_client, monkeypatch
):
    """The failure has to reach the client as a verdict, not as a worker that
    died holding the job -- which would leave the document `PROCESSING` for
    three lease expiries before anybody learned anything."""
    upload = await enqueue(client, aurora_token)

    def refuse(_text):
        raise worker.NothingToSummarize("nothing here")

    monkeypatch.setattr(worker, "summarize", refuse)

    assert await worker.run_once(worker_client) is True

    document = await client.get(f"/documents/{upload['document_id']}", headers=auth(aurora_token))
    job = document.json()["job"]
    assert job["status"] == JobStatus.FAILED
    assert job["error_code"] == "EXTRACTION_FAILED"
    assert job["summary"] is None


async def test_an_unexpected_crash_also_closes_the_job(
    client, aurora_token, worker_client, monkeypatch
):
    """Not only the failure the summarizer declares -- any exception at all."""
    upload = await enqueue(client, aurora_token)

    def explode(_text):
        raise ZeroDivisionError("something nobody predicted")

    monkeypatch.setattr(worker, "summarize", explode)

    assert await worker.run_once(worker_client) is True

    document = await client.get(f"/documents/{upload['document_id']}", headers=auth(aurora_token))
    assert document.json()["job"]["status"] == JobStatus.FAILED


async def test_a_worker_that_lost_its_lease_does_not_crash_and_does_not_overwrite(
    client, aurora_token, worker_client, monkeypatch, db
):
    """The realistic disaster: this worker stalled, its lease expired, another
    worker redid the job and finished it. Its own `complete` is refused with
    409, and that is correct -- what matters is that it takes the refusal as a
    lost race and goes back for more work instead of dying.
    """
    upload = await enqueue(client, aurora_token, content=LONG_NOTE)
    send = worker_client.post
    interfered = False

    async def lose_the_lease_just_before_reporting(url, **kwargs):
        """Everything here happens between this worker's claim and its complete
        -- which is exactly the window a stalled worker is stuck in."""
        nonlocal interfered
        if url.endswith("/complete") and not interfered:
            interfered = True
            await expire_lease(db, upload["job_id"])
            await client.post("/internal/jobs/claim", headers=INTERNAL)  # attempt 2 owns it
            await client.post(
                f"/internal/jobs/{upload['job_id']}/complete",
                json={"status": "DONE", "summary": "Written by the worker that replaced it."},
                headers=INTERNAL,
            )
        return await send(url, **kwargs)

    monkeypatch.setattr(worker_client, "post", lose_the_lease_just_before_reporting)

    assert await worker.run_once(worker_client) is True, "a lost lease is not a reason to stop"
    assert interfered, "the test did not exercise the window it exists for"

    document = await client.get(f"/documents/{upload['document_id']}", headers=auth(aurora_token))
    assert document.json()["job"]["summary"] == "Written by the worker that replaced it."
