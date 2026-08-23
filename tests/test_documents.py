import asyncio

import pytest
from sqlalchemy import func, select, text

from app.core.config import settings
from app.db.base import PG_INT_MAX
from app.models import Document, Job, JobStatus
from tests.conftest import auth

CLINICAL_NOTE = (
    b"Patient Rex, 4 years old, male, neutered. Presented with vomiting for two days. "
    b"Hydration adequate. Abdominal palpation unremarkable. Started on a bland diet."
)


def txt(content: bytes = CLINICAL_NOTE, name: str = "consultation.txt"):
    return {"file": (name, content, "text/plain")}


async def create_pet(client, token: str, name: str = "Rex") -> int:
    response = await client.post(
        "/pets", json={"name": name, "owner_name": "Ana"}, headers=auth(token)
    )
    assert response.status_code == 201
    return response.json()["id"]


async def upload(client, token: str, pet_id: int, **kwargs):
    return await client.post(f"/pets/{pet_id}/documents", files=txt(**kwargs), headers=auth(token))


# --- pets -------------------------------------------------------------------


async def test_create_pet_returns_201(client, aurora_token):
    response = await client.post(
        "/pets", json={"name": "Mel", "owner_name": "Bruno"}, headers=auth(aurora_token)
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Mel"
    assert body["owner_name"] == "Bruno"
    assert "tenant_id" not in body


async def test_create_pet_without_a_token_is_401(client):
    response = await client.post("/pets", json={"name": "Mel", "owner_name": "Bruno"})

    assert response.status_code == 401
    assert response.json()["error_code"] == "NOT_AUTHENTICATED"


async def test_tenant_id_in_the_body_is_ignored(client, aurora_token, boreal_token):
    """There is no field to set, so the extra key is simply dropped. The pet
    still lands in the caller's own tenant -- provable because the other tenant
    cannot see it."""
    response = await client.post(
        "/pets",
        json={"name": "Mel", "owner_name": "Bruno", "tenant_id": 999},
        headers=auth(aurora_token),
    )
    pet_id = response.json()["id"]

    stolen = await upload(client, boreal_token, pet_id)
    assert stolen.status_code == 404


# --- listing pets: the isolation read from the inside (D26) -----------------
#
# GET /documents/{id} proves the negative half of the rule -- you cannot reach
# what is not yours. These prove the positive half: what you *do* reach is
# exactly yours, and the count agrees.


async def test_listing_pets_shows_mine_and_only_mine(client, aurora_token, boreal_token):
    await create_pet(client, aurora_token, "Rex")
    await create_pet(client, aurora_token, "Mel")
    await create_pet(client, boreal_token, "Nina")

    aurora = (await client.get("/pets", headers=auth(aurora_token))).json()
    boreal = (await client.get("/pets", headers=auth(boreal_token))).json()

    assert [p["name"] for p in aurora["items"]] == ["Mel", "Rex"], "newest first"
    assert [p["name"] for p in boreal["items"]] == ["Nina"]

    # Same table, same request, nothing in common -- which is the whole claim.
    assert not {p["id"] for p in aurora["items"]} & {p["id"] for p in boreal["items"]}


async def test_the_total_counts_only_my_clinic(client, aurora_token, boreal_token):
    """The subtler half of a leak: a right list beside a wrong number. Counting
    without the tenant filter would quietly report how many pets the whole
    database holds."""
    for name in ("Rex", "Mel", "Thor"):
        await create_pet(client, aurora_token, name)
    await create_pet(client, boreal_token, "Nina")

    aurora = (await client.get("/pets", headers=auth(aurora_token))).json()
    boreal = (await client.get("/pets", headers=auth(boreal_token))).json()

    assert aurora["total"] == 3
    assert boreal["total"] == 1


async def test_a_clinic_with_no_pets_sees_an_empty_list_not_someone_elses(
    client, aurora_token, boreal_token
):
    await create_pet(client, aurora_token, "Rex")

    boreal = (await client.get("/pets", headers=auth(boreal_token))).json()

    assert boreal["items"] == []
    assert boreal["total"] == 0


async def test_the_list_never_carries_the_tenant_id(client, aurora_token):
    await create_pet(client, aurora_token, "Rex")

    body = (await client.get("/pets", headers=auth(aurora_token))).json()

    assert "tenant_id" not in body["items"][0]


async def test_listing_pets_without_a_token_is_401(client):
    response = await client.get("/pets")

    assert response.status_code == 401
    assert response.json()["error_code"] == "NOT_AUTHENTICATED"


async def test_a_tenant_id_query_parameter_changes_nothing(client, aurora_token, boreal_token):
    """There is no such parameter, so it is dropped. The filter comes from the
    token, and there is nothing in the request that can point somewhere else."""
    await create_pet(client, aurora_token, "Rex")
    boreal_pet = await create_pet(client, boreal_token, "Nina")

    body = (await client.get("/pets?tenant_id=1", headers=auth(boreal_token))).json()

    assert [p["id"] for p in body["items"]] == [boreal_pet]


async def test_paging_stays_inside_the_tenant(client, aurora_token, boreal_token):
    """Isolation has to survive the second page too -- an offset that walked the
    whole table instead of the filtered set would surface a neighbour's pet
    exactly where nobody looks."""
    mine = [await create_pet(client, aurora_token, f"Pet {i}") for i in range(5)]
    await create_pet(client, boreal_token, "Nina")

    seen = []
    for offset in (0, 2, 4):
        response = await client.get(f"/pets?limit=2&offset={offset}", headers=auth(aurora_token))
        page = response.json()
        seen += [p["id"] for p in page["items"]]
        assert page["limit"] == 2 and page["offset"] == offset

    assert sorted(seen) == sorted(mine), "every page, and only my pets"
    assert len(seen) == len(set(seen)), "no row served twice across pages"


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=201", "limit=-1", "offset=-1", "limit=abc"],
    ids=["zero", "over-max", "negative", "negative-offset", "not-a-number"],
)
async def test_a_page_that_cannot_exist_is_422(client, aurora_token, query):
    """The cap is on the parameter, not on the query: asking for 10000 is a 422,
    not a silently smaller page. A client that thinks it read everything because
    it asked for more and got 200 is a client that skips records."""
    response = await client.get(f"/pets?{query}", headers=auth(aurora_token))

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


