# VetGlobal Backend

Asynchronous clinical document summarization API.

A document is uploaded for a pet, a summarization job is queued, a worker
processes it, and the client follows progress through long polling — without
holding any per-request state in process memory.

> **Status:** complete and running. Thirteen routes, **233 tests** against a real
> PostgreSQL. Every functional requirement is implemented, `.txt` and `.pdf`
> alike, along with seven of the eight bonus items.
>
> **Deliberately not built** — each one explained in
> [What is not here](#what-is-not-here): real summarization, a frontend, roles,
> OCR, row-level security, object storage, deployment, encryption at rest, rate
> limiting.

**In a hurry?** [Setup](#setup) · [Running](#running) · [Tests](#tests)

### Documentation

**[Decisions and tradeoffs](https://claude.ai/code/artifact/684f4ad1-c969-4c78-ad72-1d9ffd22fee1)**
· *the design notes asked for in the brief* — five short chapters, about
twenty-five minutes: the seven questions that had no obvious answer and what was
chosen for each, the six decisions that hold the design up with the cost each one
charged, how it was verified, and what I would ask before building this for real.

**[VetGlobal Dev Docs](https://claude.ai/code/artifact/22ccfe07-90ce-40fb-87d3-9fb434f2d8a8)**
· *full developer documentation* — how to bring it up, where everything lives,
the seven invariants that must not break, the API contract, the job lifecycle,
and how to add a route or a migration without breaking the rules.

Both are also served by the API itself: the interactive docs at `/docs` narrate
the whole flow, and every route declares its own error codes there.

---

## The API

Routes are at the root, without a `/api/v1` prefix, because that is what the
assignment specified.

| method | route | |
|---|---|---|
| `POST` | `/auth/register` | creates a clinic and your account in it, returns a JWT |
| `POST` | `/auth/login` | |
| `POST` | `/auth/users` | adds a colleague to the clinic **in your token** |
| `POST` | `/pets` | |
| `GET` | `/pets?limit=50&offset=0` | your clinic's pets, and the `total` |
| `POST` | `/pets/{pet_id}/documents` | `.txt` or `.pdf`; `202`, or `200` when the content was already uploaded |
| `GET` | `/documents?limit=50&offset=0&pet_id=` | your clinic's documents, each with its latest job |
| `GET` | `/documents/{document_id}` | metadata and the latest job, with its summary |
| `GET` | `/documents/{document_id}/poll?after_job_id=0` | held open for 25 seconds |
| `POST` | `/internal/jobs/claim` | the worker's side — takes one job off the queue |
| `POST` | `/internal/jobs/{job_id}/complete` | the worker's result |
| `GET` | `/internal/stats` | queue health — the two durations, kept apart |
| `GET` | `/health` | answers without touching the database |

Job states: `ENQUEUED → PROCESSING → DONE | FAILED`.

The `/internal/*` routes take an `X-Internal-Token` header instead of the JWT.
They exist as HTTP routes on purpose: [the worker](#the-worker) calls exactly
these two and has no other way in, which is what keeps it a separate service
rather than the API under another filename. It also means the loop is
demonstrable by hand — playing the worker in one terminal while a poll waits in
another is the fastest way to see the design work.

### The walkthrough

```bash
# 1. A clinic and an account in it. Nothing to seed; register and you are in.
TOKEN=$(curl -sX POST localhost:8000/auth/register -H 'Content-Type: application/json' \
  -d '{"tenant_name":"Clinica Aurora","email":"vet@aurora.example.com","password":"Vetglobal#2026"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

# 2. A pet. The clinic comes from the token — there is no field for it here.
curl -sX POST localhost:8000/pets -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"Hank","owner_name":"John"}'

# 3. A document — .txt or .pdf. 202, with everything needed to follow along.
curl -sX POST localhost:8000/pets/1/documents -H "Authorization: Bearer $TOKEN" \
  -F 'file=@consultation.txt'
# {"document_id":1,"job_id":1,"status":"ENQUEUED"}

# 4. Start waiting BEFORE any work happens. This blocks, then answers with
#    the document — the same body GET /documents/1 gives, summary included.
curl -s "localhost:8000/documents/1/poll?after_job_id=0" -H "Authorization: Bearer $TOKEN"

# 5. In another terminal, play the worker.
curl -sX POST localhost:8000/internal/jobs/claim -H "X-Internal-Token: $INTERNAL_TOKEN"
curl -sX POST localhost:8000/internal/jobs/1/complete -H "X-Internal-Token: $INTERNAL_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"status":"DONE","summary":"Intermittent vomiting for five days.","attempt":1}'
```

Step 4 answers about a millisecond after step 5 lands. Everything above is also
clickable at <http://127.0.0.1:8000/docs>.

---

## Why it is built this way

The design notes live in **[Decisions and tradeoffs](https://claude.ai/code/artifact/684f4ad1-c969-4c78-ad72-1d9ffd22fee1)**
— why the file sits in Postgres rather than on disk, why the long poll re-reads
the row even though it is notified, how the queue guards against two workers and
against one that stalled, why another clinic's document answers `404`, and what
each of those cost.

## What is not here

**Designed, not built.** The architecture calls for these and their absence is
the difference between what is designed and what runs:

| | today |
|---|---|
| a containerized `docker compose up --scale worker=2` | would mean containerizing the API, and there is no `Dockerfile` — that is adjacent to deployment, which is out of scope. Two terminals do the same demonstration, and a test does it unattended |
| React frontend | none — the API is exercised through `/docs` or `curl`. If one is added it will be a demonstration of the polling loop, not a sample of frontend quality, and it would be said so plainly |
| keyset pagination | `GET /pets` and `GET /documents` page with `limit` / `offset` and a `total`. Keyset would be the honest answer for a list that receives inserts between pages — a document uploaded mid-walk makes an offset page repeat or skip a row. What holds it off is scale, not the argument |

**Real summarization.** `summarize()` in `worker.py` takes the opening sentences
and trims them. The assignment asks for a worker that *simulates* the work, so
the exercise is the job lifecycle rather than the quality of the summary. A real
model would go behind that one function and nothing in the loop would change —
what would change is everything around it: an API key, a per-job cost, a latency
that makes the 60 second lease too short, and a clinical record leaving the
machine.

**Out of scope by decision**, not by omission: OCR for scanned documents;
row-level security (described as the stronger mitigation for tenant isolation,
not implemented); cloud storage; deployment; encryption at rest; and rate
limiting on any route.

**Open registration is a demo affordance, not a product decision.** Anyone can
create a clinic with one call, and that exists for a single reason: whoever
reviews this clones the repository and needs a token immediately, without
hunting for seeded credentials. A real clinical system would gate this behind an
invitation or a contract, with the address verified before the tenant exists.
It is the first route to close, and closing it changes nothing underneath —
tenant isolation does not depend on who is allowed to create a tenant.

**Known limitations, stated rather than discovered.** The `413` for an oversized
upload happens *after* the whole file has been received, since there is no global
body limit. There is no rate limiting anywhere, which matters more with open
registration — doing it properly means Redis or a table, because a counter in
process memory is worth nothing with two instances, and that is the same
constraint the polling design is built around. Filenames are stored exactly as
uploaded, including markup; inert today, and it matters the day something
renders them without escaping.

---

## Requirements

You need exactly two things installed:

| tool | why |
|---|---|
| **Docker** + **Docker Compose** | runs PostgreSQL 16 |
| **uv** | manages Python and the dependencies |

You do **not** need a system Python. `uv` downloads and pins the required
interpreter (3.12, see `.python-version`) on its own, isolated from anything
already installed on your machine.

---

## Setup

### 1. Install uv

**macOS / Linux**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell)**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**Alternatives:** `brew install uv` · `pipx install uv` · `pacman -S uv`

The installer puts `uv` in `~/.local/bin`. If the shell cannot find it, add that
directory to your `PATH` and reopen the terminal:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc   # bash
fish_add_path ~/.local/bin                                 # fish
```

Check it:

```bash
uv --version
```

### 2. Install Python and the dependencies

```bash
uv sync
```

This reads `.python-version` and `pyproject.toml`, downloads CPython 3.12 if it
is missing, creates `.venv/`, and installs everything from `uv.lock` — the exact
versions this project was built against.

Confirm the interpreter:

```bash
uv run python --version     # Python 3.12.x
```

### 3. Create your `.env`

```bash
cp .env.example .env
```

Then open `.env` and fill in the three blank values. Generate each secret with:

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

| variable | what it is |
|---|---|
| `DB_PASSWORD` | password for the Postgres container |
| `JWT_SECRET` | signs the authentication tokens |
| `INTERNAL_TOKEN` | shared secret required by the `/internal/*` worker endpoints |

A **single `.env` feeds both sides**: Docker Compose substitutes the variables
when starting Postgres, and the application reads the same names through
`pydantic-settings`. There are no default values in `docker-compose.yml` on
purpose — a missing variable fails loudly instead of silently starting the
database with credentials the application does not have. The three secrets have
no default in the application either: it refuses to start without them, because
a signed token is only worth the secrecy of its key.

`.env` is git-ignored. Only `.env.example` is committed.

### 4. Start PostgreSQL

```bash
docker compose up -d
```

Wait until it reports healthy:

```bash
docker compose ps
```

The port is published on one explicit address, never on `0.0.0.0`. It defaults
to `127.0.0.1`, which keeps the database unreachable from the network; set
`DB_BIND_HOST` in `.env` to expose it to another machine on purpose — and then
`DB_HOST` has to match, or the application gets `ConnectionRefusedError`.

### 5. Apply the migrations

```bash
uv run alembic upgrade head
```

### 6. Create a clinic

There is nothing to seed. Register through the API and you are logged in:

```bash
curl -X POST http://127.0.0.1:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"tenant_name":"Clinica Aurora","email":"vet@aurora.example.com","password":"Vetglobal#2026"}'
```

The response carries an `access_token` — paste it into **Authorize** at
<http://127.0.0.1:8000/docs> and every other route opens.

Passwords need at least 8 characters with a lowercase letter, an uppercase
letter and a symbol; anything less answers `422 WEAK_PASSWORD` naming every
rule it missed.

**Register a second clinic** and you can watch tenant isolation work on data you
created yourself: Aurora asking for a Boreal document gets a `404`, not a `403`.
That second clinic is also what the test suite creates for the same reason.

---

## Running

```bash
uv run uvicorn app.main:app --reload
```

- API: <http://127.0.0.1:8000>
- Interactive docs: <http://127.0.0.1:8000/docs>
- Health check: <http://127.0.0.1:8000/health>

In **Authorize** there are two keys, and they are not interchangeable:
`HTTPBearer` takes the JWT from register or login and opens the client routes;
`APIKeyHeader` takes the `INTERNAL_TOKEN` from your `.env` and opens the two
`/internal/*` worker routes. No endpoint hands the second one out — it is a
shared secret, and a route that returned it would be the route that makes it
pointless.

### The worker

```bash
uv run python worker.py
```

A separate process that loops `claim → summarize → complete`. It is a **client**
of the API: it imports nothing from `app/`, holds no database credentials and no
signing key, and reaches the queue only through the two routes any other client
could call. Killing it is safe — the job it was holding returns to the queue
when its lease expires, and the next worker takes it as `attempt: 2`.

Point it somewhere else with `API_BASE_URL`:

```bash
API_BASE_URL=http://192.168.1.10:8000 uv run python worker.py
```

**Run two of them, in two terminals, against a queue holding two jobs** and they
claim different jobs at the same instant instead of one waiting behind the
other. That is `SKIP LOCKED`, and it is the reason the queue is a table rather
than a table two workers fight over. Kill one mid-job and the lease hands its
work to the other.

Nothing requires the worker to be running: `/internal/jobs/claim` and
`/internal/jobs/{id}/complete` are the same two routes it calls, so the loop can
be driven by hand from Swagger or `curl` — which is what the assignment asked
for when it said the completion endpoint simulates a worker callback.

## Tests

```bash
uv run pytest -q
```

The suite runs against a real PostgreSQL, not a stub — `SKIP LOCKED`, a unique
index and `NOTIFY` have no meaningful behaviour anywhere else. It creates and
migrates **its own database**, `vetglobal_test`, on first run, so nothing you
register or upload by hand is ever touched: the tests need to truncate freely
between cases, and a suite that truncates the database you are also using
destroys your work. Override the name with `TEST_DB_NAME` if it collides.

What it covers, beyond the happy path: deduplication and retry after failure,
double completion and contradicting completion, two simultaneous claims taking
different jobs, two simultaneous completions applying exactly once, a poll
answering on notification and on re-read, a poll timing out, a lease expiring
with and without attempts left, a stale worker's fencing token, cross-tenant
reads on every route, the listener reconnecting after its connection is killed,
two real workers racing for the same queue, a worker losing its lease mid-job,
a scanned PDF failing with a named reason, and every input that used to arrive
as a `500`.

The `.pdf` fixtures are **built**, not committed as opaque bytes: `tests/pdfs.py`
writes a minimal valid PDF, with or without a text layer, and the difference
between those two files is the whole point of the `.pdf` tests.

## Linting and type checking

```bash
uv run ruff check .          # report
uv run ruff check . --fix    # fix what is auto-fixable
uv run ruff format .         # format
uv run ty check              # type check
```

`ruff` does not know about types, and this codebase is asynchronous end to end:
a forgotten `await` raises nothing — it returns a coroutine that never runs, and
the suite still passes unless an assertion happens to touch that path. `ty` is
what catches that class of mistake, which is why it sits in the same gate as the
tests rather than in a nightly job.

Three suppressions exist, all of them in `app/core/errors.py`. Starlette types an
exception handler as taking a bare `Exception`, while each handler here takes the
exception it actually handles; the checker is right in general and wrong here,
and widening the signatures to silence it would trade a true annotation for a
false one. The reasoning sits in the comment above them.

---

## Troubleshooting

**`port is already allocated` on `docker compose up`**
Something else already listens on 5432 — often another project's Postgres.
Either stop it, or set a different `DB_PORT` in `.env` (for example `5433`).

**`ConnectionRefusedError` on `alembic upgrade head`**
First check `docker compose ps` and wait for `healthy`.

If the container reports `healthy` but the `PORTS` column is **empty**, the
container was created while the port was still taken: Compose kept the container
but never published the mapping, and a later `docker compose up -d` only *starts*
it without reapplying it. The database works internally and is unreachable from
outside. Recreate it:

```bash
docker compose up -d --force-recreate
```

`PORTS` should then read `127.0.0.1:5432->5432/tcp`.

**`uv: command not found`**
`~/.local/bin` is not on your `PATH` — see step 1.

**Changed `DB_USER` / `DB_PASSWORD` / `DB_NAME` and cannot connect**
Those only take effect when the volume is first created. Recreate it:

```bash
docker compose down -v && docker compose up -d
```

**A poll always takes about five seconds to answer**
The notification is not arriving, and the re-read is doing all the work. The
listener logs `listening on document_events` at startup; if it does not, check
that the application can reach the database at all.

---

## Project layout

```
app/
  main.py       FastAPI application, and the lifespan that raises the listener
  core/         settings, error handling, security
  db/
    session.py  engine and pooled sessions
    listener.py the dedicated LISTEN/NOTIFY connection, outside the pool
  models/       SQLAlchemy models
  schemas/      Pydantic request/response models
  services/
    poll.py     subscribe → read → wait
    ...         the rest of the business logic
  api/          route handlers — these never touch the database directly
worker.py       the separate process, outside app/ on purpose
migrations/     Alembic migrations
tests/
```

`worker.py` sits **outside** `app/` and imports nothing from it. That is the
point rather than tidiness: the service boundary is only real if crossing it
costs an HTTP request. A worker inside `app/`, importing a session and updating
the table directly, would be the API under another filename — and nothing would
stop the next person from importing a service "just to avoid repeating code",
which is how boundaries disappear.

It also means the worker needs no `JWT_SECRET` and no `DB_PASSWORD`. Importing
the API's settings would have demanded both, so a process that only speaks HTTP
would refuse to start without secrets it never uses — and a worker that cannot
read the database cannot bypass tenant isolation either.
