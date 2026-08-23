"""The walkthrough: the five endpoints of the assignment, in the order a client
actually calls them, against the real settings.

Every step here is covered in pieces elsewhere. What this file adds is the seam
between them -- that the `job_id` the upload hands out is the one the claim
returns and the one the poll resolves to -- and one test somebody can read to
learn how the API is used. The README describes this flow in prose; this is the
same flow as an assertion, so the prose cannot quietly stop being true.

Note what is *not* monkeypatched: the poll runs with the real 25 second ceiling
and the real 5 second recheck. It answers in milliseconds anyway, because the
completion notifies it.
"""

import asyncio

import pytest

from app.models import JobStatus
from tests.conftest import auth, register
from tests.test_internal_jobs import INTERNAL

RECORD = (
    b"Patient Hank, 6 years old, male, neutered labrador. Owner reports intermittent "
    b"vomiting for five days, worse after meals. Alert, hydrated, temperature 38.4. "
    b"Abdominal palpation unremarkable. Plan: bland diet, maropitant, recheck in 72h."
)
SUMMARY = "Intermittent vomiting for five days in a 6-year-old labrador. Bland diet, recheck 72h."


@pytest.fixture(scope="session")
async def walkthrough_token(client) -> str:
    """A clinic of its own, registered through the public route like any client.

    Its own, so this test never depends on what the other files left behind --
    and registering it here is itself the first step of the walkthrough.
    """
    clinic = await register(client, "Clinica Passo a Passo", "vet@walkthrough.example.com")
    assert clinic["access_token"]
    return clinic["access_token"]


async def test_the_whole_walkthrough(client, walkthrough_token):
    token = auth(walkthrough_token)

    # 1. A pet. The clinic comes from the token; there is no field for it here.
    pet = await client.post("/pets", json={"name": "Hank", "owner_name": "John"}, headers=token)
    assert pet.status_code == 201
    pet_id = pet.json()["id"]

    # 2. The upload, answered 202 with everything needed to follow along.
    upload = await client.post(
        f"/pets/{pet_id}/documents",
        files={"file": ("consultation.txt", RECORD, "text/plain")},
        headers=token,
    )
    assert upload.status_code == 202
    document_id, job_id = upload.json()["document_id"], upload.json()["job_id"]
    assert upload.json()["status"] == JobStatus.ENQUEUED

    # 3. The client starts waiting *before* any work happens -- which is the
    #    whole point of a long poll, and the case the ordering invariant exists
    #    for. `after_job_id=0` because a client may not have kept the job id.
    waiting = asyncio.create_task(
        client.get(f"/documents/{document_id}/poll", params={"after_job_id": 0}, headers=token)
    )
    await asyncio.sleep(0.2)

    # 4. The worker's half: take the job, with the text to summarize in hand.
    claim = await client.post("/internal/jobs/claim", headers=INTERNAL)
    assert claim.status_code == 200
    assert claim.json()["job_id"] == job_id, "the queue handed out a different job"
    assert claim.json()["content"] == RECORD.decode()

    # 5. And report the result, quoting the attempt it was given.
    done = await client.post(
        f"/internal/jobs/{job_id}/complete",
        json={"status": "DONE", "summary": SUMMARY, "attempt": claim.json()["attempt"]},
        headers=INTERNAL,
    )
    assert done.status_code == 200 and done.json()["applied"] is True

    # 6. The poll that was already open answers, without having been asked again.
    polled = await asyncio.wait_for(waiting, timeout=10)
    assert polled.status_code == 200
    assert polled.json()["timed_out"] is False
    assert polled.json()["awaiting_job_id"] == job_id, "`0` resolved to the wrong job"
    assert polled.json()["result"]["summary"] == SUMMARY

    # 7. And the same result is there for anyone reading the document later.
    document = await client.get(f"/documents/{document_id}", headers=token)
    assert document.status_code == 200
    assert document.json()["job"]["summary"] == SUMMARY
    assert document.json()["filename"] == "consultation.txt"


async def test_another_clinic_cannot_see_any_of_it(client, walkthrough_token, aurora_token):
    """The same walkthrough, watched from the outside. Ids are sequential, so
    every url above is a guess anyone can make -- and each one answers 404,
    which is the answer that does not confirm the guess (D26)."""
    pet = await client.post(
        "/pets", json={"name": "Hank", "owner_name": "John"}, headers=auth(walkthrough_token)
    )
    upload = await client.post(
        f"/pets/{pet.json()['id']}/documents",
        files={"file": ("consultation.txt", RECORD, "text/plain")},
        headers=auth(walkthrough_token),
    )
    document_id = upload.json()["document_id"]
    intruder = auth(aurora_token)

    assert (await client.get(f"/documents/{document_id}", headers=intruder)).status_code == 404
    assert (await client.get(f"/documents/{document_id}/poll", headers=intruder)).status_code == 404
    assert (
        await client.post(
            f"/pets/{pet.json()['id']}/documents",
            files={"file": ("other.txt", RECORD, "text/plain")},
            headers=intruder,
        )
    ).status_code == 404
