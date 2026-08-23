# VetGlobal Backend

Asynchronous clinical document summarization API.

A document is uploaded for a pet, a summarization job is queued, a worker
processes it, and the client follows progress through long polling — without
holding any per-request state in process memory.

> **Status:** complete and running. Twelve routes, **211 tests** against a real
> PostgreSQL. Every functional requirement of the assignment is implemented,
> `.txt` and `.pdf` alike, along with six of its eight bonus items.
>
> **Deliberately not built** — each one explained in
> [What is not here](#what-is-not-here): real summarization, a frontend, OCR,
> row-level security, cloud storage, deployment, encryption at rest, rate
> limiting.

**In a hurry?** [Setup](#setup) · [Running](#running) · [Tests](#tests)

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

## Design decisions and tradeoffs

### 1. Long polling: PostgreSQL is the broker

`complete` emits `NOTIFY`; each API instance holds **one** dedicated connection
in `LISTEN document_events` and fans the event out in memory to the polls parked
on that instance.

```
GET /documents/10/poll?after_job_id=0
 1. subscribe to document 10          ← before anything is read
 2. SELECT the job                    ← already finished? answer now
 3. wait: the notification (~1 ms), or a re-read every 5s, or 25s expire

POST /internal/jobs/55/complete
 └─ BEGIN
    UPDATE jobs SET status='DONE' ... WHERE id=55 AND status='PROCESSING'
    SELECT pg_notify('document_events', '{"document_id":10,...}')
    COMMIT ──► Postgres releases the notification here
```

**Why this and not a poll loop:** it is a bonus point the assignment names, it
gives ~1 ms latency instead of a query every second per pending client, and the
queue already lives in Postgres — adding Redis to deliver one boolean would be
an extra moving part to deploy, supervise and explain.

**What makes it correct and not merely fast.** `NOTIFY` is **not persistent**:
it reaches whoever is listening at that instant and is otherwise gone. So every
waiter also re-reads the row every five seconds. That re-read is not a
belt-and-braces afterthought — it is the reason the whole design is allowed to
keep waiters in process memory (see [stateless](#10-stateless-and-what-it-cost)).
*Memory makes it fast; the database makes it true.*

**The trap, and the ordering that avoids it.** Subscribe **before** reading. In
the other order, a job that finishes in between emits its notification with
nobody listening: the read already said `PROCESSING`, the event is gone, and the
client waits the full 25 seconds for something that already happened. There is a
test that completes a job in exactly that window
(`test_a_job_finishing_between_the_read_and_the_wait_is_not_missed`), and it was
checked against a deliberately inverted implementation to confirm it fails
there. Several poll tests also push the re-read interval past the poll's own
timeout, so that **only** a notification can answer them — at the default five
seconds, a broken `NOTIFY` would still go green, five seconds late.

**The notification is emitted inside the transaction that closes the job**, not
after the commit. Postgres holds it until `COMMIT`, so a listener is never woken
for a row it cannot read yet, and a commit that fails announces nothing. Emitted
afterwards in a second transaction, both windows open: a wake-up racing its own
data, and a committed job whose announcement was lost with the next line of
code.

**The payload is not the answer.** It carries `{document_id, job_id, status}`,
but a woken poll re-reads the row; only `document_id` is used, to know whom to
wake. A notification can be delivered twice, out of order, or be about a job
this client is not waiting for — trusting it would put correctness in the
message instead of in the database.

### 2. Two timeouts that are not the same thing

This distinction is the one most likely to be got wrong by a client, so the API
is shaped to make confusing them hard.

| | poll timeout | job lease |
|---|---|---|
| what ran out | this HTTP request (25s) | the processing itself (60s per attempt, 3 attempts) |
| the job is | alive, still working | over |
| the answer | `200` with `timed_out: true` | `200` with `result.job.status: FAILED`, `error_code: PROCESSING_TIMEOUT` |
| the client should | ask again | show the failure |

Treating the first as the second makes the UI announce an error on a document
that is about to be ready.

### 3. What the poll returns on timeout — a declared deviation

The assignment offered `204 No Content` or `200 null`. This is a third option,
so it needs justifying:

```json
{ "result": null, "awaiting_job_id": 55, "timed_out": true }
```

`204` has nowhere to carry the cursor, and a bare `null` cannot distinguish
"not yet" from "finished, and empty". This envelope answers both and hands back
what to call again with, so the client keeps no state between requests.

On success `result` is **the document** — byte for byte what
`GET /documents/{id}` returns, with the job and its `summary` nested inside. One
parser for both endpoints, and no ambiguity about whose `id` is at the top: an
earlier version answered with the job alone, whose `id` sat next to a
`document_id` in the url that is usually the same small number, with nothing in
the payload saying which was which.

`after_job_id=0` — the value in the assignment's own example, where no job `0`
exists — means *the latest job of this document*, resolved server-side. The
answer always names the id it resolved to. A client that kept its `job_id` from
the upload should send it: `0` is a convenience, not the main path.

### 4. Idempotency, in both places it matters

**Upload.** Deduplicated by `sha256` of the content plus `pet_id`. The same file
twice answers `200` with the existing `document_id` instead of `202`, and no
second job is created. The exception is a document whose last job **failed** —
re-uploading is how a client asks to try again, and it gets a *new* job rather
than a rewritten one, so the failed attempt stays readable. The unique index on
`(pet_id, sha256)` is what settles two simultaneous uploads of the same bytes;
the loser reads back the winner's row.

**Completion.** At-least-once delivery *guarantees* a worker will sometimes
report the same job twice, so a repeat must be silent: same verdict again is
`200` with `applied: false`, and nothing is written. But `FAILED` over a job
already `DONE` is not a retry, it is a bug — that answers `409`, because
swallowing it would let an error replace a summary.

The failure payload accepts both shapes: `{"status": "FAILED", "error": "…"}`,
which is the assignment's own example, and `{"status": "FAILED", "error_code":
"…", "message": "…"}`, which is what the rest of this API speaks. The first
fills in as the second, with `WORKER_REPORTED_FAILURE` for the code — "the
worker said it failed, without saying what kind" is exactly what that payload
carries. Prefer the second: a code is what a client branches on.

### 5. Queue semantics without a queue

The `jobs` table *is* the queue. Every state change is a race with another
worker, so each one is a **single conditional `UPDATE`** with the check in the
`WHERE`, never a `SELECT` followed by an `UPDATE` — there is no window between
deciding and doing.

```sql
UPDATE jobs SET status='PROCESSING', attempts=attempts+1, ...
 WHERE id = (SELECT id FROM jobs WHERE status='ENQUEUED'
             ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
```

`SKIP LOCKED` is what makes it a queue rather than a table two workers fight
over: a row someone else is claiming is stepped over instead of waited on, so N
workers make N different claims at the same instant.

**The lease** is what makes a dead worker recoverable. A claim comes with a
deadline; a job still `PROCESSING` past it is available again, and fails with
`PROCESSING_TIMEOUT` once its attempts are spent. `attempt` is a fencing token —
a worker that stalled past its lease and reports late is told `409
JOB_LEASE_LOST`, because it is writing about work somebody else already redid.

**Assumed cost:** expired leases are swept **inside the claim**, not by a
background sweeper. The queue is only read by workers, so a stuck job matters
exactly when somebody comes looking for work. The price is that with a
completely idle queue, a dead worker's job stays `PROCESSING` until the next
claim — a test pins that, so it stays a decision rather than becoming a
surprise.

### 6. `.pdf`: verified at the door, read by the worker

A `.txt` is verifiable in full on arrival — decode it and you know everything.
A `.pdf` is only verifiable as *being a PDF*: the first five bytes are `%PDF-`,
and that is all the route can say without opening the file. Whether it holds
readable text is a question for whoever parses it.

So the work splits: the route checks the extension, the size and the header and
stores the bytes; the worker opens it. Extraction is slow and it fails — in the
route, an upload would stop answering `202` in milliseconds and start holding
the connection open through a 200-page document, and an unreadable file would
become an **upload error** when it is really a **job that failed**.

A scanned document — a photograph of a consultation note, which is the normal
case in a clinic — is a perfectly valid PDF with no text layer. It gets its own
`error_code`, `PDF_HAS_NO_TEXT_LAYER`, rather than the generic extraction
failure: with it, a client can tell the owner "send this as text"; without it,
they only know something went wrong. Reading those would need OCR, which is out
of scope.

One consequence in the contract: the claim now carries `content_type` plus
either `content` (text) or `content_base64` (bytes). Two fields where one would
do, deliberately — the `.txt` path stays readable in the claim, and reading a
consultation note straight out of the response is half of what makes that
endpoint a demonstration. The cost is base64's 33%: a 10 MB PDF is 13 MB of
JSON. A raw-bytes endpoint would avoid it at the price of a second round trip
between claiming a job and starting it, which is exactly what sending the text
with the claim was meant to avoid.

### 7. Tenant isolation, and why `404` and not `403`

Every clinic is a tenant. `tenant_id` comes from the **token**, never from a
body field or a query parameter — so there is nothing in a request to point at
another clinic. A resource belonging to someone else answers `404`: ids are
sequential, `GET /documents/41` is a guess anyone can make, and `403` would
confirm the guess was right, which is itself the leak.

**Why the ids are sequential, and what UUID would cost.** Sequential ids are
what make this rule *testable*: `/documents/41` is a guess anyone can make, so
the filter above has to be real rather than assumed. UUIDs would not have made
the API safer — an unguessable id is obscurity, and it leaks through a log, a
screenshot or a shared link, while the query stays unfiltered underneath.

What they would cost is ordering. Six queries here order by id, and two of them
mean something: `ORDER BY jobs.id` **is** the FIFO of the queue, and
`ORDER BY jobs.id DESC LIMIT 1` is "the latest attempt at this document".
`created_at` does not substitute — rows written in one transaction share a
timestamp and break the tie at random, which also makes offset pagination repeat
or skip rows. UUIDv7, being time-ordered, would preserve all six; UUIDv4 would
not.

The honest cost of what is here: sequential ids leak volume. Register, upload
once, read your id, and you know roughly how much the platform has processed.
The moment to switch is when an id starts appearing in a URL that leaves the
organization — and then it goes in *alongside* the tenant filter, never instead
of it.

**The token is a claim, not the answer.** Each authenticated request reads the
user row and builds the principal from *it*, not from the JWT payload. Without
that, a deleted account keeps reading records until its token expires, and
anyone holding the signing key could mint a token for any `tenant_id` and the
isolation would obey. The cost is one primary-key lookup on a request that was
going to hit the database anyway; what it buys is revocation that works *now*.

### 8. Files live in the database, as `bytea`

The alternative was local disk, and local disk contradicts a requirement the
assignment itself wrote: stateless with multiple instances. It also brings the
orphan problem — either the transaction rolls back and the file is left behind,
or the row commits and the write failed. In the database, bytes and metadata
commit together, and the `sha256` cannot drift from the content it describes.

**Assumed cost:** this does not scale to large files or high volume. With real
traffic the answer is object storage with the metadata still in Postgres, and
the tradeoff would invert around the size where streaming beats transactional
consistency. For `.txt` clinical notes with a 10 MB ceiling, it does not.

### 9. One error shape, and no 500 for anything predictable

```json
{ "status": "FAILED", "error_code": "PROCESSING_TIMEOUT", "message": "…" }
```

`error_code` is stable and for branching on; `message` is for a person and may
be reworded. Every failure leaves this way — including the ones FastAPI would
otherwise answer itself, like a validation error or an unknown path, which would
be a second error format.

A `500` means a bug on our side, and it is the one answer the client gets no
explanation for: the explanation is the leak — table names, file paths, and in
one real case the clinical record itself, which is why bound parameters are kept
out of database errors. Tests pin both halves: predictable input never produces
a `500`, and a genuine crash still answers in the envelope without carrying the
exception out.

### 10. Stateless, and what it cost

The requirement is that request handling must not depend on in-memory state that
would break with multiple instances. Two things here look like they might:

**The waiters.** Each instance keeps its own set of parked polls in memory. It
does not break the requirement, because correctness never depends on it: every
waiter re-reads the database on a timer, and `NOTIFY` reaches *every* listening
instance, not just the one that closed the job. There is a test that closes a
job and announces it from an entirely separate connection — the shape of a
second instance — and the waiter in this process wakes anyway. Another kills the
listener's backend with `pg_terminate_backend` to check that it reconnects on
its own, since a listener that stayed down would silently degrade every poll on
that instance to the re-read.

**The uploaded file.** Same rule, applied to disk instead of memory: nothing is
written to local disk, which is what [decision 8](#8-files-live-in-the-database-as-bytea)
is about.

---

## The ambiguity the assignment left open

Its own list of unspecified details, and what was chosen for each:

| question | answer |
|---|---|
| How should the queue be simulated? | The `jobs` table, claimed with `FOR UPDATE SKIP LOCKED` and a lease. No broker. |
| How should files be stored? | `bytea` in Postgres — [decision 8](#8-files-live-in-the-database-as-bytea). |
| How should duplicate uploads be handled? | Deduplicated by `sha256` + `pet_id`; `200` instead of `202`. A failed last job gets a new job. |
| What if the worker completes the same job twice? | Same verdict: `200`, `applied: false`, silent. Contradicting verdict: `409`. |
| What should polling return on timeout? | `200` with an envelope carrying the cursor — [decision 3](#3-what-the-poll-returns-on-timeout--a-declared-deviation). |
| How should tenant isolation be modeled? | One schema, `tenant_id` on every row, taken from the token only. |
| What if the document does not exist? | `404` — the same answer as a document belonging to someone else, deliberately. |

Two questions the assignment did not ask, answered anyway because the code could
not proceed without them: what `after_job_id=0` means when job `0` cannot exist,
and how a first account is created when there is nothing to seed (open
registration, which also makes tenant isolation something a reviewer can verify
on data they created themselves).

---

## What is not here

**Designed, not built.** The architecture calls for these and their absence is
the difference between what is designed and what runs:

| | today |
|---|---|
| a containerized `docker compose up --scale worker=2` | would mean containerizing the API, and there is no `Dockerfile` — that is adjacent to deployment, which is out of scope. Two terminals do the same demonstration, and a test does it unattended |
| React frontend | none — the API is exercised through `/docs` or `curl`. If one is added it will be a demonstration of the polling loop, not a sample of frontend quality, and it would be said so plainly |
| observability | `enqueued_at`, `claimed_at` and `finished_at` are recorded per job, so duration is *available*; nothing reports on it |
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

## Linting

```bash
uv run ruff check .          # report
uv run ruff check . --fix    # fix what is auto-fixable
uv run ruff format .         # format
```

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
