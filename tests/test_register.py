import asyncio

import pytest
from sqlalchemy import func, select

from app.core.security import decode_access_token
from app.models import Tenant
from tests.conftest import PASSWORD, auth

GOOD = {"tenant_name": "Clinica Nova", "email": "new@clinic.example.com", "password": PASSWORD}


def body(**overrides) -> dict:
    return GOOD | overrides


# --- registering a clinic ---------------------------------------------------


async def test_register_creates_a_clinic_and_logs_you_in(client):
    response = await client.post("/auth/register", json=body())

    assert response.status_code == 201
    data = response.json()
    assert data["tenant"]["name"] == "Clinica Nova"
    assert data["user"]["email"] == "new@clinic.example.com"
    assert data["token_type"] == "bearer"

    # The token is usable straight away -- no second call to /auth/login.
    principal = decode_access_token(data["access_token"])
    assert principal.tenant_id == data["tenant"]["id"]
    assert principal.user_id == data["user"]["id"]


async def test_the_password_is_never_echoed_back(client):
    response = await client.post("/auth/register", json=body(email="a@b.example.com"))

    assert PASSWORD not in response.text
    assert "password" not in response.json()["user"]


async def test_registering_twice_with_the_same_email_is_409(client):
    await client.post("/auth/register", json=body(email="dup@clinic.example.com"))

    again = await client.post(
        "/auth/register", json=body(tenant_name="Outra Clinica", email="dup@clinic.example.com")
    )

    assert again.status_code == 409
    assert again.json()["error_code"] == "EMAIL_ALREADY_REGISTERED"


async def test_two_clinics_may_share_a_name(client):
    """A clinic name is a label, not an identity. Two real clinics can be called
    the same thing, and refusing the second would also say which names are
    already registered."""
    first = await client.post(
        "/auth/register", json=body(tenant_name="Vet Center", email="a1@x.example.com")
    )
    second = await client.post(
        "/auth/register", json=body(tenant_name="Vet Center", email="a2@x.example.com")
    )

    assert first.status_code == second.status_code == 201
    assert first.json()["tenant"]["id"] != second.json()["tenant"]["id"]


async def test_email_is_stored_folded_so_case_cannot_fork_an_account(client):
    """Vet@X and vet@X must be one account. Two would make the second a
    plausible way to impersonate the first."""
    created = await client.post("/auth/register", json=body(email="MiXeD@Clinic.Example.com"))
    assert created.json()["user"]["email"] == "mixed@clinic.example.com"

    collision = await client.post(
        "/auth/register", json=body(tenant_name="Outra Clinica", email="mixed@clinic.example.com")
    )
    assert collision.status_code == 409

    # And the address logs in however it is typed.
    login = await client.post(
        "/auth/login", json={"email": "MIXED@CLINIC.EXAMPLE.COM", "password": PASSWORD}
    )
    assert login.status_code == 200


async def test_concurrent_registrations_of_one_email_yield_one_account(client):
    """Both requests pass the existence check; the unique index on `email`
    decides. Never two users with the same address."""
    first, second = await asyncio.gather(
        client.post(
            "/auth/register", json=body(tenant_name="Corrida A", email="race@x.example.com")
        ),
        client.post(
            "/auth/register", json=body(tenant_name="Corrida B", email="race@x.example.com")
        ),
    )

    codes = sorted([first.status_code, second.status_code])
    assert codes == [201, 409]


# --- the password policy ----------------------------------------------------


@pytest.mark.parametrize(
    ("password", "missing"),
    [
        ("Ab#1", "at least 8 characters"),
        ("vetglobal#2026", "an uppercase letter"),
        ("VETGLOBAL#2026", "a lowercase letter"),
        ("Vetglobal2026", "a symbol"),
        ("Vetglobal 2026", "a symbol"),  # a space is not a symbol
    ],
)
async def test_weak_passwords_are_refused_and_say_why(client, password, missing):
    response = await client.post(
        "/auth/register", json=body(email="weak@x.example.com", password=password)
    )

    assert response.status_code == 422
    data = response.json()
    assert data["status"] == "FAILED"
    assert data["error_code"] == "WEAK_PASSWORD"
    assert missing in data["message"]


