from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.deps import get_current_principal, require_internal_token
from app.core.config import settings
from app.core.errors import register_error_handlers
from app.core.security import Principal, create_access_token, decode_access_token
from tests.conftest import PASSWORD, auth


@pytest.fixture(scope="session")
async def guarded_client():
    """The dependencies need a route to guard, and the real ones only arrive in
    the next step. This mounts the two of them on a throwaway app so their
    behaviour is pinned now, not later."""
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/whoami")
    async def whoami(principal: Principal = Depends(get_current_principal)):  # noqa: B008
        return {"user_id": principal.user_id, "tenant_id": principal.tenant_id}

    @app.post("/internal/ping", dependencies=[Depends(require_internal_token)])
    async def ping():
        return {"status": "ok"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --- login ------------------------------------------------------------------


async def test_login_returns_a_token_carrying_the_tenant(client, aurora_email):
    response = await client.post(
        "/auth/login", json={"email": aurora_email, "password": PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.jwt_expire_minutes * 60

    principal = decode_access_token(body["access_token"])
    assert principal.tenant_id > 0


async def test_the_two_tenants_get_different_tenant_ids(client, aurora_email, boreal_email):
    """The whole isolation story rests on this being true."""
    first = decode_access_token(await _token(client, aurora_email))
    second = decode_access_token(await _token(client, boreal_email))

    assert first.tenant_id != second.tenant_id


async def _token(client, email: str) -> str:
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return response.json()["access_token"]


async def test_wrong_password_is_rejected_in_the_d22_envelope(client, aurora_email):
    response = await client.post("/auth/login", json={"email": aurora_email, "password": "nope"})

    assert response.status_code == 401
    assert response.json() == {
        "status": "FAILED",
        "error_code": "INVALID_CREDENTIALS",
        "message": "Invalid email or password.",
    }


async def test_unknown_email_is_indistinguishable_from_a_wrong_password(client, aurora_email):
    """Different answers here would turn login into a directory of registered
    addresses."""
    unknown = await client.post(
        "/auth/login", json={"email": "nobody@nowhere.test", "password": PASSWORD}
    )
    wrong = await client.post("/auth/login", json={"email": aurora_email, "password": "nope"})

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


async def test_malformed_body_keeps_the_single_error_format(client, aurora_email):
    """FastAPI's own 422 is `{"detail": [...]}` -- a second error shape. The
    handler collapses it into the only one (invariant 5)."""
    response = await client.post("/auth/login", json={"email": aurora_email})

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["error_code"] == "VALIDATION_ERROR"
    assert "detail" not in body


async def test_unknown_route_also_answers_in_the_envelope(client):
    response = await client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json()["status"] == "FAILED"
    assert response.json()["error_code"] == "NOT_FOUND"


# --- the bearer dependency --------------------------------------------------


async def test_protected_route_without_a_token_is_401(guarded_client):
    response = await guarded_client.get("/whoami")

    assert response.status_code == 401
    assert response.json()["error_code"] == "NOT_AUTHENTICATED"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_protected_route_with_a_valid_token_sees_the_principal(guarded_client, aurora_token):
    response = await guarded_client.get("/whoami", headers=auth(aurora_token))

    assert response.status_code == 200
    assert response.json()["tenant_id"] == decode_access_token(aurora_token).tenant_id


async def test_tampered_token_is_rejected(guarded_client, aurora_token):
    forged = aurora_token[:-2] + ("ab" if not aurora_token.endswith("ab") else "cd")

    response = await guarded_client.get("/whoami", headers=auth(forged))

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_TOKEN"


async def test_token_signed_with_another_secret_is_rejected(guarded_client):
    """The signature is the only thing standing between a caller and any
    `tenant_id` they feel like claiming."""
    forged = jwt.encode(
        {"sub": "1", "tenant_id": 999, "exp": datetime.now(UTC) + timedelta(minutes=5)},
        "a-different-secret-of-a-perfectly-respectable-length",
        algorithm="HS256",
    )

    response = await guarded_client.get("/whoami", headers=auth(forged))

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_TOKEN"


async def test_expired_token_says_so_specifically(guarded_client):
    """Distinct from INVALID_TOKEN so the client knows to log in again rather
    than to treat it as an attack."""
    expired = jwt.encode(
        {"sub": "1", "tenant_id": 1, "exp": datetime.now(UTC) - timedelta(seconds=1)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    response = await guarded_client.get("/whoami", headers=auth(expired))

    assert response.status_code == 401
    assert response.json()["error_code"] == "TOKEN_EXPIRED"


async def test_token_without_tenant_id_is_rejected(guarded_client):
    """Correctly signed but not one of ours. Letting it through would mean a
    request with no tenant at all reaching a query."""
    incomplete = jwt.encode(
        {"sub": "1", "exp": datetime.now(UTC) + timedelta(minutes=5)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    response = await guarded_client.get("/whoami", headers=auth(incomplete))

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_TOKEN"


async def test_algorithm_is_pinned_so_alg_none_is_refused(guarded_client):
    """An unsigned token is a valid JWT. A decoder that reads `alg` from the
    token itself accepts this one."""
    unsigned = jwt.encode({"sub": "1", "tenant_id": 999}, key="", algorithm="none")

    response = await guarded_client.get("/whoami", headers=auth(unsigned))

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_TOKEN"


def test_create_access_token_round_trips():
    principal = Principal(user_id=7, tenant_id=3)

    assert decode_access_token(create_access_token(principal)) == principal


# --- the internal token dependency (D27) ------------------------------------


async def test_internal_route_without_the_header_is_401(guarded_client):
    response = await guarded_client.post("/internal/ping")

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_INTERNAL_TOKEN"


async def test_internal_route_with_a_wrong_token_is_401(guarded_client):
    response = await guarded_client.post(
        "/internal/ping", headers={"X-Internal-Token": "wrong"}
    )

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_INTERNAL_TOKEN"


async def test_internal_route_does_not_accept_a_user_jwt(guarded_client, aurora_token):
    """The two credentials are not interchangeable: a logged-in vet must not be
    able to complete jobs."""
    response = await guarded_client.post(
        "/internal/ping", headers={"X-Internal-Token": aurora_token}
    )

    assert response.status_code == 401


async def test_internal_route_with_the_right_token_passes(guarded_client):
    response = await guarded_client.post(
        "/internal/ping", headers={"X-Internal-Token": settings.internal_token}
    )

    assert response.status_code == 200


# --- the token is a claim, not the answer (D40) -----------------------------


async def test_a_token_stops_working_the_moment_its_user_is_deleted(client, aurora_token, db):
    """A valid signature says who the bearer *was* when it was issued. Only the
    database says who they are now.

    Without the lookup this is the gap: an account is deleted and its holder
    keeps reading clinical records until the token expires. Revocation that
    takes up to an hour is not revocation.
    """
    created = await client.post(
        "/auth/users",
        json={"email": "demitido@aurora.example.com", "password": PASSWORD},
        headers=auth(aurora_token),
    )
    login = await client.post(
        "/auth/login", json={"email": "demitido@aurora.example.com", "password": PASSWORD}
    )
    token = login.json()["access_token"]

    assert (await client.get("/pets", headers=auth(token))).status_code == 200, "sanity"

    await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": created.json()["id"]})
    await db.commit()

    response = await client.get("/pets", headers=auth(token))

    assert response.status_code == 401
    assert response.json()["error_code"] == "TOKEN_REVOKED"


async def test_a_token_we_signed_ourselves_for_a_user_that_never_existed_is_refused(client):
    """Holding the signing key is not enough to invent a person. The signature
    proves the token came from us; it does not make its subject real."""
    token = create_access_token(Principal(user_id=987654, tenant_id=1))

    response = await client.get("/pets", headers=auth(token))

    assert response.status_code == 401
    assert response.json()["error_code"] == "TOKEN_REVOKED"


async def test_a_token_naming_a_tenant_that_is_not_the_users_is_refused(client, aurora, boreal):
    """The payload asserts a clinic; the row decides which one.

    This is the case that would survive every other check in the file: real
    user, real clinic, correct signature, not expired -- and the wrong pairing.
    Reading `tenant_id` from the token instead of the row would hand Aurora's
    user Boreal's records.
    """
    crossed = create_access_token(
        Principal(user_id=aurora["user"]["id"], tenant_id=boreal["tenant"]["id"])
    )

    response = await client.get("/pets", headers=auth(crossed))

    assert response.status_code == 401
    assert response.json()["error_code"] == "TOKEN_REVOKED"


async def test_the_tenant_used_is_the_rows_and_not_the_payloads(client, aurora, aurora_token):
    """The other half of the same rule: a token that agrees with the database
    keeps working, so the check refuses mismatches rather than everything."""
    response = await client.get("/pets", headers=auth(aurora_token))

    assert response.status_code == 200
    assert aurora["user"]["tenant_id"] == aurora["tenant"]["id"]