# --- upload -----------------------------------------------------------------


async def test_upload_returns_202_with_an_enqueued_job(client, aurora_token):
    pet_id = await create_pet(client, aurora_token)

    response = await upload(client, aurora_token, pet_id)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "ENQUEUED"
    assert body["document_id"] > 0
    assert body["job_id"] > 0


async def test_the_bytes_are_stored_and_read_back(client, aurora_token):
    pet_id = await create_pet(client, aurora_token)
    document_id = (await upload(client, aurora_token, pet_id)).json()["document_id"]

    response = await client.get(f"/documents/{document_id}", headers=auth(aurora_token))

    assert response.status_code == 200
    body = response.json()
    assert body["size_bytes"] == len(CLINICAL_NOTE)
    assert body["filename"] == "consultation.txt"
    assert body["content_type"] == "text/plain"
    assert len(body["sha256"]) == 64
    assert body["job"]["status"] == "ENQUEUED"
    assert body["job"]["summary"] is None


# --- dedupe (D24) -----------------------------------------------------------


async def test_same_content_twice_is_deduplicated_with_200(client, aurora_token):
    pet_id = await create_pet(client, aurora_token)

    first = await upload(client, aurora_token, pet_id)
    second = await upload(client, aurora_token, pet_id)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["document_id"] == first.json()["document_id"]
    # No second job either: one is already under way for this content.
    assert second.json()["job_id"] == first.json()["job_id"]