async def test_every_missing_rule_is_reported_at_once(client):
    """One round trip to learn everything that is wrong, not one per rule."""
    response = await client.post(
        "/auth/register", json=body(email="w2@x.example.com", password="abc")
    )

    message = response.json()["message"]
    assert "at least 8 characters" in message
    assert "an uppercase letter" in message
    assert "a symbol" in message


async def test_a_password_longer_than_bcrypt_reads_is_refused_not_truncated(client):
    """bcrypt ignores everything past byte 72. Silently accepting a 100
    character password would store one the user never typed."""
    response = await client.post(
        "/auth/register", json=body(email="long@x.example.com", password="A#a" + "x" * 100)
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("email", ["not-an-email", "@nodomain.com", "spaces in@x.com", ""])
async def test_malformed_emails_are_refused(client, email):
    response = await client.post("/auth/register", json=body(email=email))

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


async def test_a_weak_password_creates_nothing(client, db):
    before = await db.scalar(select(func.count()).select_from(Tenant))
    await client.post("/auth/register", json=body(email="nope@x.example.com", password="weak"))
    await db.commit()

    assert await db.scalar(select(func.count()).select_from(Tenant)) == before


# --- adding a colleague -----------------------------------------------------


async def test_a_new_user_lands_in_the_callers_own_clinic(client, aurora_token):
    response = await client.post(
        "/auth/users",
        json={"email": "colleague@aurora.example.com", "password": PASSWORD},
        headers=auth(aurora_token),
    )

    assert response.status_code == 201
    assert response.json()["tenant_id"] == decode_access_token(aurora_token).tenant_id


async def test_the_colleague_sees_the_same_clinics_data(client, aurora_token):
    """Same tenant means same documents -- which is the point of adding them."""
    pet = await client.post(
        "/pets", json={"name": "Rex", "owner_name": "Ana"}, headers=auth(aurora_token)
    )

    await client.post(
        "/auth/users",
        json={"email": "colleague2@aurora.example.com", "password": PASSWORD},
        headers=auth(aurora_token),
    )
    login = await client.post(
        "/auth/login", json={"email": "colleague2@aurora.example.com", "password": PASSWORD}
    )
    colleague_token = login.json()["access_token"]

    upload = await client.post(
        f"/pets/{pet.json()['id']}/documents",
        files={"file": ("n.txt", b"A clinical note long enough to pass validation checks.")},
        headers=auth(colleague_token),
    )
    assert upload.status_code == 202


async def test_adding_a_user_without_a_token_is_401(client):
    response = await client.post(
        "/auth/users", json={"email": "x@y.example.com", "password": PASSWORD}
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "NOT_AUTHENTICATED"


async def test_there_is_no_way_to_add_a_user_to_someone_elses_clinic(
    client, aurora_token, boreal_token
):
    """`tenant_id` in the body is not a field, so it is dropped. The user lands
    in the caller's clinic regardless of what was asked for."""
    boreal_tenant = decode_access_token(boreal_token).tenant_id

    response = await client.post(
        "/auth/users",
        json={
            "email": "infiltrator@x.example.com",
            "password": PASSWORD,
            "tenant_id": boreal_tenant,
        },
        headers=auth(aurora_token),
    )

    assert response.status_code == 201
    assert response.json()["tenant_id"] == decode_access_token(aurora_token).tenant_id
    assert response.json()["tenant_id"] != boreal_tenant


async def test_a_registered_clinic_starts_empty_and_isolated(client, aurora_token):
    """What registration creates cannot reach anyone else's data: a brand new
    clinic sees nothing, including documents that exist."""
    pet = await client.post(
        "/pets", json={"name": "Rex", "owner_name": "Ana"}, headers=auth(aurora_token)
    )
    upload = await client.post(
        f"/pets/{pet.json()['id']}/documents",
        files={"file": ("n.txt", b"A clinical note long enough to pass validation checks.")},
        headers=auth(aurora_token),
    )
    document_id = upload.json()["document_id"]

    newcomer = await client.post(
        "/auth/register", json=body(tenant_name="Recem Chegada", email="new2@x.example.com")
    )
    token = newcomer.json()["access_token"]

    assert (await client.get(f"/documents/{document_id}", headers=auth(token))).status_code == 404
