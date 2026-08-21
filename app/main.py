from fastapi import FastAPI

from app.api import auth, documents, pets
from app.core.errors import register_error_handlers

DESCRIPTION = """
Upload a clinical document, a worker summarizes it, the client follows along by
long polling. Everything below is reachable from this page: click **Authorize**,
paste the token from step 1, and the padlocked routes open up.

### The walkthrough

1. **`POST /auth/login`** — there is no sign-up. Users are seeded by
   `uv run python -m scripts.seed`, which creates two clinics:
   `vet@aurora.test` and `vet@boreal.test`, password `vetglobal` for both.
   You get back a JWT carrying `{sub, tenant_id}`.
2. **`POST /pets`** — a pet belongs to the tenant *in your token*. It is never
   read from the body, so there is no field here to point at another clinic.
3. **`POST /pets/{pet_id}/documents`** — send a `.txt`. Answers `202` with a
   `job_id`, or `200` if this exact content was already uploaded for this pet
   (deduplicated by SHA-256; if the previous attempt failed, a fresh job is
   created for the same document).
4. **`GET /documents/{document_id}/poll?after_job_id=…`** — held open for up to
   25 seconds. It answers the instant the job reaches `DONE` or `FAILED`.
   `after_job_id=0` means "whatever the latest job of this document is".
5. **`POST /internal/jobs/claim`** and **`POST /internal/jobs/{job_id}/complete`** —
   what the worker calls. These take an `X-Internal-Token` header instead of the
   JWT, and they exist as HTTP routes precisely so you can play the worker by
   hand here and watch the poll in step 4 answer.

### Two timeouts that are not the same thing

A poll answering `timed_out: true` is **not** a failure. It means the 25 seconds
of *that HTTP request* ran out while the job kept going — ask again. A job
failing with `PROCESSING_TIMEOUT` is the other clock entirely: the processing
itself gave up. Treating the first as the second makes the UI announce an error
on a document that is about to be ready.

### Every error has the same shape

```json
{ "status": "FAILED", "error_code": "INVALID_CREDENTIALS", "message": "…" }
```

`error_code` is for branching on and is stable; `message` is for a human and may
be reworded. A predictable failure never arrives as a 500 — a 500 here means a
bug on our side.

### Asking for someone else's data returns 404

Ids are sequential, so `GET /documents/41` is a guess anyone can make. A
document belonging to another clinic answers `404`, not `403`: `403` would
confirm the record exists, which is itself the leak.
"""

TAGS = [
    {
        "name": "auth",
        "description": "Getting a token. Start here — the rest of the API is closed without one.",
    },
    {
        "name": "pets",
        "description": "Pets and their document uploads, scoped to the tenant in your token.",
    },
    {
        "name": "documents",
        "description": "Reading a document, and waiting on its job with a long poll.",
    },
    {
        "name": "internal",
        "description": (
            "The worker's side of the queue. Guarded by `X-Internal-Token`, not the JWT — "
            "without it, anyone could mark any job `DONE` with any text."
        ),
    },
    {"name": "health", "description": "Liveness. The one route that touches nothing."},
]

app = FastAPI(
    title="VetGlobal Backend",
    description=DESCRIPTION,
    version="0.1.0",
    openapi_tags=TAGS,
)

register_error_handlers(app)

app.include_router(auth.router)
app.include_router(pets.router)
app.include_router(documents.router)


@app.get(
    "/health",
    tags=["health"],
    summary="Liveness probe",
    description="Answers without touching the database, so it stays up while Postgres is down.",
)
async def health() -> dict[str, str]:
    return {"status": "ok"}
