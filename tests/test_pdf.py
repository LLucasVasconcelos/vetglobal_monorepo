"""`.pdf` upload, end to end (D51).

The requirement the assignment stated and this project deferred longest: it asks
for `.txt` **or** `.pdf`. What makes it more than "one more extension in a list"
is where the work lands. A `.txt` is verified completely at the door -- decode
it and you know everything. A `.pdf` can only be verified as *being a PDF*;
whether it holds readable text is known by whoever parses it, which is the
worker, seconds later and on the other side of an HTTP boundary.

So the failure that matters here is not an upload error. It is a job that ran,
looked, and reported back.
"""

from base64 import b64decode, b64encode

import pytest

import worker
from app.models import JobStatus
from tests.conftest import auth
from tests.pdfs import CONSULTATION, SCANNED, WITH_TEXT, pdf
from tests.test_worker import worker_client  # noqa: F401  (fixture)


async def upload_pdf(client, token: str, content: bytes, name: str = "consultation.pdf"):
    pet = await client.post(
        "/pets", json={"name": "Hank", "owner_name": "John"}, headers=auth(token)
    )
    return await client.post(
        f"/pets/{pet.json()['id']}/documents",
        files={"file": (name, content, "application/pdf")},
        headers=auth(token),
    )


# --- the door ---------------------------------------------------------------


async def test_a_pdf_is_accepted_and_enqueued(client, aurora_token):
    response = await upload_pdf(client, aurora_token, WITH_TEXT)

    assert response.status_code == 202
    assert response.json()["status"] == JobStatus.ENQUEUED


async def test_the_stored_type_is_the_verified_one(client, aurora_token):
    """`application/pdf` because the bytes say so, not because the multipart
    part said so -- the same rule that has always applied to `.txt`."""
    upload = await upload_pdf(client, aurora_token, WITH_TEXT)

    document = await client.get(
        f"/documents/{upload.json()['document_id']}", headers=auth(aurora_token)
    )

    assert document.json()["content_type"] == "application/pdf"
    assert document.json()["size_bytes"] == len(WITH_TEXT)


@pytest.mark.parametrize(
    "content",
    [b"Patient Hank, six years old, vomiting for five days.", b"", b"\x00\x01\x02"],
    ids=["text-renamed", "empty", "random-bytes"],
)
async def test_a_file_that_is_not_a_pdf_is_refused_by_its_header(client, aurora_token, content):
    """The counterpart of decoding a `.txt`: a name is a claim, and the first
    five bytes are the check."""
    response = await upload_pdf(client, aurora_token, content)

    assert response.status_code == 415
    assert response.json()["error_code"] == "FILE_CONTENT_MISMATCH"


async def test_a_pdf_is_deduplicated_like_any_other_upload(client, aurora_token):
    """Dedupe is over the bytes (D24), so it never had to learn about formats."""
    first = await upload_pdf(client, aurora_token, WITH_TEXT)
    pet_id = (await client.get("/pets", headers=auth(aurora_token))).json()["items"][0]["id"]

    second = await client.post(
        f"/pets/{pet_id}/documents",
        files={"file": ("same-again.pdf", WITH_TEXT, "application/pdf")},
        headers=auth(aurora_token),
    )

    assert second.status_code == 200
    assert second.json()["document_id"] == first.json()["document_id"]


# --- the claim: a PDF has no readable form in JSON --------------------------


async def test_the_claim_carries_the_pdf_as_base64(client, aurora_token, worker_client):  # noqa: F811
    upload = await upload_pdf(client, aurora_token, WITH_TEXT)

    claim = (await worker_client.post("/internal/jobs/claim")).json()

    assert claim["job_id"] == upload.json()["job_id"]
    assert claim["content_type"] == "application/pdf"
    assert claim["content"] is None, "a PDF is not text and must not pretend to be"
    assert claim["content_base64"] is not None
    # And it is the file, unchanged -- the worker parses the same bytes stored.
    assert b64decode(claim["content_base64"]) == WITH_TEXT