async def test_a_different_filename_does_not_defeat_the_dedupe(client, aurora_token):
    """The key is the content, not the name the client happened to use."""
    pet_id = await create_pet(client, aurora_token)

    first = await upload(client, aurora_token, pet_id)
    second = await upload(client, aurora_token, pet_id, name="renamed.txt")

    assert second.status_code == 200
    assert second.json()["document_id"] == first.json()["document_id"]


async def test_the_same_content_for_another_pet_is_a_new_document(client, aurora_token):
    """The dedupe key is the pair (pet, content). Two pets can legitimately
    have the identical form on file."""
    rex = await create_pet(client, aurora_token, "Rex")
    mel = await create_pet(client, aurora_token, "Mel")

    first = await upload(client, aurora_token, rex)
    second = await upload(client, aurora_token, mel)

    assert second.status_code == 202
    assert second.json()["document_id"] != first.json()["document_id"]


async def test_reupload_after_a_failure_creates_a_new_job(client, aurora_token, db):
    """Re-uploading is how a client asks to try again -- and the failed attempt
    is kept rather than overwritten."""
    pet_id = await create_pet(client, aurora_token)
    first = await upload(client, aurora_token, pet_id)
    job_id = first.json()["job_id"]

    await db.execute(
        text(
            "UPDATE jobs SET status = 'FAILED', error_code = 'PROCESSING_TIMEOUT', "
            "finished_at = now() WHERE id = :id"
        ),
        {"id": job_id},
    )
    await db.commit()

    retry = await upload(client, aurora_token, pet_id)

    assert retry.status_code == 200
    assert retry.json()["document_id"] == first.json()["document_id"]
    assert retry.json()["job_id"] != job_id
    assert retry.json()["status"] == "ENQUEUED"


async def test_reupload_of_a_done_document_returns_the_existing_summary(client, aurora_token, db):
    pet_id = await create_pet(client, aurora_token)
    first = await upload(client, aurora_token, pet_id)
    job_id = first.json()["job_id"]

    await db.execute(
        text(
            "UPDATE jobs SET status = 'DONE', summary = 'Gastritis. Bland diet.', "
            "finished_at = now() WHERE id = :id"
        ),
        {"id": job_id},
    )
    await db.commit()

    again = await upload(client, aurora_token, pet_id)

    assert again.status_code == 200
    assert again.json()["job_id"] == job_id
    assert again.json()["status"] == "DONE"

    document = await client.get(
        f"/documents/{first.json()['document_id']}", headers=auth(aurora_token)
    )
    assert document.json()["job"]["summary"] == "Gastritis. Bland diet."


# --- validation (D25) -------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "content", "status_code", "error_code"),
    [
        # Named .pdf and holding plain text: the extension is accepted now, so
        # what refuses it is the header check, not the name (D51).
        ("scan.pdf", CLINICAL_NOTE, 415, "FILE_CONTENT_MISMATCH"),
        ("notes", CLINICAL_NOTE, 415, "UNSUPPORTED_FILE_TYPE"),
        ("notes.docx", CLINICAL_NOTE, 415, "UNSUPPORTED_FILE_TYPE"),
        ("notes.txt", b"\xff\xfe\x00\x01binary", 415, "FILE_CONTENT_MISMATCH"),
        ("notes.txt", b"", 422, "FILE_EMPTY_OR_TOO_SHORT"),
        ("notes.txt", b"   \n\t  ", 422, "FILE_EMPTY_OR_TOO_SHORT"),
        ("notes.txt", b"too short", 422, "FILE_EMPTY_OR_TOO_SHORT"),
    ],
)
async def test_rejected_uploads(client, aurora_token, filename, content, status_code, error_code):
    pet_id = await create_pet(client, aurora_token)

    response = await upload(client, aurora_token, pet_id, content=content, name=filename)

    assert response.status_code == status_code
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["error_code"] == error_code


async def test_uppercase_extension_is_accepted(client, aurora_token):
    pet_id = await create_pet(client, aurora_token)

    response = await upload(client, aurora_token, pet_id, name="NOTES.TXT")

    assert response.status_code == 202


