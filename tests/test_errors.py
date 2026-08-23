"""The far end of invariant 5: the exception nobody predicted.

Every other file here pins a *predictable* failure -- a wrong password, an id no
row could have, another clinic's document -- and asserts it comes back in the
D22 envelope instead of a 500. This one pins the other end. When something
really does blow up, the answer still has the same shape, and the exception does
not leave with it.

That second half is not tidiness. The message of a real exception carries table
names, file paths and, in the case D36 was about, the clinical record itself.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import register_error_handlers

# Shaped like a real SQLAlchemy failure: the statement, and the values bound to
# it. `hide_parameters` keeps the record out of the *log*; this keeps whatever
# is left out of the *response*.
LEAKY = (
    'relation "documents" does not exist\n'
    "[SQL: INSERT INTO documents (content) VALUES (%(content)s)]\n"
    "[parameters: {'content': 'Patient Rex, 4 years old, vomiting for two days'}]"
)


@pytest.fixture(scope="session")
async def crashing_client():
    """A throwaway app whose only route raises, with the real handlers on it.

    `raise_app_exceptions=False` because Starlette re-raises after the handler
    has already answered -- that is how a real server gets the traceback into
    its log while the client still receives the response. Left at the default,
    this test would see the exception instead of the reply the browser gets.
    """
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RuntimeError(LEAKY)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_an_unpredicted_exception_still_answers_in_the_envelope(crashing_client):
    response = await crashing_client.get("/boom")

    assert response.status_code == 500
    assert response.json() == {
        "status": "FAILED",
        "error_code": "INTERNAL_ERROR",
        "message": "Unexpected error. The request was not completed.",
    }


async def test_the_exception_does_not_leave_with_the_response(crashing_client):
    """A 500 is the one answer we cannot explain to the client, because here the
    explanation *is* the leak."""
    body = (await crashing_client.get("/boom")).text

    assert "documents" not in body
    assert "Patient Rex" not in body
    assert "INSERT" not in body
    assert "RuntimeError" not in body