async def test_a_text_claim_still_carries_readable_text(client, aurora_token, worker_client):  # noqa: F811
    """The `.txt` path is untouched: reading a consultation note straight out of
    a claim is half of what makes this endpoint drivable by hand."""
    pet = await client.post(
        "/pets", json={"name": "Rex", "owner_name": "Ana"}, headers=auth(aurora_token)
    )
    note = b"Patient Rex, four years old, vomiting for two days. Hydration adequate."
    await client.post(
        f"/pets/{pet.json()['id']}/documents",
        files={"file": ("note.txt", note, "text/plain")},
        headers=auth(aurora_token),
    )

    claim = (await worker_client.post("/internal/jobs/claim")).json()

    assert claim["content_type"] == "text/plain"
    assert claim["content"] == note.decode()
    assert claim["content_base64"] is None


# --- the worker: where a PDF is actually read -------------------------------


def claimed(content: bytes) -> dict:
    """A claim as the API would hand it over, for the extractor alone."""
    return {"content_type": "application/pdf", "content_base64": b64encode(content).decode()}


def test_the_extractor_reads_a_pdf():
    assert CONSULTATION in worker.extract_text(claimed(WITH_TEXT))


def test_a_scan_with_no_text_layer_is_its_own_failure():
    """Not a crash and not a summary of nothing: a verdict with a name. Reading
    it would need OCR, which is declared out of scope -- so the client is told
    exactly that instead of getting three characters of noise."""
    with pytest.raises(worker.NoTextLayer):
        worker.extract_text(claimed(SCANNED))


def test_a_pdf_that_falls_apart_after_the_header_is_not_a_crash():
    """It passed the door because it starts with `%PDF-`; only a parser can
    know it is broken, and that parser lives here."""
    with pytest.raises(worker.NothingToSummarize):
        worker.extract_text(claimed(b"%PDF-1.4\nand then nothing that means anything"))


async def test_a_worker_summarizes_a_pdf_end_to_end(client, aurora_token, worker_client):  # noqa: F811
    upload = await upload_pdf(client, aurora_token, WITH_TEXT)

    assert await worker.run_once(worker_client) is True

    document = await client.get(
        f"/documents/{upload.json()['document_id']}", headers=auth(aurora_token)
    )
    job = document.json()["job"]
    assert job["status"] == JobStatus.DONE
    assert "Patient Hank" in job["summary"]


async def test_a_scanned_pdf_reaches_the_client_as_a_named_failure(
    client,
    aurora_token,
    worker_client,  # noqa: F811
):
    """The whole reason `.pdf` belongs to the worker, in one test: the upload
    succeeded, the job ran, and the bad news arrives through the job."""
    upload = await upload_pdf(client, aurora_token, SCANNED, name="scan.pdf")
    assert upload.status_code == 202, "a scan is a valid PDF; refusing it at the door would lie"

    await worker.run_once(worker_client)

    job = (
        await client.get(
            f"/documents/{upload.json()['document_id']}", headers=auth(aurora_token)
        )
    ).json()["job"]
    assert job["status"] == JobStatus.FAILED
    assert job["error_code"] == "PDF_HAS_NO_TEXT_LAYER"
    assert "OCR" in job["message"]


async def test_a_polling_client_learns_the_scan_failed(client, aurora_token, worker_client):  # noqa: F811
    """And it arrives on the fast path, like any other verdict."""
    upload = await upload_pdf(client, aurora_token, pdf(None), name="scan.pdf")
    document_id = upload.json()["document_id"]

    await worker.run_once(worker_client)
    polled = await client.get(
        f"/documents/{document_id}/poll", params={"after_job_id": 0}, headers=auth(aurora_token)
    )

    assert polled.json()["result"]["job"]["error_code"] == "PDF_HAS_NO_TEXT_LAYER"
    assert polled.json()["result"]["job"]["summary"] is None