async def test_file_over_the_limit_is_413(client, aurora_token, monkeypatch):
    pet_id = await create_pet(client, aurora_token)
    monkeypatch.setattr(settings, "max_upload_bytes", 32)

    response = await upload(client, aurora_token, pet_id)

    assert response.status_code == 413
    assert response.json()["error_code"] == "FILE_TOO_LARGE"


async def test_a_rejected_upload_leaves_nothing_behind(client, aurora_token, db):
    """A 4xx must not enqueue a job destined to fail, nor store the bytes."""
    pet_id = await create_pet(client, aurora_token)
    await upload(client, aurora_token, pet_id, content=b"", name="empty.txt")

    assert await db.scalar(select(func.count()).select_from(Document)) == 0
    assert await db.scalar(select(func.count()).select_from(Job)) == 0


# --- tenant isolation, the IDOR (D26) ---------------------------------------


async def test_reading_another_tenants_document_is_404(client, aurora_token, boreal_token):
    """Ids are sequential, so this is the guess the challenge leaves open. 403
    would confirm the document exists, which is itself the leak."""
    pet_id = await create_pet(client, aurora_token)
    document_id = (await upload(client, aurora_token, pet_id)).json()["document_id"]

    mine = await client.get(f"/documents/{document_id}", headers=auth(aurora_token))
    theirs = await client.get(f"/documents/{document_id}", headers=auth(boreal_token))

    assert mine.status_code == 200
    assert theirs.status_code == 404
    assert theirs.json()["error_code"] == "DOCUMENT_NOT_FOUND"


async def test_uploading_to_another_tenants_pet_is_404(client, aurora_token, boreal_token):
    pet_id = await create_pet(client, aurora_token)

    response = await upload(client, boreal_token, pet_id)

    assert response.status_code == 404
    assert response.json()["error_code"] == "PET_NOT_FOUND"


async def test_a_missing_pet_and_someone_elses_pet_are_indistinguishable(
    client, aurora_token, boreal_token
):
    """Same status and same code either way -- otherwise the difference between
    them is a way to enumerate which ids are taken."""
    pet_id = await create_pet(client, aurora_token)

    other_tenant = await upload(client, boreal_token, pet_id)
    nonexistent = await upload(client, boreal_token, 999_999)

    assert other_tenant.status_code == nonexistent.status_code == 404
    assert other_tenant.json() == nonexistent.json()


async def test_document_of_another_tenant_is_404_even_though_it_exists(
    client, aurora_token, boreal_token, db
):
    pet_id = await create_pet(client, aurora_token)
    document_id = (await upload(client, aurora_token, pet_id)).json()["document_id"]

    assert await db.scalar(select(func.count()).select_from(Document)) == 1

    response = await client.get(f"/documents/{document_id}", headers=auth(boreal_token))
    assert response.status_code == 404


async def test_concurrent_identical_uploads_produce_exactly_one_document(
    client, aurora_token, db
):
    """Two uploads of the same bytes at the same time both pass the SELECT that
    looks for a duplicate. What decides is the unique index (pet_id, sha256):
    one INSERT wins, the loser catches the violation and reads back the winner's
    row. Same answer either way, and never two copies of the file."""
    pet_id = await create_pet(client, aurora_token)

    first, second = await asyncio.gather(
        upload(client, aurora_token, pet_id),
        upload(client, aurora_token, pet_id),
    )

    assert {first.status_code, second.status_code} <= {200, 202}
    assert first.json()["document_id"] == second.json()["document_id"]
    assert await db.scalar(select(func.count()).select_from(Document)) == 1
    # And exactly one job: the loser must not enqueue a second one either.
    assert await db.scalar(select(func.count()).select_from(Job)) == 1


# --- security regressions ---------------------------------------------------


async def test_an_overlong_filename_is_refused_not_a_500(client, aurora_token):
    """`documents.filename` is varchar(255). Postgres answers a longer value
    with a truncation error, which is not an IntegrityError and escapes every
    handler -- a client-supplied name would produce the one status that is
    supposed to mean a bug on our side."""
    pet_id = await create_pet(client, aurora_token)

    response = await upload(client, aurora_token, pet_id, name="a" * 300 + ".txt")

    assert response.status_code == 422
    assert response.json()["error_code"] == "FILENAME_TOO_LONG"


async def test_a_filename_at_the_limit_still_works(client, aurora_token):
    pet_id = await create_pet(client, aurora_token)
    name = "a" * (255 - len(".txt")) + ".txt"

    response = await upload(client, aurora_token, pet_id, name=name)

    assert response.status_code == 202


async def test_a_database_error_never_carries_the_file_into_the_message(client, aurora_token, db):
    """SQLAlchemy appends bound parameters to database errors, and one of the
    parameters of this INSERT is the document itself. Unguarded, a single failed
    insert writes a clinical record in plain text to the application log."""
    from sqlalchemy.exc import SQLAlchemyError

    from app.models import Document

    pet_id = await create_pet(client, aurora_token)
    secret = b"CONFIDENTIAL clinical note that must never reach a log file."

    db.add(
        Document(
            pet_id=pet_id,
            tenant_id=1,
            filename="x" * 300,  # forces the truncation error
            content_type="text/plain",
            size_bytes=len(secret),
            sha256="0" * 64,
            content=secret,
        )
    )

    with pytest.raises(SQLAlchemyError) as caught:
        await db.flush()

    message = str(caught.value)
    assert b"CONFIDENTIAL" not in message.encode()
    assert "clinical note" not in message


@pytest.mark.parametrize(
    "document_id",
    [PG_INT_MAX + 1, 0, -1],
    ids=["over-int4", "zero", "negative"],
)
async def test_a_document_id_no_row_could_have_is_422(client, aurora_token, document_id):
    """`GET /documents/2147483648` is a URL anyone can type, and it used to be a
    500: the id does not fit the `integer` primary key, Postgres raises, and the
    error is not an `IntegrityError` so no handler caught it. Same shape of bug
    as the over-long filename above (D36), and against the same invariant."""
    response = await client.get(f"/documents/{document_id}", headers=auth(aurora_token))

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


async def test_an_upload_to_a_pet_id_no_row_could_have_is_422(client, aurora_token):
    response = await client.post(
        f"/pets/{PG_INT_MAX + 1}/documents", files=txt(), headers=auth(aurora_token)
    )

    assert response.status_code == 422


async def test_a_real_but_unknown_document_id_is_still_404(client, aurora_token):
    """The bound above must not turn a legitimate miss into a validation error:
    an id that *could* exist and does not is still `404`, and still says the same
    thing whether or not it belongs to another clinic (D26)."""
    response = await client.get("/documents/424242", headers=auth(aurora_token))

    assert response.status_code == 404
    assert response.json()["error_code"] == "DOCUMENT_NOT_FOUND"


# --- listing documents (D52) ------------------------------------------------


async def upload_n(client, token: str, pet_id: int, how_many: int) -> list[int]:
    """`how_many` distinct documents for one pet, oldest first."""
    ids = []
    for n in range(how_many):
        response = await upload(
            client, token, pet_id, content=CLINICAL_NOTE + f" Visit {n}.".encode()
        )
        ids.append(response.json()["document_id"])
    return ids


async def test_the_list_carries_each_document_with_its_latest_job(client, aurora_token):
    """The reason a document list exists: *which of these is ready*. Without the
    job in the row, every item needs a second call to be worth anything."""
    pet_id = await create_pet(client, aurora_token)
    document_id = (await upload(client, aurora_token, pet_id)).json()["document_id"]

    listing = await client.get("/documents", headers=auth(aurora_token))

    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == document_id
    assert item["filename"] == "consultation.txt"
    assert item["job"]["status"] == JobStatus.ENQUEUED


async def test_the_list_shows_the_summary_once_a_job_finished(client, aurora_token, db):
    pet_id = await create_pet(client, aurora_token)
    upload_response = await upload(client, aurora_token, pet_id)
    await db.execute(
        text("UPDATE jobs SET status = 'DONE', summary = :s WHERE document_id = :d"),
        {"s": "Vomiting, hydrated.", "d": upload_response.json()["document_id"]},
    )
    await db.commit()

    listing = await client.get("/documents", headers=auth(aurora_token))

    assert listing.json()["items"][0]["job"]["summary"] == "Vomiting, hydrated."


async def test_the_list_never_leaves_the_tenant(client, aurora_token, boreal_token):
    """The same check `GET /pets` exists for, on the other table (D39)."""
    mine = await create_pet(client, aurora_token)
    theirs = await create_pet(client, boreal_token)
    await upload_n(client, aurora_token, mine, 2)
    await upload_n(client, boreal_token, theirs, 3)

    ours = (await client.get("/documents", headers=auth(aurora_token))).json()
    others = (await client.get("/documents", headers=auth(boreal_token))).json()

    assert ours["total"] == 2 and others["total"] == 3
    assert {item["id"] for item in ours["items"]}.isdisjoint(
        {item["id"] for item in others["items"]}
    )


async def test_a_clinic_with_no_documents_sees_an_empty_list(client, boreal_token, aurora_token):
    pet_id = await create_pet(client, aurora_token)
    await upload(client, aurora_token, pet_id)

    listing = await client.get("/documents", headers=auth(boreal_token))

    assert listing.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


async def test_paging_walks_the_whole_set_without_repeating(client, aurora_token):
    pet_id = await create_pet(client, aurora_token)
    created = await upload_n(client, aurora_token, pet_id, 5)

    first = (await client.get("/documents?limit=2", headers=auth(aurora_token))).json()
    second = (await client.get("/documents?limit=2&offset=2", headers=auth(aurora_token))).json()
    third = (await client.get("/documents?limit=2&offset=4", headers=auth(aurora_token))).json()

    seen = [item["id"] for page in (first, second, third) for item in page["items"]]
    assert seen == sorted(created, reverse=True), "newest first, every row once"
    assert first["total"] == 5, "total is the whole set, not the page"


async def test_the_pet_filter_narrows_and_cannot_widen(client, aurora_token, boreal_token):
    """`pet_id` is a filter on top of the tenant filter, never instead of it."""
    mine = await create_pet(client, aurora_token)
    other_of_mine = await create_pet(client, aurora_token)
    theirs = await create_pet(client, boreal_token)
    await upload_n(client, aurora_token, mine, 2)
    await upload(client, aurora_token, other_of_mine)
    await upload_n(client, boreal_token, theirs, 3)

    narrowed = (await client.get(f"/documents?pet_id={mine}", headers=auth(aurora_token))).json()
    reaching = (await client.get(f"/documents?pet_id={theirs}", headers=auth(aurora_token))).json()

    assert narrowed["total"] == 2
    assert all(item["pet_id"] == mine for item in narrowed["items"])
    assert reaching == {"items": [], "total": 0, "limit": 50, "offset": 0}


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=201", "limit=-1", "offset=-1", "pet_id=0", f"pet_id={PG_INT_MAX + 1}"],
    ids=["zero", "over-max", "negative", "negative-offset", "pet-zero", "pet-over-int4"],
)
async def test_a_page_that_cannot_exist_is_422_here_too(client, aurora_token, query):
    response = await client.get(f"/documents?{query}", headers=auth(aurora_token))

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


async def test_listing_documents_without_a_token_is_401(client):
    assert (await client.get("/documents")).status_code == 401


async def test_the_list_does_not_carry_the_files(client, aurora_token):
    """Metadata only. A page of 50 documents that each dragged their bytes along
    would be half a gigabyte read out of Postgres to answer a listing (D52)."""
    pet_id = await create_pet(client, aurora_token)
    await upload(client, aurora_token, pet_id)

    item = (await client.get("/documents", headers=auth(aurora_token))).json()["items"][0]

    assert "content" not in item
    assert item["size_bytes"] == len(CLINICAL_NOTE)
